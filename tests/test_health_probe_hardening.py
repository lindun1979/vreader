"""V-M05a：healthz 三态 + 不健康 503（线程死/outbox 积压/db 连错/低磁盘/孤儿暂停）。

真实本地 HTTP 假服务 + prod.sh 双重判定见 test_prod_probe.py（部署验收 V-M05b 留生产）。
"""
import json
import time

from core import config, db, service


def _reset_health():
    service._health_set(
        worker_alive=False, worker_last_beat=0.0, worker_last_claim_at=0.0,
        worker_busy_until=0.0, outbox_alive=False, outbox_last_beat=0.0,
        db_consec_errors=0, paused_asr_orphan=False)


def _healthy_beat(now):
    service._health_set(
        worker_alive=True, worker_last_beat=now, worker_last_claim_at=now,
        worker_busy_until=0.0, outbox_alive=True, outbox_last_beat=now,
        db_consec_errors=0, paused_asr_orphan=False)


def test_healthy_idle_active(conn):
    _reset_health()
    now = time.time()
    _healthy_beat(now)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 200 and h["ok"] is True
    assert h["worker_state"] == "idle_active"


def test_processing_state_not_dead_despite_stale_beat(conn):
    """在执行长任务时心跳不更新，但 busy_until>now → 判 processing 而非线程死。"""
    _reset_health()
    now = time.time()
    _healthy_beat(now)
    # worker 心跳很旧但正在执行长任务；outbox 保持健康
    service._health_set(worker_last_beat=now - 1000, worker_busy_until=now + 500)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 200 and h["worker_state"] == "processing"


def test_dead_worker_thread_503(conn):
    _reset_health()
    now = time.time()
    _healthy_beat(now)
    service._health_set(worker_last_beat=now - 10000, worker_busy_until=0.0)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 503 and any("worker" in r for r in h["reasons"])


def test_outbox_backlog_503(conn):
    _reset_health()
    now = time.time()
    _healthy_beat(now)
    # 塞一条很老的未送达通知
    db.enqueue_notification(conn, "c", "old")
    conn.execute("UPDATE notification_outbox SET created_at=?",
                 (now - config.OUTBOX_STALE_S - 100,))
    conn.commit()
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 503 and any("outbox_backlog" in r for r in h["reasons"])


def test_db_errors_503(conn):
    _reset_health()
    _healthy_beat(time.time())
    service._health_set(db_consec_errors=service._DB_ERROR_THRESHOLD)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 503 and any("db_errors" in r for r in h["reasons"])


def test_asr_orphan_paused_503(conn):
    _reset_health()
    _healthy_beat(time.time())
    service._health_set(paused_asr_orphan=True)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 503 and "paused_asr_orphan" in h["reasons"]


def test_asr_stuck_detected(conn, monkeypatch):
    """本地 ASR 运行超阈值 → healthz 报 asr_stuck（in-process funasr 卡死可观测）。"""
    import time as _t
    from core import asr, config
    _reset_health()
    _healthy_beat(_t.time())
    monkeypatch.setattr(asr, "_asr_started_at", _t.time() - config.ASR_STUCK_S - 100)
    code, body = service.handle_healthz(conn, {})
    h = json.loads(body)
    assert code == 503 and any("asr_stuck" in r for r in h["reasons"])


def test_asr_normal_not_flagged(conn, monkeypatch):
    import time as _t
    from core import asr
    _reset_health()
    _healthy_beat(_t.time())
    monkeypatch.setattr(asr, "_asr_started_at", _t.time() - 5)  # 刚开始转写
    code, body = service.handle_healthz(conn, {})
    assert code == 200
