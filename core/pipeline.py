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


# 媒体产物文件名模式（终态后删除；transcript/extract 保留）
_MEDIA_GLOBS = ("video.mp4", "audio.wav", "*.part", "*.gladia.mp3")


def _cleanup_media(p: dict) -> None:
    """删该视频目录下所有媒体类文件（含 .part / .gladia.mp3 临时件，glob 覆盖孤儿）。"""
    d = Path(p["dir"])
    for pat in _MEDIA_GLOBS:
        for f in d.glob(pat):
            try:
                f.unlink()
            except OSError:
                pass


def sweep_orphan_media(conn) -> int:
    """启动/终态后孤儿清扫（C4）：扫频道目录，终态任务或无主目录的媒体文件按同规则删；
    非终态任务（可能待恢复）的媒体保留。返回删除的文件数。"""
    ch_dir = config.channel_dir(CHANNEL)
    if not ch_dir.exists():
        return 0
    removed = 0
    for vdir in ch_dir.iterdir():
        if not vdir.is_dir():
            continue
        task = db.get_task(conn, vdir.name)
        if task is not None and task["status"] not in db.TERMINAL:
            continue  # 待恢复任务的媒体保留
        for pat in _MEDIA_GLOBS:
            for f in vdir.glob(pat):
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    return removed


def _ensure_video(aweme_id: str, p: dict, task) -> str:
    """确保 video.mp4 在盘（已存在跳过下载）。返回标题。
    video 已在盘时 detail 失败不致命（title 退回 task.title）——避免恢复反向依赖上游。"""
    import os
    have_video = os.path.exists(p["video"]) and os.path.getsize(p["video"]) > 0
    if have_video:
        if os.path.getsize(p["video"]) > config.MAX_BYTES:
            raise _Terminal(f"视频过大 {os.path.getsize(p['video'])} 字节")
        try:
            meta = douyin.meta_from_detail(douyin.fetch_detail(aweme_id))
            return meta.get("title") or task["title"] or ""
        except douyin.DownloadError:
            return task["title"] or ""
    detail = douyin.fetch_detail(aweme_id)
    meta = douyin.meta_from_detail(detail)
    if meta["duration_s"] > config.MAX_DURATION_S:
        raise _Terminal(f"视频过长 {meta['duration_s']}s > {config.MAX_DURATION_S}s")
    dl = douyin.download(aweme_id, p["video"], detail=detail)
    if dl.get("bytes", 0) > config.MAX_BYTES:
        raise _Terminal(f"视频过大 {dl['bytes']} 字节")
    return meta.get("title") or task["title"] or ""


def _read_if_present(path: str) -> str | None:
    pp = Path(path)
    if pp.exists() and pp.stat().st_size > 0:
        return pp.read_text(encoding="utf-8")
    return None


def _finish(conn, aweme_id: str, chat_id: str, ex: dict, p: dict) -> str:
    """决策重判 + 渲染 + 终态成功 + 清理媒体（恢复捷径与正常路径共用）。"""
    with lock.publish_lock:  # C8 publish_extract：决策写入与渲染对 confirm 原子
        counts = extract_mod.apply_decisions(conn, aweme_id, ex)
        db.set_status(conn, aweme_id, db.RENDERING)
        render_board(conn)
    title = (ex.get("title") or "")[:30]
    rev = ex.get("result_rev", "?")
    if ex.get("no_content"):
        msg = (f"ℹ️ 已处理：{title}（{aweme_id}）\n"
               f"未发现可提取的模型对战内容（丢弃 {ex.get('dropped_count', 0)} 条不合规记录）。")
    else:
        msg = (f"✅ 已处理：{title}（{aweme_id}，rev {rev}）\n"
               f"提取 {len(ex['records'])} 条记录（自动上榜 {counts['auto_ok']}，"
               f"待确认 {counts['pending']}）")
        if counts["pending"]:
            msg += (f"\n👉 查看明细：vr明细 {aweme_id}"
                    f"\n👉 批量确认：vr确认 {aweme_id}")
    if counts.get("approved_stale"):
        msg += f"\n⚠️ {counts['approved_stale']} 条此前已确认的记录本次消失（已下榜）。"
    if config.GLADIA_API_KEY and ex.get("asr_model") != "gladia-v2":
        msg += f"\n⚠️ ASR 走了兜底 {ex.get('asr_model')}（Gladia 未生效，查额度/err.log）"
    db.finalize_task(conn, aweme_id, db.SUCCEEDED, chat_id=chat_id, content=msg)
    _cleanup_media(p)
    return db.SUCCEEDED


def process_task(conn, task) -> str:
    """处理一个任务，返回落定状态。异常内部转 retryable/terminal。

    恢复起点按产物前推（C3/2j）：产物齐全（transcript + 有效 extract）时跳过上游
    网络/下载/ASR，直接决策+渲染——不因抖音 detail API 临时不通而卡死齐全的任务
    （codex R09 反向依赖上游）。"""
    aweme_id = task["aweme_id"]
    chat_id = task["chat_id"] or ""
    p = _paths(aweme_id)
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    try:
        # 恢复捷径：先看最下游产物
        transcript = _read_if_present(p["transcript"])
        if transcript is not None:
            db.set_status(conn, aweme_id, db.EXTRACTING)
            ex = extract_mod.load_valid_extract(p["extract"], transcript=transcript,
                                                expected_video_id=aweme_id)
            if ex is not None:
                return _finish(conn, aweme_id, chat_id, ex, p)

        # 需要 transcript：确保有 video（已在盘则跳过下载，detail 失败不致命）
        if transcript is None:
            db.set_status(conn, aweme_id, db.DOWNLOADING)
            title = _ensure_video(aweme_id, p, task)
            db.set_status(conn, aweme_id, db.TRANSCRIBING)
            from . import asr
            transcript = asr.video_to_transcript(p["video"], p["wav"], p["transcript"])
        else:
            title = task["title"] or ""

        # extract（缓存读回过 validate，坏则留证重建 C3）
        db.set_status(conn, aweme_id, db.EXTRACTING)
        ex = extract_mod.load_valid_extract(p["extract"], transcript=transcript,
                                            expected_video_id=aweme_id)
        if ex is None:
            ex = extract_mod.build_extract(aweme_id=aweme_id, title=title,
                                           transcript=transcript)
            util.atomic_write_text(p["extract"],
                                   json.dumps(ex, ensure_ascii=False, indent=2))
        return _finish(conn, aweme_id, chat_id, ex, p)

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


def reprocess(conn, aweme_id: str) -> str:
    """2g：用现有 transcript 以当前 prompt/别名表重跑提取+决策+渲染（不重开裁决——
    apply_decisions 凭指纹继承 approved/rejected_conflict）。需先取数据目录 flock（调用方）。"""
    p = _paths(aweme_id)
    transcript = _read_if_present(p["transcript"])
    if transcript is None:
        raise extract_mod.ExtractError(f"无 transcript，无法重跑：{aweme_id}")
    task = db.get_task(conn, aweme_id)
    title = (task["title"] if task else "") or ""
    ex = extract_mod.build_extract(aweme_id=aweme_id, title=title, transcript=transcript)
    util.atomic_write_text(p["extract"], json.dumps(ex, ensure_ascii=False, indent=2))
    with lock.publish_lock:
        counts = extract_mod.apply_decisions(conn, aweme_id, ex)
        render_board(conn)
    return (f"reprocess {aweme_id}: auto_ok={counts['auto_ok']} pending={counts['pending']} "
            f"rev={ex.get('result_rev')}")


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
