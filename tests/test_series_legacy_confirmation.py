"""Step 2/3 legacy 确认能力（plan M13）：无 schema_rev 的合法旧记录**无需 LLM 重提**
即可批量/逐条确认，rid 不变，且无需登记；v2 envelope 混入旧形状记录 → 整文件拒绝、
数据库零写入、不自动降级。"""
import json

from core import config, db, extract as ex_mod


def _legacy_rec(canonical, bug_id="G001", score=2, conf=0.5):
    return {"model_canonical": canonical, "model_raw": "raw", "bug_level": "黄金",
            "bug_id": bug_id, "score": score, "rounds": 1, "solved": True,
            "evidence_quote": "证据够长的句子啦啦", "confidence": conf}


def _legacy_ex(aid, records):
    return {"video_id": aid, "title": "t", "extracted_at": "x", "extractor_version": "token_bug/1",
            "prompt_hash": "p", "asr_model": "gladia-v2", "records": records}


def _write(aid, ex):
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")


def test_legacy_pending_batch_confirm_no_register_rid_stable(conn):
    aid = "leg1"
    r = _legacy_rec("GLM-5.3", conf=0.5)  # 低置信 → pending
    ex = _legacy_ex(aid, [r])
    _write(aid, ex)
    rid_before = ex_mod.record_id(aid, r)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    assert db.get_decision(conn, rid_before)["decision"] == db.PENDING  # 旧记录不判新版本
    n, reg = ex_mod.approve_pending_for_video(conn, aid, "admin")
    assert n == 1 and reg == []  # legacy 无需登记
    assert db.get_decision(conn, ex_mod.record_id(aid, r))["decision"] == db.APPROVED
    assert rid_before == ex_mod.record_id(aid, r)  # rid 不变


def test_legacy_conflict_member_confirm_no_register(conn):
    aid = "leg2"
    r1 = _legacy_rec("GLM-5.3", bug_id="G001", score=2)
    r2 = _legacy_rec("GLM-5.3", bug_id="G001", score=1)
    ex = _legacy_ex(aid, [r1, r2])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid1 = ex_mod.record_id(aid, r1)
    assert db.get_decision(conn, rid1)["decision"] == db.PENDING_CONFLICT
    ok, msg = ex_mod.confirm_conflict_member(conn, aid, rid1[:8], "admin")
    assert ok and "已登记新版本" not in msg  # legacy 记录不登记
    # known 未新增
    assert len(db.list_known_versions(conn)) == 22


def test_v2_envelope_with_old_shape_record_rejected_zero_writes(conn):
    aid = "leg3"
    # v2 envelope 但某记录缺 series 三字段（旧形状）
    bad = {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
           "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
           "records": [_legacy_rec("GLM-5.3")]}  # 无 model_series/version/variant
    _write(aid, bad)
    p = str(config.video_dir("token_bug", aid) / "extract.json")
    # 整文件拒绝（load_valid_extract 返回 None，留证）
    out = ex_mod.load_valid_extract(p, expected_video_id=aid)
    assert out is None
    # 数据库零写入（未产生任何决策）
    assert not db.list_decisions_for_video(conn, aid)
