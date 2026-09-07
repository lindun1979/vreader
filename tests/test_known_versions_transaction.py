"""Step 3 事务契约（plan M05）：双写失败双回滚、helper 传入 conn 无新连接、坏缓存零写入、
提交后跨连接可见、线程屏障（确认提交后取锁的 worker 必用新集合）。"""
import json
import threading

import pytest

from core import config, db, extract as ex_mod, lock


def _rec(canonical, series, version, variant, bug_id="G001", score=2, conf=0.9):
    return {"model_canonical": canonical, "model_raw": "raw", "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": "黄金",
            "bug_id": bug_id, "score": score, "rounds": 1, "solved": True,
            "evidence_quote": "证据够长的句子啦啦", "confidence": conf}


def _v2ex(aid, records):
    return {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
            "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
            "records": records}


def _write(aid, ex):
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")


def test_two_writes_double_rollback(conn, monkeypatch):
    aid = "tx1"
    r1 = _rec("GLM-5.4", "GLM", "5.4", "", bug_id="G001")
    r2 = _rec("GLM-5.5", "GLM", "5.5", "", bug_id="G002")
    ex = _v2ex(aid, [r1, r2])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))

    def boom(*a, **k):
        raise RuntimeError("inject fail")
    monkeypatch.setattr(db, "register_known_version", boom)
    with pytest.raises(RuntimeError):
        ex_mod.approve_pending_for_video(conn, aid, "admin")
    # 回滚：决策仍是 pending_new_version，known 无新行
    for r in (r1, r2):
        rid = ex_mod.record_id(aid, r)
        assert db.get_decision(conn, rid)["decision"] == db.PENDING_NEW_VERSION
    assert ("GLM", "5.4", "") not in db.list_known_versions(conn)


def test_register_uses_passed_conn_and_commit_visibility(conn, db_path):
    c2 = db.connect(db_path)
    try:
        db.register_known_version(conn, "GLM", "5.4", "", "admin", commit=False)
        # 未提交：另一连接不可见（helper 只用传入 conn，未自开连接自提交）
        assert ("GLM", "5.4", "") not in db.list_known_versions(c2)
        conn.commit()
        # 提交后跨连接可见
        assert ("GLM", "5.4", "") in db.list_known_versions(c2)
    finally:
        c2.close()


def test_bad_cache_zero_writes(conn):
    aid = "txbad"
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text("{corrupt", encoding="utf-8")
    n, reg = ex_mod.approve_pending_for_video(conn, aid, "admin")
    assert (n, reg) == (0, [])
    assert not db.list_decisions_for_video(conn, aid)


def test_missing_extract_zero_writes(conn):
    n, reg = ex_mod.approve_pending_for_video(conn, "nonexist", "admin")
    assert (n, reg) == (0, [])


def test_thread_barrier_worker_uses_new_set_after_confirm(db_path):
    """确认线程登记+提交 GLM-5.4 后，worker 才取得 publish_lock 读集合 → 必用新集合
    （该记录判为已知版本 → auto_ok，而非 pending_new_version）。各线程独立连接。"""
    aid = "bar1"
    db.init(db_path)
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    _write(aid, ex)
    confirm_committed = threading.Event()
    worker_decision = {}

    def confirm_thread():
        c = db.connect(db_path)
        try:
            with lock.publish_lock:
                db.register_known_version(c, "GLM", "5.4", "", "admin", commit=True)
            confirm_committed.set()
        finally:
            c.close()

    def worker_thread():
        c = db.connect(db_path)
        try:
            confirm_committed.wait(5)  # 屏障：确认提交后才进锁读集合
            with lock.publish_lock:
                known_b = db.list_known_versions(c)  # 锁内读已提交（M05 时点定义）
                ex_mod.apply_decisions(c, aid, ex, known=known_b)
            rid = ex_mod.record_id(aid, ex["records"][0])
            worker_decision["d"] = db.get_decision(c, rid)["decision"]
        finally:
            c.close()

    t1 = threading.Thread(target=confirm_thread)
    t2 = threading.Thread(target=worker_thread)
    t2.start()
    t1.start()
    t1.join(5)
    t2.join(5)
    assert worker_decision["d"] == db.AUTO_OK  # 用了确认后的新集合


def test_thread_barrier_worker_before_confirm_is_new_version(db_path):
    """对照：worker 在确认前读集合 → 该版本未知 → pending_new_version。"""
    aid = "bar2"
    db.init(db_path)
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    _write(aid, ex)
    c = db.connect(db_path)
    try:
        with lock.publish_lock:
            known_b = db.list_known_versions(c)  # 确认尚未发生
            ex_mod.apply_decisions(c, aid, ex, known=known_b)
        rid = ex_mod.record_id(aid, ex["records"][0])
        assert db.get_decision(c, rid)["decision"] == db.PENDING_NEW_VERSION
    finally:
        c.close()
