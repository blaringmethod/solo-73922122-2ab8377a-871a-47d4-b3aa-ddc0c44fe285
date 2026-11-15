"""pytest 共享配置：确保仓库根目录在导入路径中，并提供归站测试夹具。"""

from __future__ import annotations

import itertools
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="reconcile-tests-"))
_DB_SEQ = itertools.count()


@pytest.fixture()
def db_path() -> Path:
    path = _TMP_DIR / f"reconcile-{os.getpid()}-{next(_DB_SEQ)}.db"
    os.environ["BUS_RECONCILE_DB"] = str(path)
    yield path
    for suffix in ("", "-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.exists():
            extra.unlink()


@pytest.fixture()
def client(db_path: Path) -> TestClient:
    # 延迟导入，确保 BUS_RECONCILE_DB 已指向临时库
    from app import reconcile_api
    from app.main import app
    from app.storage import Storage
    from app.worker import RecalcWorker

    store = Storage(db_path)
    sync_worker = RecalcWorker(store, poll_interval=0.01)

    with TestClient(app) as test_client:
        # TestClient 启动 lifespan 会建后台 worker；停掉它，测试只用同步可控 worker，
        # 避免后台线程抢作业导致状态机测试出现竞态。
        from app import reconcile_api as _api

        lifespan_worker = _api.worker
        lifespan_worker.stop()
        _api.storage = store
        _api.worker = sync_worker
        yield test_client

    sync_worker.stop()


@pytest.fixture()
def store(client: TestClient):
    from app import reconcile_api

    return reconcile_api.storage


@pytest.fixture()
def worker(client: TestClient):
    from app import reconcile_api

    return reconcile_api.worker
