"""集中配置：从 .env（或环境变量）读取，凭据不硬编码、不进仓。"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_env() -> None:
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def get(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


ROOT = _ROOT
DATA_DIR = Path(get("VREADER_DATA_DIR") or (_ROOT / "data"))
HOST = get("VREADER_HOST", "127.0.0.1")
PORT = int(get("VREADER_PORT", "8232"))
HMAC_SECRET = get("VREADER_HMAC_SECRET", "")
FEISHU_APP_ID = get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = get("FEISHU_APP_SECRET", "")
CLAUDE_BIN = get("CLAUDE_BIN", "claude")
# ffmpeg 绝对路径（launchd/非登录 shell PATH 不含 /usr/local/bin，learnings
# deploy/launchd-cron-env-not-inherited）。生产 .env 写 /usr/local/bin/ffmpeg。
FFMPEG_BIN = get("FFMPEG_BIN", "ffmpeg")
ADMIN_SENDER_ID = get("VREADER_ADMIN_SENDER_ID", "")

# Gladia 云 ASR key（主转写通道；空 = 只用本地 SenseVoice）
GLADIA_API_KEY = get("GLADIA_API_KEY", "")

# 提取 LLM 后端：openai（:8317 cliproxy，默认）| claude（本地 CLI 兜底）
LLM_BACKEND = get("LLM_BACKEND", "openai")
LLM_BASE_URL = get("LLM_BASE_URL", "http://127.0.0.1:8317/v1")
LLM_API_KEY = get("LLM_API_KEY", "")
LLM_MODEL = get("LLM_MODEL", "oc-qwen3.8-flash")
# 主模型不可用（provider 授权失效/503）时的兜底模型链（逗号分隔）
LLM_MODEL_FALLBACK = [m.strip() for m in get("LLM_MODEL_FALLBACK", "").split(",") if m.strip()]
LLM_TIMEOUT = int(get("LLM_TIMEOUT", "600"))  # 推理模型对长乱码转写可能很慢

# 资源上限
MAX_DURATION_S = 30 * 60
MAX_BYTES = 500 * 1024 * 1024
MAX_QUEUE = 20
MIN_DISK_GB = 20
PENDING_EXPIRE_DAYS = 30
CONFIDENCE_THRESHOLD = 0.7


def channel_dir(channel: str) -> Path:
    return DATA_DIR / channel


def video_dir(channel: str, video_id: str) -> Path:
    return channel_dir(channel) / video_id
