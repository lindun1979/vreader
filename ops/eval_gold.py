#!/usr/bin/env python3
"""gold 真值集评测（只读，plan M10）：把某数据目录里的 extract.json 与
tests/gold/token_bug/gold.json 逐格比对，输出两口径准确率 + 逐格 diff。

比较键 = (aweme_id, model_canonical, bug_id) → score。缺格 / 多报 / 重复键 / 同键冲突
分数**全部计错**（禁止 dict 覆盖）。两口径：
  - 提取准确率：所有 extract.json 记录 vs 真值。
  - 实际可上榜：仅 decision ∈ {auto_ok, approved}（读同目录 vreader.db）vs 真值。
低于门槛（默认 96/97）非零退出。

用法：
  python ops/eval_gold.py <data_dir> [--min-extract N] [--min-board N]
  # data_dir 应为**生产 data 的隔离副本**（cp -r + 独立 DB）；本脚本只读，不写库/不重提。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_GOLD = _ROOT / "tests" / "gold" / "token_bug" / "gold.json"


def load_gold(path: Path = _GOLD) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["videos"]


def index_records(records: list[dict]) -> tuple[dict, set]:
    """把一条视频的记录建索引 (model_canonical, bug_id) → score。
    返回 (index, bad_keys)。bad_keys = 重复键或同键冲突分数的键（这些键一律计错）。"""
    index: dict[tuple[str, str], int] = {}
    seen: dict[tuple[str, str], list[int]] = {}
    for r in records:
        key = (r.get("model_canonical"), r.get("bug_id") or "")
        seen.setdefault(key, []).append(r.get("score"))
    bad = set()
    for key, scores in seen.items():
        uniq = set(scores)
        if len(scores) > 1:               # 重复键（含同分重复与冲突分）→ 计错
            bad.add(key)
        index[key] = scores[0] if len(uniq) == 1 else None
    return index, bad


def score_video(gold_truth: dict, records: list[dict]) -> dict:
    """比对一条视频。返回 {correct,total,diffs}。
    total = 真值格数；correct = 提取分数恰等真值且键无重复/冲突。
    多报（extract 有、真值无）计入 diffs（不减 total，但作为 over-report 记录）。"""
    index, bad = index_records(records)
    gold_keys = set()
    correct = 0
    total = 0
    diffs = []
    for model, bugs in gold_truth.items():
        for bug, truth in bugs.items():
            key = (model, bug)
            gold_keys.add(key)
            total += 1
            got = None if key in bad else index.get(key)
            if got == truth and key not in bad:
                correct += 1
            else:
                reason = "重复/冲突键" if key in bad else ("缺格" if key not in index else "错分")
                diffs.append({"model": model, "bug": bug, "truth": truth,
                              "got": index.get(key), "reason": reason})
    for key in index:
        if key not in gold_keys:
            diffs.append({"model": key[0], "bug": key[1], "truth": None,
                          "got": index[key], "reason": "多报"})
    return {"correct": correct, "total": total, "diffs": diffs}


def _load_extract(data_dir: Path, aweme_id: str) -> dict | None:
    p = data_dir / "token_bug" / aweme_id / "extract.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _board_visible_ids(db_path: Path) -> set:
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT record_id FROM record_decisions WHERE decision IN ('auto_ok','approved')"
        ).fetchall()
        return {r["record_id"] for r in rows}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _visible_records(records: list[dict], aweme_id: str, visible: set) -> list[dict]:
    sys.path.insert(0, str(_ROOT))
    from core import extract as ex_mod
    return [r for r in records if ex_mod.record_id(aweme_id, r) in visible]


def evaluate(data_dir: Path, gold: list[dict]) -> dict:
    db_path = data_dir / "vreader.db"
    visible = _board_visible_ids(db_path)
    agg = {"extract": {"correct": 0, "total": 0, "diffs": []},
           "board": {"correct": 0, "total": 0, "diffs": []}}
    for v in gold:
        aid = v["aweme_id"]
        ex = _load_extract(data_dir, aid)
        recs = ex["records"] if ex else []
        er = score_video(v["truth"], recs)
        agg["extract"]["correct"] += er["correct"]
        agg["extract"]["total"] += er["total"]
        agg["extract"]["diffs"] += [dict(d, aweme_id=aid) for d in er["diffs"]]
        vis_recs = _visible_records(recs, aid, visible) if visible else []
        br = score_video(v["truth"], vis_recs)
        agg["board"]["correct"] += br["correct"]
        agg["board"]["total"] += br["total"]
        agg["board"]["diffs"] += [dict(d, aweme_id=aid) for d in br["diffs"]]
    return agg


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    data_dir = Path(argv[0])
    min_extract = _arg_int(argv, "--min-extract", 96)
    min_board = _arg_int(argv, "--min-board", 0)
    gold = load_gold()
    agg = evaluate(data_dir, gold)
    ex, bd = agg["extract"], agg["board"]
    print(f"提取准确率: {ex['correct']}/{ex['total']}")
    print(f"实际可上榜: {bd['correct']}/{bd['total']}"
          + ("" if bd["total"] else "（无 vreader.db 决策，跳过）"))
    if ex["diffs"]:
        print("\n逐格 diff（提取口径）：")
        for d in sorted(ex["diffs"], key=lambda x: (x["aweme_id"], x["model"], x["bug"])):
            print(f"  [{d['aweme_id']}] {d['model']} · {d['bug']}: "
                  f"真值={d['truth']} 实得={d['got']} ({d['reason']})")
    ok = ex["correct"] >= min_extract and (not bd["total"] or bd["correct"] >= min_board)
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}（门槛 提取≥{min_extract}"
          + (f" 上榜≥{min_board}" if min_board else "") + "）")
    return 0 if ok else 2


def _arg_int(argv: list[str], flag: str, default: int) -> int:
    if flag in argv:
        return int(argv[argv.index(flag) + 1])
    return default


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
