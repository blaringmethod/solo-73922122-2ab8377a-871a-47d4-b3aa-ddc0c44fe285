"""重算 worker：轮询 SQLite 作业队列并执行关联计算。

多个实例（容器）各跑一个后台线程，通过 ``Storage.claim_next_job`` 的
条件 UPDATE 互斥认领，因此同一条作业同一时刻只会被一个实例执行。
进程重启后，残留在 running 的作业会在下次认领时自动重新排队接续。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from typing import Optional

from .association import compute_associations
from .storage import (
    GROUP_STATION_POSITION,
    GROUP_SWIPE,
    KIND_POSITION,
    KIND_STATION,
    Storage,
    dumps_job_error,
)

logger = logging.getLogger("bus_stop.worker")

DEFAULT_POLL_INTERVAL = 1.0


class RecalcWorker:
    def __init__(
        self,
        storage: Storage,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        runner: Optional[str] = None,
    ) -> None:
        self.storage = storage
        self.poll_interval = poll_interval
        self.runner = (
            runner
            or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def process_job(self, job) -> None:
        """执行一条已认领的作业，按成败更新作业状态。"""

        job_id = job["id"]
        sp = self.storage.get_version_contents(
            GROUP_STATION_POSITION, job["station_position_version"]
        )
        sw = self.storage.get_version_contents(GROUP_SWIPE, job["swipe_version"])
        if sp is None or sw is None or KIND_STATION not in sp or KIND_POSITION not in sp:
            raise RuntimeError("作业引用的输入版本不存在或不完整")
        station_row = sp[KIND_STATION]
        position_row = sp[KIND_POSITION]
        swipe_row = next(iter(sw.values()))
        rows = compute_associations(
            station_row["content"],
            position_row["content"],
            swipe_row["content"],
            station_filename=station_row["filename"],
            position_filename=position_row["filename"],
            swipe_filename=swipe_row["filename"],
        )
        payload = [
            {
                "transaction_id": row.transaction_id,
                "trip_id": row.trip_id,
                "swipe_time": row.swipe_time,
                "route_index": row.route_index,
                "stop_name": row.stop_name,
                "status": row.status,
                "reason": row.reason,
            }
            for row in rows
        ]
        self.storage.complete_job(job_id, payload)

    def run_once(self) -> bool:
        """认领并处理一条作业。返回是否处理了作业（无论成败）。"""

        job = self.storage.claim_next_job(self.runner)
        if job is None:
            return False
        job_id = job["id"]
        logger.info("worker %s 认领作业 %s", self.runner, job_id)
        try:
            self.process_job(job)
        except Exception as exc:  # noqa: BLE001 - 任何失败都要落库保持停供
            logger.exception("作业 %s 计算失败", job_id)
            self.storage.fail_job(job_id, dumps_job_error(exc))
        return True

    def _loop(self) -> None:
        logger.info("重算 worker 启动（runner=%s）", self.runner)
        while not self._stop.is_set():
            try:
                handled = self.run_once()
            except Exception:  # noqa: BLE001 - 循环不能因单次异常退出
                logger.exception("worker 轮询异常")
                handled = False
            if not handled:
                self._stop.wait(self.poll_interval)
        logger.info("重算 worker 停止（runner=%s）", self.runner)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="recalc-worker", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
