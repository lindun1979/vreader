import json


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


def _assert_no_content(ex, n_dropped=1):
    assert ex["records"] == []
    assert ex.get("no_content") is True
    assert ex["dropped_count"] == n_dropped
    assert len(ex["dropped"]) == n_dropped and ex["dropped"][0]["reason"]


def test_score_out_of_range_dropped():
    # 青铜满分 1，score=2 越界 → 丢弃 → 无有效记录 → no_content（不再抛错，2d）
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX,
                               claude_text=_claude([_rec(score=2)]))
    _assert_no_content(ex)


def test_gold_score_derives_rounds():
    tx = "黄金题 G001，模型第二轮才把它修好，勉强拿了一分。"
    r = _rec(bug_level="黄金", bug_id="G001", score=1, evidence_quote="模型第二轮才把它修好，勉强拿了一分")
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=tx, claude_text=_claude([r]))
    assert ex["records"][0]["solved"] is True and ex["records"][0]["rounds"] == 2  # 黄金 score1=第2轮


def test_bad_bug_level_dropped_no_content():
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX,
                               claude_text=_claude([_rec(bug_level="史诗")]))
    _assert_no_content(ex)


def test_extra_field_dropped_no_content():
    # 恶意/多余字段使 record schema 校验失败 → 丢弃（不入榜）
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX,
                               claude_text=_claude([_rec(malicious="drop table")]))
    _assert_no_content(ex)


def test_empty_records_no_content():
    # 空 records（视频无对战内容）→ no_content 成功，不抛错（2d）
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=_claude([]))
    assert ex["records"] == [] and ex.get("no_content") is True


def test_json_in_code_fence_parsed():
    fenced = f"```json\n{_claude([_rec()])}\n```"
    ex = extract.build_extract(aweme_id="v1", title="t", transcript=TX, claude_text=fenced)
    assert len(ex["records"]) == 1
