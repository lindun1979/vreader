"""崩溃恢复：非终态任务重启后可再次领取，retry_count 保留。"""
from core import db


def test_recover_nonterminal_resets_to_received(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.insert_task(c, aweme_id="a1", channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")
    # 模拟卡在中间态
    db.set_status(c, "a1", db.TRANSCRIBING)
    assert db.get_task(c, "a1")["status"] == db.TRANSCRIBING

    n = db.recover_nonterminal(c)
    assert n == 1
    t = db.get_task(c, "a1")
    assert t["status"] == db.RECEIVED
    # 可被重新领取
    claimed = db.claim_next(c)
    assert claimed["aweme_id"] == "a1"
    c.close()


def test_recover_preserves_retry_count(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.insert_task(c, aweme_id="a2", channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")
    db.mark_retry_or_terminal(c, "a2", "boom")  # retry_count -> 1, retryable
    assert db.get_task(c, "a2")["retry_count"] == 1
    db.set_status(c, "a2", db.EXTRACTING)  # 又卡住
    db.recover_nonterminal(c)
    t = db.get_task(c, "a2")
    assert t["status"] == db.RECEIVED
    assert t["retry_count"] == 1  # 不清零
    c.close()


def test_terminal_after_max_retries(db_path):
    db.init(db_path)
    c = db.connect(db_path)
    db.insert_task(c, aweme_id="a3", channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="t")
    assert db.mark_retry_or_terminal(c, "a3", "e") == db.RETRYABLE_FAILED  # 1
    assert db.mark_retry_or_terminal(c, "a3", "e") == db.RETRYABLE_FAILED  # 2
    assert db.mark_retry_or_terminal(c, "a3", "e") == db.TERMINAL_FAILED   # 3 > MAX_RETRY
    c.close()
