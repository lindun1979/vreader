"""WP-0 回滚底座：schema 接受四个新可选字段；batch_slot 身份兼容（重判不 stale）。"""
import copy
import json

import pytest

from channels.token_bug import board
from core import db, extract

TX = "三道青铜全对没有问题 Kimi K3 第一轮做对了 白银S001 Kimi K3 没做对"


def _base():
    recs = [{"model_raw": "Kimi K3", "bug_level": "青铜", "bug_id": "", "solved_round": 1,
             "evidence_quote": "三道青铜全对没有问题", "confidence": 0.9}]
    return extract.build_extract(aweme_id="v", title="Kimi K3", transcript=TX,
                                 claude_text=json.dumps({"records": recs}, ensure_ascii=False))


def _with_batch(ex, n=2):
    ex = copy.deepcopy(ex)
    proto = ex["records"][0]
    ex["records"] = [dict(proto, batch_slot=f"1-{k}", confidence=0.6) for k in range(1, n + 1)]
    ex["result_rev"] = extract.result_rev("v", ex["records"])
    return ex


def test_new_optional_fields_accepted():
    ex = _with_batch(_base())
    ex["batch_merged"] = [{"model": "Kimi K3", "level": "青铜", "score": 1, "merged": 1,
                           "quotes": ["三道青铜全对没有问题"]}]
    ex["llm_model"] = "agy:gemini-3.8-flash-medium"
    ex["llm_fallback_reason"] = "timeout"
    extract.validate_extract(ex, transcript=TX, expected_video_id="v")


def test_old_extract_still_valid():
    extract.validate_extract(_base(), transcript=TX, expected_video_id="v")


@pytest.mark.parametrize("field,value", [("unknown_field", 1), ("llm_fallback_reason", "network down")])
def test_unknown_or_bad_top_level_rejected(field, value):
    ex = _base()
    ex[field] = value
    with pytest.raises(extract.ExtractError):
        extract.validate_extract(ex, transcript=TX, expected_video_id="v")


def test_bad_batch_slot_rejected():
    ex = _with_batch(_base())
    ex["records"][0]["batch_slot"] = "x"
    with pytest.raises(extract.ExtractError):
        extract.validate_extract(ex, transcript=TX, expected_video_id="v")


def test_batch_slot_ids_distinct_and_legacy_ids_unchanged():
    r = {"model_canonical": "Kimi K3", "bug_level": "青铜", "bug_id": "", "score": 1,
         "evidence_quote": "三道青铜全对没有问题"}
    # 硬编码期望值取自改动前 main 的计算结果：旧记录身份不得变化
    assert extract.record_id("v", r) == "100dd5912287038f"
    assert extract.record_id("v", dict(r, bug_id="b001")) == "1964a3fc6b291738"
    a, b = (extract.record_id("v", dict(r, batch_slot=s)) for s in ("1-1", "1-2"))
    assert a != b and extract.record_id("v", r) not in (a, b)


def test_backfill_skips_batch_records():
    recs = [{"bug_level": "白银", "bug_id": "S001"},
            {"bug_level": "白银", "bug_id": "", "batch_slot": "1-1"},
            {"bug_level": "白银", "bug_id": ""}]
    out, n = extract._backfill_bug_ids(recs)
    assert [r["bug_id"] for r in out] == ["S001", "", "S001"] and n == 1


def test_approved_batch_survives_redecide(conn):
    ex = _with_batch(_base())
    extract.apply_decisions(conn, "v", ex)
    assert db.approve_pending_for_video(conn, "v", "admin") == 2
    counts = extract.apply_decisions(conn, "v", copy.deepcopy(ex))
    decs = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(conn, "v")}
    assert set(decs.values()) == {db.APPROVED} and len(decs) == 2
    assert counts["stale"] == 0 and counts["approved_stale"] == 0
    md = board.render([ex], db.board_visible_ids(conn), extract.record_id)
    assert "| Kimi K3 | 2 | 2/2 |" in md
