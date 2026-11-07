"""D-E 贯通：兜底提取 → extract 落盘两字段 → 终态回执（outbox）含提取模型行与降级警示；恢复捷径同样。"""
import json

from core import config, db, extract, pipeline

TX = "青铜这题 GLM-5.3 一次就修对了，表现不错，全程逻辑清晰。"
REC = {"model_raw": "GLM-5.3", "model_series": "GLM", "model_version": "5.3", "model_variant": "",
       "bug_level": "青铜", "bug_id": "B001", "solved_round": 1,
       "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9}


def _outbox(conn):
    return [r[0] for r in conn.execute("SELECT content FROM notification_outbox ORDER BY id")]


def _setup(conn, aid, monkeypatch, reason):
    vd = config.video_dir("token_bug", aid)
    vd.mkdir(parents=True)
    (vd / "transcript.txt").write_text(TX, encoding="utf-8")
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="GLM-5.3 实测")
    model = "openai:gemini-3.5-flash-lite" if reason else "agy:gemini-3.8-flash-medium"
    monkeypatch.setattr(extract, "_call_llm",
                        lambda *a, **k: (json.dumps({"records": [REC]}, ensure_ascii=False), model, reason))
    return vd


def test_fallback_flows_to_extract_and_receipt(conn, monkeypatch):
    vd = _setup(conn, "fb1", monkeypatch, "invalid_model")
    assert pipeline.process_task(conn, db.get_task(conn, "fb1")) == db.SUCCEEDED
    ex = json.loads((vd / "extract.json").read_text(encoding="utf-8"))
    assert ex["llm_model"] == "openai:gemini-3.5-flash-lite"
    assert ex["llm_fallback_reason"] == "invalid_model"
    msg = _outbox(conn)[-1]
    assert "提取模型：openai:gemini-3.5-flash-lite" in msg
    assert "⚠️ 主提取模型不可用（invalid_model），本条由兜底提取，准确率较低" in msg
    assert "http" not in msg

    # 恢复捷径：extract 已在盘，不再调 LLM，回执仍取自已存 extract
    monkeypatch.setattr(extract, "_call_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调 LLM")))
    assert pipeline.process_task(conn, db.get_task(conn, "fb1")) == db.SUCCEEDED
    msg2 = _outbox(conn)[-1]
    assert "提取模型：openai:gemini-3.5-flash-lite" in msg2 and "主提取模型不可用（invalid_model）" in msg2


def test_primary_ok_has_model_line_without_warning(conn, monkeypatch):
    _setup(conn, "fb2", monkeypatch, None)
    assert pipeline.process_task(conn, db.get_task(conn, "fb2")) == db.SUCCEEDED
    msg = _outbox(conn)[-1]
    assert "提取模型：agy:gemini-3.8-flash-medium" in msg and "主提取模型不可用" not in msg
