"""消息路由分类（skill_router 侧 M4 将镜像此契约）。

普通聊天不匹配任何意图（返回 None，不转发给 vreader）。
"""
from __future__ import annotations

import re

_DOUYIN_URL = re.compile(r"(v\.douyin\.com/|douyin\.com/(?:video|note)/|iesdouyin\.com/)")
_BOARD = re.compile(r"^\s*(?:/vreader\s+榜单|vr榜单)\s*$")
_CONFIRM = re.compile(r"^\s*(?:/vreader\s+确认|vr确认)\s+(\S+)\s*$")


def classify(text: str) -> str | None:
    """返回 'ingest' | 'board' | 'confirm' | None。"""
    t = text or ""
    if _CONFIRM.match(t):
        return "confirm"
    if _BOARD.match(t):
        return "board"
    if _DOUYIN_URL.search(t):
        return "ingest"
    return None
