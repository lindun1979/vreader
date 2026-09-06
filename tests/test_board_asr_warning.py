"""board 聚合警示：建立在兜底 ASR 上的数据显式可见（不再静默）。"""
from core import db, extract as ex_mod
from channels.token_bug import board


def _rec():
    return {"model_raw": "GLM-5.3", "model_canonical": "GLM-5.3", "bug_level": "青铜",
            "bug_id": "B001", "score": 1, "solved": True, "rounds": 1,
            "evidence_quote": "十个字以上的证据片段内容", "confidence": 0.9}


def _extract(aweme_id, asr_model):
    return {"video_id": aweme_id, "title": "t", "extracted_at": "x",
            "extractor_version": ex_mod.EXTRACTOR_VERSION, "prompt_hash": "ph1",
            "asr_model": asr_model, "records": [_rec()]}


def test_fallback_asr_triggers_board_warning(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    ex = _extract("vSV", "SenseVoiceSmall")
    ex_mod.apply_decisions(c, "vSV", ex)
    md = board.render([ex], db.board_visible_ids(c), ex_mod.record_id)
    assert "数据质量警示" in md and "vSV" in md and "retranscribe" in md
    c.close()


def test_gladia_asr_no_warning(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    ex = _extract("vGL", "gladia-v2")
    ex_mod.apply_decisions(c, "vGL", ex)
    md = board.render([ex], db.board_visible_ids(c), ex_mod.record_id)
    assert "数据质量警示" not in md
    c.close()
