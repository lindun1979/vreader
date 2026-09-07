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

_last_disk_alert = 0.0
_DISK_ALERT_INTERVAL = 3600  # 低磁盘告警限频：每小时最多一次


def _disk_free_gb() -> float:
    import shutil
    return shutil.disk_usage(str(config.DATA_DIR)).free / (1024 ** 3)


def _maybe_disk_alert(conn) -> None:
    global _last_disk_alert
    now = time.time()
    if now - _last_disk_alert < _DISK_ALERT_INTERVAL:
        return
    _last_disk_alert = now
    msg = (f"⚠️ vreader 磁盘不足 {_disk_free_gb():.1f}GB < {config.MIN_DISK_GB}GB，"
           f"已暂停处理并拒收新任务。")
    print(f"[disk] {msg}", flush=True)
    if config.ADMIN_CHAT_ID:
        try:
            db.enqueue_notification(conn, config.ADMIN_CHAT_ID, msg)
        except Exception:  # noqa: BLE001
            pass


def handle_ingest(conn, payload: dict) -> tuple[int, str]:
    text = (payload.get("text") or "").strip()
    chat_id = payload.get("chat_id") or ""
    sender_id = payload.get("sender_id") or ""
    try:  # 2h：resolve 用小超时（ACK 预算内），卡住即快速失败让用户重试
        aweme_id = douyin.resolve_aweme_id(text, timeout=config.ACK_RESOLVE_TIMEOUT_S)
    except douyin.DownloadError as e:
        return 200, f"无法解析这个抖音链接（{e}）。可稍后重发。"
    existing = db.get_task(conn, aweme_id)
    if existing:
        return 200, f"这条视频已在处理/已完成（当前状态：{existing['status']}）"
    if _disk_free_gb() < config.MIN_DISK_GB:  # 磁盘门禁（C4）：不足则拒收新任务
        return 200, "磁盘空间不足，暂时无法接收新任务，请稍后再试。"
    if db.count_active(conn) >= config.MAX_QUEUE:
        return 200, "队列已满，请稍后再发。"
    # 2h：ingest 不做慢网络（不同步取 detail）——title 由 worker 处理时补，ACK 快速返回
    db.insert_task(conn, aweme_id=aweme_id, channel=pipeline.CHANNEL, raw_link=text,
                   chat_id=chat_id, sender_id=sender_id, title="")
    return 200, f"已收到，正在处理：{aweme_id}。处理完会把结果发给你。"


def handle_board(conn, payload: dict) -> tuple[int, str]:
    md = pipeline.render_board(conn)
    return 200, md


def _current_result_rev(aweme_id: str) -> str | None:
    from . import extract as ex_mod
    p = ex_mod._paths_extract(aweme_id)
    if p is None:
        return None
    try:
        return json.loads(open(p, encoding="utf-8").read()).get("result_rev")
    except (OSError, json.JSONDecodeError):
        return None


def handle_confirm(conn, payload: dict) -> tuple[int, str]:
    sender_id = payload.get("sender_id") or ""
    if not config.ADMIN_SENDER_ID or sender_id != config.ADMIN_SENDER_ID:
        return 200, "无权确认（仅管理员）。"
    from . import extract as ex_mod, lock, routing
    video_id, arg = routing.parse_confirm(payload.get("text") or "")
    if not video_id:
        return 200, "用法：vr确认 <video_id> [<记录码>|rev:<版本>]"
    with lock.publish_lock:  # C8 publish_confirm：批准与渲染对 worker 原子（锁内一致快照）
        # 逐条确认冲突组成员（arg 为 rid 短码）
        if arg and not arg.startswith("rev:"):
            ok, msg = ex_mod.confirm_conflict_member(conn, video_id, arg, sender_id)
            if ok:
                pipeline.render_board(conn)
            return 200, msg
        # 绑版本批量确认（arg = rev:<版本>）：版本不符拒绝，防确认过时内容
        if arg and arg.startswith("rev:"):
            want = arg[4:]
            cur = _current_result_rev(video_id)
            if cur and want != cur:
                return 200, f"内容已更新（当前版本 {cur}，你确认的是 {want}）。请先 vr明细 {video_id} 重新查看。"
        # 批量确认 + 新版本同事务登记（M04）
        n, registered = ex_mod.approve_pending_for_video(conn, video_id, sender_id)
        if n:
            pipeline.render_board(conn)
    if not n:
        return 200, f"没有可批量确认的待确认记录（{video_id}）。冲突/未知项需 vr明细 后逐条确认。"
    msg = f"已确认 {n} 条待确认记录入榜（{video_id}）。"
    if registered:
        uniq = sorted(set(registered))
        msg += f"\n已登记新版本：{', '.join(uniq)}（此后这些版本不再因新版本待确认）"
    return 200, msg


_HELP_TEXT = (
    "📖 vreader 用法（token 词源模型实测榜单）\n"
    "· 直接发抖音分享链接 → 自动下载/转写/提取，处理完回执\n"
    "· vr榜单 —— 取回最新「模型 × bug 难度」榜单\n"
    "· vr明细 <video_id> —— 看某视频逐条明细（记录码 + 状态 + 证据）\n"
    "· vr确认 <video_id> —— 批量确认普通/新版本待确认记录入榜（新版本首次确认即登记）\n"
    "· vr确认 <video_id> <记录码> —— 逐条确认矛盾记录\n"
    "· vr确认 <video_id> rev:<版本> —— 绑版本确认（防确认过时内容）\n"
    "· vr帮助 —— 显示本说明")


def handle_help(conn, payload: dict) -> tuple[int, str]:
    return 200, _HELP_TEXT


def handle_detail(conn, payload: dict) -> tuple[int, str]:
    from . import extract as ex_mod, lock, routing
    video_id = routing.parse_detail(payload.get("text") or "")
    if not video_id:
        return 200, "用法：vr明细 <video_id>"
    with lock.publish_lock:  # C8：锁内一致快照
        return 200, ex_mod.detail_view(conn, video_id)


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
    from . import asr  # 懒加载不触发 funasr（顶层无 import funasr）
    asr_since = getattr(asr, "_asr_started_at", None)
    if asr_since and (now - asr_since) > config.ASR_STUCK_S:
        reasons.append(f"asr_stuck:{int(now - asr_since)}s")  # in-process funasr 疑似卡死

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
    "/detail": handle_detail,
    "/confirm": handle_confirm,
    "/help": handle_help,
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
            if _disk_free_gb() < config.MIN_DISK_GB:  # 磁盘门禁：暂停处理（不判死）+ 限频告警
                _maybe_disk_alert(conn)
                _health_set(worker_busy_until=0.0)
                stop.wait(idle_s * 5)
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
                pipeline.sweep_orphan_media(conn)  # 终态后顺带孤儿清扫
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
        swept = pipeline.sweep_orphan_media(conn)  # 启动孤儿媒体清扫（C4）
        if swept:
            print(f"vreader startup: swept {swept} orphan media files")
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
