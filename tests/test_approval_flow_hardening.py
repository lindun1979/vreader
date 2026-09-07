"""V-M01/V-M10：审批闭环——逐条确认冲突组、绑版本、明细、批量排除、reprocess。"""
import json

from core import config, db, extract as ex_mod, pipeline, routing, service


def _rec(model, level, score, conf, bug_id="B001", quote="十个字以上的证据片段"):
    return {"model_raw": model, "model_canonical": model, "bug_level": level,
            "bug_id": bug_id, "score": score, "solved": score > 0,
            "rounds": (1 if score > 0 else None), "evidence_quote": quote, "confidence": conf}


def _write_extract(aid, records):
    ex = {"video_id": aid, "title": "t", "extracted_at": "x",
          "extractor_version": ex_mod.EXTRACTOR_VERSION, "prompt_hash": "ph",
          "asr_model": "gladia-v2", "records": records,
          "result_rev": ex_mod.result_rev(aid, records)}
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    return ex


def _admin(text):
    return {"text": text, "sender_id": "ADMIN", "chat_id": "c"}


def _set_admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")


def test_routing_confirm_detail(monkeypatch):
    assert routing.classify("vr明细 v1") == "detail"
    assert routing.classify("vr确认 v1") == "confirm"
    assert routing.classify("vr确认 v1 ab12cd34") == "confirm"
    assert routing.parse_confirm("vr确认 v1 ab12cd34") == ("v1", "ab12cd34")
    assert routing.parse_confirm("vr确认 v1") == ("v1", None)
    assert routing.parse_detail("vr明细 v1") == "v1"


def test_detail_shows_records(conn, data_dir, monkeypatch):
    _set_admin(monkeypatch)
    r = _rec("GLM-5.3", "青铜", 1, 0.5)  # pending
    ex = _write_extract("v1", [r])
    ex_mod.apply_decisions(conn, "v1", ex)
    code, body = service.handle_detail(conn, _admin("vr明细 v1"))
    assert code == 200 and "GLM-5.3" in body and "待确认" in body
    assert ex["result_rev"] in body


def test_confirm_conflict_member_and_group_lock(conn, data_dir, monkeypatch):
    _set_admin(monkeypatch)
    hi = _rec("GLM-5.3", "青铜", 1, 0.9, bug_id="B001")
    lo = _rec("GLM-5.3", "青铜", 0, 0.6, bug_id="B001")
    ex = _write_extract("v1", [hi, lo])
    ex_mod.apply_decisions(conn, "v1", ex)
    hi_id = ex_mod.record_id("v1", hi)
    lo_id = ex_mod.record_id("v1", lo)
    # 逐条确认 hi 成员
    code, body = service.handle_confirm(conn, _admin(f"vr确认 v1 {hi_id[:8]}"))
    assert code == 200
    assert db.get_decision(conn, hi_id)["decision"] == db.APPROVED
    assert db.get_decision(conn, lo_id)["decision"] == db.REJECTED_CONFLICT
    # 组内已有 APPROVED：对任何成员再确认被拒
    code, body = service.handle_confirm(conn, _admin(f"vr确认 v1 {lo_id[:8]}"))
    assert "已有裁决" in body
    assert db.get_decision(conn, lo_id)["decision"] == db.REJECTED_CONFLICT


def test_confirm_bind_version_rejects_stale(conn, data_dir, monkeypatch):
    _set_admin(monkeypatch)
    r = _rec("GLM-5.3", "青铜", 1, 0.5)
    ex = _write_extract("v1", [r])
    ex_mod.apply_decisions(conn, "v1", ex)
    code, body = service.handle_confirm(conn, _admin("vr确认 v1 rev:deadbeef0000"))
    assert "内容已更新" in body  # 版本不符拒绝
    assert db.get_decision(conn, ex_mod.record_id("v1", r))["decision"] == db.PENDING


def test_batch_confirm_excludes_unknown_conflict(conn, data_dir, monkeypatch):
    _set_admin(monkeypatch)
    ok = _rec("GLM-5.3", "青铜", 1, 0.5, bug_id="B001")            # 普通 pending
    unk = dict(_rec("糊", "青铜", 1, 0.5, bug_id="B002")); unk["model_canonical"] = "UNKNOWN"
    ex = _write_extract("v1", [ok, unk])
    ex_mod.apply_decisions(conn, "v1", ex)
    code, body = service.handle_confirm(conn, _admin("vr确认 v1"))
    assert "已确认 1 条" in body  # 只批普通 pending
    assert db.get_decision(conn, ex_mod.record_id("v1", ok))["decision"] == db.APPROVED
    assert db.get_decision(conn, ex_mod.record_id("v1", unk))["decision"] == db.PENDING_UNKNOWN


def test_reprocess_keeps_approved(conn, data_dir, monkeypatch):
    _set_admin(monkeypatch)
    aid = "v1"
    quote = "GLM-5.3 一次就修对了表现不错"
    r = _rec("GLM-5.3", "青铜", 1, 0.5, quote=quote)
    vd = config.video_dir("token_bug", aid)
    vd.mkdir(parents=True, exist_ok=True)
    (vd / "transcript.txt").write_text(f"青铜题 {quote}，很干净。", encoding="utf-8")
    ex = _write_extract(aid, [r])
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")
    ex_mod.apply_decisions(conn, aid, ex)
    db.approve_pending_for_video(conn, aid, "ADMIN")
    rid = ex_mod.record_id(aid, r)
    assert db.get_decision(conn, rid)["decision"] == db.APPROVED
    # reprocess 用同一事实的 LLM 输出（打桩，轮次驱动格式）重跑，approved 凭指纹继承
    llm_rec = {"model_raw": "GLM-5.3", "model_series": "GLM", "model_version": "5.3",
               "model_variant": "", "bug_level": "青铜", "bug_id": "B001", "solved_round": 1,
               "evidence_quote": quote, "confidence": 0.5}
    monkeypatch.setattr(ex_mod, "_call_llm",
                        lambda *a, **k: json.dumps({"records": [llm_rec]}, ensure_ascii=False))
    pipeline.reprocess(conn, aid)
    assert db.get_decision(conn, rid)["decision"] == db.APPROVED
