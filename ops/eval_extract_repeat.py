#!/usr/bin/env python3
"""提取 prompt 重复评测（只读；不写生产、不写 data 原件）。

对每个视频按 N 次独立运行调用 agy 主通道（不走兜底链，模型 = --model 或 config.LLM_MODEL，
`latest:<glob>` 会先解析成真实模型 id 并记入元数据），
每次在 prompt 末尾追加唯一 nonce 以绕过上游缓存；原始输出与元数据落盘，解析后与
gold 比分，并统计「同格多条」「省略号证据」「dropped」。同视频多次输出 sha 全同 →
SUSPECT_CACHE（独立性检查失败，非零退出 3）。
--gate "correct>=161,named_over<=3,batch_over<=0"：逐轮（跨全部所选 gold 视频）判定，任一轮不达标、
所选 gold 视频缺失或调用/构建失败 → 退出码 5。summary.json 的 per_run 记录逐轮汇总。

用法：
  python ops/eval_extract_repeat.py --data <数据副本目录> --videos <id,...|gold> \\
      --runs N --out <输出目录> [--prompt <模板文件>] [--model <agy 模型或 latest:<glob>>]
数据副本目录布局同生产 data：<data>/token_bug/<id>/{transcript.txt,extract.json}。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ops"))

from core import config, extract  # noqa: E402
import eval_gold  # noqa: E402

_ELLIPSIS = re.compile(r"\.{3,}|…")


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _video_ids(spec: str, gold: list[dict]) -> list[str]:
    if spec == "gold":
        return [v["aweme_id"] for v in gold]
    return [x.strip() for x in spec.split(",") if x.strip()]


def _raw_stats(raw_text: str) -> dict:
    """原始 LLM 输出（丢弃前）上的统计：同 (模型,等级,bug_id) 多条、省略号证据数。"""
    try:
        recs = extract._parse_records_json(raw_text)
    except extract.ExtractError:
        return {"parse_error": True, "dup_cells": 0, "ellipsis_evidence": 0, "raw_count": 0}
    cells: dict[tuple, int] = {}
    for r in recs:
        k = ((r.get("model_series") or "", r.get("model_version") or "",
              r.get("model_variant") or "", (r.get("model_raw") or "") if not r.get("model_series") else ""),
             r.get("bug_level"), (r.get("bug_id") or "").strip().upper())
        cells[k] = cells.get(k, 0) + 1
    return {"parse_error": False, "raw_count": len(recs),
            "dup_cells": sum(n - 1 for n in cells.values() if n > 1),
            "ellipsis_evidence": sum(1 for r in recs if _ELLIPSIS.search(r.get("evidence_quote") or ""))}


def run(args) -> int:
    data = Path(args.data)
    out = Path(args.out)
    gold = eval_gold.load_gold()
    gold_by_id = {v["aweme_id"]: v for v in gold}
    template = Path(args.prompt).read_text(encoding="utf-8") if args.prompt else None
    requested = args.model or config.LLM_MODEL
    model = extract.resolve_agy_model(requested)  # 只覆盖本次运行，不读写 .env
    summary: dict = {"model": model, "model_requested": requested, "runs": args.runs,
                     "prompt_file": args.prompt, "videos": {}}
    suspect = []
    for vid in _video_ids(args.videos, gold):
        vdir = data / "token_bug" / vid
        try:
            transcript = (vdir / "transcript.txt").read_text(encoding="utf-8")
            old = json.loads((vdir / "extract.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            summary["videos"][vid] = [{"missing_video": str(e)[:200]} for _ in range(args.runs)]
            continue
        known = {tuple(t) for t in old.get("known_versions_used") or []}
        title = old.get("title") or ""
        base = extract._build_prompt(transcript, title, known=known, template=template)
        odir = out / vid
        odir.mkdir(parents=True, exist_ok=True)
        runs = []
        for i in range(args.runs):
            nonce = str(uuid.uuid4())
            prompt = base + f"\n（评测运行编号 {nonce}，与任务无关，忽略）\n"
            t0 = time.time()
            err = None
            try:
                raw = extract._call_agy(prompt, timeout=config.LLM_TIMEOUT, model=model)
            except extract.ExtractError as e:
                raw, err = "", str(e)
            dt = time.time() - t0
            (odir / f"run{i}.txt").write_text(raw, encoding="utf-8")
            meta = {"nonce": nonce, "prompt_sha256": _sha(prompt), "output_sha256": _sha(raw),
                    "model": model, "llm_model": f"agy:{model}", "elapsed_s": round(dt, 2),
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": err}
            (odir / f"run{i}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
            res = {"meta": meta, **_raw_stats(raw)}
            if err is None:
                try:
                    ex = extract.build_extract(aweme_id=vid, title=title, transcript=transcript,
                                               claude_text=raw, known=known)
                    res["dropped"] = ex["dropped_count"]
                    res["records"] = len(ex["records"])
                    if vid in gold_by_id:
                        sv = eval_gold.score_video(gold_by_id[vid]["truth"], ex["records"])
                        res["correct"], res["total"] = sv["correct"], sv["total"]
                        res["diffs"] = sv["diffs"]
                        res["missing_cells"] = sv["missing"]
                        res["named_over"], res["batch_over"] = sv["named_over"], sv["batch_over"]
                except extract.ExtractError as e:
                    res["build_error"] = str(e)
            runs.append(res)
        shas = [r["meta"]["output_sha256"] for r in runs]
        if len(runs) > 1 and len(set(shas)) == 1:
            suspect.append(vid)
        summary["videos"][vid] = runs
    tot_c = sum(r.get("correct", 0) for rs in summary["videos"].values() for r in rs)
    tot_t = sum(r.get("total", 0) for rs in summary["videos"].values() for r in rs)
    summary["gold_correct"], summary["gold_total"] = tot_c, tot_t
    summary["suspect_cache"] = suspect
    summary["per_run"] = _per_run(summary["videos"], gold_by_id, args.runs)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'video':<22}{'run':>4}{'raw':>5}{'dup':>5}{'ell':>5}{'drop':>6}{'acc':>9}")
    for vid, rs in summary["videos"].items():
        for i, r in enumerate(rs):
            acc = f"{r['correct']}/{r['total']}" if "total" in r else "-"
            print(f"{vid:<22}{i:>4}{r.get('raw_count', 0):>5}{r.get('dup_cells', 0):>5}"
                  f"{r.get('ellipsis_evidence', 0):>5}{r.get('dropped', '-'):>6}{acc:>9}")
    print(f"gold 格准确率: {tot_c}/{tot_t}" + (f" = {tot_c / tot_t:.1%}" if tot_t else ""))
    print(f"{'run':>4}{'correct':>10}{'missing':>9}{'over':>6}{'batch':>7}{'vmiss':>7}{'fail':>6}")
    for i, pr in enumerate(summary["per_run"]):
        print(f"{i:>4}{pr['correct']:>5}/{pr['total']:<4}{pr['missing_cells']:>9}{pr['named_over']:>6}"
              f"{pr['batch_over']:>7}{pr['videos_missing']:>7}{pr['failures']:>6}")
    if suspect:
        print(f"❌ SUSPECT_CACHE（同视频多次输出完全相同）: {', '.join(suspect)}")
        return 3
    if args.gate:
        fails = _gate_failures(summary["per_run"], _parse_gate(args.gate))
        summary["gate"] = {"spec": args.gate, "failures": fails}
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        if fails:
            print("❌ GATE 未通过：" + "；".join(fails))
            return 5
        print(f"✅ GATE 通过：{args.gate}")
    return 0


def _per_run(videos: dict, gold_by_id: dict, runs: int) -> list[dict]:
    """逐轮跨全部所选 gold 视频汇总；缺视频、调用失败、构建失败分别计数。"""
    out = []
    for i in range(runs):
        agg = {"correct": 0, "total": 0, "missing_cells": 0, "named_over": 0, "batch_over": 0,
               "videos_missing": 0, "failures": 0}
        for vid, rs in videos.items():
            if vid not in gold_by_id:
                continue
            r = rs[i] if i < len(rs) else {"missing_video": "无该轮结果"}
            if "missing_video" in r:
                agg["videos_missing"] += 1
                continue
            if "total" not in r:  # 调用失败 / 构建失败 / 解析失败
                agg["failures"] += 1
                continue
            for k_src, k_dst in (("correct", "correct"), ("total", "total"),
                                 ("missing_cells", "missing_cells"), ("named_over", "named_over"),
                                 ("batch_over", "batch_over")):
                agg[k_dst] += r.get(k_src, 0)
        out.append(agg)
    return out


_GATE_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b}


def _parse_gate(spec: str) -> list[tuple[str, str, int]]:
    """'correct>=161,named_over<=3,batch_over<=0' → [(字段, 运算符, 阈值)]。"""
    out = []
    for part in spec.split(","):
        m = re.fullmatch(r"\s*(\w+)\s*(>=|<=)\s*(\d+)\s*", part)
        if not m:
            raise SystemExit(f"--gate 格式错误: {part!r}")
        out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def _gate_failures(per_run: list[dict], rules) -> list[str]:
    fails = []
    for i, pr in enumerate(per_run):
        if pr["videos_missing"] or pr["failures"]:
            fails.append(f"run{i} 缺视频 {pr['videos_missing']} / 失败 {pr['failures']}")
        for field, op, th in rules:
            if not _GATE_OPS[op](pr.get(field, 0), th):
                fails.append(f"run{i} {field}={pr.get(field, 0)} 不满足 {op}{th}")
    return fails


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--videos", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt")
    ap.add_argument("--gate", help='逐轮门槛，如 "correct>=161,named_over<=3,batch_over<=0"（不达标退出 5）')
    ap.add_argument("--model", help="本次评测用的 agy 模型（覆盖 LLM_MODEL，可为 latest:<glob>）")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
