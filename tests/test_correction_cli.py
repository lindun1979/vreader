"""V-M16 校正 CLI：--list-corrections 严格只读；--resolve-correction 仅 needs_review。"""
import json
import sqlite3

from core import cli, config, db, extract as ex_mod, routing


def _seed_op(conn, status="needs_review"):
    conn.execute(
        "INSERT INTO correction_operations(op_id, aweme_id, old_rid, new_rid,"
        " target_extract_json, source_extract_sha256, target_extract_sha256, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("op123456", "aw1", "oldrid00", "newrid00", "{}", "s" * 64, "t" * 64, status, 1.0))
    conn.commit()


def test_list_readonly_uses_connect_ro(conn, data_dir, monkeypatch, capsys):
    _seed_op(conn, status="needs_review")
    # 记录 connect_ro 是否被调用、connect(可写) 未被调用
    calls = {"ro": 0, "rw": 0}
    real_ro = db.connect_ro
    monkeypatch.setattr(db, "connect_ro", lambda p: (calls.__setitem__("ro", calls["ro"] + 1), real_ro(p))[1])
    monkeypatch.setattr(db, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not use writable connect")))
    rc = cli.main(["--list-corrections"])
    out = capsys.readouterr().out
    assert rc == 0 and calls["ro"] == 1 and "op123456"[:8] in out and "needs_review" in out


def test_list_no_init_or_journal_mode(conn, data_dir):
    """connect_ro 不写 journal_mode（保持既有模式），且只读连接写操作被拒。"""
    _seed_op(conn)
    ro = db.connect_ro(str(config.DATA_DIR / "vreader.db"))
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1  # 只读模式
        try:
            ro.execute("INSERT INTO correction_operations(op_id,aweme_id,old_rid,new_rid,"
                       "target_extract_json,source_extract_sha256,target_extract_sha256,status,created_at)"
                       " VALUES ('x','a','o','n','{}','s','t','done',1.0)")
            assert False, "只读连接不应允许写"
        except sqlite3.OperationalError:
            pass
    finally:
        ro.close()


def test_resolve_requires_needs_review(conn, data_dir, capsys):
    _seed_op(conn, status="committed")
    ok, msg = ex_mod.resolve_correction(conn, "op123456", "apply-journal")
    assert not ok and "needs_review" in msg


def test_resolve_rejects_done(conn, data_dir):
    _seed_op(conn, status="done")
    ok, msg = ex_mod.resolve_correction(conn, "op123456", "keep-file")
    assert not ok and "needs_review" in msg


def test_resolve_rejects_unknown_op(conn, data_dir):
    ok, msg = ex_mod.resolve_correction(conn, "nope", "keep-file")
    assert not ok and "不存在" in msg


def test_cli_resolve_bad_mode(conn, data_dir, capsys):
    _seed_op(conn)
    # 走 cli.main 写路径需 flock；这里直接验证参数校验分支（bad mode 早返回 1）
    rc = cli.main(["--resolve-correction", "op123456", "bogus"])
    assert rc == 1
