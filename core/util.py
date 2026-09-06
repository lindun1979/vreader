"""通用小工具：原子写（临时文件 + rename，同目录保证 rename 原子性）。

C8：产物写入用原子 rename，读者永不见半成品；临时名带 pid+uuid 防并发互踩。
不承诺断电持久性（§一.2 豁免）——只保证读者不见截断。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path


def _tmp_name(dest: Path) -> Path:
    return dest.parent / f".tmp.{os.getpid()}.{uuid.uuid4().hex}{dest.suffix}"


def atomic_write_text(dest_path: str | Path, text: str, *, encoding: str = "utf-8") -> None:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(dest)
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_bytes(dest_path: str | Path, data: bytes) -> None:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_name(dest)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
