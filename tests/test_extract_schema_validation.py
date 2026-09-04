import json

import pytest

from core import extract

TX = "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了，非常干净利落表现优秀。"


def _claude(records):
    return json.dumps({"records": records}, ensure_ascii=False)


def test_valid_extract_passes():
    txt = _claude([{
        "model_raw": "GPT 5", "model_canonical": "GPT-5", "bug_level": "青铜",
        "solved": True, "rounds": 1,
        "evidence_quote": "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了",
        "confidence": 0.9,
    }])
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=txt)
    assert ex["records"][0]["model_canonical"] == "GPT-5"
    assert ex["extractor_version"] and ex["prompt_hash"]


def test_solved_true_requires_rounds():
    txt = _claude([{
        "model_raw": "GPT 5", "model_canonical": "GPT-5", "bug_level": "青铜",
        "solved": True, "rounds": None,  # 违反 solved=true ⇒ rounds int
        "evidence_quote": "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了",
        "confidence": 0.9,
    }])
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=txt)


def test_bad_bug_level_rejected():
    txt = _claude([{
        "model_raw": "GPT 5", "model_canonical": "GPT-5", "bug_level": "史诗",
        "solved": True, "rounds": 1,
        "evidence_quote": "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了",
        "confidence": 0.9,
    }])
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=txt)


def test_extra_field_rejected():
    txt = _claude([{
        "model_raw": "GPT 5", "model_canonical": "GPT-5", "bug_level": "青铜",
        "solved": True, "rounds": 1,
        "evidence_quote": "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了",
        "confidence": 0.9, "malicious": "drop table",  # additionalProperties: false
    }])
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=txt)


def test_empty_records_rejected_by_minitems():
    with pytest.raises(extract.ExtractError):
        extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([]))


def test_json_in_code_fence_parsed():
    inner = _claude([{
        "model_raw": "GPT 5", "model_canonical": "GPT-5", "bug_level": "青铜",
        "solved": True, "rounds": 1,
        "evidence_quote": "我让 GPT 5 解一个青铜级别的空指针，它一次就解出来了",
        "confidence": 0.9,
    }])
    fenced = f"```json\n{inner}\n```"
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=fenced)
    assert len(ex["records"]) == 1
