"""WP-B：空 bug_id 回填（某等级本视频只有一个 bug_id 时）。"""
import copy
import json

from core import db, extract

TX = "黄金题开始了 Kimi K3 第一轮做对了 Kimi K3 第二轮也做对了 另一个黄金题 Kimi K3 没做对"


def _rec(bug_id, level="黄金", sr=1, ev="Kimi K3 第一轮做对了"):
    return {"model_raw": "Kimi K3", "bug_level": level, "bug_id": bug_id,
            "solved_round": sr, "evidence_quote": ev, "confidence": 0.9}


def _build(recs):
    return extract.build_extract(aweme_id="v", title="t", transcript=TX,
                                 claude_text=json.dumps({"records": recs}, ensure_ascii=False))


def test_single_bug_id_in_level_backfilled():
    ex = _build([_rec("G006"), _rec("", sr=2, ev="Kimi K3 第二轮也做对了")])
    assert [r["bug_id"] for r in ex["records"]] == ["G006", "G006"]


def test_two_bug_ids_not_backfilled():
    ex = _build([_rec("G005"), _rec("G006", ev="另一个黄金题 Kimi K3 没做对", sr=0), _rec("", sr=2, ev="Kimi K3 第二轮也做对了")])
    assert ex["records"][2]["bug_id"] == ""


def test_all_empty_not_backfilled():
    ex = _build([_rec(""), _rec("", sr=2, ev="Kimi K3 第二轮也做对了")])
    assert [r["bug_id"] for r in ex["records"]] == ["", ""]


def test_other_level_not_used():
    ex = _build([_rec("D003", level="钻石"), _rec("", ev="Kimi K3 第二轮也做对了")])
    assert ex["records"][1]["bug_id"] == ""


def test_input_not_mutated():
    recs = [{"bug_level": "黄金", "bug_id": "g006"}, {"bug_level": "黄金", "bug_id": ""}]
    before = copy.deepcopy(recs)
    out, n = extract._backfill_bug_ids(recs)
    assert recs == before and n == 1 and out[1]["bug_id"] == "G006"
    assert out[0] is not recs[0]


def test_backfill_aligns_attempt_key_and_triggers_conflict(conn):
    ex = _build([_rec("G006", sr=1), _rec("", sr=2, ev="Kimi K3 第二轮也做对了")])
    a, b = ex["records"]
    assert extract._attempt_key("v", a) == extract._attempt_key("v", b)
    assert extract.record_id("v", a) != extract.record_id("v", b)
    extract.apply_decisions(conn, "v", ex)
    decs = {r["decision"] for r in db.list_decisions_for_video(conn, "v")}
    assert decs == {db.PENDING_CONFLICT}
    assert len(db.list_decisions_for_video(conn, "v")) == 2
