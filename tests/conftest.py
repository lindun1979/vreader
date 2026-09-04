import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 config 的数据目录指到 tmp，隔离每个测试。"""
    from core import config
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", d)
    return d


@pytest.fixture
def conn(data_dir):
    from core import db, service
    dbp = str(data_dir / "vreader.db")
    monkeypatch_dbpath(service, dbp)
    db.init(dbp)
    c = db.connect(dbp)
    yield c
    c.close()


def monkeypatch_dbpath(service, path):
    service._DB_PATH = path


@pytest.fixture
def db_path(data_dir):
    return str(data_dir / "vreader.db")
