"""V-M16 校正命令：改名+登记+确认，含 same_rid/INSERT列/碰撞/审计/决策溯源/
journal CAS 恢复/needs_review/命令内恢复限当前视频。"""
import hashlib
import json
from pathlib import Path

from core import config, db, extract as ex_mod, models, routing

# 证据须 ≥10 字且为 transcript（去空白后）子串；用无空格 token，transcript 空格分隔。
EV_UNK = "白银bugS001做对了呀哈"
EV_HY = "钻石混元4Proveil第三轮做对"
EV_GLM = "黄金bugG001证据够长啦啦"
EV_KIMI = "白银kimi做对了呀哈啦啦"
_TRANSCRIPT = " ".join([EV_UNK, EV_HY, EV_GLM, EV_KIMI, "点击验证OK没有问题其他内容"])


def _rec(canonical, series, version, variant, *, bug_level="白银", bug_id="S001",
         solved_round=1, conf=0.95, evidence=EV_UNK, raw="DeepSick Flash"):
    solved, score, rounds = ex_mod.derive_from_round(bug_level, solved_round)
    return {"model_canonical": canonical, "model_raw": raw, "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": bug_level,
            "bug_id": bug_id, "solved_round": solved_round, "score": score, "rounds": rounds,
            "solved": solved, "evidence_quote": evidence, "confidence": conf}


def _v2ex(aid, records, **extra):
    ex = {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
          "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
          "records": records, "result_rev": ex_mod.result_rev(aid, records)}
    ex.update(extra)
    return ex


def _write(aid, ex, transcript=_TRANSCRIPT):
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False, indent=2), encoding="utf-8")
    if transcript is not None:
        (d / "transcript.txt").write_text(transcript, encoding="utf-8")


def _corr(rid, series, version, variant=""):
    return routing.Correction(rid=rid[:8], result_rev="x", series=series,
                              version=version, variant=variant)


def _decision(conn, aid, ex, canonical):
    rec = next(r for r in ex["records"] if r["model_canonical"] == canonical)
    return db.get_decision(conn, ex_mod.record_id(aid, rec))


# ---------- 正例 ----------

def test_pending_unknown_correction_registers_and_approves(conn):
    aid = "vunk"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, old_rid)["decision"] == db.PENDING_UNKNOWN

    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "DeepSeek", "4.1", "Flash"), "admin")
    assert ok, msg
    assert ("DeepSeek", "4.1", "Flash") in db.list_known_versions(conn)
    # 盘上 canonical 已改
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert disk["records"][0]["model_canonical"] == "DeepSeek V4.1 Flash"
    new_rid = ex_mod.record_id(aid, disk["records"][0])
    d = db.get_decision(conn, new_rid)
    assert d["decision"] == db.APPROVED and d["approved_by"] == "admin" and d["approved_at"]
    assert db.get_decision(conn, old_rid)["decision"] == db.STALE
    assert new_rid in db.board_visible_ids(conn)
    # op 收尾 done
    assert all(o["status"] == db.CORR_DONE for o in db.list_all_corrections(conn))


def test_pending_new_version_wrong_version_to_preview(conn):
    aid = "vhy"
    ex = _v2ex(aid, [_rec("Hunyuan 4", "Hunyuan", "4", "", raw="混元4Proveil", evidence=EV_HY)])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, old_rid)["decision"] == db.PENDING_NEW_VERSION

    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "Hunyuan", "4.0", "Preview"), "admin")
    assert ok, msg
    known = db.list_known_versions(conn)
    assert ("Hunyuan", "4.0", "Preview") in known
    assert ("Hunyuan", "4", "") not in known  # 错误版本不残留
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert disk["records"][0]["model_canonical"] == "Hunyuan 4.0 Preview"


def test_version_map_opus_5(conn):
    # Opus 5.0 → stored version "5"（version_map），canonical "Claude Opus 5"
    canonical, series, sv, variant = models.normalize_triple("Claude Opus", "5.0", "")
    assert (canonical, sv) == ("Claude Opus 5", "5")


# ---------- same_rid / INSERT 列 ----------

def test_same_rid_updates_in_place(conn):
    aid = "vsame"
    ex = _v2ex(aid, [_rec("GLM-5.4", "GLM", "5.4", "", bug_level="黄金", bug_id="G001",
                          solved_round=1, evidence=EV_GLM)])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    assert db.get_decision(conn, old_rid)["decision"] == db.PENDING_NEW_VERSION

    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "GLM", "5.4", ""), "admin")
    assert ok, msg
    # canonical 未变 → rid 未变 → 原行 UPDATE，无 stale、无第二行
    d = db.get_decision(conn, old_rid)
    assert d["decision"] == db.APPROVED and d["approved_by"] == "admin"
    rows = db.list_decisions_for_video(conn, aid)
    assert len(rows) == 1  # 未新增行、未 stale
    assert ("GLM", "5.4", "") in db.list_known_versions(conn)


def test_insert_columns_present(conn):
    aid = "vins"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    ok, _ = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "DeepSeek", "4.1", "Flash"), "admin")
    assert ok
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    new_rid = ex_mod.record_id(aid, disk["records"][0])
    row = conn.execute("SELECT * FROM record_decisions WHERE record_id=?", (new_rid,)).fetchone()
    assert row["created_at"] and row["extractor_version"] == "token_bug/2" and row["prompt_hash"] == "p"


# ---------- 碰撞 ----------

def test_rid_collision_rejected(conn):
    aid = "vcol"
    # Y：已 approved 的 DeepSeek V4.1 Flash 白银 S001 score1；X：UNKNOWN 同 bug/难度/score
    y = _rec("DeepSeek V4.1 Flash", "DeepSeek", "4.1", "Flash", evidence=EV_UNK)
    x = _rec("UNKNOWN", "", "", "", evidence=EV_UNK)
    ex = _v2ex(aid, [y, x])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    y_rid = ex_mod.record_id(aid, y)
    x_rid = ex_mod.record_id(aid, x)
    # 让 Y approved
    db.upsert_decision(conn, record_id=y_rid, aweme_id=aid, decision=db.APPROVED,
                       extractor_version="token_bug/2", prompt_hash="p",
                       approved_by="admin", approved_at=1.0, commit=True)
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(x_rid, "DeepSeek", "4.1", "Flash"), "admin")
    assert not ok and "冲突" in msg
    # X 未被改动
    assert db.get_decision(conn, x_rid)["decision"] == db.PENDING_UNKNOWN


# ---------- 坏输入 / 零副作用 ----------

def test_invalid_series_rejected_no_side_effect(conn):
    aid = "vbad"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    before = db.list_known_versions(conn)
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "NoSuchSeries", "1", ""), "admin")
    assert not ok and "无法归一" in msg
    assert db.list_known_versions(conn) == before
    assert db.get_decision(conn, old_rid)["decision"] == db.PENDING_UNKNOWN
    assert db.list_all_corrections(conn) == []


def test_unknown_variant_rejected(conn):
    aid = "vbadv"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "DeepSeek", "4.1", "Nope"), "admin")
    assert not ok and "无法归一" in msg


def test_prefix_not_found_and_ambiguous(conn):
    aid = "vpref"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr("zzzzzz", "DeepSeek", "4.1", "Flash"), "admin")
    assert not ok and "没有匹配" in msg


def test_pending_conflict_rejected(conn):
    aid = "vconf"
    a = _rec("GLM-5.4", "GLM", "5.4", "", bug_level="黄金", bug_id="G001", solved_round=1,
             evidence=EV_GLM)
    b = _rec("GLM-5.4", "GLM", "5.4", "", bug_level="黄金", bug_id="G001", solved_round=2,
             evidence=EV_GLM)
    ex = _v2ex(aid, [a, b])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    rid_a = ex_mod.record_id(aid, a)
    assert db.get_decision(conn, rid_a)["decision"] == db.PENDING_CONFLICT
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(rid_a, "GLM", "5.4", ""), "admin")
    assert not ok and "矛盾" in msg


# ---------- 决策溯源 ----------

def test_decision_snapshot_written(conn):
    aid = "vsnap"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")], known_versions_used=[["GLM", "5.3", ""]])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    ok, _ = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "DeepSeek", "4.1", "Flash"), "admin")
    assert ok
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert ["DeepSeek", "4.1", "Flash"] in disk["known_versions_used_at_decision"]
    assert disk["decided_at"]
    assert disk["known_versions_used"] == [["GLM", "5.3", ""]]  # 输入快照不改


# ---------- journal CAS 恢复 ----------

def _make_committed_state(conn, aid, monkeypatch):
    """跑到「DB 提交、写文件抛错」态：返回 old_rid。用 toggle 而非 monkeypatch.undo()
    （undo 会连带撤销 conftest 的 DATA_DIR 隔离）。"""
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    from core import util as _util
    orig = _util.atomic_write_text
    state = {"fail": True}

    def flaky(*a, **k):
        if state["fail"]:
            raise OSError("disk full")
        return orig(*a, **k)

    monkeypatch.setattr(_util, "atomic_write_text", flaky)
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(old_rid, "DeepSeek", "4.1", "Flash"), "admin")
    assert not ok and "待恢复" in msg
    state["fail"] = False  # 后续写恢复正常（不 undo，避免撤销 DATA_DIR 隔离）
    return old_rid


def test_crash_after_commit_before_write_then_resume(conn, monkeypatch):
    aid = "vrec"
    old_rid = _make_committed_state(conn, aid, monkeypatch)
    # DB 已提交：known 已登记、op committed、文件仍旧
    assert ("DeepSeek", "4.1", "Flash") in db.list_known_versions(conn)
    ops = db.list_committed_ops(conn, aid)
    assert len(ops) == 1
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert disk["records"][0]["model_canonical"] == "UNKNOWN"  # 文件未改
    # 恢复：CAS source 命中 → 补写
    wrote = ex_mod.resume_pending_corrections(conn, aid)
    assert wrote
    disk2 = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert disk2["records"][0]["model_canonical"] == "DeepSeek V4.1 Flash"
    assert db.list_committed_ops(conn, aid) == []  # 已 done


def test_resume_idempotent_target_match(conn, monkeypatch):
    aid = "vrec2"
    _make_committed_state(conn, aid, monkeypatch)
    ex_mod.resume_pending_corrections(conn, aid)  # 补写
    # 再次恢复：文件==target → 幂等（不报错、无新写）
    assert ex_mod.resume_pending_corrections(conn, aid) is False


def test_resume_does_not_overwrite_changed_extract(conn, monkeypatch):
    aid = "vrec3"
    _make_committed_state(conn, aid, monkeypatch)
    # 模拟停机期被 reprocess 改写成第三种内容
    p = config.video_dir("token_bug", aid) / "extract.json"
    reprocessed = _v2ex(aid, [_rec("Kimi K3", "Kimi", "3", "", raw="kimi", evidence=EV_KIMI)])
    p.write_text(json.dumps(reprocessed, ensure_ascii=False, indent=2), encoding="utf-8")
    changed_bytes = p.read_bytes()
    wrote = ex_mod.resume_pending_corrections(conn, aid)
    assert wrote is False
    assert p.read_bytes() == changed_bytes  # 未被覆盖
    op = db.get_correction_op(conn, db.list_all_corrections(conn)[0]["op_id"])
    assert op["status"] == db.CORR_NEEDS_REVIEW


def test_command_resume_scoped_to_aweme(conn, monkeypatch):
    # A 处于 committed 待恢复；对 B 跑命令不应触碰 A
    _make_committed_state(conn, "vA", monkeypatch)
    assert len(db.list_committed_ops(conn, "vA")) == 1
    # 命令内恢复限 B
    ex_mod.resume_pending_corrections(conn, "vB")
    assert len(db.list_committed_ops(conn, "vA")) == 1  # A 未动


def test_needs_review_blocks_new_correction(conn, monkeypatch):
    aid = "vblk"
    _make_committed_state(conn, aid, monkeypatch)
    # 改写文件触发 needs_review
    p = config.video_dir("token_bug", aid) / "extract.json"
    p.write_text(json.dumps(_v2ex(aid, [_rec("Kimi K3", "Kimi", "3", "", raw="k", evidence=EV_KIMI)]), ensure_ascii=False, indent=2), encoding="utf-8")
    ex_mod.resume_pending_corrections(conn, aid)
    # 新校正被阻断
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr("abcdef", "GLM", "5.4", ""), "admin")
    assert not ok and "未完成校正" in msg


# ---------- resolve CLI 闭环 ----------

def _to_needs_review(conn, aid, monkeypatch):
    _make_committed_state(conn, aid, monkeypatch)
    p = config.video_dir("token_bug", aid) / "extract.json"
    p.write_text(json.dumps(_v2ex(aid, [_rec("Kimi K3", "Kimi", "3", "", raw="k", evidence=EV_KIMI)]), ensure_ascii=False, indent=2), encoding="utf-8")
    ex_mod.resume_pending_corrections(conn, aid)
    return db.list_all_corrections(conn)[0]["op_id"]


def test_resolve_keep_file(conn, monkeypatch):
    aid = "vkeep"
    op_id = _to_needs_review(conn, aid, monkeypatch)
    ok, msg = ex_mod.resolve_correction(conn, op_id, "keep-file")
    assert ok and "done" in msg
    assert db.get_correction_op(conn, op_id)["status"] == db.CORR_DONE
    # 已登记的全局版本保留
    assert ("DeepSeek", "4.1", "Flash") in db.list_known_versions(conn)


def test_resolve_apply_journal(conn, monkeypatch):
    aid = "vapply"
    op_id = _to_needs_review(conn, aid, monkeypatch)
    ok, msg = ex_mod.resolve_correction(conn, op_id, "apply-journal")
    assert ok
    disk = json.loads((config.video_dir("token_bug", aid) / "extract.json").read_text("utf-8"))
    assert disk["records"][0]["model_canonical"] == "DeepSeek V4.1 Flash"


def test_resolve_rejects_non_needs_review(conn, monkeypatch):
    aid = "vdone"
    old = _make_committed_state(conn, aid, monkeypatch)
    ex_mod.resume_pending_corrections(conn, aid)  # → done
    op_id = db.list_all_corrections(conn)[0]["op_id"]
    ok, msg = ex_mod.resolve_correction(conn, op_id, "apply-journal")
    assert not ok and "needs_review" in msg


# ---------- handle_confirm 端到端（含强制 result_rev）----------

def _admin(text):
    return {"text": text, "sender_id": "ADMIN", "chat_id": "c"}


def test_handle_confirm_correction_success(conn, data_dir, monkeypatch):
    from core import config, service
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")
    aid = "vhc"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    rev = ex["result_rev"]
    code, body = service.handle_confirm(
        conn, _admin(f"vr确认 {aid} {old_rid[:8]}@{rev}=DeepSeek/4.1/Flash"))
    assert code == 200 and "已校正" in body
    assert ("DeepSeek", "4.1", "Flash") in db.list_known_versions(conn)


def test_handle_confirm_stale_result_rev_rejected(conn, data_dir, monkeypatch):
    from core import config, service
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")
    aid = "vstale"
    ex = _v2ex(aid, [_rec("UNKNOWN", "", "", "")])
    _write(aid, ex)
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    old_rid = ex_mod.record_id(aid, ex["records"][0])
    code, body = service.handle_confirm(
        conn, _admin(f"vr确认 {aid} {old_rid[:8]}@deadbeef00=DeepSeek/4.1/Flash"))
    assert code == 200 and "内容已更新" in body
    # 未登记、未改动
    assert ("DeepSeek", "4.1", "Flash") not in db.list_known_versions(conn)
    assert db.get_decision(conn, old_rid)["decision"] == db.PENDING_UNKNOWN


def test_handle_confirm_missing_rev_syntax(conn, data_dir, monkeypatch):
    from core import config, service
    monkeypatch.setattr(config, "ADMIN_SENDER_ID", "ADMIN")
    # arg 含 = 但缺 @rev → 语法提示
    code, body = service.handle_confirm(conn, _admin("vr确认 vx abcdef=DeepSeek/4.1/Flash"))
    assert code == 200 and "校正语法" in body
