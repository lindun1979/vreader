"""资源上限与清理：队列满拒收、终态删媒体、幂等 ingest。"""
from pathlib import Path

from core import config, db, douyin, service, pipeline


def test_queue_full_rejects(data_dir, monkeypatch):
    dbp = str(data_dir / "vreader.db")
    service._DB_PATH = dbp
    db.init(dbp)
    c = db.connect(dbp)
    monkeypatch.setattr(config, "MAX_QUEUE", 2)
    monkeypatch.setattr(douyin, "resolve_aweme_id", lambda t: t.strip().rsplit("/", 1)[-1])
    monkeypatch.setattr(douyin, "fetch_detail", lambda a: {"desc": "x", "video": {}})
    # 填到上限
    for i in range(2):
        code, reply = service.handle_ingest(c, {"text": f"https://x/{i}", "chat_id": "c", "sender_id": "s"})
        assert code == 200 and "已收到" in reply
    # 第三个被拒
    code, reply = service.handle_ingest(c, {"text": "https://x/3", "chat_id": "c", "sender_id": "s"})
    assert "队列已满" in reply
    c.close()


def test_duplicate_ingest_idempotent(data_dir, monkeypatch):
    dbp = str(data_dir / "vreader.db")
    service._DB_PATH = dbp
    db.init(dbp)
    c = db.connect(dbp)
    monkeypatch.setattr(douyin, "resolve_aweme_id", lambda t: "same_id")
    monkeypatch.setattr(douyin, "fetch_detail", lambda a: {"desc": "x", "video": {}})
    code, r1 = service.handle_ingest(c, {"text": "https://x/1", "chat_id": "c", "sender_id": "s"})
    assert "已收到" in r1
    code, r2 = service.handle_ingest(c, {"text": "https://x/1", "chat_id": "c", "sender_id": "s"})
    assert "已在处理" in r2
    assert db.count_active(c) == 1
    c.close()


def test_cleanup_media_removes_video_and_wav(data_dir, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    p = pipeline._paths("vX")
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    Path(p["video"]).write_bytes(b"x")
    Path(p["wav"]).write_bytes(b"y")
    Path(p["transcript"]).write_text("t")
    pipeline._cleanup_media(p)
    assert not Path(p["video"]).exists()
    assert not Path(p["wav"]).exists()
    assert Path(p["transcript"]).exists()  # 转写保留


def test_low_disk_rejects_ingest(data_dir, monkeypatch):
    dbp = str(data_dir / "vreader.db")
    service._DB_PATH = dbp
    db.init(dbp)
    c = db.connect(dbp)
    monkeypatch.setattr(douyin, "resolve_aweme_id", lambda t: "vid_lowdisk")
    monkeypatch.setattr(service, "_disk_free_gb", lambda: config.MIN_DISK_GB - 1)
    code, reply = service.handle_ingest(c, {"text": "https://x/1", "chat_id": "c", "sender_id": "s"})
    assert code == 200 and "磁盘空间不足" in reply
    assert db.get_task(c, "vid_lowdisk") is None  # 未建任务
    c.close()


def test_cleanup_glob_removes_part_and_gladia_tmp(data_dir, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    p = pipeline._paths("vY")
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    Path(p["video"] + ".part").write_bytes(b"x")
    Path(p["video"] + ".gladia.mp3").write_bytes(b"y")
    Path(p["transcript"]).write_text("t")
    Path(p["extract"]).write_text("{}")
    pipeline._cleanup_media(p)
    assert not Path(p["video"] + ".part").exists()
    assert not Path(p["video"] + ".gladia.mp3").exists()
    assert Path(p["transcript"]).exists() and Path(p["extract"]).exists()


def test_sweep_orphan_media_respects_nonterminal(data_dir, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    dbp = str(data_dir / "vreader.db")
    db.init(dbp)
    c = db.connect(dbp)
    # 终态任务：媒体应删
    db.insert_task(c, aweme_id="done", channel="token_bug", raw_link="x", chat_id="c", sender_id="s", title="t")
    db.finalize_task(c, "done", db.SUCCEEDED)
    # 非终态任务：媒体保留
    db.insert_task(c, aweme_id="live", channel="token_bug", raw_link="x", chat_id="c", sender_id="s", title="t")
    for aid in ("done", "live", "orphan"):
        pd = pipeline._paths(aid)
        Path(pd["dir"]).mkdir(parents=True, exist_ok=True)
        Path(pd["video"]).write_bytes(b"x")
    removed = pipeline.sweep_orphan_media(c)
    assert not Path(pipeline._paths("done")["video"]).exists()   # 终态删
    assert Path(pipeline._paths("live")["video"]).exists()       # 非终态保留
    assert not Path(pipeline._paths("orphan")["video"]).exists() # 无主删
    assert removed == 2
    c.close()
