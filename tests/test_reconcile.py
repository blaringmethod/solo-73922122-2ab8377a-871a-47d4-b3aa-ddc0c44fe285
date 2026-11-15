"""按趟号刷卡归站：解析、关联计算、持久化、版本与作业状态机测试。"""

from __future__ import annotations

import io

import pytest

from app.association import compute_associations
from app.models import InputError
from app.parsing import parse_swipe_csv, parse_trip_position_csv
from app.storage import (
    GROUP_STATION_POSITION,
    GROUP_SWIPE,
    JOB_QUEUED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    Storage,
)

STATIONS = (
    "站名,纬度,经度,半径米\n"
    "A,30.0000,120.0000,60\n"
    "B,30.0060,120.0000,60\n"
).encode()

# 两个趟号，每趟都在 A、B 形成确定停靠事实
POSITIONS = (
    "趟号,时刻UTC,纬度,经度\n"
    "T1,2026-11-15T08:00:00Z,30.0000,120.0000\n"
    "T1,2026-11-15T08:00:30Z,30.0000,120.0000\n"
    "T1,2026-11-15T08:03:00Z,30.0060,120.0000\n"
    "T1,2026-11-15T08:03:30Z,30.0060,120.0000\n"
    "T2,2026-11-15T09:00:00Z,30.0000,120.0000\n"
    "T2,2026-11-15T09:00:30Z,30.0000,120.0000\n"
    "T2,2026-11-15T09:03:00Z,30.0060,120.0000\n"
    "T2,2026-11-15T09:03:30Z,30.0060,120.0000\n"
).encode()

SWIPES = (
    "流水号,趟号,时刻UTC\n"
    "S1,T1,2026-11-15T08:00:00Z\n"   # 恰好到站，闭区间内 -> A
    "S2,T1,2026-11-15T08:03:30Z\n"   # 恰好离站 -> B
    "S3,T1,2026-11-15T08:10:00Z\n"   # 区间外 -> 未匹配
    "S4,T2,2026-11-15T09:00:15Z\n"   # A 区间内
    "S5,T3,2026-11-15T10:00:00Z\n"   # 无该趟定位 -> 未匹配
).encode()


# ---------- 解析层 ----------


def test_swipe_parser_happy_path() -> None:
    swipes, warnings = parse_swipe_csv(SWIPES, filename="sw.csv")
    assert len(swipes) == 5
    assert swipes[0].transaction_id == "S1"
    assert swipes[0].trip_id == "T1"
    assert swipes[0].time.tzinfo is not None


def test_swipe_parser_rejects_duplicate_transaction_id() -> None:
    bad = (
        "流水号,趟号,时刻UTC\n"
        "S1,T1,2026-11-15T08:00:00Z\n"
        "S1,T1,2026-11-15T08:01:00Z\n"
    ).encode()
    with pytest.raises(InputError) as exc:
        parse_swipe_csv(bad)
    assert exc.value.code == "duplicate_transaction_id"


def test_swipe_parser_rejects_over_100_rows() -> None:
    lines = ["流水号,趟号,时刻UTC"]
    for i in range(101):
        lines.append(f"S{i},T1,2026-11-15T08:{i:02d}:00Z")
    with pytest.raises(InputError) as exc:
        parse_swipe_csv("\n".join(lines).encode())
    assert exc.value.code == "too_many_swipes"


def test_swipe_parser_rejects_empty_trip_id() -> None:
    bad = "流水号,趟号,时刻UTC\nS1,,2026-11-15T08:00:00Z\n".encode()
    with pytest.raises(InputError) as exc:
        parse_swipe_csv(bad)
    assert exc.value.code == "empty_trip_id"


def test_trip_position_parser_enforces_order_within_trip() -> None:
    bad = (
        "趟号,时刻UTC,纬度,经度\n"
        "T1,2026-11-15T08:00:00Z,30.0,120.0\n"
        "T1,2026-11-15T07:59:00Z,30.0,120.0\n"
    ).encode()
    with pytest.raises(InputError) as exc:
        parse_trip_position_csv(bad)
    assert exc.value.code == "time_not_increasing"


def test_trip_position_parser_accepts_interleaved_trips() -> None:
    interleaved = (
        "趟号,时刻UTC,纬度,经度\n"
        "T1,2026-11-15T08:00:00Z,30.0,120.0\n"
        "T2,2026-11-15T09:00:00Z,30.0,120.0\n"
        "T1,2026-11-15T08:01:00Z,30.0,120.0\n"
    ).encode()
    positions, _ = parse_trip_position_csv(interleaved)
    assert [p.trip_id for p in positions] == ["T1", "T2", "T1"]


# ---------- 关联计算 ----------


def test_compute_associations_closed_interval_and_unmatched() -> None:
    rows = compute_associations(STATIONS, POSITIONS, SWIPES)
    assert len(rows) == 5
    by_id = {row.transaction_id: row for row in rows}

    assert by_id["S1"].status == "matched"
    assert by_id["S1"].route_index == 1  # A 站
    assert by_id["S2"].status == "matched"
    assert by_id["S2"].route_index == 2  # B 站，恰好离站时刻
    assert by_id["S4"].route_index == 1

    assert by_id["S3"].status == "unmatched"
    assert by_id["S3"].route_index is None
    assert "闭区间" in by_id["S3"].reason
    assert by_id["S5"].status == "unmatched"
    assert "没有定位数据" in by_id["S5"].reason


def test_compute_associations_trip_without_facts_marks_unmatched() -> None:
    # T9 趟的定位完全不靠近任何站 -> 无事实，该趟刷卡全部未匹配
    positions = (
        "趟号,时刻UTC,纬度,经度\n"
        "T9,2026-11-15T08:00:00Z,40.0,130.0\n"
        "T9,2026-11-15T08:01:00Z,40.0,130.0\n"
    ).encode()
    swipes = (
        "流水号,趟号,时刻UTC\n"
        "X1,T9,2026-11-15T08:00:30Z\n"
    ).encode()
    rows = compute_associations(STATIONS, positions, swipes)
    assert rows[0].status == "unmatched"
    assert "没有确定停靠事实" in rows[0].reason


# ---------- 存储层状态机 ----------


def test_accept_creates_versions_and_queues_job(store: Storage) -> None:
    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    # 只有一组时不创建作业
    assert store.current_job() is None
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")
    job = store.current_job()
    assert job is not None
    assert job["status"] == JOB_QUEUED
    assert job["station_position_version"] == 1
    assert job["swipe_version"] == 1
    # 作业未完成前下载停供
    assert store.result_csv() is None
    assert store.status()["pending_recalc"] is True


def test_full_recalc_opens_download_and_csv_has_versions(
    store: Storage, worker
) -> None:
    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")
    assert worker.run_once() is True

    job = store.current_job()
    assert job["status"] == JOB_SUCCEEDED
    csv_text = store.result_csv()
    assert csv_text is not None
    assert "流水号" in csv_text
    assert "站点定位输入版本" in csv_text and "刷卡输入版本" in csv_text
    lines = [line for line in csv_text.splitlines() if line.strip()]
    assert len(lines) == 6  # 表头 + 5 条
    assert store.status()["download_available"] is True


def test_correction_stops_download_and_late_result_does_not_overwrite(
    store: Storage, worker
) -> None:
    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")
    worker.run_once()
    assert store.result_csv() is not None

    # 模拟旧作业被认领（running）期间受理刷卡更正
    old_job = store.current_job()
    claimed = store.claim_next_job("test-runner")
    # 队列里没有新作业时 claim 返回 None；先制造更正
    assert claimed is None

    store.accept_swipe(filename="w2.csv", content=SWIPES, sha256="d")
    assert store.result_csv() is None  # 受理即停供
    new_job = store.current_job()
    assert new_job["id"] != old_job["id"]
    assert new_job["status"] == JOB_QUEUED

    # 旧作业此刻才“迟到完成”：结果必须被拒绝写入
    accepted = store.complete_job(
        old_job["id"],
        [
            {
                "transaction_id": "LATE",
                "trip_id": "T1",
                "swipe_time": "2026-11-15T08:00:00Z",
                "route_index": 1,
                "stop_name": "A",
                "status": "matched",
                "reason": "",
            }
        ],
    )
    assert accepted is False
    # 旧作业没有关联行，当前下载依然停供
    assert store.result_csv() is None

    # 新作业完成后才开放
    assert worker.run_once() is True
    csv_text = store.result_csv()
    assert csv_text is not None
    assert "LATE" not in csv_text
    assert "S1" in csv_text


def test_failed_job_keeps_download_unavailable_and_retries(
    store: Storage, monkeypatch
) -> None:
    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")

    from app import worker as worker_module

    def boom(*args, **kwargs):
        raise RuntimeError("模拟计算失败")

    monkeypatch.setattr(worker_module, "compute_associations", boom)
    worker = worker_module.RecalcWorker(store)
    assert worker.run_once() is True
    job = store.current_job()
    assert job["status"] == JOB_QUEUED  # 失败后回到队列
    assert "模拟计算失败" in job["last_error"]
    assert store.result_csv() is None

    # 恢复后重试成功
    monkeypatch.undo()
    worker.run_once()
    assert store.current_job()["status"] == JOB_SUCCEEDED
    assert store.result_csv() is not None


def test_concurrent_workers_claim_each_job_once(store: Storage) -> None:
    """两个实例的 worker 并发轮询时，同一条作业只能被一个 runner 认领。"""

    import threading

    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")

    claimed: list[str] = []
    barrier = threading.Barrier(4)

    def claim(runner: str) -> None:
        barrier.wait()
        row = store.claim_next_job(runner)
        if row is not None:
            claimed.append(runner)

    threads = [
        threading.Thread(target=claim, args=(f"runner-{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 1  # 只有一个 runner 认领成功
    job = store.current_job()
    assert job["status"] == JOB_RUNNING
    assert job["attempts"] == 1


def test_restart_recovers_running_job(store: Storage, worker) -> None:
    store.accept_station_position(
        station_filename="s.csv",
        station_content=STATIONS,
        station_sha256="a",
        position_filename="p.csv",
        position_content=POSITIONS,
        position_sha256="b",
    )
    store.accept_swipe(filename="w.csv", content=SWIPES, sha256="c")
    claimed = store.claim_next_job("crashed-runner")
    assert claimed["status"] == JOB_RUNNING
    assert claimed["lease_expires_at"] is not None

    # 活跃租约内：其他 worker 不能抢占，也没有别的作业可认领
    assert store.claim_next_job("other-runner") is None

    # 模拟崩溃后租约过期（直接置空租约，等价于重启后发现的残留 running）
    store.conn.execute(
        "UPDATE jobs SET lease_expires_at = NULL WHERE id = ?", (claimed["id"],)
    )

    # 新进程启动后的 worker：过期/空租约的 running 被重新排队并认领
    new_worker = worker.__class__(store, runner="fresh-runner")
    recovered = new_worker.storage.claim_next_job("fresh-runner")
    assert recovered["id"] == claimed["id"]
    assert recovered["status"] == JOB_RUNNING
    new_worker.process_job(recovered)
    assert store.current_job()["status"] == JOB_SUCCEEDED


def test_illegal_input_is_rejected_without_state_change_api(client) -> None:
    # 先合法受理第一组
    resp = client.post(
        "/api/reconcile/inputs/station-position",
        files={
            "station_file": ("s.csv", io.BytesIO(STATIONS), "text/csv"),
            "position_file": ("p.csv", io.BytesIO(POSITIONS), "text/csv"),
        },
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["version"] == 1

    # 非法刷卡（重复流水号）必须拒收
    bad = (
        "流水号,趟号,时刻UTC\n"
        "S1,T1,2026-11-15T08:00:00Z\n"
        "S1,T1,2026-11-15T08:01:00Z\n"
    ).encode()
    resp = client.post(
        "/api/reconcile/inputs/swipe",
        files={"swipe_file": ("w.csv", io.BytesIO(bad), "text/csv")},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "duplicate_transaction_id"

    # 状态未变：刷卡组仍然不存在
    status = client.get("/api/reconcile/status").json()["data"]["status"]
    assert status["has_swipe"] is False
    assert status["has_station_position"] is True
