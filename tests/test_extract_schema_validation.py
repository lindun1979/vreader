import json

import pytest

from core import extract

TX = "我让 Qwen3.8 解一个青铜级别的空指针，它一次就解出来了，非常干净利落表现优秀。"


def _claude(records):
    return json.dumps({"records": records}, ensure_ascii=False)


def _rec(**over):
    r = {"model_raw": "Qwen3.8", "model_canonical": "Qwen3.8", "bug_level": "青铜",
         "bug_id": "B001", "score": 1,
         "evidence_quote": "我让 Qwen3.8 解一个青铜级别的空指针，它一次就解出来了",
         "confidence": 0.9}
    r.update(over)
    return r


def test_valid_extract_passes():
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([_rec()]))
    assert ex["records"][0]["model_canonical"] == "Qwen3.8"
    assert ex["records"][0]["solved"] is True and ex["records"][0]["rounds"] == 1
    assert ex["extractor_version"] and ex["prompt_hash"]


def test_score_out_of_range_dropped():
    # 青铜满分 1，score=2 越界 → 丢弃 → 无有效记录
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([_rec(score=2)]))


def test_gold_score_derives_rounds():
    tx = "黄金题 G001，模型第二轮才把它修好，勉强拿了一分。"
    r = _rec(bug_level="黄金", bug_id="G001", score=1, evidence_quote="模型第二轮才把它修好，勉强拿了一分")
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=tx, claude_text=_claude([r]))
    assert ex["records"][0]["solved"] is True and ex["records"][0]["rounds"] == 2  # 黄金 score1=第2轮


def test_bad_bug_level_rejected():
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([_rec(bug_level="史诗")]))


def test_extra_field_rejected():
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX,
                              claude_text=_claude([_rec(malicious="drop table")]))


def test_empty_records_rejected_by_minitems():
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([]))


def test_json_in_code_fence_parsed():
    fenced = f"```json\n{_claude([_rec()])}\n```"
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=fenced)
    assert len(ex["records"]) == 1
