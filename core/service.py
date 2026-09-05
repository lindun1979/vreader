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
    n = db.approve_pending_for_video(conn, video_id, sender_id)
    if n:
        pipeline.render_board(conn)
    return 200, f"已确认 {n} 条待确认记录入榜（{video_id}）。" if n else f"没有可确认的待确认记录（{video_id}）。"


def handle_healthz(conn, payload: dict) -> tuple[int, str]:
    import shutil
    free_gb = shutil.disk_usage(str(config.DATA_DIR)).free / (1024 ** 3)
    health = {
        "ok": True,
        "active_tasks": db.count_active(conn),
        "disk_free_gb": round(free_gb, 1),
        "outbox": db.outbox_health(conn),
    }
    return 200, json.dumps(health, ensure_ascii=False)


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

def worker_loop(stop: threading.Event, idle_s: float = 2.0) -> None:
    conn = db.connect(db_path())
    try:
        while not stop.is_set():
            task = db.claim_next(conn)
            if task is None:
                stop.wait(idle_s)
                continue
            try:
                pipeline.process_task(conn, task)
            except Exception as e:  # noqa: BLE001 兜底，避免 worker 崩
                db.mark_retry_or_terminal(conn, task["aweme_id"], f"worker: {e}"[:300])
    finally:
        conn.close()


def outbox_loop(stop: threading.Event, idle_s: float = 3.0) -> None:
    from . import feishu
    conn = db.connect(db_path())
    try:
        while not stop.is_set():
            due = db.claim_due_notifications(conn)
            if not due:
                stop.wait(idle_s)
                continue
            for n in due:
                try:
                    feishu.send_text(n["chat_id"], n["content"])
                    db.mark_notification_sent(conn, n["id"])
                except Exception as e:  # noqa: BLE001
                    db.mark_notification_failed(conn, n["id"], str(e))
    finally:
        conn.close()


def serve() -> None:
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


if __name__ == "__main__":
    serve()
