"""整数版本与 .0 已登记写法的归一，以及旧待确认记录的确认门禁。"""
import json
from pathlib import Path

import pytest

from core import config, db, extract, models, pipeline, routing, service


def _resolve(raw, series, version, variant, known, transcript):
    anchors = models.build_anchors(transcript)
    return models.resolve_record(raw, series, version, variant, anchors, known=known)


@pytest.mark.parametrize("version,twin", [("5", "5.0"), ("5.0", "5"), ("4", "4.0")])
def test_dot0_twin_exact_forms(version, twin):
    assert models._dot0_twin(version) == twin


@pytest.mark.parametrize("version", ["5.1", "5.00", "5.0.0", "v5", ""])
def test_dot0_twin_rejects_other_forms(version):
    assert models._dot0_twin(version) is None


def test_step_spoken_five_uses_registered_five_dot_zero():
    known = {("Step", "5.0", "Preview")}
    assert _resolve("Stayp5Proveil", "Step", "5", "Preview", known,
                    "Stayp5Proveil 第一轮做对") == ("Step 5.0 Preview", "Step", "5.0", "Preview")


def test_hunyuan_spoken_four_dot_zero_uses_registered_four():
    known = {("Hunyuan", "4", "Preview")}
    assert _resolve("混元4.0预览版", "Hunyuan", "4.0", "Preview", known,
                    "混元4.0预览版 第一轮做对") == ("Hunyuan 4 Preview", "Hunyuan", "4", "Preview")


def test_neither_spelling_known_stays_new():
    result = _resolve("Stayp5Proveil", "Step", "5", "Preview", set(), "Stayp5Proveil 做对")
    assert result == ("Step 5 Preview", "Step", "5", "Preview")
    assert extract._classify({"model_canonical": result[0], "model_series": result[1],
                              "model_version": result[2], "model_variant": result[3]}, False, set()) == db.PENDING_NEW_VERSION


def test_fractional_version_unaffected():
    known = {("Step", "5.0", "Preview")}
    assert _resolve("Stayp5.1Proveil", "Step", "5.1", "Preview", known,
                    "Stayp5.1Proveil 做对")[2] == "5.1"


@pytest.mark.parametrize("version,canonical", [("5", "Step 5 Preview"), ("5.0", "Step 5.0 Preview")])
def test_both_registered_keep_exact_spelling(version, canonical):
    known = {("Step", "5", "Preview"), ("Step", "5.0", "Preview")}
    assert _resolve("Stayp" + version + "Proveil", "Step", version, "Preview", known,
                    "Stayp" + version + "Proveil 做对")[0] == canonical


def test_normalize_triple_uses_registered_spelling():
    known = {("Step", "5.0", "Preview")}
    assert models.normalize_triple("step", "5", "preview", known=known) == (
        "Step 5.0 Preview", "Step", "5.0", "Preview")


def _record(canonical="Step 5 Preview", version="5", bug="G001"):
    return {"model_canonical": canonical, "model_raw": "Stayp5Proveil",
            "model_series": "Step", "model_version": version, "model_variant": "Preview",
            "bug_level": "黄金", "bug_id": bug, "solved_round": 1, "solved": True,
            "score": 2, "rounds": 1, "evidence_quote": "Stayp5Proveil 第一轮做对", "confidence": 0.9}


def _old_pending(conn, aid, records):
    ex = {"schema_rev": 2, "video_id": aid, "title": "Stayp5Proveil", "extracted_at": "x",
          "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
          "records": records, "result_rev": extract.result_rev(aid, records)}
    p = config.video_dir("token_bug", aid)
    p.mkdir(parents=True, exist_ok=True)
    (p / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    for r in records:
        rid = extract.record_id(aid, r)
        db.upsert_decision(conn, record_id=rid, aweme_id=aid, decision=db.PENDING_NEW_VERSION,
                           extractor_version="token_bug/2", prompt_hash="p", commit=True)
    return ex


def test_bulk_pending_twin_remains_pending_and_off_board(conn):
    aid = "bulk_twin"
    db.register_known_version(conn, "Step", "5.0", "Preview", "admin", commit=True)
    r = _record()
    _old_pending(conn, aid, [r])
    rid = extract.record_id(aid, r)
    before = len(db.known_versions_rows(conn))
    assert extract.approve_pending_for_video(conn, aid, "admin") == (0, [])
    assert extract.twin_blocked_pending(conn, aid) == [(rid, "Step 5 Preview", "Step 5.0 Preview")]
    assert db.get_decision(conn, rid)["decision"] == db.PENDING_NEW_VERSION
    assert len(db.known_versions_rows(conn)) == before
    assert "Step 5 Preview" not in pipeline.render_board(conn)


def test_finish_or_outbox_uses_registered_identity(conn, data_dir, monkeypatch):
    aid = "finish_twin"
    monkeypatch.setattr(config, "GLADIA_API_KEY", "")
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="Stayp5Proveil")
    db.register_known_version(conn, "Step", "5.0", "Preview", "admin", commit=True)
    tx = "黄金G001 Stayp5Proveil 第一轮做对"
    raw = {"model_raw": "Stayp5Proveil", "model_series": "Step", "model_version": "5",
           "model_variant": "Preview", "bug_level": "黄金", "bug_id": "G001", "solved_round": 1,
           "evidence_quote": "Stayp5Proveil 第一轮做对", "confidence": 0.95}
    ex = extract.build_extract(aweme_id=aid, title="Stayp5Proveil", transcript=tx,
                               claude_text=json.dumps({"records": [raw]}, ensure_ascii=False),
                               known=db.list_known_versions(conn))
    p = pipeline._paths(aid)
    p["dir"].mkdir(parents=True, exist_ok=True)
    pipeline._finish(conn, aid, "c", ex, p)
    r = ex["records"][0]
    assert db.get_decision(conn, extract.record_id(aid, r))["decision"] == db.AUTO_OK
    msg = conn.execute("SELECT content FROM notification_outbox").fetchone()["content"]
    assert "待确认 0" in msg and "Step 5 Preview" not in msg
    assert Path(p["extract"]).exists()
    assert "Step 5.0 Preview" in (config.channel_dir("token_bug") / "board.md").read_text()


def test_service_blocked_only(conn, monkeypatch):
    aid = "blocked_only"
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")
    db.register_known_version(conn, "Step", "5.0", "Preview", "admin", commit=True)
    r = _record()
    _old_pending(conn, aid, [r])
    rid = extract.record_id(aid, r)
    code, msg = service.handle_confirm(conn, {"sender_id": "ADMIN", "text": f"vr确认 {aid}"})
    assert code == 200 and rid[:8] in msg and "=Step/5.0/Preview" in msg
    assert "没有可批量确认" not in msg


def test_service_mixed_approved_and_blocked(conn, monkeypatch):
    aid = "mixed_twin"
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")
    db.register_known_version(conn, "Step", "5.0", "Preview", "admin", commit=True)
    blocked = _record()
    ordinary = _record("Step 5.0 Preview", "5.0", "G002")
    _old_pending(conn, aid, [blocked, ordinary])
    ordinary_id = extract.record_id(aid, ordinary)
    db.upsert_decision(conn, record_id=ordinary_id, aweme_id=aid, decision=db.PENDING,
                       extractor_version="token_bug/2", prompt_hash="p", commit=True)
    code, msg = service.handle_confirm(conn, {"sender_id": "ADMIN", "text": f"vr确认 {aid}"})
    assert code == 200 and "已确认 1 条" in msg and "=Step/5.0/Preview" in msg
    assert db.get_decision(conn, ordinary_id)["decision"] == db.APPROVED


def test_correction_reuses_registered_spelling(conn):
    aid = "correct_twin"
    db.register_known_version(conn, "Step", "5.0", "Preview", "admin", commit=True)
    r = _record()
    _old_pending(conn, aid, [r])
    rid = extract.record_id(aid, r)
    corr = routing.Correction(rid=rid[:8], result_rev="", series="step", version="5", variant="preview")
    ok, msg = extract.correct_and_confirm(conn, aid, corr, "admin")
    assert ok, msg
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text())
    assert disk["records"][0]["model_canonical"] == "Step 5.0 Preview"
    assert ("Step", "5", "Preview") not in db.list_known_versions(conn)
