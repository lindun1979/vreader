"""SQLite 持久层。

并发纪律（learnings concurrency/sqlite-shared-connection-threads）：连接绝不跨线程
共享——每个线程/请求调用 connect() 各持一个连接；WAL + busy_timeout；写用短事务。
原子领取用 BEGIN IMMEDIATE（可移植，不依赖 RETURNING，兼容 macOS 系统 libsqlite3）。
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# 任务状态
RECEIVED = "received"
DOWNLOADING = "downloading"
TRANSCRIBING = "transcribing"
EXTRACTING = "extracting"
RENDERING = "rendering"
SUCCEEDED = "succeeded"
RETRYABLE_FAILED = "retryable_failed"
TERMINAL_FAILED = "terminal_failed"

NONTERMINAL = (RECEIVED, DOWNLOADING, TRANSCRIBING, EXTRACTING, RENDERING)
TERMINAL = (SUCCEEDED, TERMINAL_FAILED)
MAX_RETRY = 2

# 决策（C2/C9）
AUTO_OK = "auto_ok"
PENDING = "pending"                 # 普通低置信：批量确认可批
PENDING_UNKNOWN = "pending_unknown"  # 模型名未知：批量排除，补别名表后 reprocess
PENDING_CONFLICT = "pending_conflict"  # 同一 attempt 矛盾得分：逐条确认（组内无裁决时）
APPROVED = "approved"
REJECTED_CONFLICT = "rejected_conflict"  # 冲突组落败方：裁决恒存
EXPIRED = "expired"
STALE = "stale"
BOARD_VISIBLE = (AUTO_OK, APPROVED)
# 凭指纹继承（apply_decisions 遇到旧裁决不重判）：人工批准与冲突拒绝
PERSISTENT_DECISIONS = (APPROVED, REJECTED_CONFLICT)
# mark_stale 豁免（缺失也不置 stale）：仅 rejected_conflict（裁决恒存，消除"stale 抹拒绝
# 再现重开"反例）。APPROVED 缺失仍照常 stale（规则5，带 ⚠️ 单独计数提示）。
STALE_EXEMPT = (REJECTED_CONFLICT,)
PENDING_KINDS = (PENDING, PENDING_UNKNOWN, PENDING_CONFLICT)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  aweme_id     TEXT PRIMARY KEY,
  channel      TEXT NOT NULL,
  raw_link     TEXT NOT NULL,
  chat_id      TEXT,
  sender_id    TEXT,
  title        TEXT,
  status       TEXT NOT NULL,
  retry_count  INTEGER NOT NULL DEFAULT 0,
  next_retry_at REAL NOT NULL DEFAULT 0,
  error        TEXT,
  created_at   REAL NOT NULL,
  updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, next_retry_at);

CREATE TABLE IF NOT EXISTS notification_outbox (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id       TEXT NOT NULL,
  content       TEXT NOT NULL,
  created_at    REAL NOT NULL,
  sent_at       REAL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  last_attempt_at REAL,
  last_error    TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_undelivered ON notification_outbox(sent_at, next_attempt_at);

CREATE TABLE IF NOT EXISTS record_decisions (
  record_id     TEXT PRIMARY KEY,
  aweme_id      TEXT NOT NULL,
  decision      TEXT NOT NULL,
  extractor_version TEXT,
  prompt_hash   TEXT,
  approved_by   TEXT,
  approved_at   REAL,
  created_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_aweme ON record_decisions(aweme_id);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------- tasks ----------

def insert_task(conn: sqlite3.Connection, *, aweme_id: str, channel: str,
                raw_link: str, chat_id: str, sender_id: str, title: str) -> bool:
    """返回 True=新建，False=已存在（幂等）。"""
    now = time.time()
    try:
        conn.execute(
            "INSERT INTO tasks(aweme_id, channel, raw_link, chat_id, sender_id, title,"
            " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (aweme_id, channel, raw_link, chat_id, sender_id, title, RECEIVED, now, now))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """原子领取一个可执行任务（received 或到点的 retryable_failed）。"""
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM tasks WHERE (status=? OR (status=? AND next_retry_at<=?))"
            " ORDER BY created_at LIMIT 1",
            (RECEIVED, RETRYABLE_FAILED, now)).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE aweme_id=?",
                     (DOWNLOADING, now, row["aweme_id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute("SELECT * FROM tasks WHERE aweme_id=?", (row["aweme_id"],)).fetchone()


def set_status(conn: sqlite3.Connection, aweme_id: str, status: str,
               *, error: str | None = None) -> None:
    conn.execute("UPDATE tasks SET status=?, error=?, updated_at=? WHERE aweme_id=?",
                 (status, error, time.time(), aweme_id))
    conn.commit()


def finalize_task(conn: sqlite3.Connection, aweme_id: str, status: str, *,
                  error: str | None = None, chat_id: str | None = None,
                  content: str | None = None, retry_count: int | None = None,
                  next_retry_at: float | None = None) -> None:
    """C1 事务所有权：终态/退避状态更新 + （可选）通知写 outbox，同一连接一次 commit；
    异常 rollback 后 re-raise（有 chat_id 的任务任何终态路径必经此带通知）。"""
    now = time.time()
    sets = ["status=?", "error=?", "updated_at=?"]
    vals: list = [status, error, now]
    if retry_count is not None:
        sets.append("retry_count=?"); vals.append(retry_count)
    if next_retry_at is not None:
        sets.append("next_retry_at=?"); vals.append(next_retry_at)
    vals.append(aweme_id)
    try:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE aweme_id=?", vals)
        if chat_id and content:
            enqueue_notification(conn, chat_id, content, commit=False)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def mark_retry_or_terminal(conn: sqlite3.Connection, aweme_id: str, error: str,
                           *, backoff_base: float = 60.0,
                           chat_id: str | None = None) -> str:
    """失败后按 retry_count 决定 retryable 还是 terminal。返回落定的状态。
    落 terminal 时，通知与状态在同一事务写出（C1，无静默失败）。"""
    row = conn.execute("SELECT retry_count FROM tasks WHERE aweme_id=?", (aweme_id,)).fetchone()
    rc = (row["retry_count"] if row else 0) + 1
    if rc > MAX_RETRY:
        content = f"❌ 处理失败（已重试耗尽）：{error[:120]}" if chat_id else None
        finalize_task(conn, aweme_id, TERMINAL_FAILED, error=error,
                      chat_id=chat_id, content=content, retry_count=rc)
        return TERMINAL_FAILED
    next_at = time.time() + backoff_base * (2 ** (rc - 1))
    finalize_task(conn, aweme_id, RETRYABLE_FAILED, error=error,
                  retry_count=rc, next_retry_at=next_at)
    return RETRYABLE_FAILED


def recover_nonterminal(conn: sqlite3.Connection) -> int:
    """启动恢复：把卡在执行中的任务递增 retry_count 后置回 received（毒丸防护，cf5 #1
    ——反复崩溃进程的任务不无限恢复）；超 MAX_RETRY 的置 terminal 并写通知。
    返回置回 received（可再领取）的数量。RECEIVED 态任务（未启动）不动、不计次。"""
    rows = conn.execute(
        "SELECT aweme_id, chat_id, retry_count FROM tasks WHERE status IN (?,?,?,?)",
        (DOWNLOADING, TRANSCRIBING, EXTRACTING, RENDERING)).fetchall()
    now = time.time()
    recovered = 0
    for r in rows:
        rc = r["retry_count"] + 1
        if rc > MAX_RETRY:
            content = "❌ 处理失败（多次中断已放弃）。可重新发链接重试。" if r["chat_id"] else None
            finalize_task(conn, r["aweme_id"], TERMINAL_FAILED,
                          error="recover: 超过恢复次数上限（疑似毒丸）",
                          chat_id=r["chat_id"], content=content, retry_count=rc)
        else:
            finalize_task(conn, r["aweme_id"], RECEIVED, retry_count=rc, next_retry_at=0)
            recovered += 1
    return recovered


def count_active(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) c FROM tasks WHERE status IN (?,?,?,?,?,?)",
        (RECEIVED, DOWNLOADING, TRANSCRIBING, EXTRACTING, RENDERING, RETRYABLE_FAILED)
    ).fetchone()["c"]


def get_task(conn: sqlite3.Connection, aweme_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE aweme_id=?", (aweme_id,)).fetchone()


# ---------- notification outbox ----------

def enqueue_notification(conn: sqlite3.Connection, chat_id: str, content: str,
                         *, commit: bool = True) -> None:
    """写通知。commit=False 时由调用方在同一事务提交（与状态更新原子）。"""
    conn.execute(
        "INSERT INTO notification_outbox(chat_id, content, created_at, next_attempt_at)"
        " VALUES (?,?,?,?)", (chat_id, content, time.time(), 0.0))
    if commit:
        conn.commit()


def claim_due_notifications(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    now = time.time()
    return conn.execute(
        "SELECT * FROM notification_outbox WHERE sent_at IS NULL AND next_attempt_at<=?"
        " ORDER BY id LIMIT ?", (now, limit)).fetchall()


def mark_notification_sent(conn: sqlite3.Connection, nid: int) -> None:
    now = time.time()
    conn.execute("UPDATE notification_outbox SET sent_at=?, last_attempt_at=? WHERE id=?",
                 (now, now, nid))
    conn.commit()


def mark_notification_failed(conn: sqlite3.Connection, nid: int, err: str,
                             *, backoff_base: float = 30.0, max_backoff: float = 1800.0) -> None:
    now = time.time()
    row = conn.execute("SELECT attempt_count FROM notification_outbox WHERE id=?", (nid,)).fetchone()
    ac = (row["attempt_count"] if row else 0) + 1
    delay = min(backoff_base * (2 ** (ac - 1)), max_backoff)
    conn.execute(
        "UPDATE notification_outbox SET attempt_count=?, last_attempt_at=?, last_error=?,"
        " next_attempt_at=? WHERE id=?", (ac, now, err[:500], now + delay, nid))
    conn.commit()


def outbox_health(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) c, MIN(created_at) oldest FROM notification_outbox WHERE sent_at IS NULL"
    ).fetchone()
    oldest_age = (time.time() - row["oldest"]) if row["oldest"] else 0
    return {"undelivered": row["c"], "oldest_age_s": round(oldest_age, 1)}


# ---------- record decisions ----------

def upsert_decision(conn: sqlite3.Connection, *, record_id: str, aweme_id: str,
                    decision: str, extractor_version: str, prompt_hash: str,
                    approved_by: str | None = None, approved_at: float | None = None,
                    commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO record_decisions(record_id, aweme_id, decision, extractor_version,"
        " prompt_hash, approved_by, approved_at, created_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(record_id) DO UPDATE SET decision=excluded.decision,"
        " extractor_version=excluded.extractor_version, prompt_hash=excluded.prompt_hash",
        (record_id, aweme_id, decision, extractor_version, prompt_hash,
         approved_by, approved_at, time.time()))
    if commit:
        conn.commit()


def get_decision(conn: sqlite3.Connection, record_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM record_decisions WHERE record_id=?", (record_id,)).fetchone()


def mark_stale(conn: sqlite3.Connection, aweme_id: str, keep_ids: set[str],
               *, commit: bool = True) -> int:
    """当前 extract 已不含的记录置 stale；裁决恒存的（approved/rejected_conflict）跳过
    ——裁决不是产物状态，记录本次缺失不抹裁决（C2.3，消除 stale 抹拒绝再现重开的反例）。"""
    rows = conn.execute(
        "SELECT record_id, decision FROM record_decisions WHERE aweme_id=?", (aweme_id,)).fetchall()
    n = 0
    for r in rows:
        if r["record_id"] not in keep_ids and r["decision"] not in STALE_EXEMPT:
            conn.execute("UPDATE record_decisions SET decision=? WHERE record_id=?",
                         (STALE, r["record_id"]))
            n += 1
    if commit:
        conn.commit()
    return n


def list_decisions_for_video(conn: sqlite3.Connection, aweme_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT record_id, decision FROM record_decisions WHERE aweme_id=?", (aweme_id,)).fetchall()


def approve_pending_for_video(conn: sqlite3.Connection, aweme_id: str, approver: str) -> int:
    now = time.time()
    cur = conn.execute(
        "UPDATE record_decisions SET decision=?, approved_by=?, approved_at=? WHERE aweme_id=? AND decision=?",
        (APPROVED, approver, now, aweme_id, PENDING))
    conn.commit()
    return cur.rowcount


def expire_old_pending(conn: sqlite3.Connection, older_than_s: float) -> int:
    cutoff = time.time() - older_than_s
    cur = conn.execute(
        "UPDATE record_decisions SET decision=? WHERE decision=? AND created_at<?",
        (EXPIRED, PENDING, cutoff))
    conn.commit()
    return cur.rowcount


def board_visible_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT record_id FROM record_decisions WHERE decision IN (?,?)",
                        BOARD_VISIBLE).fetchall()
    return {r["record_id"] for r in rows}
