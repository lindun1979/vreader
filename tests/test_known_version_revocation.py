"""Step 5 撤销登记 ≠ 撤销批准（plan M11）：DELETE known_version 后同 rid 仍 APPROVED；
显式撤销流程后回到 pending_new_version。"""
from core import db, extract as ex_mod


def _rec(canonical="GLM-5.4", series="GLM", version="5.4", variant=""):
    return {"model_canonical": canonical, "model_raw": "raw", "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": "黄金",
            "bug_id": "G001", "score": 2, "rounds": 1, "solved": True,
            "evidence_quote": "证据够长的句子啦啦", "confidence": 0.9}


def _v2ex(aid, records):
    return {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
            "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
            "records": records}


def test_delete_registration_keeps_approved(conn):
    aid = "rev1"
    r = _rec()
    ex = _v2ex(aid, [r])
    # 登记 + 批准
    db.register_known_version(conn, "GLM", "5.4", "", "admin", commit=True)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, r)
    db.upsert_decision(conn, record_id=rid, aweme_id=aid, decision=db.APPROVED,
                       extractor_version="e", prompt_hash="p", approved_by="a", approved_at=1.0)
    # 撤销登记（免审资格）：不动既有 APPROVED
    conn.execute("DELETE FROM known_versions WHERE series='GLM' AND version='5.4'")
    conn.commit()
    assert ("GLM", "5.4", "") not in db.list_known_versions(conn)
    # reprocess（apply_decisions 恒存继承先于分类）→ 同 rid 仍 APPROVED
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    assert db.get_decision(conn, rid)["decision"] == db.APPROVED


def test_explicit_revocation_returns_to_pending_new_version(conn):
    aid = "rev2"
    r = _rec()
    ex = _v2ex(aid, [r])
    db.register_known_version(conn, "GLM", "5.4", "", "admin", commit=True)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, r)
    db.upsert_decision(conn, record_id=rid, aweme_id=aid, decision=db.APPROVED,
                       extractor_version="e", prompt_hash="p", approved_by="a", approved_at=1.0)
    # 显式撤销既有批准（手册流程）：DELETE 登记 + 显式 UPDATE 受影响 rid 回 pending_new_version
    conn.execute("DELETE FROM known_versions WHERE series='GLM' AND version='5.4'")
    conn.execute("UPDATE record_decisions SET decision=? WHERE record_id=?",
                 (db.PENDING_NEW_VERSION, rid))
    conn.commit()
    assert db.get_decision(conn, rid)["decision"] == db.PENDING_NEW_VERSION
