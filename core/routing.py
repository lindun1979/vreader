"""消息路由分类（skill_router 侧 M4 将镜像此契约）。

普通聊天不匹配任何意图（返回 None，不转发给 vreader）。命令三式：
  vr榜单 / /vreader 榜单            → board
  vr明细 <video_id>                → detail
  vr确认 <video_id> [rid8|rev:xxx] → confirm（批量 / 逐条冲突成员 / 绑版本）
"""
from __future__ import annotations

import re

_DOUYIN_URL = re.compile(r"(v\.douyin\.com/|douyin\.com/(?:video|note)/|iesdouyin\.com/)")
_HELP = re.compile(r"^\s*(?:/vreader\s+帮助|vr帮助)\s*$")
_BOARD = re.compile(r"^\s*(?:/vreader\s+榜单|vr榜单)\s*$")
_DETAIL = re.compile(r"^\s*(?:/vreader\s+明细|vr明细)\s+(\S+)\s*$")
_CONFIRM = re.compile(r"^\s*(?:/vreader\s+确认|vr确认)\s+(\S+)(?:\s+(\S+))?\s*$")


def classify(text: str) -> str | None:
    """返回 'ingest' | 'board' | 'detail' | 'confirm' | 'help' | None。"""
    t = text or ""
    if _HELP.match(t):
        return "help"
    if _CONFIRM.match(t):
        return "confirm"
    if _DETAIL.match(t):
        return "detail"
    if _BOARD.match(t):
        return "board"
    if _DOUYIN_URL.search(t):
        return "ingest"
    return None


def parse_confirm(text: str) -> tuple[str, str | None]:
    """→ (video_id, arg)；arg 为逐条 rid 短码 / 'rev:<版本>' / None（批量）。"""
    m = _CONFIRM.match(text or "")
    if not m:
        return "", None
    return m.group(1), m.group(2)


def parse_detail(text: str) -> str:
    m = _DETAIL.match(text or "")
    return m.group(1) if m else ""
