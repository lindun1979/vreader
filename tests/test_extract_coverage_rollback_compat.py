"""Commit A 兼容层守卫（plan v4 Step 0/7）：含覆盖复跑新字段的 extract.json 必须能被
validate_extract / load_valid_extract / board 加载路径正常读取——不判坏、不重建、不跳过。

这是「回滚下界 = Commit A」的硬保证：功能上线后产出的带新字段产物，回滚到只含本 schema
兼容层的版本仍可读，不会因旧 schema 的 additionalProperties:false 把新产物判坏→触发 LLM
重建→跑坏榜单（[[board-reprocess-nondeterminism]]）。
"""
import json

from core import extract as ex_mod
from channels.token_bug import board

TX = "青铜这题 GLM-5.3 一次就修对了，表现不错，全程逻辑清晰。"


def _v2_record(**over):
    r = {"model_canonical": "GLM-5.3", "model_raw": "glm5.3", "model_series": "GLM",
         "model_version": "5.3", "model_variant": "", "bug_level": "青铜",
         "bug_id": "b1", "solved_round": 1, "score": 1, "rounds": 1, "solved": True,
         "evidence_quote": "GLM-5.3 一次就修对了", "confidence": 0.9}
    r.update(over)
    return r


def _full_featured_extract(vid="v1"):
    """带全部覆盖复跑新字段 + 单边标记的 v2 产物。"""
    return {
        "schema_rev": 2, "video_id": vid, "title": "t",
        "extracted_at": "2026-01-01T00:00:00", "extractor_version": "token_bug/2",
        "prompt_hash": "abc", "asr_model": "gladia-v2",
        "coverage_runs": 2,
        "coverage_rerun_status": "completed",
        "coverage_trigger_missing": ["Step"],
        "coverage_missing": [],
        "coverage_anchor_series": ["GLM", "Step"],
        "prompt_full_hash": "deadbeef0001",
        "prompt_full_hash_rerun": "deadbeef0002",
        "dropped_count": 1,
        "dropped": [{"reason": "evidence 非转写子串", "pass": 2}],
        "records": [_v2_record(single_run=True)],
    }


def _write(tmp_path, ex):
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    return p


def test_full_featured_envelope_validates():
    ex_mod.validate_extract(_full_featured_extract(), transcript=TX, expected_video_id="v1")


def test_full_featured_cache_not_rebuilt(tmp_path):
    p = _write(tmp_path, _full_featured_extract())
    out = ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert out is not None
    assert out["coverage_runs"] == 2 and out["records"][0]["single_run"] is True
    assert not list(tmp_path.glob("extract.json.bad.*"))  # 未判坏另存


def test_full_featured_survives_board_load(tmp_path):
    # board.load_extracts 逐 <vid>/extract.json 读，render_board 内对每份跑 validate_extract
    vdir = tmp_path / "v1"
    vdir.mkdir()
    _write(vdir, _full_featured_extract())
    loaded = board.load_extracts(tmp_path)
    assert len(loaded) == 1
    kept = [ex for ex in loaded if _valid(ex)]
    assert len(kept) == 1  # 未被 render_board 的 schema 层校验跳过
    md = board.render(kept, visible_ids={ex_mod.record_id("v1", kept[0]["records"][0])},
                      record_id_fn=ex_mod.record_id)
    assert isinstance(md, str)


def _valid(ex):
    try:
        ex_mod.validate_extract(ex)
        return True
    except ex_mod.ExtractError:
        return False


def test_records_without_new_fields_still_valid(tmp_path):
    """存量（无任何新字段）产物照常有效——兼容层不破坏旧路径。"""
    ex = _full_featured_extract()
    for k in ("coverage_runs", "coverage_rerun_status", "coverage_trigger_missing",
              "coverage_missing", "coverage_anchor_series", "prompt_full_hash_rerun"):
        ex.pop(k)
    ex["records"][0].pop("single_run")
    p = _write(tmp_path, ex)
    assert ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id="v1") is not None
