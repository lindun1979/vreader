"""worker 编排：claim → download → asr → extract → decisions → render → notify。

各阶段先查落盘产物决定跳过（幂等恢复）。失败落 retryable/terminal，终态写通知
outbox（与状态同事务）。成功后删媒体、留 transcript+extract。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config, db, douyin, extract as extract_mod

CHANNEL = "token_bug"


def _paths(aweme_id: str) -> dict:
    d = config.video_dir(CHANNEL, aweme_id)
    return {
        "dir": d,
        "video": str(d / "video.mp4"),
        "wav": str(d / "audio.wav"),
        "transcript": str(d / "transcript.txt"),
        "extract": str(d / "extract.json"),
    }


def _enqueue_and_status(conn, aweme_id: str, status: str, chat_id: str,
                        content: str, *, error: str | None = None) -> None:
    """终态状态更新 + 通知写 outbox，同一事务提交（先崩溃也不丢通知）。"""
    now = time.time()
    conn.execute("UPDATE tasks SET status=?, error=?, updated_at=? WHERE aweme_id=?",
                 (status, error, now, aweme_id))
    if chat_id:
        db.enqueue_notification(conn, chat_id, content, commit=False)
    conn.commit()


def _disk_free_gb() -> float:
    usage = shutil.disk_usage(str(config.DATA_DIR))
    return usage.free / (1024 ** 3)


def _cleanup_media(p: dict) -> None:
    for key in ("video", "wav"):
        try:
            Path(p[key]).unlink(missing_ok=True)
        except OSError:
            pass


def process_task(conn, task) -> str:
    """处理一个任务，返回落定状态。异常内部转 retryable/terminal。"""
    aweme_id = task["aweme_id"]
    chat_id = task["chat_id"] or ""
    p = _paths(aweme_id)
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    try:
        # 1. download
        db.set_status(conn, aweme_id, db.DOWNLOADING)
        detail = douyin.fetch_detail(aweme_id)
        meta = douyin.meta_from_detail(detail)
        if meta["duration_s"] > config.MAX_DURATION_S:
            raise _Terminal(f"视频过长 {meta['duration_s']}s > {config.MAX_DURATION_S}s")
        dl = douyin.download(aweme_id, p["video"], detail=detail)
        if dl.get("bytes", 0) > config.MAX_BYTES:
            raise _Terminal(f"视频过大 {dl['bytes']} 字节")

        # 2. transcribe
        db.set_status(conn, aweme_id, db.TRANSCRIBING)
        from . import asr
        transcript = asr.video_to_transcript(p["video"], p["wav"], p["transcript"])

        # 3. extract + schema/evidence 校验
        db.set_status(conn, aweme_id, db.EXTRACTING)
        if Path(p["extract"]).exists() and Path(p["extract"]).stat().st_size > 0:
            ex = json.loads(Path(p["extract"]).read_text(encoding="utf-8"))
        else:
            ex = extract_mod.build_extract(aweme_id=aweme_id, title=meta["title"],
                                           transcript=transcript)
            Path(p["extract"]).write_text(json.dumps(ex, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        # decisions（幂等重算，写文件后崩溃可从此重入）
        counts = extract_mod.apply_decisions(conn, aweme_id, ex)

        # 4. render board
        db.set_status(conn, aweme_id, db.RENDERING)
        render_board(conn)

        # 5. success（终态+通知同事务）+ 清理媒体
        n_rec = len(ex["records"])
        msg = (f"✅ 已处理：{meta['title'][:30]}\n"
               f"提取 {n_rec} 条记录（自动上榜 {counts['auto_ok']}，"
               f"待确认 {counts['pending']}）")
        _enqueue_and_status(conn, aweme_id, db.SUCCEEDED, chat_id, msg)
        _cleanup_media(p)
        return db.SUCCEEDED

    except _Terminal as e:
        _cleanup_media(p)
        _enqueue_and_status(conn, aweme_id, db.TERMINAL_FAILED, chat_id,
                            f"❌ 处理失败（不再重试）：{e}", error=str(e))
        return db.TERMINAL_FAILED
    except Exception as e:  # noqa: BLE001
        status = db.mark_retry_or_terminal(conn, aweme_id, str(e)[:300])
        if status == db.TERMINAL_FAILED:
            _cleanup_media(p)
            if chat_id:
                db.enqueue_notification(conn, chat_id, f"❌ 处理失败（已重试耗尽）：{str(e)[:120]}")
        return status


class _Terminal(Exception):
    """不可重试的失败（超限等）。"""


def render_board(conn) -> str:
    from channels.token_bug import board
    ch_dir = config.channel_dir(CHANNEL)
    extracts = board.load_extracts(ch_dir)
    visible = db.board_visible_ids(conn)
    md = board.render(extracts, visible, extract_mod.record_id)
    out = ch_dir / "board.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    return md
