"""D-A 批量公布题：身份与输出顺序无关、数量增减与结果变化的批准语义、校正拒绝。"""
import hashlib
import itertools
import json

from channels.token_bug import board
from core import config, db, extract, pipeline, routing

TX = ("混元4正式版参加史山考核了 最长的依然是白银BugS001 混元4用了67分钟 但是依然没有作对 "
      "除了S001之外其他八道题 包括下面三道黄金 混元4正式版全部 一轮作对 "
      "另外两道白银混元4也是一轮作对 青铜混元4一道没做对")
Q_ALL = "除了S001之外其他八道题 包括下面三道黄金 混元4正式版全部 一轮作对"
Q_SILVER = "另外两道白银混元4也是一轮作对"


def _b(level, n, sr=1, q=Q_ALL, conf=0.9):
    return {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4", "model_variant": "",
            "bug_level": level, "bug_id": "", "solved_round": sr, "batch_count": n,
            "evidence_quote": q, "confidence": conf}


S001 = {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4", "model_variant": "",
        "bug_level": "白银", "bug_id": "S001", "solved_round": 0,
        "evidence_quote": "最长的依然是白银BugS001 混元4用了67分钟 但是依然没有作对", "confidence": 0.9}


KNOWN = {("Hunyuan", "4.0", "Preview"), ("Hunyuan", "3.0", "")}


def _build(recs, aid="v"):
    return extract.build_extract(aweme_id=aid, title="Hy4正式版挑战祖传代码", transcript=TX,
                                 claude_text=json.dumps({"records": recs}, ensure_ascii=False),
                                 known=KNOWN)


def _rids(ex, aid="v"):
    return {extract.record_id(aid, r) for r in ex["records"]}


def test_7704_shape_expands_to_nine_basic():
    ex = _build([S001, _b("青铜", 3), _b("白银", 2), _b("黄金", 3)])
    recs = ex["records"]
    assert len(recs) == 9 and len(_rids(ex)) == 9
    assert {r["model_canonical"] for r in recs} == {"Hunyuan 4.0"}
    assert sorted(r.get("batch_slot", "") for r in recs if r["bug_level"] == "青铜") == ["1-1", "1-2", "1-3"]
    assert all(r["confidence"] <= 0.6 for r in recs if r.get("batch_slot"))
    s001 = [r for r in recs if r["bug_id"] == "S001"]
    assert len(s001) == 1  # 白银 batch 未被回填成 S001
    assert sum(r["score"] for r in recs) == 2 * 3 + 2 + 3  # 11 分（黄金 2×3 + 白银 2 + 青铜 3）


def test_permutations_give_same_ids():
    base = [S001, _b("青铜", 3), _b("白银", 2), _b("黄金", 3), _b("青铜", 1, sr=0, q="青铜混元4一道没做对")]
    seen = set()
    for perm in itertools.permutations(base):
        seen.add(frozenset(_rids(_build(list(perm)))))
    assert len(seen) == 1


def test_other_group_insert_or_delete_keeps_ids():
    a = _rids(_build([_b("青铜", 2), _b("黄金", 3)]))
    b = _rids(_build([_b("白银", 2), _b("青铜", 2), _b("黄金", 3)]))
    assert a <= b
    gold_only = _rids(_build([_b("黄金", 3)]))
    assert gold_only <= a


def test_count_increase_keeps_existing_ids(conn):
    ex2 = _build([_b("青铜", 2)])
    extract.apply_decisions(conn, "v", ex2)
    db.approve_pending_for_video(conn, "v", "admin")
    ex3 = _build([_b("青铜", 3)])
    assert _rids(ex2) < _rids(ex3) and len(_rids(ex3) - _rids(ex2)) == 1
    extract.apply_decisions(conn, "v", ex3)
    decs = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(conn, "v")}
    assert all(decs[x] == db.APPROVED for x in _rids(ex2))


def test_result_change_does_not_inherit_approval(conn):
    ok = _build([_b("青铜", 2)])
    extract.apply_decisions(conn, "v", ok)
    db.approve_pending_for_video(conn, "v", "admin")
    bad = _build([_b("青铜", 2, sr=0, q="青铜混元4一道没做对")])
    assert not (_rids(ok) & _rids(bad))
    extract.apply_decisions(conn, "v", bad)
    md = board.render([bad], db.board_visible_ids(conn), extract.record_id)
    assert "Hunyuan 4.0" not in md


def test_legacy_empty_bug_id_identity_unchanged():
    r = {"model_canonical": "Kimi K3", "bug_level": "青铜", "bug_id": "", "score": 1,
         "evidence_quote": "三道青铜全对没有问题"}
    assert extract.record_id("v", r) == "100dd5912287038f"


def test_correction_command_rejects_batch_record(conn, data_dir):
    aid = "vb"
    ex = _build([S001, _b("青铜", 3)], aid=aid)
    vd = config.video_dir("token_bug", aid)
    vd.mkdir(parents=True)
    (vd / "transcript.txt").write_text(TX, encoding="utf-8")
    p = vd / "extract.json"
    p.write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    extract.apply_decisions(conn, aid, ex)
    pipeline.render_board(conn)
    rid = next(extract.record_id(aid, r) for r in ex["records"] if r.get("batch_slot") == "1-1")
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    board_before = (data_dir / "token_bug" / "board.md").read_bytes()
    corr = routing.Correction(rid=rid[:8], result_rev=None, series="Hunyuan", version="4", variant="")
    ok, msg = extract.correct_and_confirm(conn, aid, corr, "admin")
    assert not ok and "批量记录" in msg and "repair_extract" in msg
    assert hashlib.sha256(p.read_bytes()).hexdigest() == sha
    assert conn.execute("SELECT COUNT(*) FROM correction_operations").fetchone()[0] == 0
    assert (data_dir / "token_bug" / "board.md").read_bytes() == board_before
