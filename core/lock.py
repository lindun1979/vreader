"""执行权互斥（C6）。

跨进程：数据目录 flock（serve 与 CLI 写路径互斥，防双实例互踩 / CLI 双处理）。
锁 FD 不继承到子进程（受控子进程 spawn 时不带走锁）。
进程内：publish 锁（C8）保护榜单/决策发布的读-校验-渲染一致性（worker 线程 vs
confirm HTTP 线程并发渲染，防旧覆盖新）。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

try:
    import fcntl
except ImportError:  # 仅类 Unix（生产 macOS / dev Linux）；无 fcntl 环境降级为无锁
    fcntl = None  # type: ignore


class LockBusy(Exception):
    """数据目录已被另一实例持有。"""


class DataDirLock:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / ".vreader.lock"
        self._fd: int | None = None

    def acquire(self, *, blocking: bool = False) -> bool:
        if fcntl is None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        os.set_inheritable(fd, False)  # 锁 FD 不入子进程
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None

    def __enter__(self):
        if not self.acquire(blocking=False):
            raise LockBusy(f"数据目录已被另一实例持有: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()


# 进程内发布锁（C8）：可重入，允许同线程 confirm→render 嵌套加锁。
publish_lock = threading.RLock()
