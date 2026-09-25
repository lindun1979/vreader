"""WP-F：回放脚本 known 集合时点（A=提取输入、B=决策时点）分别传入，禁止 None。"""
import json
import sys
from pathlib import Path

from core import db

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import replay_extract_fixture as rp  # noqa: E402

FX = json.loads((Path(__file__).parent / "fixtures" / "extract_replay_synthetic.json")
                .read_text(encoding="utf-8"))


def test_replay_uses_fixture_extract_and_decision_known_sets():
    doubao = ["Doubao Seed", "2.1", ""]
    fx = {"aweme_id": "k1", "title": "豆包挑战赛",
          "transcript": "黄金G001 豆包第一轮的修改结果 点击验证 没有问题恭喜加两分",
          "raw_llm_records": [{"model_raw": "豆包", "model_series": "", "model_version": "",
                               "model_variant": "", "bug_level": "黄金", "bug_id": "G001",
                               "solved_round": 1, "confidence": 0.95,
                               "evidence_quote": "豆包第一轮的修改结果 点击验证 没有问题恭喜加两分"}],
          "known_at_extract": [doubao], "known_at_decision": [["GLM", "5.3", ""]]}
    res = rp.replay(fx)
    r = res["extract"]["records"][0]
    assert r["model_canonical"] == "Doubao Seed 2.1"       # 解析用 known_at_extract
    assert list(res["decisions"].values()) == [db.PENDING_NEW_VERSION]  # 判定用 known_at_decision
    fx2 = dict(fx, known_at_decision=[doubao])
    assert list(rp.replay(fx2)["decisions"].values()) == [db.AUTO_OK]
    fx3 = dict(fx, known_at_extract=[["GLM", "5.3", ""]], known_at_decision=[doubao])
    assert rp.replay(fx3)["extract"]["records"][0]["model_canonical"] == "UNKNOWN"


def test_replay_refuses_missing_known_set():
    import pytest
    fx = dict(FX)
    fx.pop("known_at_decision")
    with pytest.raises(ValueError):
        rp.replay(fx)
