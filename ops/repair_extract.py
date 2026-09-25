#!/usr/bin/env python3
"""受控外科式修复单个视频的 extract（不重跑 LLM）：按私有 spec 编辑/删除/新增记录，
再走与正常管线相同的决策与渲染。

spec（JSON，gitignore，含证据原文）：
  {"aweme_id": ..., "edit": [{"match": {字段: 值}, "set": {...}}],
   "delete": [{"match": {...}}], "add": [完整 LLM 字段记录], "expect_total": {canonical: 总分}}
  每个 match 必须恰好命中 1 条（按 edit → delete 顺序在当前记录上匹配）。
  add 记录经 resolve_record（提及锚定 + extract 的 known_versions_used）核验三元组、
  derive_from_round 派生 solved/score/rounds。

用法：
  python ops/repair_extract.py --data <data目录> --spec <spec> --expect-spec-sha256 <sha> [--apply]
  - 默认 dry-run：只在内存 + 临时 DB 副本上计算，打印预期逐 rid 决策，不写原 data/DB。
  - --apply：取数据目录 flock（serve 未停则失败）→ 重做全部检查 → 备份到
    data/backup/repair-<id>-<ts>/ → 写 extract + 决策 + 渲染 → 实际决策须与 dry-run 预期一致，
    否则恢复备份并非零退出。
回滚：停 serve → 用 data/backup/repair-<id>-<ts>/ 的 extract.json 与 vreader.db 覆盖 → load。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core import config, db, extract, lock, models, pipeline, util  # noqa: E402


class RepairError(Exception):
    pass


def _match_one(records: list[dict], match: dict, what: str) -> int:
    hits = [i for i, r in enumerate(records) if all(r.get(k) == v for k, v in match.items())]
    if len(hits) != 1:
        raise RepairError(f"{what} match 命中 {len(hits)} 条（须恰好 1 条）: {match}")
    return hits[0]


def _derive(r: dict) -> None:
    solved, score, rounds = extract.derive_from_round(r.get("bug_level"), r.get("solved_round"))
    if solved is None:
        raise RepairError(f"solved_round 越界: {r.get('bug_level')} {r.get('solved_round')}")
    r["solved"], r["score"], r["rounds"] = solved, score, rounds


def build_target(ex: dict, transcript: str, spec: dict) -> tuple[dict, list[str]]:
    """按 spec 生成目标 extract（纯计算）；返回 (target, 操作日志行)。"""
    aid = spec["aweme_id"]
    records = copy.deepcopy(ex["records"])
    log: list[str] = []
    for e in spec.get("edit") or []:
        i = _match_one(records, e["match"], "edit")
        log.append(f"edit  #{i} {_brief(records[i])} set {e['set']}")
        records[i].update(e["set"])
        _derive(records[i])
    for d in spec.get("delete") or []:
        i = _match_one(records, d["match"], "delete")
        log.append(f"delete #{i} {_brief(records[i])}")
        records.pop(i)
    data = models.load_series()
    anchors = models.build_anchors(transcript, ex.get("title") or "", data=data)
    known = {tuple(t) for t in ex.get("known_versions_used") or []}
    for a in spec.get("add") or []:
        canonical, s, v, var = models.resolve_record(
            a.get("model_raw", ""), a.get("model_series", ""), a.get("model_version", ""),
            a.get("model_variant", ""), anchors, data=data, known=known)
        want = (a.get("model_series", ""), a.get("model_version", ""), a.get("model_variant", ""))
        if (s, v, var) != want:
            raise RepairError(f"add 三元组核验失败: spec={want} 锚定解析={(s, v, var)}")
        r = {"model_canonical": canonical, "model_raw": a["model_raw"], "model_series": s,
             "model_version": v, "model_variant": var, "bug_level": a["bug_level"],
             "bug_id": a["bug_id"], "solved_round": a["solved_round"],
             "evidence_quote": a["evidence_quote"], "confidence": a["confidence"]}
        _derive(r)
        records.append(r)
        log.append(f"add   {_brief(r)}")
    target = copy.deepcopy(ex)
    target["records"] = records
    target["result_rev"] = extract.result_rev(aid, records)
    target.pop("no_content", None)
    extract.validate_extract(target, transcript=transcript, expected_video_id=aid)
    totals = _totals(records)
    if totals != spec.get("expect_total"):
        raise RepairError(f"总分与 spec.expect_total 不一致: 计算={totals} 预期={spec.get('expect_total')}")
    return target, log


def _totals(records: list[dict]) -> dict:
    out: dict[str, int] = {}
    for r in records:
        out[r["model_canonical"]] = out.get(r["model_canonical"], 0) + r["score"]
    return out


def _brief(r: dict) -> str:
    return (f"{r.get('model_canonical')}({r.get('model_raw')}) {r.get('bug_level')} "
            f"{r.get('bug_id') or '∅'} r{r.get('solved_round')} score={r.get('score')} "
            f"conf={r.get('confidence')}")


def _decisions(conn, aid: str) -> dict[str, str]:
    return {row["record_id"]: row["decision"] for row in db.list_decisions_for_video(conn, aid)}


def _verify(expected: dict, actual: dict) -> bool:
    return expected == actual


def _backup_db(src_path: Path, dst_path: Path, *, readonly: bool) -> None:
    src = db.connect_ro(src_path) if readonly else sqlite3.connect(str(src_path))
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _predict(db_path: Path, aid: str, target: dict) -> dict[str, str]:
    """在 DB 临时副本上跑真正的 apply_decisions，读回逐 rid 决策作为预期。"""
    with tempfile.TemporaryDirectory() as td:
        cp = Path(td) / "vreader.db"
        _backup_db(db_path, cp, readonly=True)
        c = db.connect(cp)
        try:
            known_b = db.list_known_versions(c)
            extract.apply_decisions(c, aid, copy.deepcopy(target), known=known_b)
            return _decisions(c, aid)
        finally:
            c.close()


def _report(aid, spec_sha, log, ex, target, expected, before) -> None:
    print(f"video_id: {aid}\nspec sha256: {spec_sha}")
    print("操作：")
    for line in log:
        print("  " + line)
    old = {extract.record_id(aid, r) for r in ex["records"]}
    new = {extract.record_id(aid, r) for r in target["records"]}
    print(f"目标 records（{len(target['records'])} 条，result_rev {target['result_rev']}）：")
    for r in target["records"]:
        rid = extract.record_id(aid, r)
        print(f"  {rid} {_brief(r)} → {expected.get(rid)}")
    print(f"rid 移除: {sorted(old - new)}\nrid 新增: {sorted(new - old)}")
    print("预期逐 rid 决策（含 stale）：")
    for rid in sorted(expected):
        print(f"  {rid}: {before.get(rid, '-')} → {expected[rid]}")
    print(f"分数汇总: {_totals(target['records'])}")


def _restore(bdir: Path, db_path: Path, ex_path: Path) -> None:
    """恢复：调用方已关闭全部写连接 → backup API 写回 DB → 恢复 extract → 新连接渲染。"""
    _backup_db(bdir / "vreader.db", db_path, readonly=False)
    shutil.copy2(bdir / "extract.json", ex_path)
    c = db.connect(db_path)
    try:
        pipeline.render_board(c)
    finally:
        c.close()


def run(args) -> int:
    data = Path(args.data).resolve()
    spec_bytes = Path(args.spec).read_bytes()
    spec_sha = hashlib.sha256(spec_bytes).hexdigest()
    if spec_sha != args.expect_spec_sha256:
        print(f"❌ spec sha256 不符: 实际 {spec_sha} ≠ 预期 {args.expect_spec_sha256}", file=sys.stderr)
        return 2
    spec = json.loads(spec_bytes)
    aid = spec["aweme_id"]
    config.DATA_DIR = data
    vdir = config.video_dir(pipeline.CHANNEL, aid)
    ex_path, tx_path, db_path = vdir / "extract.json", vdir / "transcript.txt", data / "vreader.db"

    dirlock = lock.DataDirLock(data) if args.apply else None
    if dirlock is not None and not dirlock.acquire(blocking=False):
        print("❌ 数据目录被 serve 或另一实例占用（先 launchctl unload）", file=sys.stderr)
        return 3
    try:
        ex_bytes = ex_path.read_bytes()
        ex = json.loads(ex_bytes)
        transcript = tx_path.read_text(encoding="utf-8")
        try:
            target, log = build_target(ex, transcript, spec)
        except (RepairError, extract.ExtractError) as e:
            print(f"❌ {e}", file=sys.stderr)
            return 4
        ro = db.connect_ro(db_path)
        try:
            before = _decisions(ro, aid)
        finally:
            ro.close()
        expected = _predict(db_path, aid, target)
        _report(aid, spec_sha, log, ex, target, expected, before)
        if not args.apply:
            print("\n（dry-run：未写入任何文件/DB）")
            return 0

        bdir = data / "backup" / f"repair-{aid}-{time.strftime('%Y%m%d-%H%M%S')}"
        bdir.mkdir(parents=True)
        (bdir / "extract.json").write_bytes(ex_bytes)
        _backup_db(db_path, bdir / "vreader.db", readonly=False)
        print(f"\n备份: {bdir}")
        c = db.connect(db_path)
        try:
            with lock.publish_lock:
                util.atomic_write_text(ex_path, json.dumps(target, ensure_ascii=False, indent=2))
                known_b = db.list_known_versions(c)
                extract.apply_decisions(c, aid, target, known=known_b)
                pipeline._write_decision_snapshot(target, {"extract": str(ex_path)}, known_b)
                pipeline.render_board(c)
            actual = _decisions(c, aid)
        finally:
            c.close()
        if not _verify(expected, actual):
            print(f"❌ 实际决策与预期不一致，恢复备份\n预期 {expected}\n实际 {actual}", file=sys.stderr)
            _restore(bdir, db_path, ex_path)
            return 5
        print("✅ apply 完成，实际决策与预期一致")
        return 0
    finally:
        if dirlock is not None:
            dirlock.release()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="受控外科式修复单视频 extract")
    ap.add_argument("--data", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--expect-spec-sha256", required=True)
    ap.add_argument("--apply", action="store_true")
    return run(ap.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
