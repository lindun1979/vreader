"""token_bug 频道榜单渲染：extract.json[] + 可见决策 → board.md（模型 × 难度）。

确定性：每次从全部 extract.json 中 decision∈{auto_ok,approved} 的记录全量重建。
"""
from __future__ import annotations

import json
from pathlib import Path

LEVELS = ["青铜", "白银", "黄金", "钻石", "王者"]


def _cell(attempts: list[dict]) -> str:
    if not attempts:
        return "—"
    n = len(attempts)
    solved = [a for a in attempts if a["solved"]]
    if solved:
        avg_r = sum(a["rounds"] for a in solved) / len(solved)
        return f"{len(solved)}/{n}胜·均{avg_r:.1f}轮"
    return f"0/{n}胜"


def render(extracts: list[dict], visible_ids, record_id_fn) -> str:
    """extracts: extract dict 列表；visible_ids: 允许上榜的 record_id 集合；
    record_id_fn(aweme_id, record)->id。返回 board.md 文本。"""
    # 汇总 (model, level) -> attempts
    grid: dict[tuple[str, str], list[dict]] = {}
    models: list[str] = []
    n_videos = 0
    for ex in extracts:
        n_videos += 1
        aid = ex["video_id"]
        for r in ex["records"]:
            rid = record_id_fn(aid, r)
            if rid not in visible_ids:
                continue
            mc = r["model_canonical"]
            if mc == "UNKNOWN":
                continue
            if mc not in models:
                models.append(mc)
            grid.setdefault((mc, r["bug_level"]), []).append(r)

    models.sort()
    lines = ["# token（词源）模型 × 难度 榜单", ""]
    lines.append(f"_覆盖 {n_videos} 条视频；单元格 = 解出场次/总场次·平均轮次。空=未测。_")
    lines.append("")
    lines.append("| 模型 | " + " | ".join(LEVELS) + " |")
    lines.append("|---|" + "---|" * len(LEVELS))
    for m in models:
        cells = [_cell(grid.get((m, lv), [])) for lv in LEVELS]
        lines.append(f"| {m} | " + " | ".join(cells) + " |")
    if not models:
        lines.append("| _（暂无上榜记录）_ |" + " |" * len(LEVELS))
    lines.append("")
    return "\n".join(lines)


def load_extracts(channel_dir: str | Path) -> list[dict]:
    out = []
    for p in sorted(Path(channel_dir).glob("*/extract.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out
