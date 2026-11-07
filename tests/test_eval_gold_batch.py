"""D-A gold 口径：batch 记录不匹配编号真值，计「batch 多报」；点名全对 + 额外 batch 判不过门槛。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import eval_extract_repeat as er  # noqa: E402
import eval_gold  # noqa: E402

TRUTH = {"Hunyuan 4.0": {"S001": 0, "S002": 1}}


def _r(bug, score, slot=None):
    r = {"model_canonical": "Hunyuan 4.0", "bug_level": "白银", "bug_id": bug, "score": score}
    if slot:
        r["batch_slot"] = slot
    return r


def test_batch_not_matched_to_named_truth():
    sv = eval_gold.score_video(TRUTH, [_r("S001", 0), _r("", 1, "1-1")])
    assert sv["correct"] == 1 and sv["total"] == 2
    assert sv["missing"] == 1 and sv["batch_over"] == 1 and sv["named_over"] == 0
    assert any(d["reason"] == "缺格" and d["bug"] == "S002" for d in sv["diffs"])


def test_all_named_correct_plus_batch_fails_gate():
    sv = eval_gold.score_video(TRUTH, [_r("S001", 0), _r("S002", 1), _r("", 1, "1-1"), _r("", 1, "1-2")])
    assert sv["correct"] == 2 and sv["batch_over"] == 2
    per_run = [{"correct": 2, "total": 2, "missing_cells": 0, "named_over": 0, "batch_over": 2,
                "videos_missing": 0, "failures": 0}]
    fails = er._gate_failures(per_run, er._parse_gate("correct>=2,named_over<=3,batch_over<=0"))
    assert fails == ["run0 batch_over=2 不满足 <=0"]
