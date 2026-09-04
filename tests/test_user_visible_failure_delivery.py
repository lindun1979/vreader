"""终态失败必写飞书 outbox；outbox 投递循环把未送达发出并标记。"""
from core import config, db, douyin, pipeline, service


def test_terminal_failure_enqueues_notification(data_dir, monkeypatch):
    dbp = str(data_dir / "vreader.db")
    db.init(dbp)
    c = db.connect(dbp)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    db.insert_task(c, aweme_id="fail1", channel="token_bug", raw_link="x",
                   chat_id="chatX", sender_id="s", title="t")
    # download 阶段直接抛 → 走 retry/terminal 路径

    def boom(_a):
        raise douyin.DownloadError("网络挂了")
    monkeypatch.setattr(douyin, "fetch_detail", boom)

    t = db.claim_next(c)
    # 连续处理直到 terminal
    for _ in range(4):
        t = db.get_task(c, "fail1")
        if t["status"] == db.TERMINAL_FAILED:
            break
        # 重置到可领取再处理（模拟重试到点）
        db.set_status(c, "fail1", db.RECEIVED)
        claimed = db.claim_next(c)
        pipeline.process_task(c, claimed)
    assert db.get_task(c, "fail1")["status"] == db.TERMINAL_FAILED
    # 必须已写通知
    due = db.claim_due_notifications(c)
    assert any(n["chat_id"] == "chatX" for n in due)
    c.close()


def test_outbox_loop_sends_and_marks(data_dir, monkeypatch):
    import threading
    dbp = str(data_dir / "vreader.db")
    service._DB_PATH = dbp
    db.init(dbp)
    c = db.connect(dbp)
    db.enqueue_notification(c, "chatX", "hello")
    c.close()

    sent = []
    import core.feishu as feishu
    monkeypatch.setattr(feishu, "send_text", lambda chat, text: sent.append((chat, text)))

    stop = threading.Event()
    th = threading.Thread(target=service.outbox_loop, args=(stop, 0.05))
    th.start()
    # 等投递
    import time
    for _ in range(40):
        c2 = db.connect(dbp)
        done = db.claim_due_notifications(c2) == []
        c2.close()
        if done and sent:
            break
        time.sleep(0.05)
    stop.set()
    th.join(timeout=2)
    assert sent == [("chatX", "hello")]
