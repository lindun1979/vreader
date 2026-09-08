#!/usr/bin/env python3
"""存量提取覆盖审计（只读，plan v4 Step 5）：对历史 extract.json 跑与生产同款的确定性
锚点覆盖检查，列出「转写/标题提及了某系列版本，但提取记录里整段缺失」的疑似丢弃视频。

**只读**：不连库、不调 LLM、不写任何文件、不取 flock。输出到 stdout，人工据此决定
逐个 `python -m core.cli --reprocess <aweme_id>`（符合「榜单勿 LLM 全量重跑」约定）。

覆盖判定：
  - v2 记录（有 model_series）：直接用 model_series。
  - 遗留记录（无 model_series）：_reverse_parse_all(canonical) 反解——**唯一反解才计覆盖**；
    0 或多个结果 → 归「无法判定」单列（既不消除缺失信号漏报，也不任取一个误报）。
  - UNKNOWN 记录：不覆盖任何系列（跳过）。
对已经历过覆盖复跑的产物（envelope 带 coverage_anchor_series），并列展示「当时触发依据」
与「按当前 models.yml 重算」的差异（models.yml 漂移时二者可能不同）。

用法：
  python ops/audit_extract_coverage.py [data_dir]
  # data_dir 缺省用 config.DATA_DIR；生产机直接对生产 data 跑（只读安全）。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from core import config, models as m  # noqa: E402

CHANNEL = "token_bug"


def _covered_series(records: list[dict], data: dict) -> tuple[set[str], list[tuple[str, str]]]:
    """返回 (covered_series, undecidable)。undecidable = [(canonical, 原因)]（遗留记录反解 0/多）。"""
    covered: set[str] = set()
    undecidable: list[tuple[str, str]] = []
    for r in records:
        if r.get("model_canonical") == "UNKNOWN":
            continue
        s = r.get("model_series")
        if s:                                   # v2 记录
            covered.add(s)
            continue
        hits = m._reverse_parse_all(r.get("model_canonical", ""), data)  # 遗留反解
        if len(hits) == 1:
            covered.add(hits[0][0])
        else:
            undecidable.append((r.get("model_canonical", ""),
                                f"反解 {len(hits)} 个系列" if hits else "反解 0 个系列"))
    return covered, undecidable


def _evidence_snippet(series: str, transcript: str, title: str, data: dict) -> str:
    """在归一源文本里找该系列 alias 的一处出现，截前后 ±40 字符（供人工判断误触发）。"""
    src = m._norm_source(transcript + "\n" + title)
    aliases = m._series_alias_tokens(data.get(series, {}))
    for a in sorted(aliases, key=len, reverse=True):
        idx = src.find(a)
        if idx >= 0:
            lo, hi = max(0, idx - 40), min(len(src), idx + len(a) + 40)
            return "…" + src[lo:hi].replace("\n", " ") + "…"
    return "（未定位到 alias，可能仅命中版本号邻接）"


def audit(data_dir: Path) -> int:
    data = m.load_series()
    ch_dir = data_dir / CHANNEL
    if not ch_dir.exists():
        print(f"[audit] 频道目录不存在: {ch_dir}")
        return 0
    suspects = 0
    undecided_videos = 0
    total = 0
    for vdir in sorted(p for p in ch_dir.iterdir() if p.is_dir()):
        tpath, epath = vdir / "transcript.txt", vdir / "extract.json"
        if not tpath.exists() or not epath.exists():
            continue
        try:
            transcript = tpath.read_text(encoding="utf-8")
            ex = json.loads(epath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"[audit] 跳过 {vdir.name}: 读取失败 {e}")
            continue
        total += 1
        title = ex.get("title", "")
        anchors = m.build_anchors(transcript, title, data=data)
        anchor_series = set(anchors["versions"])
        covered, undecidable = _covered_series(ex.get("records", []), data)
        missing = anchor_series - covered

        if not missing and not undecidable:
            continue
        print(f"\n═══ {vdir.name} — {title[:40]}")
        if missing:
            suspects += 1
            for s in sorted(missing):
                print(f"  ⚠️ 缺失系列 {s}: {_evidence_snippet(s, transcript, title, data)}")
        if undecidable:
            undecided_videos += 1
            for canon, why in undecidable:
                print(f"  ❓ 无法判定 {canon}（{why}）")
        # 溯源对照（若产物记过触发依据）
        if ex.get("coverage_anchor_series") is not None:
            then = sorted(ex.get("coverage_anchor_series") or [])
            print(f"  ↺ 当时锚点系列: {then}  →  当前重算: {sorted(anchor_series)}")
            if ex.get("coverage_trigger_missing"):
                print(f"    当时触发缺失: {sorted(ex['coverage_trigger_missing'])}"
                      f"  状态: {ex.get('coverage_rerun_status')}")

    print(f"\n──────── 汇总：疑似丢弃 {suspects} / 无法判定 {undecided_videos} / 总视频 {total}")
    if suspects:
        print("  处置：核对后逐个  python -m core.cli --reprocess <aweme_id>")
    return suspects


if __name__ == "__main__":
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else config.DATA_DIR
    audit(d)
