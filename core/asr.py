"""本地 ASR：SenseVoiceSmall（funasr，CPU）。

参考生产机 VAssistant/server/stt_smoke.py。模型进程内单例，非线程安全——本项目
worker 串行（并发=1），单例即可。SenseVoice 不支持 hotword，术语纠错在提取层做。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_MODEL_NAME = "iic/SenseVoiceSmall"
_ASR_MODEL_ID = "SenseVoiceSmall"
_model = None


def model_id() -> str:
    return _ASR_MODEL_ID


def _load():
    global _model
    if _model is None:
        from funasr import AutoModel
        _model = AutoModel(model=_MODEL_NAME, device="cpu", disable_update=True)
    return _model


def extract_wav(video_path: str, wav_path: str) -> str:
    """ffmpeg 抽 16k 单声道 wav。已存在则跳过。"""
    if Path(wav_path).exists() and Path(wav_path).stat().st_size > 0:
        return wav_path
    Path(wav_path).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
         "-f", "wav", wav_path],
        check=True, capture_output=True)
    return wav_path


def transcribe(wav_path: str) -> str:
    """转写整段音频，返回纯文本（已做 rich 后处理）。"""
    from funasr.utils.postprocess_utils import rich_transcription_postprocess
    m = _load()
    res = m.generate(input=wav_path, cache={}, language="auto", use_itn=True,
                     batch_size_s=60, merge_vad=True, merge_length_s=15)
    text = res[0]["text"] if res else ""
    return rich_transcription_postprocess(text)


def video_to_transcript(video_path: str, wav_path: str, transcript_path: str) -> str:
    """完整链路：视频→wav→转写→落盘。transcript 已存在则直接读回（幂等）。"""
    if Path(transcript_path).exists() and Path(transcript_path).stat().st_size > 0:
        return Path(transcript_path).read_text(encoding="utf-8")
    extract_wav(video_path, wav_path)
    text = transcribe(wav_path)
    Path(transcript_path).parent.mkdir(parents=True, exist_ok=True)
    Path(transcript_path).write_text(text, encoding="utf-8")
    return text
