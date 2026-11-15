"""SQLite 持久层：输入版本、重算作业、关联结果与下载可用性。

两组输入分别版本化
------------------
* ``station_position`` 组：站点 CSV + 定位 CSV，作为一个整体整份更正，
  同一次受理写入两条内容行（kind = station / position），共享一个版本号。
* ``swipe`` 组：刷卡 CSV，单独整份更正（kind = swipe）。

作业模型
--------
每次任一组产生新版本，都登记一条引用“两组当前最新已受理版本”的重算作业。
作业状态机：``queued -> running -> succeeded | failed``（失败回到 queued 等待重试）。
只有状态为 succeeded 且引用版本仍为两组最新版本、且有关联行的结果才开放下载；
迟到完成的旧作业不会覆盖新作业的状态或结果。

并发
----
多个实例/线程共享同一个 SQLite 文件（WAL 模式 + busy_timeout），
用条件 UPDATE 的原子性认领作业，保证同一条作业同一时刻只被一个 worker 执行。
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

GROUP_STATION_POSITION = "station_position"
GROUP_SWIPE = "swipe"
GROUPS = (GROUP_STATION_POSITION, GROUP_SWIPE)

KIND_STATION = "station"
KIND_POSITION = "position"
KIND_SWIPE = "swipe"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

# running 作业的租约时长：超过该时间没有心跳/完成即视为 worker 崩溃，可被重新认领
LEASE_SECONDS = 60

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_name TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    filename TEXT NOT NULL,
    content BLOB NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    accepted_at TEXT NOT NULL,
    UNIQUE(group_name, version, kind)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    station_position_version INTEGER NOT NULL,
    swipe_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    runner TEXT,
    lease_expires_at TEXT,
    UNIQUE(station_position_version, swipe_version)
);

CREATE TABLE IF NOT EXISTS associations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    transaction_id TEXT NOT NULL,
    trip_id TEXT NOT NULL,
    swipe_time TEXT NOT NULL,
    route_index INTEGER,
    stop_name TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    row_order INTEGER NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_associations_job ON associations(job_id, row_order);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_future_iso(seconds: float) -> str:
    moment = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + seconds, tz=timezone.utc
    )
    return moment.isoformat().replace("+00:00", "Z")


class Storage:
    """线程安全的 SQLite 封装；多实例通过文件锁协作。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    # ---------- 连接管理 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # 自动提交，事务手动 BEGIN/COMMIT
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            # executescript 会隐式提交，DDL 本身可重入（IF NOT EXISTS），无需显式事务
            conn.executescript(_SCHEMA)
            # 轻量迁移：旧版库的 jobs 表没有租约列
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "lease_expires_at" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN lease_expires_at TEXT")
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        finally:
            conn.close()

    # ---------- 版本管理 ----------

    def latest_version_number(self, group_name: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM input_versions WHERE group_name = ?",
            (group_name,),
        ).fetchone()
        return int(row["v"])

    def latest_version(self, group_name: str) -> Optional[sqlite3.Row]:
        """返回该组最新版本的代表性行（刷卡组即唯一行）。"""

        version = self.latest_version_number(group_name)
        if version == 0:
            return None
        return self.conn.execute(
            "SELECT * FROM input_versions WHERE group_name = ? AND version = ? "
            "ORDER BY id LIMIT 1",
            (group_name, version),
        ).fetchone()

    def get_version_contents(
        self, group_name: str, version: int
    ) -> Optional[dict[str, sqlite3.Row]]:
        rows = self.conn.execute(
            "SELECT * FROM input_versions WHERE group_name = ? AND version = ?",
            (group_name, version),
        ).fetchall()
        if not rows:
            return None
        return {row["kind"]: row for row in rows}

    def list_versions(self, group_name: str) -> list[dict[str, Any]]:
        version_numbers = [
            row["version"]
            for row in self.conn.execute(
                "SELECT DISTINCT version FROM input_versions WHERE group_name = ? "
                "ORDER BY version DESC",
                (group_name,),
            ).fetchall()
        ]
        result: list[dict[str, Any]] = []
        for number in version_numbers:
            contents = self.get_version_contents(group_name, number)
            assert contents is not None
            first = next(iter(contents.values()))
            result.append(
                {
                    "version": number,
                    "accepted_at": first["accepted_at"],
                    "files": [
                        {
                            "kind": row["kind"],
                            "filename": row["filename"],
                            "byte_size": row["byte_size"],
                            "sha256": row["sha256"],
                        }
                        for row in contents.values()
                    ],
                }
            )
        return result

    def accept_station_position(
        self,
        *,
        station_filename: str,
        station_content: bytes,
        station_sha256: str,
        position_filename: str,
        position_content: bytes,
        position_sha256: str,
    ) -> int:
        """受理站点+定位整组更正并登记重算作业，返回新版本号。

        解析在调用前完成；本方法只持久化。新作业在同一事务内入队，
        因此受理成功的瞬间旧结果即停供、页面显示待重算。
        """

        return self._accept_group(
            GROUP_STATION_POSITION,
            [
                (KIND_STATION, station_filename, station_content, station_sha256),
                (KIND_POSITION, position_filename, position_content, position_sha256),
            ],
        )

    def accept_swipe(
        self, *, filename: str, content: bytes, sha256: str
    ) -> int:
        """受理刷卡整组更正并登记重算作业，返回新版本号。"""

        return self._accept_group(
            GROUP_SWIPE,
            [(KIND_SWIPE, filename, content, sha256)],
        )

    def _accept_group(
        self,
        group_name: str,
        files: list[tuple[str, str, bytes, str]],
    ) -> int:
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            new_version = self.latest_version_number(group_name) + 1
            now = utc_now_iso()
            for kind, filename, content, digest in files:
                conn.execute(
                    "INSERT INTO input_versions"
                    "(group_name, version, kind, filename, content, byte_size, "
                    " sha256, accepted_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        group_name,
                        new_version,
                        kind,
                        filename,
                        content,
                        len(content),
                        digest,
                        now,
                    ),
                )

            target_sp = self.latest_version_number(GROUP_STATION_POSITION)
            target_sw = self.latest_version_number(GROUP_SWIPE)
            if target_sp > 0 and target_sw > 0:
                conn.execute(
                    "INSERT OR IGNORE INTO jobs"
                    "(station_position_version, swipe_version, status, attempts, "
                    " created_at, updated_at) VALUES(?, ?, ?, 0, ?, ?)",
                    (target_sp, target_sw, JOB_QUEUED, now, now),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return new_version

    # ---------- 作业认领与状态 ----------

    def claim_next_job(self, runner: str) -> Optional[sqlite3.Row]:
        """原子认领一条可执行作业。

        可执行作业包括：
        * 状态为 queued 的作业（正常排队、失败重试）；
        * 状态为 running 但租约已过期的作业（worker 崩溃/进程重启后的接续）。

        仍在租约期内的 running 作业不会被其他 worker 抢占，
        因此多个实例并发轮询时同一条作业同一时刻只会被一个 runner 执行。
        """

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now_iso()
            lease = utc_future_iso(LEASE_SECONDS)
            # 仅回收租约过期的 running 作业；活跃 worker 的作业保持 running
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ?, runner = NULL, "
                "lease_expires_at = NULL WHERE status = ? AND "
                "(lease_expires_at IS NULL OR lease_expires_at <= ?)",
                (JOB_QUEUED, now, JOB_RUNNING, now),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY id LIMIT 1",
                (JOB_QUEUED,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE jobs SET status = ?, attempts = attempts + 1, "
                "updated_at = ?, runner = ?, lease_expires_at = ? WHERE id = ?",
                (JOB_RUNNING, now, runner, lease, row["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()

    def heartbeat_job(self, job_id: int) -> None:
        """续租，防止长计算在 worker 存活时被误判为崩溃。"""

        self.conn.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND status = ?",
            (utc_future_iso(LEASE_SECONDS), job_id, JOB_RUNNING),
        )

    def latest_versions(self) -> dict[str, int]:
        return {
            GROUP_STATION_POSITION: self.latest_version_number(GROUP_STATION_POSITION),
            GROUP_SWIPE: self.latest_version_number(GROUP_SWIPE),
        }

    def job_is_current(self, job: sqlite3.Row) -> bool:
        latest = self.latest_versions()
        return (
            job["station_position_version"] == latest[GROUP_STATION_POSITION]
            and job["swipe_version"] == latest[GROUP_SWIPE]
        )

    def complete_job(self, job_id: int, rows: list[dict[str, Any]]) -> bool:
        """作业成功：仅当作业仍引用两组最新版本时才写入结果并置 succeeded。

        返回 False 表示该作业已过时（两组中出现了更新的已受理输入），
        调用方应丢弃结果——迟到计算不得覆盖当前结果。
        """

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                conn.execute("ROLLBACK")
                return False
            latest = self.latest_versions()
            is_current = (
                job["station_position_version"] == latest[GROUP_STATION_POSITION]
                and job["swipe_version"] == latest[GROUP_SWIPE]
            )
            now = utc_now_iso()
            if not is_current:
                conn.execute(
                    "UPDATE jobs SET status = ?, last_error = ?, updated_at = ?, "
                    "runner = NULL, lease_expires_at = NULL WHERE id = ?",
                    (
                        JOB_SUCCEEDED,
                        "作业完成时输入已更新，结果已按规则丢弃（迟到计算不覆盖）",
                        now,
                        job_id,
                    ),
                )
                conn.execute("COMMIT")
                return False

            conn.execute("DELETE FROM associations WHERE job_id = ?", (job_id,))
            conn.executemany(
                "INSERT INTO associations"
                "(job_id, transaction_id, trip_id, swipe_time, route_index, "
                " stop_name, status, reason, row_order) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        job_id,
                        item["transaction_id"],
                        item["trip_id"],
                        item["swipe_time"],
                        item["route_index"],
                        item["stop_name"],
                        item["status"],
                        item["reason"],
                        order,
                    )
                    for order, item in enumerate(rows)
                ],
            )
            conn.execute(
                "UPDATE jobs SET status = ?, last_error = NULL, updated_at = ?, "
                "runner = NULL, lease_expires_at = NULL WHERE id = ?",
                (JOB_SUCCEEDED, now, job_id),
            )
            conn.execute("COMMIT")
            return True
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def fail_job(self, job_id: int, error: str) -> None:
        """作业失败：保持停供（回到 queued 以便自动/手动重试）。"""

        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            now = utc_now_iso()
            conn.execute(
                "UPDATE jobs SET status = ?, last_error = ?, updated_at = ?, "
                "runner = NULL, lease_expires_at = NULL WHERE id = ?",
                (JOB_QUEUED, error, now, job_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def current_job(self) -> Optional[sqlite3.Row]:
        """对应两组最新已受理输入的作业。"""

        latest = self.latest_versions()
        if latest[GROUP_STATION_POSITION] == 0 or latest[GROUP_SWIPE] == 0:
            return None
        return self.conn.execute(
            "SELECT * FROM jobs WHERE station_position_version = ? AND swipe_version = ?",
            (latest[GROUP_STATION_POSITION], latest[GROUP_SWIPE]),
        ).fetchone()

    def list_jobs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        )

    # ---------- 结果读取与导出 ----------

    def current_result_rows(self) -> Optional[list[sqlite3.Row]]:
        """仅当最新作业 succeeded、为当前版本且有关联行时返回，否则 None（停供）。"""

        job = self.current_job()
        if job is None or job["status"] != JOB_SUCCEEDED:
            return None
        if not self.job_is_current(job):
            return None
        count = self.conn.execute(
            "SELECT COUNT(*) AS n FROM associations WHERE job_id = ?", (job["id"],)
        ).fetchone()["n"]
        if count == 0:
            return None
        return list(
            self.conn.execute(
                "SELECT * FROM associations WHERE job_id = ? ORDER BY row_order",
                (job["id"],),
            ).fetchall()
        )

    def result_csv(self) -> Optional[str]:
        """生成下载 CSV：流水号、站序及两组输入版本等。"""

        rows = self.current_result_rows()
        if rows is None:
            return None
        job = self.current_job()
        assert job is not None
        sp_version = job["station_position_version"]
        swipe_version = job["swipe_version"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\r\n")
        writer.writerow(
            [
                "流水号",
                "趟号",
                "刷卡时刻UTC",
                "站序",
                "站名",
                "关联状态",
                "未匹配原因",
                "站点定位输入版本",
                "刷卡输入版本",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row["transaction_id"],
                    row["trip_id"],
                    row["swipe_time"],
                    "" if row["route_index"] is None else row["route_index"],
                    row["stop_name"],
                    "已匹配" if row["status"] == "matched" else "未匹配",
                    row["reason"],
                    sp_version,
                    swipe_version,
                ]
            )
        return "﻿" + buffer.getvalue()

    # ---------- 页面状态汇总 ----------

    def status(self) -> dict[str, Any]:
        latest_sp = self.latest_version(GROUP_STATION_POSITION)
        latest_sw = self.latest_version(GROUP_SWIPE)
        job = self.current_job()
        download_available = self.current_result_rows() is not None
        job_state: Optional[dict[str, Any]] = None
        if job is not None:
            job_state = {
                "id": job["id"],
                "station_position_version": job["station_position_version"],
                "swipe_version": job["swipe_version"],
                "status": job["status"],
                "status_label": _JOB_STATUS_LABELS.get(job["status"], job["status"]),
                "attempts": job["attempts"],
                "last_error": job["last_error"],
                "updated_at": job["updated_at"],
                "is_current": self.job_is_current(job),
            }
        return {
            "has_station_position": latest_sp is not None,
            "has_swipe": latest_sw is not None,
            "station_position": _version_brief(self, GROUP_STATION_POSITION),
            "swipe": _version_brief(self, GROUP_SWIPE),
            "job": job_state,
            "download_available": download_available,
            "pending_recalc": job is not None
            and job["status"] in (JOB_QUEUED, JOB_RUNNING),
        }


_JOB_STATUS_LABELS = {
    JOB_QUEUED: "待重算",
    JOB_RUNNING: "重算中",
    JOB_SUCCEEDED: "已完成",
    JOB_FAILED: "失败",
}


def _version_brief(storage: Storage, group_name: str) -> Optional[dict[str, Any]]:
    version = storage.latest_version_number(group_name)
    if version == 0:
        return None
    contents = storage.get_version_contents(group_name, version)
    assert contents is not None
    first = next(iter(contents.values()))
    return {
        "version": version,
        "accepted_at": first["accepted_at"],
        "files": [
            {
                "kind": row["kind"],
                "filename": row["filename"],
                "byte_size": row["byte_size"],
                "sha256": row["sha256"],
            }
            for row in contents.values()
        ],
    }


def dumps_job_error(exc: BaseException) -> str:
    return json.dumps(
        {"type": type(exc).__name__, "message": str(exc)}, ensure_ascii=False
    )
