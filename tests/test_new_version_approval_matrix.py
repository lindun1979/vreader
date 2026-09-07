"""Step 3 新版本确认状态矩阵（plan M04）：批量/冲突赢家登记、未批准不登记、
重复幂等、第二条低置信仍 pending。"""
import json

from core import config, db, extract as ex_mod


def _rec(canonical, series, version, variant, bug_id="G001", score=2, conf=0.9):
    return {"model_canonical": canonical, "model_raw": "raw", "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": "黄金",
            "bug_id": bug_id, "score": score, "rounds": 1, "solved": True,
            "evidence_quote": "证据够长的句子啦啦", "confidence": conf}


def _v2ex(aid, records):
    return {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
            "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
            "records": records}


def _write(aid, ex):
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")


def test_batch_approve_registers_new_version(conn):
    aid = "a1"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, rid)["decision"] == db.PENDING_NEW_VERSION
    n, reg = ex_mod.approve_pending_for_video(conn, aid, "admin")
    assert n == 1 and reg == ["GLM-5.4"]
    assert ("GLM", "5.4", "") in db.list_known_versions(conn)
    assert db.get_decision(conn, rid)["decision"] == db.APPROVED


def test_second_video_same_version_auto_ok(conn):
    db.register_known_version(conn, "GLM", "5.4", "", "admin", commit=True)
    aid = "a2"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, rid)["decision"] == db.AUTO_OK


def test_unapproved_new_version_not_registered(conn):
    aid = "a3"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    assert ("GLM", "5.4", "") not in db.list_known_versions(conn)


def test_duplicate_confirm_idempotent(conn):
    aid = "a4"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    n1, reg1 = ex_mod.approve_pending_for_video(conn, aid, "admin")
    n2, reg2 = ex_mod.approve_pending_for_video(conn, aid, "admin")
    assert (n1, n2) == (1, 0)
    rows = [r for r in db.known_versions_rows(conn)
            if r["series"] == "GLM" and r["version"] == "5.4"]
    assert len(rows) == 1  # OR IGNORE 不重复


def test_low_conf_pending_when_version_known(conn):
    db.register_known_version(conn, "GLM", "5.4", "", "admin", commit=True)
    aid = "a5"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "", conf=0.5)])
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, rid)["decision"] == db.PENDING  # 已知版本 → 只剩低置信


def test_conflict_winner_registers_new_version(conn):
    aid = "a6"
    r1 = _rec("GLM-5.4", "GLM", "5.4", "", bug_id="G001", score=2)
    r2 = _rec("GLM-5.4", "GLM", "5.4", "", bug_id="G001", score=1)
    ex = _v2ex(aid, [r1, r2])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid1 = ex_mod.record_id(aid, r1)
    # 冲突优先于新版本分类
    assert db.get_decision(conn, rid1)["decision"] == db.PENDING_CONFLICT
    ok, msg = ex_mod.confirm_conflict_member(conn, aid, rid1[:8], "admin")
    assert ok and "已登记新版本" in msg
    assert ("GLM", "5.4", "") in db.list_known_versions(conn)


def test_unknown_not_registered_on_approve(conn):
    aid = "a7"
    unk = _rec("UNKNOWN", "", "", "")
    unk["model_raw"] = "糊糊糊"
    ex = _v2ex(aid, [unk])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid = ex_mod.record_id(aid, unk)
    assert db.get_decision(conn, rid)["decision"] == db.PENDING_UNKNOWN
    # pending_unknown 不在批量批准范围（只批 pending/pending_new_version）
    n, reg = ex_mod.approve_pending_for_video(conn, aid, "admin")
    assert n == 0 and reg == []
