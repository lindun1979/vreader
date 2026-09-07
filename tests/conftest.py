import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """M09 全量隔离：把 config.DATA_DIR 指向每个用例独立的 tmp 目录，任何读消费点
    （gladia 热词、seed、known_versions 短连接）都落在 tmp，绝不碰生产数据目录。"""
    from core import config
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr("core.service._DB_PATH", None, raising=False)
    return d


@pytest.fixture
def data_dir(_isolate_data_dir):
    """复用 autouse 隔离目录（已指向 tmp）。"""
    return _isolate_data_dir


@pytest.fixture
def conn(data_dir, monkeypatch):
    from core import db, service
    dbp = str(data_dir / "vreader.db")
    monkeypatch.setattr(service, "_DB_PATH", dbp)  # 真 monkeypatch：测试后自动恢复
    db.init(dbp)
    c = db.connect(dbp)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _reset_service_health():
    """隔离 service 模块级共享健康态（避免测试间串扰）。"""
    yield
    try:
        from core import service
        service._health_set(
            worker_alive=False, worker_last_beat=0.0, worker_last_claim_at=0.0,
            worker_busy_until=0.0, outbox_alive=False, outbox_last_beat=0.0,
            db_consec_errors=0, paused_asr_orphan=False)
        service._DB_PATH = None
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture
def db_path(data_dir):
    return str(data_dir / "vreader.db")
