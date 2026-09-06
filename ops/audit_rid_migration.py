#!/usr/bin/env python3
"""rid 迁移审计（只读，C9/V-数据）。

身份键改造（2c）后 record_id 算法变化：旧决策的 rid 与新算法不同，部署新代码前
必须迁移，否则旧 approved 失效。本脚本**只读**：扫 record_decisions + 各 extract.json，
用旧/新算法各算一次 rid，输出「按目标 new_rid 分组的迁移单」JSON 供人工审阅确认。
不改任何数据。确认后由迁移执行步骤（core.db.migrate_rid_group）落库。

用法：/usr/local/bin/python3.11 -m ops.audit_rid_migration [> migration_plan.json]
部署 gate：存在未决人工组（rejected_manual 风险）时不得上线新 rid 规则。
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import config, db, extract as ex_mod  # noqa: E402


def _old_record_id(aweme_id: str, r: dict) -> str:
    """基线 46d9239 的旧算法（含 bug_id 原样、无 model_key/bug_slot 归一）。"""
    key = (f"{aweme_id}|{r.get('model_canonical')}|{r.get('bug_level')}|{r.get('bug_id','')}"
           f"|{r.get('score')}")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def build_plan() -> dict:
    dbp = str(config.DATA_DIR / "vreader.db")
    conn = db.connect(dbp)
    try:
        known = {r["record_id"]: r["decision"]
                 for r in conn.execute("SELECT record_id, decision FROM record_decisions")}
    finally:
        conn.close()

    groups: dict[str, list] = defaultdict(list)
    ch_dir = config.channel_dir("token_bug")
    for ep in sorted(Path(ch_dir).glob("*/extract.json")):
        try:
            ex = json.loads(ep.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        aid = ex.get("video_id", "")
        for r in ex.get("records", []):
            old = _old_record_id(aid, r)
            new = ex_mod.record_id(aid, r)
            if old in known and old != new:
                groups[new].append({"old_rid": old, "decision": known[old], "aweme_id": aid})

    plan = {"target_groups": [], "n_targets": 0, "n_sources": 0}
    for new_rid, srcs in groups.items():
        # 目标已存在决策也纳入源集合（多对一）
        source_rids = [s["old_rid"] for s in srcs]
        if new_rid in known:
            source_rids.append(new_rid)
        manual = any(s["decision"] == db.APPROVED for s in srcs) and \
            any(s["decision"] == db.REJECTED_CONFLICT for s in srcs)
        plan["target_groups"].append({
            "target_rid": new_rid, "aweme_id": srcs[0]["aweme_id"],
            "sources": srcs, "needs_manual": manual,
            "expected_before": {s["old_rid"]: s["decision"] for s in srcs},
        })
        plan["n_sources"] += len(source_rids)
    plan["n_targets"] = len(plan["target_groups"])
    plan["n_manual"] = sum(1 for g in plan["target_groups"] if g["needs_manual"])
    return plan


if __name__ == "__main__":
    print(json.dumps(build_plan(), ensure_ascii=False, indent=2))
