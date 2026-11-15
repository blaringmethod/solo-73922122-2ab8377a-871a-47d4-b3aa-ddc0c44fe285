"""按趟号关联刷卡的网页与 API 路由。

入口与原“停靠事实分析”（``/``、``/analyze``）相互独立、互不影响。

流程
----
1. 首次分别上传站点+定位（一组）与刷卡（另一组）；
2. 之后任一组都可以整份更正，非法输入在解析阶段被拒绝，不改变任何已持久化状态；
3. 受理更正即停供旧下载并登记重算作业，后台 worker 异步重算，期间仍可继续受理更正；
4. 仅当两组最新已受理输入对应的作业成功完成后才开放下载；
5. 计算失败保持停供，作业回到队列，可由 worker 自动重试或手动触发重试。
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from .models import MAX_REQUEST_BYTES, InputError
from .parsing import parse_station_csv, parse_swipe_csv, parse_trip_position_csv
from .storage import (
    GROUP_STATION_POSITION,
    GROUP_SWIPE,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Storage,
)
from .worker import RecalcWorker

logger = logging.getLogger("bus_stop.reconcile")

router = APIRouter()

# main 模块在应用启动时注入
storage: Storage | None = None
worker: RecalcWorker | None = None
templates: Jinja2Templates | None = None


def require_storage() -> Storage:
    if storage is None:  # pragma: no cover - 启动期配置错误
        raise RuntimeError("归站模块尚未初始化存储")
    assert templates is not None
    return storage


async def _read_upload(upload: UploadFile | None | str, label: str) -> bytes:
    if upload is None or isinstance(upload, str) or not getattr(upload, "filename", None):
        raise InputError(f"没有收到{label}，请选择要上传的 CSV 文件。", code="missing_file")
    if upload.filename.lower().endswith((".xlsx", ".xls")):
        raise InputError("请上传 CSV 文件；Excel 文件请先另存为 CSV。", code="unsupported_file")
    data = await upload.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise InputError(f"{label}（{upload.filename}）超过请求大小上限。", code="file_too_large")
    return data


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


# ---------- 页面 ----------


@router.get("/reconcile", response_class=HTMLResponse)
async def reconcile_page(request: Request) -> HTMLResponse:
    store = require_storage()
    return templates.TemplateResponse(
        request,
        "reconcile.html",
        {
            "state": store.status(),
            "versions": {
                GROUP_STATION_POSITION: store.list_versions(GROUP_STATION_POSITION),
                GROUP_SWIPE: store.list_versions(GROUP_SWIPE),
            },
            "jobs": [dict(row) for row in store.list_jobs()],
            "error": None,
            "notice": None,
        },
    )


# ---------- 首次上传 / 整份更正 ----------


@router.post("/api/reconcile/inputs/station-position")
async def upload_station_position(
    station_file: UploadFile = File(...),
    position_file: UploadFile = File(...),
) -> JSONResponse:
    """受理站点+定位整组输入（首次上传或整份更正）。

    先完整解析校验两组文件，全部通过后才写库；任一非法则 422 且原状态不变。
    """

    store = require_storage()
    station_label = station_file.filename or "站点文件"
    position_label = position_file.filename or "定位文件"
    station_bytes = await _read_upload(station_file, "站点文件")
    position_bytes = await _read_upload(position_file, "定位文件")
    # 解析即校验：抛 InputError 时本函数尚未触碰数据库，状态保持不变
    parse_station_csv(station_bytes, filename=station_label)
    parse_trip_position_csv(position_bytes, filename=position_label)

    version = store.accept_station_position(
        station_filename=station_label,
        station_content=station_bytes,
        station_sha256=_digest(station_bytes),
        position_filename=position_label,
        position_content=position_bytes,
        position_sha256=_digest(position_bytes),
    )
    logger.info("站点+定位组已受理新版本 v%s，旧结果已停供并登记重算", version)
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "group": GROUP_STATION_POSITION,
                "version": version,
                "status": store.status(),
            },
        }
    )


@router.post("/api/reconcile/inputs/swipe")
async def upload_swipe(swipe_file: UploadFile = File(...)) -> JSONResponse:
    """受理刷卡整组输入（首次上传或整份更正）。"""

    store = require_storage()
    label = swipe_file.filename or "刷卡文件"
    content = await _read_upload(swipe_file, "刷卡文件")
    parse_swipe_csv(content, filename=label)  # 校验失败则不写库

    version = store.accept_swipe(
        filename=label, content=content, sha256=_digest(content)
    )
    logger.info("刷卡组已受理新版本 v%s，旧结果已停供并登记重算", version)
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "group": GROUP_SWIPE,
                "version": version,
                "status": store.status(),
            },
        }
    )


# ---------- 状态、重试、下载 ----------


@router.get("/api/reconcile/status")
async def reconcile_status() -> JSONResponse:
    store = require_storage()
    return JSONResponse(
        {
            "ok": True,
            "data": {
                "status": store.status(),
                "jobs": [dict(row) for row in store.list_jobs()],
            },
        }
    )


@router.post("/api/reconcile/retry")
async def retry_current_job() -> JSONResponse:
    """手动重试当前失败/排队中的作业。

    失败作业在 fail_job 时已回到 queued，worker 会自动接续；
    本接口在 worker 间隔较长时主动同步处理一次，并返回最新状态。
    """

    store = require_storage()
    job = store.current_job()
    if job is None:
        raise InputError("两组输入尚未齐全，没有可重算的作业。", code="no_job")
    if job["status"] == JOB_SUCCEEDED and store.current_result_rows() is not None:
        return JSONResponse(
            {"ok": True, "data": {"retried": False, "status": store.status()}}
        )
    if job["status"] not in (JOB_QUEUED, JOB_RUNNING):
        raise InputError(
            f"当前作业状态为 {job['status']}，无法重试。", code="job_not_retriable"
        )
    if job["status"] == JOB_RUNNING and job["lease_expires_at"]:
        from .storage import utc_now_iso

        if job["lease_expires_at"] > utc_now_iso():
            # 其他实例的 worker 正在执行且租约有效，不抢占；等待其完成即可
            return JSONResponse(
                {
                    "ok": True,
                    "data": {
                        "retried": False,
                        "status": store.status(),
                        "message": "作业正在其他实例执行，无需重复重试。",
                    },
                }
            )
    assert worker is not None
    handled = worker.run_once()
    return JSONResponse(
        {
            "ok": True,
            "data": {"retried": handled, "status": store.status()},
        }
    )


@router.get("/api/reconcile/download")
async def download_result() -> Response:
    """下载当前版本关联结果；不满足开放条件时返回 409 停供。"""

    store = require_storage()
    body = store.result_csv()
    if body is None:
        state = store.status()
        job = state["job"]
        if job is None:
            message = "两组输入尚未齐全，暂无可下载结果。"
        elif job["status"] in (JOB_QUEUED, JOB_RUNNING):
            message = "结果正在重算，旧表已停供，请等待重算完成后再下载。"
        else:
            message = "当前版本结果不可用（计算失败或已被更新输入取代），请重试重算。"
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": {"code": "download_unavailable", "message": message, "details": []},
            },
        )
    return Response(
        content=body.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="swipe_stop_associations.csv"'
        },
    )


# ---------- 表单风格的 HTML 入口（可直接操作网页，不依赖 JS） ----------


@router.post("/reconcile/upload/station-position", response_class=HTMLResponse)
async def web_upload_station_position(request: Request) -> HTMLResponse:
    store = require_storage()
    form = await request.form()
    notice = None
    error = None
    try:
        station_upload = form.get("station_file")
        position_upload = form.get("position_file")
        station_bytes = await _read_upload(station_upload, "站点文件")
        position_bytes = await _read_upload(position_upload, "定位文件")
        parse_station_csv(station_bytes, filename=station_upload.filename)
        parse_trip_position_csv(
            position_bytes, filename=position_upload.filename
        )
        version = store.accept_station_position(
            station_filename=station_upload.filename,
            station_content=station_bytes,
            station_sha256=_digest(station_bytes),
            position_filename=position_upload.filename,
            position_content=position_bytes,
            position_sha256=_digest(position_bytes),
        )
        notice = f"站点+定位第 {version} 版已受理，旧归站表已停供，等待重算。"
    except InputError as exc:
        error = exc.to_dict()
    return _render_page(request, error=error, notice=notice, status_code=422 if error else 200)


@router.post("/reconcile/upload/swipe", response_class=HTMLResponse)
async def web_upload_swipe(request: Request) -> HTMLResponse:
    store = require_storage()
    form = await request.form()
    notice = None
    error = None
    try:
        swipe_upload = form.get("swipe_file")
        content = await _read_upload(swipe_upload, "刷卡文件")
        parse_swipe_csv(content, filename=swipe_upload.filename)
        version = store.accept_swipe(
            filename=swipe_upload.filename,
            content=content,
            sha256=_digest(content),
        )
        notice = f"刷卡第 {version} 版已受理，旧归站表已停供，等待重算。"
    except InputError as exc:
        error = exc.to_dict()
    return _render_page(request, error=error, notice=notice, status_code=422 if error else 200)


@router.post("/reconcile/retry", response_class=HTMLResponse)
async def web_retry(request: Request) -> HTMLResponse:
    store = require_storage()
    notice = None
    error = None
    try:
        job = store.current_job()
        if job is None:
            raise InputError("两组输入尚未齐全，没有可重算的作业。", code="no_job")
        if job["status"] in (JOB_QUEUED, JOB_RUNNING):
            from .storage import utc_now_iso

            active_lease = (
                job["status"] == JOB_RUNNING
                and job["lease_expires_at"]
                and job["lease_expires_at"] > utc_now_iso()
            )
            if active_lease:
                notice = "作业正在其他实例执行，无需重复重试。"
            else:
                assert worker is not None
                worker.run_once()
                notice = "已触发一次重算。"
        elif job["status"] == JOB_SUCCEEDED and store.current_result_rows() is not None:
            notice = "当前版本结果已完成，可直接下载。"
        else:
            raise InputError("当前作业状态不允许重试。", code="job_not_retriable")
    except InputError as exc:
        error = exc.to_dict()
    return _render_page(request, error=error, notice=notice, status_code=422 if error else 200)


def _render_page(
    request: Request,
    *,
    error: dict | None,
    notice: str | None,
    status_code: int,
) -> HTMLResponse:
    store = require_storage()
    return templates.TemplateResponse(
        request,
        "reconcile.html",
        {
            "state": store.status(),
            "versions": {
                GROUP_STATION_POSITION: store.list_versions(GROUP_STATION_POSITION),
                GROUP_SWIPE: store.list_versions(GROUP_SWIPE),
            },
            "jobs": [dict(row) for row in store.list_jobs()],
            "error": error,
            "notice": notice,
        },
        status_code=status_code,
    )
