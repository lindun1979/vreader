"""M09 测试隔离：新消费点打开的 SQLite 路径在 tmp 下；哨兵「生产目录」前后无变化。"""
from pathlib import Path

from core import config, db, extract as ex_mod


def _snapshot(d: Path):
    return {p.name: p.stat().st_mtime_ns for p in d.iterdir()} if d.exists() else {}


def test_gladia_hotwords_touches_tmp_not_sentinel(tmp_path):
    # 哨兵「生产目录」：任何消费点都不该碰它
    sentinel = tmp_path / "PROD_DO_NOT_TOUCH"
    sentinel.mkdir()
    (sentinel / "vreader.db").write_text("SENTINEL", encoding="utf-8")
    before = _snapshot(sentinel)

    # config.DATA_DIR 已被 autouse 指向另一 tmp；gladia_hotwords 开的库应落在那里
    assert config.DATA_DIR != sentinel
    words = ex_mod.gladia_hotwords()
    assert "青铜" in words and "GLM" in words  # 系列裸名 + 等级词固定保留

    # 哨兵目录字节不变（未被打开/写入）
    assert _snapshot(sentinel) == before
    assert (sentinel / "vreader.db").read_text(encoding="utf-8") == "SENTINEL"


def test_seed_and_known_reads_go_to_tmp(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    # 打开的库路径在 tmp（config.DATA_DIR）下
    assert str(config.DATA_DIR) in db_path
    assert len(db.list_known_versions(c)) == 22
    c.close()


def test_gladia_hotwords_capped_at_100(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    # 灌入大量已知版本，验证截断（系列裸名+等级词固定保留，总量 ≤ 100）
    for i in range(200):
        db.register_known_version(c, "GLM", f"9.{i}", "", "admin", commit=False)
    c.commit()
    words = ex_mod.gladia_hotwords(c)
    assert len(words) <= ex_mod._HOTWORD_CAP
    for lv in ["青铜", "白银", "黄金", "钻石", "王者"]:
        assert lv in words  # 等级词固定保留
    c.close()
