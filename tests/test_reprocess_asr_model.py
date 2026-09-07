"""reprocess 不重转时保留原 ASR 引擎（不把 Gladia 转写误标成默认 SenseVoice）。"""
import json

from core import config, db, extract as ex_mod, pipeline

_QUOTE = "GLM-5.3 一次就修对了表现不错"


def _rec():
    return {"model_raw": "GLM-5.3", "model_canonical": "GLM-5.3", "bug_level": "青铜",
            "bug_id": "B001", "score": 1, "solved": True, "rounds": 1,
            "evidence_quote": _QUOTE, "confidence": 0.9}


def test_reprocess_preserves_asr_model(conn, data_dir, monkeypatch):
    aid = "v1"
    vd = config.video_dir("token_bug", aid)
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "transcript.txt").write_text(f"青铜题 {_QUOTE}，很干净。", encoding="utf-8")
    r = _rec()
    ex = {"video_id": aid, "title": "t", "extracted_at": "x",
          "extractor_version": ex_mod.EXTRACTOR_VERSION, "prompt_hash": "ph",
          "asr_model": "gladia-v2", "records": [r],
          "result_rev": ex_mod.result_rev(aid, [r])}
    (vd / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")
    monkeypatch.setattr(ex_mod, "_call_llm",
                        lambda *a, **k: json.dumps({"records": [r]}, ensure_ascii=False))
    pipeline.reprocess(conn, aid)
    got = json.loads((vd / "extract.json").read_text(encoding="utf-8"))
    assert got["asr_model"] == "gladia-v2"  # 保留原引擎，不误标默认 SenseVoice


def test_build_extract_asr_model_override():
    tx = f"青铜题 {_QUOTE}，很干净。"
    ct = json.dumps({"records": [_rec()]}, ensure_ascii=False)
    ex = ex_mod.build_extract(aweme_id="v", title="t", transcript=tx,
                              claude_text=ct, asr_model="gladia-v2")
    assert ex["asr_model"] == "gladia-v2"
