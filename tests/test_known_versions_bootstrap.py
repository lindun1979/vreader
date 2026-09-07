"""Step 1 引导：db.init 建表+seed 幂等、confirm 不被 seed 覆盖、纯读入口不建库（M07）。"""
import sqlite3

import pytest

from core import db, models


def test_init_seeds_all_legacy_triples(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    kv = db.list_known_versions(c)
    # seed 三元组拼合结果恰等于冻结 legacy 名单
    comp = {models.compose_canonical(s, v, var) for s, v, var in kv}
    assert comp == set(models.LEGACY_CANONICALS)
    c.close()


def test_serve_and_cli_paths_seed_identically(tmp_path):
    """serve 与 CLI 写路径都过 db.init → 两库 seed 完全一致。"""
    p1, p2 = str(tmp_path / "a.db"), str(tmp_path / "b.db")
    db.init(p1)
    db.init(p2)
    c1, c2 = db.connect(p1), db.connect(p2)
    assert db.list_known_versions(c1) == db.list_known_versions(c2)
    c1.close()
    c2.close()


def test_seed_idempotent(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    n0 = len(db.list_known_versions(c))
    db.seed_known_versions(c)
    db.seed_known_versions(c)
    assert len(db.list_known_versions(c)) == n0
    c.close()


def test_confirm_row_not_overwritten_by_seed(db_path):
    """先有 confirm 行的三元组，再 seed 不覆盖其 source/审计（OR IGNORE）。"""
    db.init(db_path)  # 已 seed
    c = db.connect(db_path)
    # 对一个 seed 三元组用 register（confirm）—— OR IGNORE 不覆盖，仍是 seed
    db.register_known_version(c, "GLM", "5.3", "", "admin", commit=True)
    src = c.execute("SELECT source FROM known_versions WHERE series='GLM' AND version='5.3'"
                    " AND variant=''").fetchone()["source"]
    assert src == "seed"
    # 新三元组先 confirm，再 seed（seed 无此项）→ 仍 confirm
    db.register_known_version(c, "GLM", "5.9", "", "admin", commit=True)
    db.seed_known_versions(c)
    src2 = c.execute("SELECT source FROM known_versions WHERE series='GLM' AND version='5.9'"
                     " AND variant=''").fetchone()["source"]
    assert src2 == "confirm"
    c.close()


def test_pure_read_connect_does_not_create_table(tmp_path):
    """纯读入口（未 init）连接 → known_versions 表不存在（不建库）。"""
    p = str(tmp_path / "noinit.db")
    c = db.connect(p)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("SELECT * FROM known_versions")
    c.close()
