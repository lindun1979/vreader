"""Step 5 gold 评测契约（plan M10）：缺格/错分/多报/重复/冲突/错视频映射全部计错。"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_gold", ROOT / "ops" / "eval_gold.py")
eval_gold = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(eval_gold)


def _rec(canonical, bug_id, score):
    return {"model_canonical": canonical, "bug_id": bug_id, "score": score}


GOLD = {"GLM-5.3": {"G001": 2, "D001": 3}, "Grok 4.6": {"G001": 1}}


def test_all_correct():
    recs = [_rec("GLM-5.3", "G001", 2), _rec("GLM-5.3", "D001", 3), _rec("Grok 4.6", "G001", 1)]
    r = eval_gold.score_video(GOLD, recs)
    assert (r["correct"], r["total"]) == (3, 3) and r["diffs"] == []


def test_missing_cell_counts_error():
    recs = [_rec("GLM-5.3", "G001", 2), _rec("Grok 4.6", "G001", 1)]  # 缺 GLM D001
    r = eval_gold.score_video(GOLD, recs)
    assert r["correct"] == 2 and any(d["reason"] == "缺格" for d in r["diffs"])


def test_wrong_score_counts_error():
    recs = [_rec("GLM-5.3", "G001", 1), _rec("GLM-5.3", "D001", 3), _rec("Grok 4.6", "G001", 1)]
    r = eval_gold.score_video(GOLD, recs)
    assert r["correct"] == 2 and any(d["reason"] == "错分" and d["got"] == 1 for d in r["diffs"])


def test_over_report_counts_error():
    recs = [_rec("GLM-5.3", "G001", 2), _rec("GLM-5.3", "D001", 3), _rec("Grok 4.6", "G001", 1),
            _rec("Kimi K3", "B001", 1)]  # 多报
    r = eval_gold.score_video(GOLD, recs)
    assert r["correct"] == 3
    assert any(d["reason"] == "多报" and d["model"] == "Kimi K3" for d in r["diffs"])


def test_duplicate_key_counts_error_no_overwrite():
    # 同键出现两次（同分）→ 计错，禁 dict 覆盖
    recs = [_rec("GLM-5.3", "G001", 2), _rec("GLM-5.3", "G001", 2),
            _rec("GLM-5.3", "D001", 3), _rec("Grok 4.6", "G001", 1)]
    r = eval_gold.score_video(GOLD, recs)
    assert r["correct"] == 2  # G001 键因重复计错
    assert any(d["model"] == "GLM-5.3" and d["bug"] == "G001" and d["reason"] == "重复/冲突键"
               for d in r["diffs"])


def test_conflict_score_counts_error():
    recs = [_rec("GLM-5.3", "G001", 2), _rec("GLM-5.3", "G001", 0),  # 同键冲突分
            _rec("GLM-5.3", "D001", 3), _rec("Grok 4.6", "G001", 1)]
    r = eval_gold.score_video(GOLD, recs)
    assert r["correct"] == 2
    assert any(d["bug"] == "G001" and d["reason"] == "重复/冲突键" for d in r["diffs"])


def test_index_records_no_overwrite():
    idx, bad = eval_gold.index_records(
        [_rec("A", "B001", 1), _rec("A", "B001", 0)])
    assert ("A", "B001") in bad  # 冲突键标坏，不被后者覆盖成 0 静默通过


def test_gold_has_aweme_id_for_all_videos():
    gold = eval_gold.load_gold()
    assert gold and all(v.get("aweme_id", "").isdigit() for v in gold)
