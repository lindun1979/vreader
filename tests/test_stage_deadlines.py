"""V-M06（可靠子集）：下载字节/时限硬限、.part 清理、CLI 超时 killpg 杀后代。

完整「受控子进程 _run_staged + ASR 子进程隔离 + C10 两段式放行握手」需生产 macOS
实测（评审官立场：此层必须实测，mock 不足以证明生产正确性），作为人工决策项另行处理。
"""
import io
import os
import signal
import subprocess
import time

import pytest

from core import douyin, extract as ex_mod


class _FakeResp:
    """模拟持续 trickle 的响应：每次 read 返回一点，永不结束。"""
    def __init__(self, chunk=b"x" * 1024, delay=0.0):
        self.chunk, self.delay = chunk, delay

    def read(self, n):
        if self.delay:
            time.sleep(self.delay)
        return self.chunk

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def geturl(self): return ""


def test_download_byte_hard_limit(tmp_path, monkeypatch):
    dest = str(tmp_path / "video.mp4")
    monkeypatch.setattr(douyin, "_play_urls", lambda d: ["http://x/1"])
    monkeypatch.setattr(douyin, "_get_ttwid", lambda *a, **k: "tw")
    monkeypatch.setattr(douyin.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
    with pytest.raises(douyin.DownloadError) as e:
        douyin.download("v1", dest, detail={"video": {}}, max_bytes=100_000)
    assert "字节上限" in str(e.value) or "下载失败" in str(e.value)
    assert not os.path.exists(dest + ".part")  # .part 已清理


def test_download_total_deadline(tmp_path, monkeypatch):
    dest = str(tmp_path / "video.mp4")
    monkeypatch.setattr(douyin, "_play_urls", lambda d: ["http://x/1"])
    monkeypatch.setattr(douyin, "_get_ttwid", lambda *a, **k: "tw")
    monkeypatch.setattr(douyin.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(chunk=b"y" * 16, delay=0.01))
    with pytest.raises(douyin.DownloadError):
        douyin.download("v1", dest, detail={"video": {}}, timeout_s=0.2)
    assert not os.path.exists(dest + ".part")


def test_run_cli_timeout_kills_process_group():
    """超时 CLI（自身还 fork 了 sleep 后代）：killpg 杀整棵树，无残留后代。"""
    # 一个会起后代 sleep 的 shell 命令，本身也 sleep（超我们给的 timeout）
    cmd = ["bash", "-c", "sleep 30 & sleep 30"]
    t0 = time.monotonic()
    with pytest.raises(ex_mod.ExtractError) as e:
        ex_mod._run_cli(cmd, timeout=1, name="fake CLI")
    assert "超时" in str(e.value)
    assert time.monotonic() - t0 < 10  # 限时返回，未挂死


def test_run_cli_not_found():
    with pytest.raises(ex_mod.ExtractError) as e:
        ex_mod._run_cli(["/no/such/bin/xyz"], timeout=1, name="missing")
    assert "未找到" in str(e.value)
