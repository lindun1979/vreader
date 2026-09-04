"""并发领取：同一任务只被领一次，多线程各自连接无 locked/损坏。"""
import threading

from core import db


def test_concurrent_claim_single_execution(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    for i in range(5):
        db.insert_task(c, aweme_id=f"a{i}", channel="token_bug", raw_link="x",
                       chat_id="cid", sender_id="s", title="t")
    c.close()

    claimed: list[str] = []
    lock = threading.Lock()

    def worker():
        conn = db.connect(db_path)  # 每线程独立连接（并发纪律）
        try:
            while True:
                t = db.claim_next(conn)
                if t is None:
                    return
                with lock:
                    claimed.append(t["aweme_id"])
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 5 个任务各被领恰好一次，无重复
    assert sorted(claimed) == ["a0", "a1", "a2", "a3", "a4"]
    assert len(claimed) == len(set(claimed))


def test_insert_idempotent(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    assert db.insert_task(c, aweme_id="dup", channel="token_bug", raw_link="x",
                          chat_id="c", sender_id="s", title="t") is True
    assert db.insert_task(c, aweme_id="dup", channel="token_bug", raw_link="x",
                          chat_id="c", sender_id="s", title="t") is False
    c.close()
