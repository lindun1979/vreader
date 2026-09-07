"""Step 2 双轨缓存兼容（plan M06/M13）：envelope 定轨，整文件判轨。"""
import json

from core import extract as ex_mod

TX = "青铜这题 GLM-5.3 一次就修对了，表现不错，全程逻辑清晰。"


def _legacy_record():
    return {"model_canonical": "GLM-5.3", "model_raw": "glm5.3", "bug_level": "青铜",
            "bug_id": "b1", "score": 1, "rounds": 1, "solved": True,
            "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9}


def _legacy_extract(vid="v1"):
    return {"video_id": vid, "title": "t", "extracted_at": "2026-01-01T00:00:00",
            "extractor_version": "token_bug/1", "prompt_hash": "abc",
            "asr_model": "gladia-v2", "records": [_legacy_record()]}


def _v2_record():
    return {"model_canonical": "GLM-5.3", "model_raw": "glm5.3", "model_series": "GLM",
            "model_version": "5.3", "model_variant": "", "bug_level": "青铜",
            "bug_id": "b1", "score": 1, "rounds": 1, "solved": True,
            "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9}


def _v2_extract(vid="v1"):
    return {"schema_rev": 2, "video_id": vid, "title": "t", "extracted_at": "2026-01-01T00:00:00",
            "extractor_version": "token_bug/2", "prompt_hash": "abc",
            "asr_model": "gladia-v2", "records": [_v2_record()]}


def _write(tmp_path, ex):
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    return p


def test_legal_legacy_cache_not_rebuilt(tmp_path):
    p = _write(tmp_path, _legacy_extract())
    out = ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert out is not None and out["records"][0]["model_canonical"] == "GLM-5.3"
    assert not list(tmp_path.glob("extract.json.bad.*"))


def test_legal_v2_cache_not_rebuilt(tmp_path):
    p = _write(tmp_path, _v2_extract())
    out = ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert out is not None and out["schema_rev"] == 2


def test_v2_missing_series_field_whole_file_rejected(tmp_path):
    ex = _v2_extract()
    del ex["records"][0]["model_series"]  # v2 轨缺字段 = 坏缓存，整文件拒
    p = _write(tmp_path, ex)
    out = ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert out is None
    assert list(tmp_path.glob("extract.json.bad.*"))


def test_v2_compose_mismatch_rejected(tmp_path):
    ex = _v2_extract()
    ex["records"][0]["model_version"] = "5.2"  # compose→GLM-5.2 ≠ canonical GLM-5.3
    p = _write(tmp_path, ex)
    assert ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1") is None


def test_schema_rev_3_rejected(tmp_path):
    ex = _v2_extract()
    ex["schema_rev"] = 3
    p = _write(tmp_path, ex)
    assert ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1") is None
    assert list(tmp_path.glob("extract.json.bad.*"))


def test_pseudo_legacy_format_rejected(tmp_path):
    # 无 schema_rev 但 canonical 不在冻结名单（伪旧格式）→ 拒
    ex = _legacy_extract()
    ex["records"][0]["model_canonical"] = "GLM-9.9"  # 长得像模型名的伪旧数据
    p = _write(tmp_path, ex)
    assert ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1") is None


def test_unknown_readable_both_tracks(tmp_path):
    tx = "有个没听清的模型糊糊 解了青铜题一次过"
    # legacy UNKNOWN
    lex = _legacy_extract()
    lex["records"][0].update(model_canonical="UNKNOWN", model_raw="糊糊",
                             evidence_quote="有个没听清的模型糊糊 解了青铜题一次过")
    p = _write(tmp_path, lex)
    assert ex_mod.load_valid_extract(str(p), transcript=tx, expected_video_id="v1") is not None
    # v2 UNKNOWN（三字段空）
    (tmp_path / "extract.json").unlink()
    vex = _v2_extract()
    vex["records"][0].update(model_canonical="UNKNOWN", model_raw="糊糊", model_series="",
                             model_version="", model_variant="",
                             evidence_quote="有个没听清的模型糊糊 解了青铜题一次过")
    p2 = _write(tmp_path, vex)
    assert ex_mod.load_valid_extract(str(p2), transcript=tx, expected_video_id="v1") is not None


def test_v2_unknown_with_nonempty_triple_rejected(tmp_path):
    vex = _v2_extract()
    vex["records"][0].update(model_canonical="UNKNOWN")  # 但 series 仍是 GLM → 非法
    p = _write(tmp_path, vex)
    assert ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1") is None
