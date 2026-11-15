"""归站网页与 HTTP API 端到端测试。"""

from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient

STATIONS = (
    "站名,纬度,经度,半径米\n"
    "A,30.0000,120.0000,60\n"
    "B,30.0060,120.0000,60\n"
).encode()

POSITIONS = (
    "趟号,时刻UTC,纬度,经度\n"
    "T1,2026-11-15T08:00:00Z,30.0000,120.0000\n"
    "T1,2026-11-15T08:00:30Z,30.0000,120.0000\n"
    "T1,2026-11-15T08:03:00Z,30.0060,120.0000\n"
    "T1,2026-11-15T08:03:30Z,30.0060,120.0000\n"
).encode()

SWIPES = (
    "流水号,趟号,时刻UTC\n"
    "S1,T1,2026-11-15T08:00:10Z\n"
    "S2,T1,2026-11-15T08:03:30Z\n"
    "S3,T1,2026-11-15T08:20:00Z\n"
).encode()


def _upload_sp(client: TestClient) -> None:
    resp = client.post(
        "/api/reconcile/inputs/station-position",
        files={
            "station_file": ("stations.csv", io.BytesIO(STATIONS), "text/csv"),
            "position_file": ("positions.csv", io.BytesIO(POSITIONS), "text/csv"),
        },
    )
    assert resp.status_code == 200, resp.text


def _upload_sw(client: TestClient) -> None:
    resp = client.post(
        "/api/reconcile/inputs/swipe",
        files={"swipe_file": ("swipes.csv", io.BytesIO(SWIPES), "text/csv")},
    )
    assert resp.status_code == 200, resp.text


def _wait_for_completion(client: TestClient, timeout: float = 5.0) -> dict:
    # 测试环境已停用后台轮询线程，用重试接口同步推进作业
    resp = client.post("/api/reconcile/retry")
    assert resp.status_code == 200
    status = client.get("/api/reconcile/status").json()["data"]["status"]
    assert status["job"] is not None and status["job"]["status"] == "succeeded"
    return status


def test_reconcile_page_renders(client: TestClient) -> None:
    resp = client.get("/reconcile")
    assert resp.status_code == 200
    assert "刷卡按趟归站" in resp.text
    # 原分析入口仍然保留
    home = client.get("/")
    assert home.status_code == 200
    assert "刷卡按趟归站（新）" in home.text


def test_full_flow_upload_recalc_download(client: TestClient) -> None:
    # 未齐全时下载停供
    resp = client.get("/api/reconcile/download")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "download_unavailable"

    _upload_sp(client)
    _upload_sw(client)
    status = _wait_for_completion(client)
    assert status["download_available"] is True

    resp = client.get("/api/reconcile/download")
    assert resp.status_code == 200
    assert "attachment" in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8-sig")
    lines = [line for line in text.splitlines() if line.strip()]
    assert len(lines) == 4  # 表头 + 3 条
    assert "S1" in text and "S3" in text
    # 未匹配行
    unmatched = [line for line in lines if line.startswith("S3")][0]
    assert "未匹配" in unmatched
    # 版本列存在
    assert ",1,1" in text or "1,1\r" in text


def test_correction_flow_via_api(client: TestClient) -> None:
    _upload_sp(client)
    _upload_sw(client)
    _wait_for_completion(client)
    assert client.get("/api/reconcile/download").status_code == 200

    # 更正刷卡组：受理即停供
    new_swipes = (
        "流水号,趟号,时刻UTC\n"
        "N1,T1,2026-11-15T08:00:10Z\n"
    ).encode()
    resp = client.post(
        "/api/reconcile/inputs/swipe",
        files={"swipe_file": ("swipes-v2.csv", io.BytesIO(new_swipes), "text/csv")},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["version"] == 2
    status = resp.json()["data"]["status"]
    assert status["download_available"] is False
    assert status["pending_recalc"] is True
    assert client.get("/api/reconcile/download").status_code == 409

    _wait_for_completion(client)
    text = client.get("/api/reconcile/download").content.decode("utf-8-sig")
    assert "N1" in text and "S1" not in text
    # 下载内容标注刷卡版本 2、站点定位版本 1
    assert "2" in text


def test_invalid_station_position_group_rejected_atomically(client: TestClient) -> None:
    bad_station = "站名,纬度,半径米\nA,30.0,60\n".encode()  # 缺经度列
    resp = client.post(
        "/api/reconcile/inputs/station-position",
        files={
            "station_file": ("bad.csv", io.BytesIO(bad_station), "text/csv"),
            "position_file": ("p.csv", io.BytesIO(POSITIONS), "text/csv"),
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    status = client.get("/api/reconcile/status").json()["data"]["status"]
    assert status["has_station_position"] is False  # 整组未受理


def test_retry_endpoint_advances_job(client: TestClient) -> None:
    _upload_sp(client)
    _upload_sw(client)
    resp = client.post("/api/reconcile/retry")
    assert resp.status_code == 200
    assert resp.json()["data"]["status"]["job"]["status"] == "succeeded"
    assert client.get("/api/reconcile/download").status_code == 200


def test_retry_without_inputs_returns_422(client: TestClient) -> None:
    resp = client.post("/api/reconcile/retry")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "no_job"


def test_retry_does_not_preempt_active_lease(client: TestClient, store) -> None:
    _upload_sp(client)
    _upload_sw(client)
    # 由后台之外的 worker 认领带出有效租约
    from app.worker import RecalcWorker

    external = RecalcWorker(store, runner="external-instance")
    job_row = store.claim_next_job("external-instance")
    assert job_row is not None and job_row["lease_expires_at"] is not None

    resp = client.post("/api/reconcile/retry")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["retried"] is False
    # 作业仍由 external-instance 持有，未被抢占
    held = store.current_job()
    assert held["runner"] == "external-instance"
    external.process_job(held)
    assert client.get("/api/reconcile/download").status_code == 200


def test_html_forms_work_without_javascript(client: TestClient) -> None:
    with open("/dev/null", "rb") as empty:  # noqa: ASYNC230 - 构造缺文件场景
        resp = client.post(
            "/reconcile/upload/swipe",
            files={"swipe_file": ("", empty, "text/csv")},
        )
    assert resp.status_code == 422
    assert "输入被拒收" in resp.text

    resp = client.post(
        "/reconcile/upload/station-position",
        files={
            "station_file": ("stations.csv", io.BytesIO(STATIONS), "text/csv"),
            "position_file": ("positions.csv", io.BytesIO(POSITIONS), "text/csv"),
        },
    )
    assert resp.status_code == 200
    assert "已受理" in resp.text

    resp = client.post(
        "/reconcile/upload/swipe",
        files={"swipe_file": ("swipes.csv", io.BytesIO(SWIPES), "text/csv")},
    )
    assert resp.status_code == 200
    page = client.get("/reconcile").text
    assert "开放" in page


def test_excel_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/reconcile/inputs/swipe",
        files={
            "swipe_file": (
                "swipes.xlsx",
                io.BytesIO(b"PK\x03\x04"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_file"
