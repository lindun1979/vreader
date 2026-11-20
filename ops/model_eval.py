#!/usr/bin/env python3
"""提取模型黄金集评测（标准流程；研发机运行，只读生产）。

  python ops/model_eval.py snapshot [--out DIR]
      从生产拉 gold 13 条 + 检查视频的 transcript/extract，并只读导出 known_versions
      （建只含该表的 vreader.db，供已学变体归一）。默认 data/eval/snapshots/<ts>/。
  python ops/model_eval.py run --models "A,B" [--snapshot DIR] [--videos gold+check|id,...] [--out DIR]
      未给 --snapshot 先拉快照。按 cliproxy /v1/models 的 owned_by 分出口：同出口串行、出口间并行；
      限流类错误（429/503/high demand/auth_unavailable，含 cliproxy 冷却期秒回）等待 5/10/15 分钟重试，
      空返回/超时/其他错误不重试。--out 指向已有运行目录 = 续跑（已成功的视频跳过）。
  python ops/model_eval.py report RUN [RUN...] [--snapshot DIR] [--out FILE]
      离线复算（不调 LLM）：用落盘原始输出按**当前代码**重算并出对比表。RUN 可为本脚本的运行目录，
      或 eval_extract_repeat 的旧输出目录（<vid>/run0.txt，须给 --snapshot）。

检查集真值（可选）：tests/gold/token_bug/check.json，格式同 gold.json，另有
"batch": {canonical: {等级: {"<score>": 条数}}} 记未念编号的批量公布题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ops"))

from core import config, db, extract  # noqa: E402
import eval_gold  # noqa: E402

PROD_HOST = "chivox@100.121.81.8"
PROD_DATA = "workspace/vreader/data"          # 相对生产机 home
CHECK_VIDEOS = ["7704993286842354980", "7709426400641502504"]
CHECK_PATH = _ROOT / "tests" / "gold" / "token_bug" / "check.json"
EVAL_ROOT = _ROOT / "data" / "eval"
BACKOFF = [300, 600, 900]                     # 限流等待（秒），次数即最大重试次数
LANE_GAP = {"google": 30}                     # 同出口请求最小间隔：Gemini key 池与生产共用
_THROTTLE = re.compile(r"429|503|high demand|auth_unavailable|UNAVAILABLE|RESOURCE_EXHAUSTED|rate.?limit",
                       re.I)
_TIMEOUT = re.compile(r"超时|timed out|timeout", re.I)
_sleep = time.sleep                           # 测试注入


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _ts() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model)


def _git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(_ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _gold_ids() -> list[str]:
    return [v["aweme_id"] for v in eval_gold.load_gold()]


def _video_ids(spec: str) -> list[str]:
    if spec == "gold+check":
        return _gold_ids() + CHECK_VIDEOS
    if spec == "gold":
        return _gold_ids()
    return [x.strip() for x in spec.split(",") if x.strip()]


# ---------------- 快照 ----------------

def snapshot(out: Path, vids: list[str]) -> Path:
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"快照目录非空: {out}")
    for vid in vids:
        d = out / "token_bug" / vid
        d.mkdir(parents=True, exist_ok=True)
        src = f"{PROD_DATA}/token_bug/{vid}"
        subprocess.run(["scp", "-q", "-o", "ConnectTimeout=15",
                        f"{PROD_HOST}:{src}/transcript.txt", f"{PROD_HOST}:{src}/extract.json", str(d)],
                       check=True, timeout=120)
    # known_versions：严格只读打开生产库（不开可写连接）
    r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", PROD_HOST,
                        f"sqlite3 -readonly -json {PROD_DATA}/vreader.db "
                        "'SELECT series,version,variant,source,approved_by,created_at FROM known_versions'"],
                       capture_output=True, text=True, check=True, timeout=60)
    rows = json.loads(r.stdout) if r.stdout.strip() else []
    if not rows:
        raise SystemExit("生产 known_versions 为空或读取失败")
    db.init(out / "vreader.db")
    conn = db.connect(out / "vreader.db")
    try:
        conn.executemany("INSERT OR REPLACE INTO known_versions(series,version,variant,source,approved_by,"
                         "created_at) VALUES (:series,:version,:variant,:source,:approved_by,:created_at)", rows)
        conn.commit()
    finally:
        conn.close()
    files = {}
    for vid in vids:
        for name in ("transcript.txt", "extract.json"):
            p = out / "token_bug" / vid / name
            b = p.read_bytes()
            if not b.strip():
                raise SystemExit(f"快照文件为空: {p}")
            files[f"token_bug/{vid}/{name}"] = _sha(b)
        json.loads((out / "token_bug" / vid / "extract.json").read_text(encoding="utf-8"))
    manifest = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": f"{PROD_HOST}:{PROD_DATA}",
                "videos": vids, "known_versions": len(rows), "files": files}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"快照完成: {out}（{len(vids)} 视频，known_versions {len(rows)} 行）")
    return out


def _video_input(snap: Path, vid: str) -> tuple[str, str, set]:
    """已知集：gold 视频用旧 extract 的 known_versions_used（与历史基线可比）；
    其他（检查）视频用快照里生产当前的全部 known_versions（贴近现在处理新视频的情形）。"""
    d = snap / "token_bug" / vid
    transcript = (d / "transcript.txt").read_text(encoding="utf-8")
    old = json.loads((d / "extract.json").read_text(encoding="utf-8"))
    known = {tuple(t) for t in old.get("known_versions_used") or []}
    if vid not in _gold_ids() and (snap / "vreader.db").exists():
        conn = db.connect_ro(snap / "vreader.db")
        try:
            known = {tuple(r) for r in conn.execute("SELECT series, version, variant FROM known_versions")}
        finally:
            conn.close()
    return transcript, old.get("title") or "", known


# ---------------- 运行 ----------------

def model_lanes(models: list[str]) -> dict[str, str]:
    """模型 → 出口（cliproxy /v1/models 的 owned_by）；不在列表里的模型直接报错（防拼写错）。"""
    req = urllib.request.Request(config.LLM_BASE_URL.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {config.LLM_API_KEY}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        owned = {m["id"]: m.get("owned_by") or "unknown" for m in json.loads(resp.read())["data"]}
    missing = [m for m in models if m not in owned]
    if missing:
        raise SystemExit(f"cliproxy 上没有这些模型: {missing}")
    return {m: owned[m] for m in models}


def _classify(err: str | None, raw: str) -> str:
    if err is None:
        return "ok" if raw.strip() else "empty"
    if _THROTTLE.search(err):
        return "throttled"
    if _TIMEOUT.search(err):
        return "timeout"
    return "error"


def _call_one(model: str, lane: str, prompt: str, vdir: Path, last_call: list) -> dict:
    attempts, waited = 0, 0
    while True:
        gap = LANE_GAP.get(lane, 0)
        if last_call[0] is not None and gap:
            rest = gap - (time.time() - last_call[0])
            if rest > 0:
                _sleep(rest)
        attempts += 1
        t0 = time.time()
        err = None
        try:
            raw = extract._dispatch("openai", model, prompt, timeout=config.LLM_TIMEOUT)
        except extract.ExtractError as e:
            raw, err = "", str(e)
        dt = time.time() - t0
        last_call[0] = time.time()
        status = _classify(err, raw)
        if status == "throttled" and attempts <= len(BACKOFF):
            w = BACKOFF[attempts - 1]
            print(f"  [{lane}] {model} {vdir.name} 限流（{err[:80]}），等 {w}s 重试", flush=True)
            _sleep(w)
            waited += w
            continue
        break
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "raw.txt").write_text(raw, encoding="utf-8")
    meta = {"model": model, "lane": lane, "status": status, "error": (err or "")[:300],
            "elapsed_s": round(dt, 2), "attempts": attempts, "waited_s": waited,
            "prompt_sha256": _sha(prompt.encode()), "output_sha256": _sha(raw.encode()),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (vdir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


def _done(vdir: Path) -> bool:
    try:
        return json.loads((vdir / "meta.json").read_text(encoding="utf-8"))["status"] == "ok"
    except (OSError, json.JSONDecodeError, KeyError):
        return False


def _run_lane(lane: str, models: list[str], run_dir: Path, snap: Path, vids: list[str]) -> None:
    last_call = [None]
    for model in models:
        for vid in vids:
            vdir = run_dir / _slug(model) / vid
            if _done(vdir):
                continue
            transcript, title, known = _video_input(snap, vid)
            prompt = extract._build_prompt(transcript, title, known=known)
            m = _call_one(model, lane, prompt, vdir, last_call)
            print(f"  [{lane}] {model} {vid} {m['status']} {m['elapsed_s']}s", flush=True)


def run(models: list[str], run_dir: Path, snap: Path | None, vids: list[str],
        lanes: dict[str, str] | None = None) -> Path:
    mpath = run_dir / "manifest.json"
    if mpath.exists():                       # 续跑：沿用原快照/视频/模型
        man = json.loads(mpath.read_text(encoding="utf-8"))
        snap, vids = Path(man["snapshot"]), man["videos"]
        lanes = lanes or {m: man["models"].get(m) or model_lanes([m])[m] for m in models}
        man["models"].update(lanes)
    else:
        if snap is None:
            snap = snapshot(EVAL_ROOT / "snapshots" / _ts(), vids)
        lanes = lanes or model_lanes(models)
        man = {"snapshot": str(snap.resolve()), "videos": vids, "models": lanes,
               "git_head": _git_head(), "llm_timeout": config.LLM_TIMEOUT, "backoff": BACKOFF,
               "lane_gap": LANE_GAP, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    run_dir.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
    by_lane: dict[str, list[str]] = {}
    for m in models:
        by_lane.setdefault(lanes[m], []).append(m)
    threads = [threading.Thread(target=_run_lane, args=(lane, ms, run_dir, snap, vids), name=lane)
               for lane, ms in by_lane.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return run_dir


# ---------------- 复算与报告 ----------------

def load_run(run_dir: Path) -> list[dict]:
    """→ [{model, lane, snapshot, outputs: {vid: (raw|None, meta)}}]；兼容 eval_extract_repeat 旧输出。"""
    mpath = run_dir / "manifest.json"
    if mpath.exists():
        man = json.loads(mpath.read_text(encoding="utf-8"))
        res = []
        for model, lane in man["models"].items():
            outs = {}
            for vid in man["videos"]:
                vdir = run_dir / _slug(model) / vid
                try:
                    meta = json.loads((vdir / "meta.json").read_text(encoding="utf-8"))
                    outs[vid] = ((vdir / "raw.txt").read_text(encoding="utf-8"), meta)
                except (OSError, json.JSONDecodeError):
                    outs[vid] = (None, {"status": "not_run"})
            res.append({"model": model, "lane": lane, "snapshot": man["snapshot"], "outputs": outs})
        return res
    summ = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    outs = {}
    for vid in summ["videos"]:
        try:
            raw = (run_dir / vid / "run0.txt").read_text(encoding="utf-8")
            m = json.loads((run_dir / vid / "run0.meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            outs[vid] = (None, {"status": "not_run"})
            continue
        outs[vid] = (raw, {"status": _classify(m.get("error"), raw), "elapsed_s": m.get("elapsed_s"),
                           "attempts": 1, "waited_s": 0, "error": m.get("error") or ""})
    return [{"model": summ["model"], "lane": "-", "snapshot": None, "outputs": outs}]


def load_check(path: Path = CHECK_PATH) -> dict:
    if not path.exists():
        return {}
    return {v["aweme_id"]: v for v in json.loads(path.read_text(encoding="utf-8"))["videos"]}


def score_check(truth: dict, records: list[dict]) -> dict:
    """检查集：编号题同 gold 口径；batch 题按 (canonical, 等级, score) 计条数，取 min 记对、多出记多报。"""
    num = eval_gold.score_video(truth.get("truth") or {}, records)
    got = Counter((r.get("model_canonical"), r.get("bug_level"), r.get("score"))
                  for r in records if r.get("batch_slot"))
    exp = Counter()
    for model, levels in (truth.get("batch") or {}).items():
        for level, by_score in levels.items():
            for score, n in by_score.items():
                exp[(model, level, int(score))] += int(n)
    matched = sum(min(n, got[k]) for k, n in exp.items())
    diffs = [d for d in num["diffs"] if d["reason"] != "batch 多报"]   # batch 在下面按条数另算
    for k in set(exp) | set(got):
        if exp[k] != got[k]:
            diffs.append({"model": k[0], "bug": f"{k[1]}#batch({k[2]}分)", "truth": exp[k], "got": got[k],
                          "reason": "batch 条数不符"})
    return {"correct": num["correct"] + matched, "total": num["total"] + sum(exp.values()),
            "named_over": num["named_over"], "batch_over": sum(got.values()) - matched,
            "batch_missing": sum(exp.values()) - matched, "diffs": diffs}


def _summarize_records(ex: dict) -> dict:
    recs = ex["records"]
    batch = Counter(f"{r.get('model_canonical')} {r.get('bug_level')}({r.get('score')}分)"
                    for r in recs if r.get("batch_slot"))
    return {"records": len(recs), "canonical": dict(Counter(r.get("model_canonical") for r in recs)),
            "batch": dict(batch), "unknown": sum(1 for r in recs if r.get("model_canonical") == "UNKNOWN"),
            "dropped": dict(Counter(re.sub(r":.*", "", d.get("reason", "")) for d in ex.get("dropped") or [])),
            "dropped_count": ex.get("dropped_count", 0)}


def score_model(entry: dict, snap: Path) -> dict:
    gold = {v["aweme_id"]: v for v in eval_gold.load_gold()}
    check = load_check()
    old_dir = config.DATA_DIR
    config.DATA_DIR = snap                   # 已学变体从快照库读
    try:
        per = {}
        for vid, (raw, meta) in entry["outputs"].items():
            row = {"status": meta.get("status"), "elapsed_s": meta.get("elapsed_s"),
                   "attempts": meta.get("attempts", 1), "waited_s": meta.get("waited_s", 0)}
            if vid in gold:
                row["total"] = sum(len(b) for b in gold[vid]["truth"].values())
            if row["status"] == "ok":
                transcript, title, known = _video_input(snap, vid)
                try:
                    ex = extract.build_extract(aweme_id=vid, title=title, transcript=transcript,
                                               claude_text=raw, known=known)
                except extract.ExtractError as e:
                    row["status"], row["error"] = "build_error", str(e)[:200]
                else:
                    row["summary"] = _summarize_records(ex)
                    if vid in gold:
                        sv = eval_gold.score_video(gold[vid]["truth"], ex["records"])
                        row.update(correct=sv["correct"], named_over=sv["named_over"],
                                   batch_over=sv["batch_over"], missing=sv["missing"],
                                   diffs=sv["diffs"])
                    if vid in check:
                        row["check"] = score_check(check[vid], ex["records"])
            per[vid] = row
    finally:
        config.DATA_DIR = old_dir
    g = [r for v, r in per.items() if v in gold]
    times = [r["elapsed_s"] for r in per.values() if r.get("elapsed_s") is not None and r["status"] != "not_run"]
    fails = Counter(r["status"] for r in per.values() if r["status"] != "ok")
    chk = [r["check"] for r in per.values() if "check" in r]
    return {"model": entry["model"], "lane": entry["lane"], "videos": per,
            "gold_correct": sum(r.get("correct", 0) for r in g), "gold_total": sum(r.get("total", 0) for r in g),
            "missing": sum(r.get("missing", 0) for r in g), "named_over": sum(r.get("named_over", 0) for r in g),
            "batch_over": sum(r.get("batch_over", 0) for r in g), "failures": dict(fails),
            "retries": sum(r["attempts"] - 1 for r in per.values() if r.get("attempts")),
            "time_total": round(sum(times), 1), "time_median": round(statistics.median(times), 1) if times else None,
            "time_max": round(max(times), 1) if times else None,
            "check_correct": sum(c["correct"] for c in chk) if chk else None,
            "check_total": sum(c["total"] for c in chk) if chk else None}


_STATUS_CN = {"empty": "空", "timeout": "超时", "error": "错", "throttled": "限流", "build_error": "解析错",
              "not_run": "未跑"}


def render(results: list[dict]) -> str:
    gold_ids = _gold_ids()
    L = [f"# 提取模型评测对比（代码 {_git_head()}，{time.strftime('%Y-%m-%d %H:%M')}）", "",
         "| 模型 | 出口 | gold | 缺/错 | 多报 | batch多报 | 检查集 | 失败 | 限流重试 | 调用耗时合计 | 中位/最大 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        fails = "、".join(f"{_STATUS_CN.get(k, k)}{n}" for k, n in r["failures"].items()) or "0"
        chk = f"{r['check_correct']}/{r['check_total']}" if r["check_total"] else "未标注"
        L.append(f"| {r['model']} | {r['lane']} | **{r['gold_correct']}/{r['gold_total']}** | {r['missing']} | "
                 f"{r['named_over']} | {r['batch_over']} | {chk} | {fails} | {r['retries']} | {r['time_total']}s | "
                 f"{r['time_median']}/{r['time_max']}s |")
    L += ["", "## 逐视频（正确/总数 · 耗时）", "",
          "| 视频 | " + " | ".join(r["model"] for r in results) + " |",
          "|---|" + "---|" * len(results)]
    vids = list(dict.fromkeys(v for r in results for v in r["videos"]))
    for vid in vids:
        cells = []
        for r in results:
            row = r["videos"].get(vid)
            if row is None:
                cells.append("")
            elif row["status"] != "ok":
                cells.append(_STATUS_CN.get(row["status"], row["status"]))
            else:
                score = (f"{row['correct']}/{row['total']}" if "correct" in row else
                         f"检 {row['check']['correct']}/{row['check']['total']}" if "check" in row else
                         f"{row['summary']['records']}条")
                cells.append(f"{score} · {row['elapsed_s']}s")
        L.append(f"| {vid}{'' if vid in gold_ids else '（检查）'} | " + " | ".join(cells) + " |")
    L += ["", "## 检查视频明细", ""]
    for vid in [v for v in vids if v not in gold_ids]:
        L.append(f"### {vid}")
        for r in results:
            row = r["videos"].get(vid)
            if not row or "summary" not in row:
                L.append(f"- **{r['model']}**：{_STATUS_CN.get(row['status'], row['status']) if row else '-'}")
                continue
            s = row["summary"]
            can = "、".join(f"{k}×{n}" for k, n in s["canonical"].items())
            bat = "、".join(f"{k}×{n}" for k, n in s["batch"].items()) or "无"
            drop = "、".join(f"{k}×{n}" for k, n in s["dropped"].items()) or "无"
            chk = (f"；检查集 {row['check']['correct']}/{row['check']['total']}"
                   f"（batch 缺 {row['check']['batch_missing']} 多 {row['check']['batch_over']}）"
                   if "check" in row else "")
            L.append(f"- **{r['model']}**：{s['records']} 条；{can}；batch：{bat}；UNKNOWN {s['unknown']}；"
                     f"丢弃 {s['dropped_count']}（{drop}）{chk}")
        L.append("")
    return "\n".join(L)


def render_diffs(r: dict) -> str:
    L = [f"# {r['model']} 逐格 diff", ""]
    for vid, row in r["videos"].items():
        for d in (row.get("diffs") or []) + (row.get("check") or {}).get("diffs", []):
            L.append(f"- [{vid}] {d['model']} · {d['bug']}: 真值={d['truth']} 实得={d['got']}（{d['reason']}）")
    return "\n".join(L) + "\n"


def report(run_dirs: list[Path], snap: Path | None, out: Path | None) -> list[dict]:
    results = []
    for rd in run_dirs:
        for entry in load_run(rd):
            s = snap or (Path(entry["snapshot"]) if entry["snapshot"] else None)
            if s is None:
                raise SystemExit(f"{rd} 是旧格式输出，须给 --snapshot")
            results.append(score_model(entry, s))
    text = render(results)
    print(text)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        (out.parent / "scores.json").write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                                encoding="utf-8")
        for r in results:
            (out.parent / f"diff-{_slug(r['model'])}.md").write_text(render_diffs(r), encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("snapshot")
    p.add_argument("--out")
    p.add_argument("--videos", default="gold+check")
    p = sub.add_parser("run")
    p.add_argument("--models", required=True, help="逗号分隔（模型 id 可含空格）")
    p.add_argument("--snapshot")
    p.add_argument("--videos", default="gold+check")
    p.add_argument("--out", help="运行目录；指向已有目录=续跑")
    p = sub.add_parser("report", aliases=["rescore"])
    p.add_argument("runs", nargs="+")
    p.add_argument("--snapshot")
    p.add_argument("--out", help="报告文件（同目录另写 scores.json 与 diff-*.md）")
    a = ap.parse_args(argv)
    if a.cmd == "snapshot":
        snapshot(Path(a.out) if a.out else EVAL_ROOT / "snapshots" / _ts(), _video_ids(a.videos))
        return 0
    if a.cmd == "run":
        models = [m.strip() for m in a.models.split(",") if m.strip()]
        rd = run(models, Path(a.out) if a.out else EVAL_ROOT / "runs" / _ts(),
                 Path(a.snapshot) if a.snapshot else None, _video_ids(a.videos))
        report([rd], None, rd / "report.md")
        return 0
    report([Path(x) for x in a.runs], Path(a.snapshot) if a.snapshot else None,
           Path(a.out) if a.out else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
