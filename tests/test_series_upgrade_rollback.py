"""Step 5 升级/回滚演练（plan M11）：隔离副本上 backup→mutate→rollback，
断言旧 extract 字节恢复、新文件/目录按清单清除、db 与 known 恢复、锁释放后可再取。"""
import importlib.util
import json
from pathlib import Path

from core import db, lock

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("upgrade_rollback", ROOT / "ops" / "upgrade_rollback.py")
ur = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ur)


def _setup(data_dir: Path):
    ch = data_dir / "token_bug"
    # vidA：既有 extract + transcript
    (ch / "vidA").mkdir(parents=True)
    (ch / "vidA" / "extract.json").write_text('{"orig":"A"}', encoding="utf-8")
    (ch / "vidA" / "transcript.txt").write_text("A转写", encoding="utf-8")
    # vidT：仅 transcript、无 extract（停服时的中间态目录）
    (ch / "vidT").mkdir()
    (ch / "vidT" / "transcript.txt").write_text("T转写原文", encoding="utf-8")
    (ch / "board.md").write_text("# 原始榜单", encoding="utf-8")
    # db + 决策 + known
    dbp = str(data_dir / "vreader.db")
    db.init(dbp)
    c = db.connect(dbp)
    db.upsert_decision(c, record_id="rid_old", aweme_id="vidA", decision=db.APPROVED,
                       extractor_version="e", prompt_hash="p", approved_by="a", approved_at=1.0)
    c.commit()
    c.close()


def test_backup_rollback_restores_snapshot(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "token_bug").mkdir(parents=True)
    _setup(data_dir)
    backup = tmp_path / "backup"

    ur.backup(data_dir, backup)
    assert (backup / "manifest-files.txt").exists()
    # manifest-dirs 含无 extract 的 vidT
    dirs = (backup / "manifest-dirs.txt").read_text(encoding="utf-8")
    assert "token_bug/vidT" in dirs and "token_bug/vidA" in dirs

    ch = data_dir / "token_bug"
    # —— 模拟升级期变更 ——
    (ch / "vidA" / "extract.json").write_text('{"changed":"A2"}', encoding="utf-8")  # 覆盖既有
    (ch / "vidT" / "extract.json").write_text('{"new":"T"}', encoding="utf-8")       # 旧目录新产 extract
    (ch / "vidNEW").mkdir()                                                          # 真正新增视频
    (ch / "vidNEW" / "extract.json").write_text('{"new":"video"}', encoding="utf-8")
    (ch / "board.md").write_text("# 升级后榜单", encoding="utf-8")
    c = db.connect(str(data_dir / "vreader.db"))
    db.register_known_version(c, "GLM", "9.9", "", "admin", commit=True)
    db.upsert_decision(c, record_id="rid_new", aweme_id="vidNEW", decision=db.AUTO_OK,
                       extractor_version="e", prompt_hash="p")
    c.close()

    # —— 回滚 ——
    ur.rollback(data_dir, backup)

    # 旧 extract 字节恢复
    assert (ch / "vidA" / "extract.json").read_text(encoding="utf-8") == '{"orig":"A"}'
    # 旧目录里新产的 extract 被移除，但目录与 transcript 原字节不变
    assert not (ch / "vidT" / "extract.json").exists()
    assert (ch / "vidT").is_dir()
    assert (ch / "vidT" / "transcript.txt").read_text(encoding="utf-8") == "T转写原文"
    # 真正新增视频目录整体移除
    assert not (ch / "vidNEW").exists()
    # board 恢复
    assert (ch / "board.md").read_text(encoding="utf-8") == "# 原始榜单"
    # db 恢复：known 无 9.9、决策回到快照
    c = db.connect(str(data_dir / "vreader.db"))
    assert ("GLM", "9.9", "") not in db.list_known_versions(c)
    assert db.get_decision(c, "rid_old")["decision"] == db.APPROVED
    assert db.get_decision(c, "rid_new") is None
    c.close()


def test_rollback_handles_wal_sidecar_present(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "token_bug").mkdir(parents=True)
    _setup(data_dir)
    # 制造 -wal 旁文件（WAL 模式连接常驻）
    (data_dir / "vreader.db-wal").write_bytes(b"waldata")
    backup = tmp_path / "backup"
    ur.backup(data_dir, backup)
    assert (backup / "vreader.db-wal").exists()  # 旁文件入备份
    # 升级期又新增一个旁文件残留
    (data_dir / "vreader.db-shm").write_bytes(b"shm-residue")
    ur.rollback(data_dir, backup)
    # 残留 -shm（备份里没有）被清除
    assert not (data_dir / "vreader.db-shm").exists()
    assert (data_dir / "vreader.db-wal").read_bytes() == b"waldata"


def test_ops_manual_has_backup_restore_and_lock_order():
    """手册文件检查（M11）：含备份/恢复命令原文、锁与 healthz/load 先后顺序。"""
    doc = (ROOT / "docs" / "ops-series-norm-upgrade-rollback.md").read_text(encoding="utf-8")
    assert "manifest-files.txt" in doc and "manifest-dirs.txt" in doc
    assert "extracts.tgz" in doc and "launchctl unload" in doc and "launchctl load" in doc
    # 锁与 healthz 顺序：load 之后跑 healthz；释放维护锁必须在 load 之前
    assert "load 之后" in doc and "释放" in doc
    assert "不在持锁状态下调 CLI `--render`" in doc
    # 撤销登记 ≠ 撤销批准
    assert "DELETE FROM known_versions" in doc and "pending_new_version" in doc


def test_maintenance_lock_release_then_service_lock_acquires(tmp_path):
    data_dir = tmp_path / "lockdir"
    data_dir.mkdir()
    maint = lock.DataDirLock(data_dir)
    assert maint.acquire(blocking=False)
    maint.release()
    # 释放维护锁后，服务锁申请成功（load 前必须已释放）
    svc = lock.DataDirLock(data_dir)
    assert svc.acquire(blocking=False)
    svc.release()
