"""升级/回滚的快照与恢复逻辑（plan M11）。回滚 = 恢复到停服快照（有界丢弃，非无损）。

两份清单（快照时点的真相）：
  manifest-files.txt —— 产物文件（vreader.db*、*/extract.json、board.md），相对 data_dir。
  manifest-dirs.txt  —— 视频目录（token_bug/<id>/），**含尚无 extract 的中间态目录**。
回滚删除规则（M11）：
  - 不在 manifest-dirs 的视频目录 = 真正新增视频 → 整目录删。
  - 在旧目录中新产的 extract.json（不在 manifest-files）→ **只删该文件**，不动目录/transcript。
  - 备份的 db 文件族 + extracts.tgz（extract.json 与 board.md）覆盖恢复。

调用方负责锁与服务生命周期（unload → 取维护锁 → backup/rollback → 释放锁 → load）；
本模块只做文件与清单操作，不碰 flock、不起服务、不调 CLI --render。
"""
from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

CHANNEL = "token_bug"
_DB_SIDECARS = ("", "-wal", "-shm")


def _db_family(data_dir: Path) -> list[Path]:
    return [data_dir / f"vreader.db{s}" for s in _DB_SIDECARS
            if (data_dir / f"vreader.db{s}").exists()]


def _extract_and_board_files(data_dir: Path) -> list[Path]:
    out = []
    board = data_dir / CHANNEL / "board.md"
    if board.exists():
        out.append(board)
    out += sorted((data_dir / CHANNEL).glob("*/extract.json"))
    return out


def backup(data_dir: Path, backup_dir: Path) -> None:
    """停服 + 持维护锁后调用：写两份清单 + 复制 db 文件族 + 打包 extract.json/board.md。"""
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    # 产物清单（db 旁文件按实际存在与否入清单）
    files = _db_family(data_dir) + _extract_and_board_files(data_dir)
    (backup_dir / "manifest-files.txt").write_text(
        "\n".join(str(p.relative_to(data_dir)) for p in files) + "\n", encoding="utf-8")
    # 视频目录清单（含尚无 extract 的中间态目录）
    ch = data_dir / CHANNEL
    dirs = sorted(d for d in ch.glob("*") if d.is_dir()) if ch.exists() else []
    (backup_dir / "manifest-dirs.txt").write_text(
        "\n".join(str(d.relative_to(data_dir)) for d in dirs) + "\n", encoding="utf-8")
    # 复制 db 文件族
    for p in _db_family(data_dir):
        shutil.copy2(p, backup_dir / p.name)
    # 打包 extract.json 与 board.md
    with tarfile.open(backup_dir / "extracts.tgz", "w:gz") as tar:
        for p in _extract_and_board_files(data_dir):
            tar.add(p, arcname=str(p.relative_to(data_dir)))


def _read_manifest(backup_dir: Path, name: str) -> set[str]:
    p = backup_dir / name
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()}


def rollback(data_dir: Path, backup_dir: Path) -> None:
    """停服 + 持维护锁后调用：按备份清单恢复到快照点，清除快照后新增内容。"""
    data_dir, backup_dir = Path(data_dir), Path(backup_dir)
    files_manifest = _read_manifest(backup_dir, "manifest-files.txt")
    dirs_manifest = _read_manifest(backup_dir, "manifest-dirs.txt")

    # 1) 先删目标处现有 db 文件族全族（清残留旁文件），再恢复备份 db 文件族
    for p in _db_family(data_dir):
        p.unlink()
    for s in _DB_SIDECARS:
        src = backup_dir / f"vreader.db{s}"
        if src.exists():
            shutil.copy2(src, data_dir / f"vreader.db{s}")

    # 2) 解包 extracts.tgz 覆盖恢复既有 extract.json 与 board.md
    tgz = backup_dir / "extracts.tgz"
    if tgz.exists():
        with tarfile.open(tgz, "r:gz") as tar:
            tar.extractall(data_dir)  # noqa: S202 受控备份包

    # 3) 清除快照后新增内容：目录与产物分别对照各自清单
    ch = data_dir / CHANNEL
    if ch.exists():
        for d in sorted(x for x in ch.glob("*") if x.is_dir()):
            rel = str(d.relative_to(data_dir))
            if rel not in dirs_manifest:
                shutil.rmtree(d)  # 真正新增的视频目录 → 整目录删
                continue
            # 旧目录：新产的 extract.json（不在 files_manifest）只删该文件
            ex = d / "extract.json"
            if ex.exists() and str(ex.relative_to(data_dir)) not in files_manifest:
                ex.unlink()
