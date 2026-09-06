import json


from core import extract

TX = "我让 Kimi K3 解一个黄金级别的并发问题，它第二轮才解出来，还算可以。"


def _one(evidence):
    return json.dumps({"records": [{
        "model_raw": "Kimi K3", "model_canonical": "Kimi K3", "bug_level": "黄金", "bug_id": "G001",
        "score": 1, "evidence_quote": evidence, "confidence": 0.8,
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
