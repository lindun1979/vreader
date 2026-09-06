"""V-M15：rid 迁移（C9 v6）——目标已存在无 UNIQUE 冲突、优先级合并、幂等、
前置校验拒绝、APPROVED+rejected 并存标人工、DELETE 排除目标不清空。"""
import time

from core import db


def _seed(c, rid, aweme, decision, approved_by=None, approved_at=None):
    db.upsert_decision(c, record_id=rid, aweme_id=aweme, decision=decision,
                       extractor_version="e", prompt_hash="p",
                       approved_by=approved_by, approved_at=approved_at)


def test_many_to_one_no_unique_conflict_and_keeps_one_row(db_path):
    db.init(db_path); c = db.connect(db_path)
    # 目标 rid 属于源集合（多对一）
    _seed(c, "T", "v1", db.AUTO_OK)
    _seed(c, "S1", "v1", db.APPROVED, approved_by="admin", approved_at=100.0)
    res = db.migrate_rid_group(c, "T", ["T", "S1"], "v1")
    assert res == "migrated"
    rows = c.execute("SELECT * FROM record_decisions WHERE aweme_id='v1'").fetchall()
    assert len(rows) == 1                       # 恰好一条（非空表）
    assert rows[0]["record_id"] == "T"
    assert rows[0]["decision"] == db.APPROVED   # 优先级 APPROVED 胜
    assert rows[0]["approved_by"] == "admin"    # 审批来源保留
    c.close()


def test_multiple_approved_takes_earliest(db_path):
    db.init(db_path); c = db.connect(db_path)
    _seed(c, "T", "v1", db.APPROVED, approved_by="late", approved_at=200.0)
    _seed(c, "S1", "v1", db.APPROVED, approved_by="early", approved_at=100.0)
    db.migrate_rid_group(c, "T", ["T", "S1"], "v1")
    row = c.execute("SELECT * FROM record_decisions WHERE record_id='T'").fetchone()
    assert row["approved_by"] == "early" and row["approved_at"] == 100.0
    c.close()


def test_idempotent_rerun(db_path):
    db.init(db_path); c = db.connect(db_path)
    _seed(c, "T", "v1", db.AUTO_OK)
    _seed(c, "S1", "v1", db.APPROVED, approved_by="a", approved_at=1.0)
    assert db.migrate_rid_group(c, "T", ["T", "S1"], "v1") == "migrated"
    # 再跑一次：非目标源已消失，目标已是预期 → no-op
    assert db.migrate_rid_group(c, "T", ["T", "S1"], "v1") == "noop"
    assert len(c.execute("SELECT * FROM record_decisions WHERE aweme_id='v1'").fetchall()) == 1
    c.close()


def test_precondition_mismatch_rejected(db_path):
    db.init(db_path); c = db.connect(db_path)
    _seed(c, "S1", "v1", db.APPROVED, approved_by="a", approved_at=1.0)
    # 审计单登记 S1 是 pending，但现状是 approved（迁移后有新裁决）→ 拒绝
    res = db.migrate_rid_group(c, "T", ["T", "S1"], "v1",
                               expected_before={"S1": db.PENDING})
    assert res == "rejected_precondition"
    # 未改动
    assert c.execute("SELECT decision FROM record_decisions WHERE record_id='S1'").fetchone()["decision"] == db.APPROVED
    c.close()


def test_approved_plus_rejected_flagged_manual(db_path):
    db.init(db_path); c = db.connect(db_path)
    _seed(c, "S1", "v1", db.APPROVED, approved_by="a", approved_at=1.0)
    _seed(c, "S2", "v1", db.REJECTED_CONFLICT)
    res = db.migrate_rid_group(c, "T", ["S1", "S2"], "v1")
    assert res == "rejected_manual"
    assert len(c.execute("SELECT * FROM record_decisions WHERE aweme_id='v1'").fetchall()) == 2  # 不动
    c.close()


def test_single_source_rename(db_path):
    db.init(db_path); c = db.connect(db_path)
    _seed(c, "OLD", "v1", db.APPROVED, approved_by="a", approved_at=1.0)
    assert db.migrate_rid_group(c, "NEW", ["OLD"], "v1") == "migrated"
    assert c.execute("SELECT record_id FROM record_decisions WHERE aweme_id='v1'").fetchone()["record_id"] == "NEW"
    c.close()
