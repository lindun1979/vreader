#!/usr/bin/env python3
"""rid 迁移审计（只读，C9/V-数据）——**完全自包含**，可直接在生产旧代码环境跑。

身份键改造（2c）后 record_id 算法变化：旧决策的 rid 与新算法不同，部署新代码前
必须迁移，否则旧 approved 失效。本脚本只读：扫 record_decisions + 各 extract.json，
内联新旧两套算法各算一次 rid，输出「按目标 new_rid 分组的迁移单」JSON 供人工审阅。
不改任何数据，不 import 项目代码（新旧算法都内联，结果与部署代码无关）。

用法：
    VREADER_DATA_DIR=/abs/path python3 ops/audit_rid_migration.py [> plan.json]
    # 或： python3 ops/audit_rid_migration.py --data-dir /abs/path
默认 data 目录 = 仓库同级 ./data。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

APPROVED = "approved"
REJECTED_CONFLICT = "rejected_conflict"


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _old_rid(aweme_id: str, r: dict) -> str:
    """基线 46d9239 旧算法（bug_id 原样、无 model_key/bug_slot 归一）。"""
    key = (f"{aweme_id}|{r.get('model_canonical')}|{r.get('bug_level')}|{r.get('bug_id','')}"
           f"|{r.get('score')}")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _new_rid(aweme_id: str, r: dict) -> str:
    """2c 新算法（须与 core/extract.py _record_id 一致）。"""
    mc = r.get("model_canonical", "")
    model_key = ("UNKNOWN:" + _norm(r.get("model_raw", ""))) if mc == "UNKNOWN" else mc
    bn = (r.get("bug_id", "") or "").strip().upper()
    bug_slot = bn if bn else "e:" + hashlib.sha256(_norm(r.get("evidence_quote", "")).encode()).hexdigest()[:8]
    key = f"{aweme_id}|{model_key}|{r.get('bug_level')}|{bug_slot}|{r.get('score')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def build_plan(data_dir: Path) -> dict:
    dbp = data_dir / "vreader.db"
    known: dict[str, str] = {}
    if dbp.exists():
        conn = sqlite3.connect(str(dbp))
        conn.row_factory = sqlite3.Row
        try:
            known = {r["record_id"]: r["decision"]
                     for r in conn.execute("SELECT record_id, decision FROM record_decisions")}
        finally:
            conn.close()

    groups: dict[str, list] = defaultdict(list)
    for ep in sorted((data_dir / "token_bug").glob("*/extract.json")):
        try:
            ex = json.loads(ep.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        aid = ex.get("video_id", "")
        for r in ex.get("records", []):
            old = _old_rid(aid, r)
            new = _new_rid(aid, r)
            if old in known and old != new:
                groups[new].append({"old_rid": old, "decision": known[old], "aweme_id": aid})

    plan: dict = {"target_groups": []}
    for new_rid, srcs in groups.items():
        source_rids = [s["old_rid"] for s in srcs]
        if new_rid in known:
            source_rids.append(new_rid)
        manual = (any(s["decision"] == APPROVED for s in srcs)
                  and any(s["decision"] == REJECTED_CONFLICT for s in srcs))
        plan["target_groups"].append({
            "target_rid": new_rid, "aweme_id": srcs[0]["aweme_id"],
            "sources": srcs, "source_rids": source_rids, "needs_manual": manual,
            "expected_before": {s["old_rid"]: s["decision"] for s in srcs},
        })
    plan["n_targets"] = len(plan["target_groups"])
    plan["n_manual"] = sum(1 for g in plan["target_groups"] if g["needs_manual"])
    plan["n_affected_decisions"] = sum(len(g["sources"]) for g in plan["target_groups"])
    return plan


def _resolve_data_dir(argv: list[str]) -> Path:
    if "--data-dir" in argv:
        return Path(argv[argv.index("--data-dir") + 1])
    env = os.environ.get("VREADER_DATA_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "data"


if __name__ == "__main__":
    dd = _resolve_data_dir(sys.argv[1:])
    print(f"# data_dir = {dd}", file=sys.stderr)
    print(json.dumps(build_plan(dd), ensure_ascii=False, indent=2))
