#!/usr/bin/env python3
"""Gemini Live 转写（gemini-3.5-transcribe-live）：切段并发 + 近实时喂 + 接缝去重。

背景与完整评测见 `docs/asr-gemini-transcribe-vs-gladia-eval.md`。要点：
  - Live 无 RPM/RPD 限（仅 20K TPM），故用「切 N 段 / N 路并发 / 各段 1x 实时喂」换速度
    （~4.5x 实时），下游 gold 准确率与 Gladia 打平。
  - 三个坑（本脚本已处理）：① 段边界须**整秒对齐**（否则落奇数字节=半个 16bit 采样，服务端
    报 1007 invalid argument）；② 喂得比服务端快会触发突发限 1011 → 按 1x 实时喂；
    ③ 段间 ±overlap 秒重叠防边界截断，末尾用最长公共块去重拼接（C 方案）。

凭据：环境变量 `GEMINI_API_KEY`（裸 Gemini key；生产机存 ~/workspace/res/geminikey.md）。
连通：dev box(GCP) 直连 Google 可用；**生产机不通 Google**，wss 需经代理——本脚本暂未内建
  wss 代理（websockets 不自动读 HTTPS_PROXY），生产使用需另加 ProxyCommand/隧道，见文档。

用法：
  GEMINI_API_KEY=... python ops/live_transcribe.py <audio 文件> [--out FILE]
      [--concurrency 5] [--overlap 10] [--model models/gemini-3.5-transcribe-live]
  # audio 任意 ffmpeg 可读格式；--out 缺省打印到 stdout。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import time
import difflib

import websockets

_URL_TMPL = ("wss://generativelanguage.googleapis.com/ws/"
             "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key={key}")
_MODEL = "models/gemini-3.5-transcribe-live"
_FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
BPS = 32000  # 16000 Hz * 2 bytes（16bit 单声道）


def _pcm16k(audio_path: str) -> bytes:
    """任意音频 → 16kHz 单声道 s16le 裸 PCM。"""
    return subprocess.run(
        [_FFMPEG, "-i", audio_path, "-f", "s16le", "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", "-"],
        capture_output=True, check=True).stdout


def stitch_overlap(seg_texts: list[str], m: int = 600, minblk: int = 8) -> str:
    """C 方案接缝去重：相邻段在「前段尾 m 字 × 后段头」找最长公共块，裁掉后段头部重叠再拼。
    对 ASR 模糊差异稳健、保内容。"""
    segs = [s for s in seg_texts if s]
    if not segs:
        return ""
    merged = segs[0]
    for nxt in segs[1:]:
        sm = difflib.SequenceMatcher(None, merged[-m:], nxt[:m], autojunk=False)
        cut = max([0] + [b.b + b.size for b in sm.get_matching_blocks() if b.size >= minblk])
        merged += nxt[cut:]
    return merged


async def _transcribe_seg(pcm: bytes, url: str, model: str, idx: int,
                          s0: float, s1: float, *, speed: float = 1.0,
                          attempts: int = 5) -> tuple[int, str]:
    """转写 [s0, s1) 秒。边界整秒对齐（避 1007）；1x 实时喂（避 1011）；空/错重试（新连接）。"""
    b0 = int(round(s0)) * BPS
    b1 = min(len(pcm), int(round(s1)) * BPS)
    await asyncio.sleep(idx * 1.5)  # 错峰建连
    for att in range(attempts):
        finals: list[str] = []
        last = ""
        try:
            async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
                await ws.send(json.dumps({"setup": {"model": model}}))
                await ws.recv()  # setupComplete
                done = {"v": False}

                async def feeder():
                    i = b0
                    while i < b1:
                        await ws.send(json.dumps({"realtimeInput": {"audio": {
                            "data": base64.b64encode(pcm[i:i + BPS]).decode(),
                            "mimeType": "audio/pcm;rate=16000"}}}))
                        i += BPS
                        await asyncio.sleep(1.0 / speed)  # 1x 实时
                    await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
                    done["v"] = True

                ft = asyncio.create_task(feeder())
                while True:
                    try:
                        r = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        if done["v"]:
                            break
                        continue
                    sc = json.loads(r).get("serverContent", {})
                    if "inputTranscription" in sc:
                        finals.append(sc["inputTranscription"].get("text", ""))
                    if "interimInputTranscription" in sc:
                        last = sc["interimInputTranscription"].get("text", "")
                    if sc.get("generationComplete") and done["v"]:
                        break
                ft.cancel()
            txt = "".join(finals) or last
            if txt.strip():
                return idx, txt
            print(f"[seg{idx}] att{att} 空结果，重试", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[seg{idx}] att{att} err {repr(e)[:80]}", file=sys.stderr, flush=True)
        await asyncio.sleep(6)
    print(f"[seg{idx}] 全部重试失败", file=sys.stderr, flush=True)
    return idx, ""


async def _transcribe_async(pcm: bytes, *, key: str, model: str,
                            concurrency: int, overlap_s: float) -> str:
    total_s = len(pcm) / BPS
    seg = total_s / concurrency
    url = _URL_TMPL.format(key=key)
    tasks = []
    for i in range(concurrency):
        s0 = max(0.0, i * seg - overlap_s)
        s1 = min(total_s, (i + 1) * seg + overlap_s)
        tasks.append(_transcribe_seg(pcm, url, model, i, s0, s1))
    res = sorted(await asyncio.gather(*tasks))
    return stitch_overlap([t for _, t in res])


def transcribe(audio_path: str, *, key: str | None = None, model: str = _MODEL,
               concurrency: int = 5, overlap_s: float = 10.0) -> str:
    """把 audio 文件转写为整段文本（并发 Live + C 去重）。可作库函数调用。"""
    key = key or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("缺 GEMINI_API_KEY（裸 Gemini key）")
    pcm = _pcm16k(audio_path)
    return asyncio.run(_transcribe_async(
        pcm, key=key, model=model, concurrency=concurrency, overlap_s=overlap_s))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Gemini Live 并发转写 + 接缝去重")
    ap.add_argument("audio", help="音频/视频文件（ffmpeg 可读）")
    ap.add_argument("--out", help="输出文本文件（缺省打印到 stdout）")
    ap.add_argument("--concurrency", type=int, default=5, help="并发段数（默认 5；5×1x≈9.6K<20K TPM）")
    ap.add_argument("--overlap", type=float, default=10.0, help="段间重叠秒数（默认 10）")
    ap.add_argument("--model", default=_MODEL)
    a = ap.parse_args(argv)
    t0 = time.time()
    txt = transcribe(a.audio, model=a.model, concurrency=a.concurrency, overlap_s=a.overlap)
    dur = subprocess.run(
        [_FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "error", "-show_entries",
         "format=duration", "-of", "csv=p=0", a.audio],
        capture_output=True, text=True).stdout.strip()
    wall = time.time() - t0
    spd = (float(dur) / wall) if dur else 0.0
    print(f"[live_transcribe] 墙钟 {wall:.0f}s (~{spd:.1f}x 实时) {len(txt)} 字",
          file=sys.stderr, flush=True)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"已写 {a.out}", file=sys.stderr)
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
