import json


from core import extract

TX = "我让 Kimi K3 解一个黄金级别的并发问题，它第二轮才解出来，还算可以。"


def _one(evidence):
    return json.dumps({"records": [{
        "model_raw": "Kimi K3", "model_canonical": "Kimi K3", "bug_level": "黄金", "bug_id": "G001",
        "solved_round": 2, "evidence_quote": evidence, "confidence": 0.8,
    }]}, ensure_ascii=False)


def test_evidence_substring_ok():
    ex = extract.build_extract(aweme_id="v", title="t", transcript=TX,
                               claude_text=_one("它第二轮才解出来，还算可以"))
    assert ex["records"][0]["rounds"] == 2


def test_evidence_not_in_transcript_dropped():
    # 伪证据（非转写子串）→ 丢弃 → 无有效记录 → no_content（不入榜，2d）
    ex = extract.build_extract(aweme_id="v", title="t", transcript=TX,
                               claude_text=_one("它一次就完美解决了所有问题"))
    assert ex["records"] == [] and ex.get("no_content") is True
    assert ex["dropped"][0]["reason"] == "evidence 非转写子串"


def test_evidence_whitespace_normalized():
    # 转写有此句；证据带多余空白仍应匹配（归一化后子串）
    ex = extract.build_extract(aweme_id="v", title="t", transcript=TX,
                               claude_text=_one("它  第二轮 才解出来"))
    assert ex["records"][0]["model_canonical"] == "Kimi K3"


# ---- WP-C 分段证据（省略号分隔的有序多段原文）----
import pytest

SEG_A = "王者题K005第二轮的修改结果正在验证中"
SEG_B = "最后的时刻它还是把他给拿下了恭喜再加一分"


def _tx(gap: int, a: str = SEG_A, b: str = SEG_B, prefix: str = "") -> str:
    return prefix + a + ("填" * gap) + b


def _seg(tx, quote, conf=0.95):
    return extract.build_extract(aweme_id="v", title="t", transcript=tx, claude_text=json.dumps(
        {"records": [{"model_raw": "Kimi K3", "bug_level": "王者", "bug_id": "K005",
                      "solved_round": 2, "evidence_quote": quote, "confidence": conf}]},
        ensure_ascii=False))


def test_two_segments_in_order_kept_with_confidence_capped():
    ex = _seg(_tx(100), f"{SEG_A}...{SEG_B}")
    assert len(ex["records"]) == 1
    assert ex["records"][0]["confidence"] <= 0.6


def test_segments_reversed_dropped():
    ex = _seg(_tx(100), f"{SEG_B}...{SEG_A}")
    assert ex["records"] == [] and ex["dropped"][0]["reason"] == "evidence 非转写子串"


def test_segment_shorter_than_8_dropped():
    ex = _seg(_tx(10), f"{SEG_A}...{SEG_B[:7]}")
    assert ex["records"] == []


@pytest.mark.parametrize("gap,kept", [(800, True), (801, False)])
def test_gap_boundary(gap, kept):
    ex = _seg(_tx(gap), f"{SEG_A}...{SEG_B}")
    assert bool(ex["records"]) is kept


def test_chinese_ellipsis_works():
    ex = _seg(_tx(50), f"{SEG_A}……{SEG_B}")
    assert len(ex["records"]) == 1 and ex["records"][0]["confidence"] <= 0.6


def test_segment_not_in_transcript_dropped():
    ex = _seg(_tx(50), f"{SEG_A}...这句话根本没有出现在转写里面")
    assert ex["records"] == []


def test_repeated_segment_uses_feasible_ordered_match():
    # SEG_A 首次出现距 SEG_B >800，第二次出现距 SEG_B 100 → 应保留（不能首次出现贪心）
    tx = SEG_A + "填" * 900 + SEG_A + "填" * 100 + SEG_B
    ex = _seg(tx, f"{SEG_A}...{SEG_B}")
    assert len(ex["records"]) == 1


def test_single_segment_confidence_untouched():
    ex = _seg(_tx(10), SEG_A)
    assert ex["records"][0]["confidence"] == 0.95


def test_validate_extract_segmented_evidence():
    tx = _tx(100)
    ex = _seg(tx, f"{SEG_A}...{SEG_B}")
    extract.validate_extract(ex, transcript=tx, expected_video_id="v")  # 合法多段不报错
    ex["records"][0]["evidence_quote"] = f"{SEG_B}...{SEG_A}"
    with pytest.raises(extract.ExtractError, match="evidence 非转写子串"):
        extract.validate_extract(ex, transcript=tx, expected_video_id="v")
