"""WP-F：合成回放集成测试（build_extract → apply_decisions → 回执告警），复现 7689 事故形态。"""
import json
import sys
from pathlib import Path

from core import db, extract

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import replay_extract_fixture as rp  # noqa: E402

FX = json.loads((Path(__file__).parent / "fixtures" / "extract_replay_synthetic.json")
                .read_text(encoding="utf-8"))


def _find(recs, model, bug, sr):
    hits = [r for r in recs if r["model_canonical"] == model and r["bug_id"] == bug
            and r["solved_round"] == sr]
    assert len(hits) == 1, (model, bug, sr, hits)
    return hits[0]


def test_synthetic_replay_end_to_end():
    res = rp.replay(FX)
    ex, dec = res["extract"], res["decisions"]
    recs = ex["records"]
    assert len(FX["raw_llm_records"]) == 11
    assert len(recs) == 9 and ex["dropped_count"] == 2
    assert {d["reason"] for d in ex["dropped"]} == {"evidence 非转写子串"}
    # 分段证据两条被保留、强制复核
    glm3 = _find(recs, "GLM-5.3", "K005", 3)
    qwen2 = _find(recs, "Qwen3.8", "K005", 2)
    assert glm3["confidence"] <= 0.6 and qwen2["confidence"] <= 0.6
    # 空 bug_id 被回填
    assert _find(recs, "GLM-5.3", "G006", 1)
    # 拆轮的 r1/r3 进 pending_conflict
    glm1 = _find(recs, "GLM-5.3", "K005", 1)
    assert dec[extract.record_id(FX["aweme_id"], glm1)] == db.PENDING_CONFLICT
    assert dec[extract.record_id(FX["aweme_id"], glm3)] == db.PENDING_CONFLICT
    # 回执含黄金漏提行
    assert any(w.startswith("疑似漏提：Qwen3.8 × 黄金 G006") for w in res["warnings"])
    assert any(w.startswith("丢弃 2 条") for w in res["warnings"])
