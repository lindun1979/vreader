"""V-M01：身份键 + 归并七规则（C2/C9）反例断言。"""
from core import config, db, extract as ex_mod
from channels.token_bug import board


def _rec(model, level, score, conf, bug_id="B001", canonical=None, quote="十个字以上的证据片段"):
    return {"model_raw": model, "model_canonical": canonical or model, "bug_level": level,
            "bug_id": bug_id, "score": score, "solved": score > 0,
            "rounds": (1 if score > 0 else None), "evidence_quote": quote, "confidence": conf}


def _ex(aid, records):
    return {"video_id": aid, "title": "t", "extracted_at": "x",
            "extractor_version": ex_mod.EXTRACTOR_VERSION, "prompt_hash": "ph",
            "asr_model": "gladia-v2", "records": records}


def test_unknown_model_pending_unknown(db_path):
    db.init(db_path); c = db.connect(db_path)
    r = _rec("乱码模型", "青铜", 1, 0.95, canonical="UNKNOWN")
    ex_mod.apply_decisions(c, "v1", _ex("v1", [r]))
    rid = ex_mod.record_id("v1", r)
    assert db.get_decision(c, rid)["decision"] == db.PENDING_UNKNOWN  # 即便高置信也不 auto_ok
    c.close()


def test_different_unknown_models_not_merged(db_path):
    db.init(db_path); c = db.connect(db_path)
    r1 = _rec("糊音甲", "青铜", 1, 0.9, canonical="UNKNOWN")
    r2 = _rec("糊音乙", "青铜", 1, 0.9, canonical="UNKNOWN")
    assert ex_mod.record_id("v1", r1) != ex_mod.record_id("v1", r2)  # raw 不同 → 不合并


def test_empty_bug_id_forces_pending(db_path):
    db.init(db_path); c = db.connect(db_path)
    r = _rec("GLM-5.3", "青铜", 1, 0.99, bug_id="")
    ex_mod.apply_decisions(c, "v1", _ex("v1", [r]))
    rid = ex_mod.record_id("v1", r)
    assert db.get_decision(c, rid)["decision"] == db.PENDING


def test_empty_bug_id_no_collision(db_path):
    # 两条空 bug_id 但 evidence 不同 → bug_slot 用 evidence 指纹 → 不碰撞
    r1 = _rec("GLM-5.3", "青铜", 1, 0.9, bug_id="", quote="第一段证据内容甲甲甲")
    r2 = _rec("GLM-5.3", "青铜", 1, 0.9, bug_id="", quote="第二段证据内容乙乙乙")
    assert ex_mod.record_id("v1", r1) != ex_mod.record_id("v1", r2)


def test_prefix_conflict_downgrades(db_path):
    db.init(db_path); c = db.connect(db_path)
    r = _rec("GLM-5.3", "钻石", 3, 0.99, bug_id="G005")  # G→黄金，却标钻石
    ex_mod.apply_decisions(c, "v1", _ex("v1", [r]))
    rid = ex_mod.record_id("v1", r)
    assert db.get_decision(c, rid)["decision"] == db.PENDING


def test_conflict_group_both_pending_conflict(db_path):
    db.init(db_path); c = db.connect(db_path)
    # 同 attempt（GLM 青铜 B001）不同 score → 矛盾组，即便一条高置信也不 auto_ok
    hi = _rec("GLM-5.3", "青铜", 1, 0.95, bug_id="B001")
    lo = _rec("GLM-5.3", "青铜", 0, 0.6, bug_id="B001")
    counts = ex_mod.apply_decisions(c, "v1", _ex("v1", [hi, lo]))
    for r in (hi, lo):
        assert db.get_decision(c, ex_mod.record_id("v1", r))["decision"] == db.PENDING_CONFLICT
    assert counts.get("auto_ok", 0) == 0
    c.close()


def test_exact_duplicate_dedup_order_independent(db_path):
    db.init(db_path); c = db.connect(db_path)
    r_a = _rec("GLM-5.3", "青铜", 1, 0.6)
    r_b = _rec("GLM-5.3", "青铜", 1, 0.9)  # 同 rid，更高置信
    counts = ex_mod.apply_decisions(c, "v1", _ex("v1", [r_a, r_b]))
    rid = ex_mod.record_id("v1", r_a)
    assert ex_mod.record_id("v1", r_b) == rid
    assert counts["dup"] == 1
    # 留高置信 → auto_ok
    assert db.get_decision(c, rid)["decision"] == db.AUTO_OK
    c.close()


def test_rejected_conflict_persists_and_not_reopened(db_path):
    db.init(db_path); c = db.connect(db_path)
    r = _rec("GLM-5.3", "青铜", 1, 0.9)
    rid = ex_mod.record_id("v1", r)
    db.upsert_decision(c, record_id=rid, aweme_id="v1", decision=db.REJECTED_CONFLICT,
                       extractor_version="e", prompt_hash="p")
    # 该记录本次缺失：不被置 stale（裁决恒存）
    ex_mod.apply_decisions(c, "v1", _ex("v1", [_rec("Qwen3.8", "白银", 1, 0.9, bug_id="S001")]))
    assert db.get_decision(c, rid)["decision"] == db.REJECTED_CONFLICT
    # 再次出现：仍保持 rejected_conflict（不重开待审）
    ex_mod.apply_decisions(c, "v1", _ex("v1", [r]))
    assert db.get_decision(c, rid)["decision"] == db.REJECTED_CONFLICT
    c.close()


def test_render_defends_double_count(db_path):
    db.init(db_path); c = db.connect(db_path)
    r = _rec("GLM-5.3", "青铜", 1, 0.9)
    ex_mod.apply_decisions(c, "v1", _ex("v1", [r]))
    # 同一 extract 里意外出现两条完全相同记录（同 rid）
    md = board.render([_ex("v1", [r, dict(r)])], db.board_visible_ids(c), ex_mod.record_id)
    # 青铜单元格应为 1/1（只计一次），而非 2/2
    assert "1/1" in md and "2/2" not in md
    c.close()
