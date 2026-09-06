"""V-M02：缓存 extract.json 读回必过 validate；坏产物留证重建（C3）。

覆盖 schema 不拦的三类脏数据：伪 video_id、青铜 score=3（越界/derive 不一致）、伪证据。
"""
import json
from pathlib import Path

import pytest

from core import extract as ex_mod

TRANSCRIPT = "青铜这题 GLM-5.3 一次就修对了，表现不错，全程逻辑清晰。"


def _good_record():
    return {
        "model_canonical": "GLM-5.3", "model_raw": "glm5.3", "bug_level": "青铜",
        "bug_id": "b1", "score": 1, "rounds": 1, "solved": True,
        "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9,
    }


def _good_extract(video_id="vid1"):
    return {
        "video_id": video_id, "title": "t", "extracted_at": "2026-01-01T00:00:00",
        "extractor_version": "token_bug/1", "prompt_hash": "abc123",
        "asr_model": "gladia-v2", "records": [_good_record()],
    }


def test_good_extract_passes(tmp_path):
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(_good_extract()), encoding="utf-8")
    out = ex_mod.load_valid_extract(str(p), transcript=TRANSCRIPT, expected_video_id="vid1")
    assert out is not None and out["records"][0]["model_canonical"] == "GLM-5.3"


def test_wrong_video_id_rebuilds(tmp_path):
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(_good_extract(video_id="OTHER")), encoding="utf-8")
    out = ex_mod.load_valid_extract(str(p), transcript=TRANSCRIPT, expected_video_id="vid1")
    assert out is None
    assert list(tmp_path.glob("extract.json.bad.*")), "坏文件应留证"


def test_score_out_of_range_rebuilds(tmp_path):
    ex = _good_extract()
    ex["records"][0]["score"] = 3  # 青铜满分=1，3 越界 + derive 不一致
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(ex), encoding="utf-8")
    out = ex_mod.load_valid_extract(str(p), transcript=TRANSCRIPT, expected_video_id="vid1")
    assert out is None


def test_fake_evidence_rebuilds(tmp_path):
    ex = _good_extract()
    ex["records"][0]["evidence_quote"] = "这段证据根本不在转写里出现过"
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(ex), encoding="utf-8")
    out = ex_mod.load_valid_extract(str(p), transcript=TRANSCRIPT, expected_video_id="vid1")
    assert out is None


def test_corrupt_json_rebuilds(tmp_path):
    p = tmp_path / "extract.json"
    p.write_text("{not valid json", encoding="utf-8")
    out = ex_mod.load_valid_extract(str(p), transcript=TRANSCRIPT, expected_video_id="vid1")
    assert out is None
    assert list(tmp_path.glob("extract.json.bad.*"))


def test_derive_mismatch_solved_rounds(tmp_path):
    ex = _good_extract()
    ex["records"][0]["rounds"] = 5  # score=1 应 rounds=1
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(ex), encoding="utf-8")
    with pytest.raises(ex_mod.ExtractError):
        ex_mod.validate_extract(ex, transcript=TRANSCRIPT)
