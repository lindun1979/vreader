#!/usr/bin/env python3
"""提取 prompt 重复评测（只读；不写生产、不写 data 原件）。

对每个视频按 N 次独立运行调用 agy 主通道（不走兜底链，模型固定 config.LLM_MODEL），
每次在 prompt 末尾追加唯一 nonce 以绕过上游缓存；原始输出与元数据落盘，解析后与
gold 比分，并统计「同格多条」「省略号证据」「dropped」。同视频多次输出 sha 全同 →
SUSPECT_CACHE（独立性检查失败，非零退出）。

用法：
  python ops/eval_extract_repeat.py --data <数据副本目录> --videos <id,...|gold> \\
      --runs N --out <输出目录> [--prompt <模板文件>]
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
    summary: dict = {"model": config.LLM_MODEL, "runs": args.runs,
                     "prompt_file": args.prompt, "videos": {}}
    suspect = []
    for vid in _video_ids(args.videos, gold):
        vdir = data / "token_bug" / vid
        transcript = (vdir / "transcript.txt").read_text(encoding="utf-8")
        old = json.loads((vdir / "extract.json").read_text(encoding="utf-8"))
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
                raw = extract._call_agy(prompt, timeout=config.LLM_TIMEOUT, model=config.LLM_MODEL)
            except extract.ExtractError as e:
                raw, err = "", str(e)
            dt = time.time() - t0
            (odir / f"run{i}.txt").write_text(raw, encoding="utf-8")
            meta = {"nonce": nonce, "prompt_sha256": _sha(prompt), "output_sha256": _sha(raw),
                    "model": config.LLM_MODEL, "elapsed_s": round(dt, 2),
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
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'video':<22}{'run':>4}{'raw':>5}{'dup':>5}{'ell':>5}{'drop':>6}{'acc':>9}")
    for vid, rs in summary["videos"].items():
        for i, r in enumerate(rs):
            acc = f"{r['correct']}/{r['total']}" if "total" in r else "-"
            print(f"{vid:<22}{i:>4}{r.get('raw_count', 0):>5}{r.get('dup_cells', 0):>5}"
                  f"{r.get('ellipsis_evidence', 0):>5}{r.get('dropped', '-'):>6}{acc:>9}")
    print(f"gold 格准确率: {tot_c}/{tot_t}" + (f" = {tot_c / tot_t:.1%}" if tot_t else ""))
    if suspect:
        print(f"❌ SUSPECT_CACHE（同视频多次输出完全相同）: {', '.join(suspect)}")
        return 3
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True)
    ap.add_argument("--videos", required=True)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--prompt")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
