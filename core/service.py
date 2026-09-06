"""vreader HTTP 服务：ingest / board / confirm / healthz + worker + outbox 线程。

仅绑 127.0.0.1（同机 skill_router 透传）。HMAC 覆盖时间戳+body，±300s 窗口。
请求处理逻辑（handle_*）为纯函数（吃 conn），HTTP 层只做路由与签名。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, douyin, pipeline

_TS_WINDOW = 300
_DB_PATH = None

# ---------- 共享健康心跳（C5/C7）----------
# 后台线程每次循环更新自己的心跳；healthz 据此做三态 + 存活 + db 连错 + outbox 判定。
_HEALTH_LOCK = threading.Lock()
_HEALTH = {
    "worker_alive": False,
    "worker_last_beat": 0.0,        # 最近一次循环迭代
    "worker_last_claim_at": 0.0,    # 最近一次尝试领取任务
    "worker_busy_until": 0.0,       # >now 表示正在处理任务（阶段截止，2b 精化）
    "outbox_alive": False,
    "outbox_last_beat": 0.0,
    "db_consec_errors": 0,          # 连续 db 错误数（≥5 判不健康）
    "paused_asr_orphan": False,     # C10：孤儿清不掉，暂停重型 ASR
}
_DB_ERROR_THRESHOLD = 5


def _health_set(**kw) -> None:
    with _HEALTH_LOCK:
        _HEALTH.update(kw)


def _health_beat(who: str) -> None:
    with _HEALTH_LOCK:
        _HEALTH[f"{who}_alive"] = True
        _HEALTH[f"{who}_last_beat"] = time.time()


def _db_ok() -> None:
    with _HEALTH_LOCK:
        _HEALTH["db_consec_errors"] = 0


def _db_error() -> None:
    with _HEALTH_LOCK:
        _HEALTH["db_consec_errors"] += 1


def health_snapshot() -> dict:
    with _HEALTH_LOCK:
        return dict(_HEALTH)


def db_path() -> str:
    global _DB_PATH
    if _DB_PATH is None:
        _DB_PATH = str(config.DATA_DIR / "vreader.db")
    return _DB_PATH


# ---------- HMAC ----------

def sign(ts: str, raw_body: bytes) -> str:
    mac = hmac.new(config.HMAC_SECRET.encode(), (ts + ".").encode() + raw_body, hashlib.sha256)
    return mac.hexdigest()


def verify(ts: str, sig: str, raw_body: bytes) -> bool:
    if not config.HMAC_SECRET:
        return False
    try:
        if abs(time.time() - float(ts)) > _TS_WINDOW:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(ts, raw_body), sig or "")


# ---------- 纯逻辑处理器 ----------

def handle_ingest(conn, payload: dict) -> tuple[int, str]:
    text = (payload.get("text") or "").strip()
    chat_id = payload.get("chat_id") or ""
    sender_id = payload.get("sender_id") or ""
    try:
        aweme_id = douyin.resolve_aweme_id(text)
    except douyin.DownloadError as e:
        return 200, f"无法解析这个抖音链接：{e}"
    existing = db.get_task(conn, aweme_id)
    if existing:
        return 200, f"这条视频已在处理/已完成（当前状态：{existing['status']}）"
    if db.count_active(conn) >= config.MAX_QUEUE:
        return 200, "队列已满，请稍后再发。"
    try:
        detail = douyin.fetch_detail(aweme_id)
        title = douyin.meta_from_detail(detail)["title"]
    except douyin.DownloadError:
        title = ""
    db.insert_task(conn, aweme_id=aweme_id, channel=pipeline.CHANNEL, raw_link=text,
                   chat_id=chat_id, sender_id=sender_id, title=title)
    return 200, f"已收到，正在处理：{title[:30] or aweme_id}。处理完会把结果发给你。"


def handle_board(conn, payload: dict) -> tuple[int, str]:
    md = pipeline.render_board(conn)
    return 200, md


def handle_confirm(conn, payload: dict) -> tuple[int, str]:
    sender_id = payload.get("sender_id") or ""
    if not config.ADMIN_SENDER_ID or sender_id != config.ADMIN_SENDER_ID:
        return 200, "无权确认（仅管理员）。"
    text = (payload.get("text") or "").strip()
    parts = text.split()
    video_id = parts[-1] if parts else ""
    if not video_id:
        return 200, "用法：vr确认 <video_id>"
    from . import lock
    with lock.publish_lock:  # C8 publish_confirm：批准与渲染对 worker 原子
        n = db.approve_pending_for_video(conn, video_id, sender_id)
        if n:
            pipeline.render_board(conn)
    return 200, f"已确认 {n} 条待确认记录入榜（{video_id}）。" if n else f"没有可确认的待确认记录（{video_id}）。"


def _thread_state(hs: dict, who: str, now: float) -> tuple[bool, str]:
    """返回 (存活?, 态描述)。在执行（busy_until>now）不因心跳旧判死。"""
    if not hs.get(f"{who}_alive"):
        return False, "not_started"
    busy = hs.get("worker_busy_until", 0.0) if who == "worker" else 0.0
    if busy > now:
        return True, "processing"
    stale = (now - hs.get(f"{who}_last_beat", 0.0)) > config.HEALTH_STALE_BEAT_S
    if stale:
        return False, "stale_beat"
    if who == "worker":
        # 空闲有活：近 60s 有领取尝试
        if (now - hs.get("worker_last_claim_at", 0.0)) <= 60:
            return True, "idle_active"
        return True, "idle"
    return True, "idle"


def handle_healthz(conn, payload: dict) -> tuple[int, str]:
    import shutil
    now = time.time()
    hs = health_snapshot()
    free_gb = shutil.disk_usage(str(config.DATA_DIR)).free / (1024 ** 3)
    outbox = db.outbox_health(conn)

    worker_ok, worker_state = _thread_state(hs, "worker", now)
    outbox_ok, outbox_state = _thread_state(hs, "outbox", now)

    reasons: list[str] = []
    if not worker_ok:
        reasons.append(f"worker:{worker_state}")
    if not outbox_ok:
        reasons.append(f"outbox:{outbox_state}")
    if outbox["undelivered"] and outbox["oldest_age_s"] > config.OUTBOX_STALE_S:
        reasons.append(f"outbox_backlog:{outbox['oldest_age_s']}s")
    if hs.get("db_consec_errors", 0) >= _DB_ERROR_THRESHOLD:
        reasons.append(f"db_errors:{hs['db_consec_errors']}")
    if free_gb < config.MIN_DISK_GB:
        reasons.append(f"low_disk:{free_gb:.1f}GB")
    if hs.get("paused_asr_orphan"):
        reasons.append("paused_asr_orphan")

    ok = not reasons
    health = {
        "ok": ok,
        "worker_state": worker_state,
        "outbox_state": outbox_state,
        "active_tasks": db.count_active(conn),
        "disk_free_gb": round(free_gb, 1),
        "outbox": outbox,
        "db_consec_errors": hs.get("db_consec_errors", 0),
        "reasons": reasons,
    }
    return (200 if ok else 503), json.dumps(health, ensure_ascii=False)


_ROUTES = {
    "/ingest": handle_ingest,
    "/board": handle_board,
    "/confirm": handle_confirm,
    "/healthz": handle_healthz,
}


# ---------- HTTP 层 ----------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音默认日志
        pass

    def _send(self, code: int, reply: str):
        body = json.dumps({"ok": code == 200, "reply_text": reply}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        handler = _ROUTES.get(self.path)
        if not handler:
            self._send(404, "not found")
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        # healthz 免签方便本地探活；其余强制 HMAC
        if self.path != "/healthz":
            ts = self.headers.get("X-Timestamp", "")
            sig = self.headers.get("X-Signature", "")
            if not verify(ts, sig, raw):
                self._send(401, "签名校验失败")
                return
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._send(400, "bad json")
            return
        conn = db.connect(db_path())
        try:
            code, reply = handler(conn, payload)
        except Exception as e:  # noqa: BLE001 兜底：handler 任何异常都不许崩连接
            code, reply = 500, f"内部错误：{str(e)[:100]}"
        finally:
            conn.close()
        self._send(code, reply)


# ---------- 后台线程 ----------

def _safe_close(conn) -> None:
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass
    try:
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def worker_loop(stop: threading.Event, idle_s: float = 2.0) -> None:
    """C7 三分支：无领取 / 已领取失败 / 初连或领取时连接失败（旧连接 rollback+close 重连）。
    线程内整循环守护——任何异常不逸出（否则线程静默死、healthz 恒 ok）。"""
    conn = None
    try:
        while not stop.is_set():
            _health_beat("worker")
            if conn is None:  # 分支3a：（重）建连接
                try:
                    conn = db.connect(db_path())
                    _db_ok()
                except Exception:  # noqa: BLE001
                    _db_error()
                    stop.wait(idle_s)
                    continue
            _health_set(worker_last_claim_at=time.time())
            try:
                task = db.claim_next(conn)
            except Exception:  # noqa: BLE001 分支3b：领取时连接/锁错误 → 弃连重连
                _db_error()
                _safe_close(conn)
                conn = None
                stop.wait(idle_s)
                continue
            if task is None:  # 分支1：无可领取
                _health_set(worker_busy_until=0.0)
                _db_ok()
                stop.wait(idle_s)
                continue
            # 分支2：已领取，处理（process_task 内部把失败转 retry/terminal）
            _health_set(worker_busy_until=time.time() + config.TASK_BUDGET_S)
            try:
                pipeline.process_task(conn, task)
                _db_ok()
            except Exception as e:  # noqa: BLE001 兜底：process 未自处理的异常
                try:
                    conn.rollback()
                    db.mark_retry_or_terminal(conn, task["aweme_id"], f"worker: {e}"[:300],
                                              chat_id=task["chat_id"] or None)
                    _db_ok()
                except Exception:  # noqa: BLE001 连 finalize 都失败 → 弃连重连
                    _db_error()
                    _safe_close(conn)
                    conn = None
            finally:
                _health_set(worker_busy_until=0.0)
    finally:
        _safe_close(conn)
        _health_set(worker_alive=False)


def outbox_loop(stop: threading.Event, idle_s: float = 3.0) -> None:
    """同构守护（C7）：连接错误弃连重连；投递失败按 outbox 退避，不套任务 finalize。"""
    from . import feishu
    conn = None
    try:
        while not stop.is_set():
            _health_beat("outbox")
            if conn is None:
                try:
                    conn = db.connect(db_path())
                    _db_ok()
                except Exception:  # noqa: BLE001
                    _db_error()
                    stop.wait(idle_s)
                    continue
            try:
                due = db.claim_due_notifications(conn)
            except Exception:  # noqa: BLE001
                _db_error()
                _safe_close(conn)
                conn = None
                stop.wait(idle_s)
                continue
            if not due:
                _db_ok()
                stop.wait(idle_s)
                continue
            for n in due:
                try:
                    feishu.send_text(n["chat_id"], n["content"])
                    db.mark_notification_sent(conn, n["id"])
                except Exception as e:  # noqa: BLE001 投递失败 → 退避（不动任务）
                    try:
                        db.mark_notification_failed(conn, n["id"], str(e))
                    except Exception:  # noqa: BLE001
                        _db_error()
                        _safe_close(conn)
                        conn = None
                        break
    finally:
        _safe_close(conn)
        _health_set(outbox_alive=False)


def serve() -> None:
    from . import lock
    # C6 顺序：flock（最先，独占执行权）→ init → recover → expire → bind
    dirlock = lock.DataDirLock(config.DATA_DIR)
    if not dirlock.acquire(blocking=False):
        raise SystemExit(f"另一个 vreader 实例已在运行（{config.DATA_DIR}/.vreader.lock），拒绝启动")
    db.init(db_path())
    conn = db.connect(db_path())
    try:
        recovered = db.recover_nonterminal(conn)
        db.expire_old_pending(conn, config.PENDING_EXPIRE_DAYS * 86400)
    finally:
        conn.close()
    stop = threading.Event()
    threading.Thread(target=worker_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=outbox_loop, args=(stop,), daemon=True).start()
    httpd = ThreadingHTTPServer((config.HOST, config.PORT), _Handler)
    print(f"vreader serving on {config.HOST}:{config.PORT}, recovered={recovered}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop.set()
        httpd.shutdown()
    finally:
        dirlock.release()


if __name__ == "__main__":
    serve()
