"""飞书回执覆盖复跑文案（plan v4 Step 7）：completed / 两跑均缺 / no_content / failed /
skipped_budget 五分支——文案、缺失系列、单边去重计数、未验证提示。"""
from core import pipeline


def _rec(series="GLM", single=False):
    r = {"model_canonical": f"{series}-x", "model_series": series, "model_version": "1",
         "model_variant": "", "bug_level": "青铜", "bug_id": "B001", "solved_round": 1,
         "score": 1, "rounds": 1, "solved": True, "evidence_quote": "q" * 10, "confidence": 0.9}
    if single:
        r["single_run"] = True
    return r


def test_no_notice_when_not_triggered():
    ex = {"records": [_rec()]}
    assert pipeline._coverage_notice(ex) == ""


def test_completed_notice_counts_single_run():
    ex = {"coverage_rerun_status": "completed", "coverage_trigger_missing": ["Step"],
          "coverage_missing": [], "records": [_rec(), _rec("Step", single=True)]}
    msg = pipeline._coverage_notice(ex)
    assert "触发覆盖复跑" in msg and "疑似丢弃：Step" in msg
    assert "单边记录 1 条" in msg
    assert "两跑均未提取到" not in msg


def test_completed_with_remaining_missing():
    ex = {"coverage_rerun_status": "completed", "coverage_trigger_missing": ["Step"],
          "coverage_missing": ["Step"], "records": [_rec()]}
    msg = pipeline._coverage_notice(ex)
    assert "单边记录 0 条" in msg
    assert "两跑均未提取到：Step" in msg


def test_failed_notice_unverified():
    ex = {"coverage_rerun_status": "failed", "coverage_trigger_missing": ["Step"],
          "records": [_rec()]}
    msg = pipeline._coverage_notice(ex)
    assert "覆盖复跑未完成（失败）" in msg and "未经复跑验证" in msg
    assert "Step" in msg


def test_skipped_budget_notice_unverified():
    ex = {"coverage_rerun_status": "skipped_budget", "coverage_trigger_missing": ["Step"],
          "records": [_rec()]}
    msg = pipeline._coverage_notice(ex)
    assert "覆盖复跑未完成（预算不足）" in msg and "未经复跑验证" in msg
