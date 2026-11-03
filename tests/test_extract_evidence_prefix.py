"""P0-1：LLM 自加的「等级+编号」证据前缀剥离（仅认本条自己的编号）。"""
import json

from core import extract

TAIL = ("阿斯刷已经彻底征服这个王者K005了定位加修复用了46分钟改了22个文件之后"
        "在最关键的淘汰赛第一轮又一次一脚定千坤恭喜他直接进了三个球")
TX = "因为此时此刻DBT6" + TAIL
QUOTE = "王者BUGK005GBT6" + TAIL  # LLM 自加前缀，并把 DBT6 改成 GBT6


def _build(transcript, quote, bug_id="K005"):
    return extract.build_extract(
        aweme_id="prefix-v", title="GPT6Astra 对战 DeepSeek Flash", transcript=transcript,
        claude_text=json.dumps({"records": [{
            "model_raw": "GBT6 Astra", "model_series": "GPT", "model_version": "6",
            "model_variant": "Astra", "bug_level": "王者", "bug_id": bug_id,
            "solved_round": 1, "evidence_quote": quote, "confidence": 0.95,
        }]}, ensure_ascii=False),
    )


def test_strip_own_label():
    assert extract._strip_own_bug_label(QUOTE, "王者", "K005") == (
        "GBT6" + TAIL, "王者BUGK005")
    assert extract._strip_own_bug_label("王者 bug K005 GBT6" + TAIL, "王者", "K005") == (
        "GBT6" + TAIL, "王者 bug K005")
    assert extract._strip_own_bug_label("王者K005 GBT6" + TAIL, "王者", "K005") == (
        "GBT6" + TAIL, "王者K005")
    assert extract._strip_own_bug_label("\n 王者BUGK005" + "GBT6" + TAIL, "王者", "K005") == (
        "GBT6" + TAIL, "王者BUGK005")


def test_strip_refuses_other_bug_or_short_rest():
    assert extract._strip_own_bug_label("王者BUG003GBT6" + TAIL, "王者", "K005") is None
    assert extract._strip_own_bug_label("王者K005", "王者", "K005") is None  # 剥后为空
    assert extract._strip_own_bug_label("王者K005短证据", "王者", "K005") is None  # 剥后 <12 字
    assert extract._strip_own_bug_label(QUOTE, "王者", "") is None


def test_prefix_stripped_then_fuzzy_repair_with_cap():
    ex = _build(TX, QUOTE)
    assert ex["dropped_count"] == 0
    r = ex["records"][0]
    assert r["model_canonical"] == "GPT 6 Astra"
    assert r["confidence"] == 0.6  # 剥前缀后仍需 1 处模糊修正 → 强制人工复核
    assert r["evidence_quote"] in TX
    assert ex["evidence_repaired"][0]["edits"] == 1
    assert ex["evidence_repaired"][0]["original_quote"] == QUOTE


def test_prefix_stripped_then_exact_keeps_confidence():
    ex = _build("前文。" + "GBT6" + TAIL + "。后文", QUOTE)
    assert ex["dropped_count"] == 0
    r = ex["records"][0]
    assert r["confidence"] == 0.95  # 引文剥前缀后是原文，不降置信度
    assert r["evidence_quote"] == "GBT6" + TAIL
    assert ex["evidence_repaired"] == []


def test_mismatched_label_still_dropped():
    ex = _build(TX, "王者BUG003GBT6" + TAIL)
    assert ex["records"] == []
    assert ex["dropped"][0]["reason"] == "evidence 非转写子串"
