"""审批状态跨重跑的身份与版本一致性（review-v4/v5 核心）。"""
from core import db, extract as ex_mod
from channels.token_bug import board


def _rec(model, level, solved, rounds, conf, quote="十个字以上的证据片段内容"):
    return {"model_raw": model, "model_canonical": model, "bug_level": level,
            "solved": solved, "rounds": rounds, "evidence_quote": quote, "confidence": conf}


def _extract(aweme_id, records):
    return {"video_id": aweme_id, "title": "t", "extracted_at": "x",
            "extractor_version": ex_mod.EXTRACTOR_VERSION, "prompt_hash": "ph1",
            "asr_model": "SenseVoiceSmall", "records": records}


def test_auto_ok_vs_pending_by_confidence(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    ex = _extract("v1", [_rec("GLM-5.3", "青铜", True, 1, 0.9),
                         _rec("Kimi K3", "白银", True, 2, 0.5)])
    counts = ex_mod.apply_decisions(c, "v1", ex)
    assert counts["auto_ok"] == 1 and counts["pending"] == 1
    c.close()


def test_renderer_only_shows_visible(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    ex = _extract("v1", [_rec("GLM-5.3", "青铜", True, 1, 0.9),
                         _rec("Kimi K3", "白银", True, 2, 0.5)])  # pending
    ex_mod.apply_decisions(c, "v1", ex)
    md = board.render([ex], db.board_visible_ids(c), ex_mod.record_id)
    assert "GLM-5.3" in md          # auto_ok 上榜
    assert "Kimi K3" not in md    # pending 不上榜
    c.close()


def test_records_reorder_no_misattribution(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    r_gpt = _rec("GLM-5.3", "青铜", True, 1, 0.9)
    r_kimi = _rec("Kimi K3", "白银", True, 2, 0.4)
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r_gpt, r_kimi]))
    # 人工确认 kimi（pending→approved）
    db.approve_pending_for_video(c, "v1", "admin")
    kimi_id = ex_mod.record_id("v1", r_kimi)
    assert db.get_decision(c, kimi_id)["decision"] == db.APPROVED
    # 重跑：records 顺序颠倒，内容不变
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r_kimi, r_gpt]))
    # approved 仍绑在 kimi 那条内容上（凭指纹），没错位到 gpt
    assert db.get_decision(c, kimi_id)["decision"] == db.APPROVED
    gpt_id = ex_mod.record_id("v1", r_gpt)
    assert db.get_decision(c, gpt_id)["decision"] == db.AUTO_OK
    c.close()


def test_confidence_drop_moves_out_of_board(db_path):
    """同事实记录 confidence 0.8→0.6：重跑后从 auto_ok 变 pending，退出主榜。"""
    db.init(db_path)
    c = db.connect(db_path)
    r_hi = _rec("GLM-5.3", "青铜", True, 1, 0.8)
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r_hi]))
    rid = ex_mod.record_id("v1", r_hi)
    assert db.get_decision(c, rid)["decision"] == db.AUTO_OK
    # 重跑，同事实（指纹不变）但置信度掉到 0.6
    r_lo = _rec("GLM-5.3", "青铜", True, 1, 0.6)
    assert ex_mod.record_id("v1", r_lo) == rid  # 指纹不含 confidence
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r_lo]))
    assert db.get_decision(c, rid)["decision"] == db.PENDING  # auto_ok 不沿用
    c.close()


def test_approved_persists_across_rerun(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    r = _rec("Kimi K3", "黄金", True, 3, 0.4)
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r]))
    db.approve_pending_for_video(c, "v1", "admin")
    rid = ex_mod.record_id("v1", r)
    # 重跑同事实记录，即便置信度仍低，approved 继承
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r]))
    assert db.get_decision(c, rid)["decision"] == db.APPROVED
    c.close()


def test_disappeared_record_marked_stale(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    r1 = _rec("GLM-5.3", "青铜", True, 1, 0.9)
    r2 = _rec("Qwen3.8", "白银", True, 1, 0.9)
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r1, r2]))
    id2 = ex_mod.record_id("v1", r2)
    # 重跑只剩 r1
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r1]))
    assert db.get_decision(c, id2)["decision"] == db.STALE
    # 不再上榜
    md = board.render([_extract("v1", [r1])], db.board_visible_ids(c), ex_mod.record_id)
    assert "Qwen3.8" not in md
    c.close()


def test_confirm_promotes_pending(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    r = _rec("Grok 4.6", "钻石", False, None, 0.5)
    ex_mod.apply_decisions(c, "v1", _extract("v1", [r]))
    rid = ex_mod.record_id("v1", r)
    assert db.get_decision(c, rid)["decision"] == db.PENDING
    n = db.approve_pending_for_video(c, "v1", "admin")
    assert n == 1
    assert db.get_decision(c, rid)["decision"] == db.APPROVED
    c.close()
