"""worker 编排：claim → download → asr → extract → decisions → render → notify。

各阶段先查落盘产物决定跳过（幂等恢复）。失败落 retryable/terminal，终态写通知
outbox（与状态同事务）。成功后删媒体、留 transcript+extract。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import config, db, douyin, extract as extract_mod, lock, util

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

        # 3. extract + schema/evidence 校验（缓存读回也过 validate，坏则留证重建 C3）
        db.set_status(conn, aweme_id, db.EXTRACTING)
        ex = extract_mod.load_valid_extract(p["extract"], transcript=transcript,
                                            expected_video_id=aweme_id)
        if ex is None:
            ex = extract_mod.build_extract(aweme_id=aweme_id, title=meta["title"],
                                           transcript=transcript)
            util.atomic_write_text(p["extract"],
                                   json.dumps(ex, ensure_ascii=False, indent=2))
        # decisions（幂等重算，写文件后崩溃可从此重入）+ render，同持发布锁保证一致
        with lock.publish_lock:  # C8 publish_extract：决策写入与渲染对 confirm 原子
            counts = extract_mod.apply_decisions(conn, aweme_id, ex)
            db.set_status(conn, aweme_id, db.RENDERING)
            render_board(conn)

        # 5. success（终态+通知同事务）+ 清理媒体
        n_rec = len(ex["records"])
        msg = (f"✅ 已处理：{meta['title'][:30]}\n"
               f"提取 {n_rec} 条记录（自动上榜 {counts['auto_ok']}，"
               f"待确认 {counts['pending']}）")
        # Gladia 配了却没用上 = 静默降级，回执里显式可见（额度/网络问题别只躺日志）
        if config.GLADIA_API_KEY and ex.get("asr_model") != "gladia-v2":
            msg += f"\n⚠️ ASR 走了兜底 {ex.get('asr_model')}（Gladia 未生效，查额度/err.log）"
        db.finalize_task(conn, aweme_id, db.SUCCEEDED, chat_id=chat_id, content=msg)
        _cleanup_media(p)
        return db.SUCCEEDED

    except _Terminal as e:
        conn.rollback()  # C1：清掉任何半完成事务再走终态
        db.finalize_task(conn, aweme_id, db.TERMINAL_FAILED, error=str(e)[:300],
                         chat_id=chat_id, content=f"❌ 处理失败（不再重试）：{e}")
        _cleanup_media(p)
        return db.TERMINAL_FAILED
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        status = db.mark_retry_or_terminal(conn, aweme_id, str(e)[:300], chat_id=chat_id)
        if status == db.TERMINAL_FAILED:
            _cleanup_media(p)
        return status


class _Terminal(Exception):
    """不可重试的失败（超限等）。"""


def render_board(conn) -> str:
    """C8 publish_render：读产物→校验→渲染→原子写，全程持发布锁（防并发旧覆盖新）。"""
    from channels.token_bug import board
    ch_dir = config.channel_dir(CHANNEL)
    with lock.publish_lock:
        extracts = []
        for ex in board.load_extracts(ch_dir):
            try:  # 单个坏 extract.json 不放倒榜单（cf5 #5）：schema 层校验跳过
                extract_mod.validate_extract(ex)
                extracts.append(ex)
            except extract_mod.ExtractError:
                continue
        visible = db.board_visible_ids(conn)
        md = board.render(extracts, visible, extract_mod.record_id)
        util.atomic_write_text(ch_dir / "board.md", md)
    return md
