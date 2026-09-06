"""ASR：Gladia 云转写（主）+ 本地 SenseVoiceSmall（funasr，CPU，兜底）。

Gladia：gold 97 格实测 100%（SenseVoice 89.7%），模型名不糊、~11s/条；带
models.yml 热词。失败（网络/额度/超时）自动回落 SenseVoice 分块方案。
SenseVoice：模型进程内单例，非线程安全——本项目 worker 串行（并发=1），单例即可。
不支持 hotword，术语纠错在提取层做。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

_MODEL_NAME = "iic/SenseVoiceSmall"
_GLADIA_MODEL_ID = "gladia-v2"
_SV_MODEL_ID = "SenseVoiceSmall"
_model = None
_last_engine = _SV_MODEL_ID  # 最近一次实际转写用的引擎（worker 串行，全局即可）


def model_id() -> str:
    return _last_engine


# ---------- Gladia ----------

_GLADIA_BASE = "https://api.gladia.io"
_GLADIA_POLL_S = 5
_GLADIA_DEADLINE_S = 900


class GladiaError(Exception):
    pass


def _gladia_api(url: str, key: str, data: dict | None = None) -> dict:
    headers = {"x-gladia-key": key}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url if url.startswith("http") else _GLADIA_BASE + url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def _gladia_upload(mp3_path: str, key: str) -> str:
    boundary = uuid.uuid4().hex
    raw = Path(mp3_path).read_bytes()
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; "
            f"filename=\"audio.mp3\"\r\nContent-Type: audio/mpeg\r\n\r\n").encode() \
        + raw + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(_GLADIA_BASE + "/v2/upload", data=body, headers={
        "x-gladia-key": key,
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["audio_url"]


def _vocabulary() -> list[str]:
    """热词：models.yml 规范名 + 等级词（提升模型名/判决段识别）。"""
    from . import extract
    return [*extract._load_models().keys(), "青铜", "白银", "黄金", "钻石", "王者"]


def _transcribe_gladia(video_path: str, key: str) -> str:
    """视频→mp3→上传→pre-recorded→轮询。任何失败抛 GladiaError 由上层兜底。"""
    from . import config
    mp3 = video_path + ".gladia.mp3"
    try:
        subprocess.run(
            [config.FFMPEG_BIN, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
             "-b:a", "64k", mp3],
            check=True, capture_output=True)
        audio_url = _gladia_upload(mp3, key)
    except Exception as e:
        raise GladiaError(f"上传失败: {e}") from e
    finally:
        Path(mp3).unlink(missing_ok=True)
    try:
        job = _gladia_api("/v2/pre-recorded", key, {
            "audio_url": audio_url,
            "language_config": {"languages": ["zh"], "code_switching": False},
            "detect_language": False,
            "custom_vocabulary": True,
            "custom_vocabulary_config": {"vocabulary": _vocabulary()},
        })
        deadline = time.time() + _GLADIA_DEADLINE_S
        while time.time() < deadline:
            time.sleep(_GLADIA_POLL_S)
            r = _gladia_api(job["result_url"], key)
            if r["status"] == "done":
                tr = r["result"]["transcription"]
                lines = [u["text"] for u in tr.get("utterances", [])]
                text = "\n".join(lines) if lines else (tr.get("full_transcript") or "")
                if not text.strip():
                    raise GladiaError("返回空转写")
                return text
            if r["status"] == "error":
                raise GladiaError(f"任务失败: {json.dumps(r)[:300]}")
        raise GladiaError(f"轮询超时 {_GLADIA_DEADLINE_S}s")
    except GladiaError:
        raise
    except Exception as e:
        raise GladiaError(f"转写失败: {e}") from e


def _load():
    global _model
    if _model is None:
        from funasr import AutoModel
        _model = AutoModel(model=_MODEL_NAME, device="cpu", disable_update=True)
    return _model


# 分块时长（秒）：长视频整段喂入 funasr 会占用大量内存（实测 16min 视频峰值
# 10GB+，拖垮 16GB 生产机）。按此窗口切块逐块转写、拼接，把内存限制在单块。
_CHUNK_S = 300


def extract_wav(video_path: str, wav_path: str) -> str:
    """ffmpeg 抽 16k 单声道 wav。已存在则跳过。"""
    if Path(wav_path).exists() and Path(wav_path).stat().st_size > 0:
        return wav_path
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
    from . import config
    subprocess.run(
        [config.FFMPEG_BIN, "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", wav_path],
        check=True, capture_output=True)
    return wav_path


def _wav_duration(wav_path: str) -> float:
    import wave
    with wave.open(wav_path, "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def _transcribe_file(wav_path: str) -> str:
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    m = _load()
    res = m.generate(input=wav_path, cache={}, language="auto", use_itn=True,
                     batch_size_s=60, merge_vad=True, merge_length_s=15)
    return rich_transcription_postprocess(res[0]["text"] if res else "")


def transcribe(wav_path: str) -> str:
    """转写音频，返回纯文本。长音频按 _CHUNK_S 分块逐块转写（限内存）。"""
    import subprocess
    import tempfile
    from . import config
    dur = _wav_duration(wav_path)
    if dur <= _CHUNK_S:
        return _transcribe_file(wav_path)
    parts: list[str] = []
    tmpdir = tempfile.mkdtemp(prefix="vr_asr_")
    try:
        offset = 0.0
        while offset < dur:
            chunk = f"{tmpdir}/chunk.wav"
            subprocess.run(
                [config.FFMPEG_BIN, "-y", "-ss", str(offset), "-t", str(_CHUNK_S),
                 "-i", wav_path, "-ac", "1", "-ar", "16000", "-f", "wav", chunk],
                check=True, capture_output=True)
            parts.append(_transcribe_file(chunk))
            offset += _CHUNK_S
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return "".join(parts)


def video_to_transcript(video_path: str, wav_path: str, transcript_path: str) -> str:
    """完整链路：视频→转写→落盘。Gladia 主（配了 key），失败回落 SenseVoice。
    transcript 已存在则直接读回（幂等）。"""
    global _last_engine
    if Path(transcript_path).exists() and Path(transcript_path).stat().st_size > 0:
        return Path(transcript_path).read_text(encoding="utf-8")
    from . import config
    text = None
    if config.GLADIA_API_KEY:
        try:
            text = _transcribe_gladia(video_path, config.GLADIA_API_KEY)
            _last_engine = _GLADIA_MODEL_ID
        except GladiaError as e:
            print(f"[asr] Gladia 失败，回落 SenseVoice: {e}", file=sys.stderr, flush=True)
    if text is None:
        extract_wav(video_path, wav_path)
        text = transcribe(wav_path)
        _last_engine = _SV_MODEL_ID
    Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
    Path(transcript_path).write_text(text, encoding="utf-8")
    return text
