import copy
import json

import pytest

from core import extract, pipeline


NEEDLE = "".join(chr(0x4E00 + i) for i in range(40))


def _wrong(s, *positions):
    chars = list(s)
    for pos in positions:
        chars[pos] = "错"
    return "".join(chars)


def _build(transcript, quote, confidence=0.95):
    return extract.build_extract(
        aweme_id="fuzzy-v", title="Kimi K3 黄金测试", transcript=transcript,
        claude_text=json.dumps({"records": [{
            "model_raw": "Kimi K3", "bug_level": "黄金", "bug_id": "G001",
            "solved_round": 2, "evidence_quote": quote, "confidence": confidence,
        }]}, ensure_ascii=False),
    )


def test_original_substring_and_manual_review(conn):
    transcript = "Kimi K3 说：" + NEEDLE[:12] + "，\n  " + NEEDLE[12:] + "。"
    quote = NEEDLE[:12] + "，" + _wrong(NEEDLE[12:], 5)
    ex = _build(transcript, quote)
    record = ex["records"][0]
    repair = ex["evidence_repaired"][0]
    assert repair["original_quote"] == quote
    assert repair["edits"] == 1
    assert repair["matched_quote"] in transcript
    assert record["evidence_quote"] == repair["matched_quote"]
    assert "，\n  " in repair["matched_quote"]
    assert record["confidence"] == 0.6
    counts = extract.apply_decisions(conn, "fuzzy-v", ex)
    assert counts["auto_ok"] == 0
    assert counts["pending"] == 1


def test_gai_fa_like_typo_recovers():
    phrase = "那这改法在这一轮完整测试之后终于成功通过了"
    quote = phrase.replace("那这改法", "那G改法")
    assert extract._fuzzy_evidence_span(quote, "前文。" + phrase + "。后文") == (phrase, 1)


def test_at_edit_limit_recovers():
    assert extract._fuzzy_evidence_span(_wrong(NEEDLE, 5, 30), NEEDLE) == (NEEDLE, 2)


def test_over_edit_limit_drops():
    ex = _build("Kimi K3 " + NEEDLE, _wrong(NEEDLE, 5, 20, 35))
    assert ex["records"] == []
    assert ex["dropped"][0]["reason"] == "evidence 非转写子串"


def test_disjoint_equal_best_spans_drop():
    transcript = "Kimi K3 " + NEEDLE + "。隔开。" + NEEDLE
    assert extract._fuzzy_evidence_span(_wrong(NEEDLE, 10), transcript) is None
    assert _build(transcript, _wrong(NEEDLE, 10))["records"] == []


def test_overlapping_equal_windows_use_union():
    quote = "甲" * 20
    transcript = "乙" + "甲" * 19
    assert extract._fuzzy_evidence_span(quote, transcript) == (transcript, 1)


def test_short_quote_is_not_repaired():
    quote = _wrong(NEEDLE[:19], 4)
    assert extract._fuzzy_evidence_span(quote, NEEDLE) is None
    assert _build("Kimi K3 " + NEEDLE, quote)["records"] == []


def test_multi_segment_quote_is_not_repaired():
    quote = _wrong(NEEDLE[:20], 4) + "..." + NEEDLE[20:]
    assert extract._fuzzy_evidence_span(quote, NEEDLE) is None
    assert _build("Kimi K3 " + NEEDLE, quote)["records"] == []


def test_repaired_envelope_validates_against_transcript():
    transcript = "Kimi K3 " + NEEDLE
    ex = _build(transcript, _wrong(NEEDLE, 10))
    extract.validate_extract(ex, transcript=transcript, expected_video_id="fuzzy-v")
    assert not list(extract._validator_v2.iter_errors(ex))


@pytest.mark.parametrize("bad", [0, 4, "1"])
def test_schema_rejects_invalid_edit_counts(bad):
    ex = _build("Kimi K3 " + NEEDLE, _wrong(NEEDLE, 10))
    ex = copy.deepcopy(ex)
    ex["evidence_repaired"][0]["edits"] = bad
    with pytest.raises(extract.ExtractError, match="envelope schema"):
        extract.validate_extract(ex)


def test_schema_rejects_extra_repair_fields():
    ex = _build("Kimi K3 " + NEEDLE, _wrong(NEEDLE, 10))
    ex["evidence_repaired"][0]["unexpected"] = True
    with pytest.raises(extract.ExtractError, match="envelope schema"):
        extract.validate_extract(ex)


def test_receipt_mentions_repair_count():
    ex = _build("Kimi K3 " + NEEDLE, _wrong(NEEDLE, 10))
    assert any("证据错字修正 1 条" in warning
               for warning in pipeline._receipt_warnings(ex, set()))


def test_trailing_ellipsis_zero_edits_stays_dropped():
    ex = _build("Kimi K3 " + NEEDLE, NEEDLE + "……")
    assert ex["records"] == [] and ex["evidence_repaired"] == []
    assert ex["dropped"][0]["reason"] == "evidence 非转写子串"
