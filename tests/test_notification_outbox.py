"""通知 outbox：同事务写入、退避持久化、重启后按 next_attempt_at 恢复。"""
import time

from core import db


def test_enqueue_and_claim(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.enqueue_notification(c, "chat1", "hi")
    due = db.claim_due_notifications(c)
    assert len(due) == 1 and due[0]["chat_id"] == "chat1"
    db.mark_notification_sent(c, due[0]["id"])
    assert db.claim_due_notifications(c) == []
    c.close()


def test_failure_sets_backoff_persistently(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.enqueue_notification(c, "chat1", "hi")
    nid = db.claim_due_notifications(c)[0]["id"]
    db.mark_notification_failed(c, nid, "network down")
    row = c.execute("SELECT * FROM notification_outbox WHERE id=?", (nid,)).fetchone()
    assert row["attempt_count"] == 1
    assert row["last_error"] == "network down"
    assert row["next_attempt_at"] > time.time()  # 退避到未来
    # 未到点不领取
    assert db.claim_due_notifications(c) == []
    c.close()


def test_restart_resumes_from_next_attempt_at(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.enqueue_notification(c, "chat1", "hi")
    nid = db.claim_due_notifications(c)[0]["id"]
    db.mark_notification_failed(c, nid, "e")
    c.close()
    # 模拟重启：新连接，把 next_attempt_at 拉到过去 → 应恢复投递
    c2 = db.connect(db_path)
    c2.execute("UPDATE notification_outbox SET next_attempt_at=0 WHERE id=?", (nid,))
    c2.commit()
    assert len(db.claim_due_notifications(c2)) == 1
    c2.close()


def test_status_and_notification_same_transaction(db_path):
    """终态状态更新与通知写入在同一事务：一起可见（C1 finalize_task）。"""
    db.init(db_path)
    c = db.connect(db_path)
    db.insert_task(c, aweme_id="a1", channel="token_bug", raw_link="x",
                   chat_id="chat1", sender_id="s", title="t")
    db.finalize_task(c, "a1", db.TERMINAL_FAILED, error="e",
                     chat_id="chat1", content="failed msg")
    assert db.get_task(c, "a1")["status"] == db.TERMINAL_FAILED
    assert len(db.claim_due_notifications(c)) == 1
    c.close()


def test_outbox_health(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.enqueue_notification(c, "chat1", "hi")
    h = db.outbox_health(c)
    assert h["undelivered"] == 1 and h["oldest_age_s"] >= 0
    c.close()
