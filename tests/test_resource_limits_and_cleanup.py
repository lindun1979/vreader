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
