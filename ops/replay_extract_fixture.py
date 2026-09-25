#!/usr/bin/env python3
"""提取回放（不调 LLM）：把 fixture 里的 LLM 原始记录走 build_extract → 临时 DB 上
apply_decisions → 回执告警，逐项对比 expect 文件，失败非零退出。

fixture：{aweme_id, title, transcript, raw_llm_records, known_at_extract, known_at_decision}
  known_at_extract 传 build_extract(known=)，known_at_decision 传 apply_decisions(known=)（不得为 None）。
expect：{records_count, dropped_reasons: {原因: 条数},
         records: [{model, bug_id, solved_round, decision, confidence_max?}], warning_substrings: [...]}

用法：
  python ops/replay_extract_fixture.py --fixture <path> --sha256 <hex> \\
      --expect-file <path> --expect-sha256 <hex>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core import db, extract, models, pipeline  # noqa: E402


def _known(fx: dict, key: str) -> set[tuple[str, str, str]]:
    if fx.get(key) is None:
        raise ValueError(f"fixture 缺 {key}（不得为 None：会跳过新版本判定）")
    return {tuple(t) for t in fx[key]}


def replay(fx: dict) -> dict:
    """回放一次，返回 {extract, decisions: {rid: decision}, warnings}。"""
    aid = fx["aweme_id"]
    ex = extract.build_extract(
        aweme_id=aid, title=fx["title"], transcript=fx["transcript"],
        claude_text=json.dumps({"records": fx["raw_llm_records"]}, ensure_ascii=False),
        known=_known(fx, "known_at_extract"))
    with tempfile.TemporaryDirectory() as td:
        dbp = Path(td) / "vreader.db"
        db.init(dbp)
        conn = db.connect(dbp)
        try:
            extract.apply_decisions(conn, aid, ex, known=_known(fx, "known_at_decision"))
            decisions = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(conn, aid)}
        finally:
            conn.close()
    a = models.build_anchors("", fx["title"])
    warnings = pipeline._receipt_warnings(ex, set(a["versions"]) | a["series"])
    return {"extract": ex, "decisions": decisions, "warnings": warnings}


def check(result: dict, expect: dict, aid: str) -> list[str]:
    """对比 expect，返回失败项（空 = 通过）。"""
    ex, fails = result["extract"], []
    recs = ex["records"]
    if len(recs) != expect["records_count"]:
        fails.append(f"records 条数 {len(recs)} ≠ {expect['records_count']}")
    reasons: dict[str, int] = {}
    for d in ex["dropped"]:
        reasons[d["reason"]] = reasons.get(d["reason"], 0) + 1
    if reasons != expect["dropped_reasons"]:
        fails.append(f"dropped {reasons} ≠ {expect['dropped_reasons']}")
    for e in expect["records"]:
        hits = [r for r in recs if r["model_canonical"] == e["model"]
                and r["bug_id"] == e["bug_id"] and r["solved_round"] == e["solved_round"]]
        if len(hits) != 1:
            fails.append(f"记录 {e['model']} {e['bug_id']} r{e['solved_round']} 命中 {len(hits)} 条")
            continue
        got = result["decisions"].get(extract.record_id(aid, hits[0]))
        if got != e["decision"]:
            fails.append(f"记录 {e['model']} {e['bug_id']} r{e['solved_round']} 决策 {got} ≠ {e['decision']}")
        if "confidence_max" in e and hits[0]["confidence"] > e["confidence_max"]:
            fails.append(f"记录 {e['model']} {e['bug_id']} r{e['solved_round']} "
                         f"confidence {hits[0]['confidence']} > {e['confidence_max']}")
    for s in expect["warning_substrings"]:
        if not any(s in w for w in result["warnings"]):
            fails.append(f"告警缺「{s}」")
    return fails


def _load_checked(path: str, sha: str) -> dict:
    b = Path(path).read_bytes()
    actual = hashlib.sha256(b).hexdigest()
    if actual != sha:
        raise SystemExit(f"❌ sha256 不符: {path} 实际 {actual} ≠ 预期 {sha}")
    return json.loads(b)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="提取回放（不调 LLM）")
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--sha256", required=True)
    ap.add_argument("--expect-file", required=True)
    ap.add_argument("--expect-sha256", required=True)
    args = ap.parse_args(argv)
    fx = _load_checked(args.fixture, args.sha256)
    expect = _load_checked(args.expect_file, args.expect_sha256)
    aid = fx["aweme_id"]
    res = replay(fx)
    ex = res["extract"]
    print(f"输入 LLM 记录: {len(fx['raw_llm_records'])} 条")
    print(f"产出 records: {len(ex['records'])} 条；dropped: {ex['dropped_count']} 条")
    for r in ex["records"]:
        rid = extract.record_id(aid, r)
        print(f"  {rid} {r['model_canonical']} {r['bug_level']} {r['bug_id'] or '∅'} "
              f"r{r['solved_round']} conf={r['confidence']} → {res['decisions'].get(rid)}")
    for d in ex["dropped"]:
        print(f"  [丢弃] {d['record'].get('model_raw')} {d['record'].get('bug_id')} "
              f"r{d['record'].get('solved_round')}: {d['reason']}")
    print("冲突 rid:", sorted(k for k, v in res["decisions"].items() if v == db.PENDING_CONFLICT))
    print("回执告警:")
    for w in res["warnings"]:
        print("  ⚠️ " + w)
    fails = check(res, expect, aid)
    if fails:
        print("\n❌ 回放不符：")
        for f in fails:
            print("  - " + f)
        return 1
    print("\n✅ 回放与 expect 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
