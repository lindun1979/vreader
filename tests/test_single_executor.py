"""V-M08：数据目录 flock 执行权互斥（C6）+ --board 纯读不取锁不渲染。"""
import sys

import pytest

from core import config, lock


def test_flock_excludes_second_instance(data_dir):
    l1 = lock.DataDirLock(data_dir)
    assert l1.acquire(blocking=False) is True
    l2 = lock.DataDirLock(data_dir)
    assert l2.acquire(blocking=False) is False  # 第二实例取不到
    l1.release()
    assert l2.acquire(blocking=False) is True    # 释放后可取
    l2.release()


def test_board_pure_read_no_lock_no_render(data_dir, monkeypatch):
    """--board 在写锁被占用时仍能读；不调用 render_board。"""
    from core import cli, pipeline
    # 预置一份 board.md
    ch = config.channel_dir(pipeline.CHANNEL)
    ch.mkdir(parents=True, exist_ok=True)
    (ch / "board.md").write_text("# 榜单快照", encoding="utf-8")
    # 占住写锁
    held = lock.DataDirLock(data_dir)
    assert held.acquire(blocking=False)
    # render_board 不应被调用
    called = {"n": 0}
    monkeypatch.setattr(pipeline, "render_board", lambda conn: called.__setitem__("n", called["n"] + 1))
    rc = cli.main(["--board"])
    held.release()
    assert rc == 0 and called["n"] == 0


def test_cli_write_refused_when_locked(data_dir, monkeypatch, capsys):
    from core import cli
    held = lock.DataDirLock(data_dir)
    assert held.acquire(blocking=False)
    rc = cli.main(["https://v.douyin.com/abc/"])
    held.release()
    assert rc == 3  # 写路径被拒
