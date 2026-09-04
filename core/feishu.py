"""飞书 REST 发送（复用 life-assistant 的 app 凭据）。仅发送，不开 WS。

tenant_access_token 缓存到过期前。发送失败抛异常，由 outbox 投递循环重试。
"""
from __future__ import annotations

import json
import time
import urllib.request
import urllib.error

from . import config

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
_token_cache = {"token": None, "exp": 0.0}


class FeishuError(Exception):
    pass


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        raise FeishuError(f"飞书请求失败: {e}") from e


def _tenant_token() -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["exp"]:
        return _token_cache["token"]
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        raise FeishuError("FEISHU_APP_ID/SECRET 未配置")
    data = _post_json(_TOKEN_URL, {"app_id": config.FEISHU_APP_ID,
                                   "app_secret": config.FEISHU_APP_SECRET}, {})
    if data.get("code") != 0:
        raise FeishuError(f"取 tenant_access_token 失败: {data}")
    _token_cache.update(token=data["tenant_access_token"],
                        exp=now + data.get("expire", 7200) - 120)
    return _token_cache["token"]


def send_text(chat_id: str, text: str) -> None:
    token = _tenant_token()
    payload = {"receive_id": chat_id, "msg_type": "text",
               "content": json.dumps({"text": text}, ensure_ascii=False)}
    data = _post_json(_MSG_URL, payload, {"Authorization": f"Bearer {token}"})
    if data.get("code") != 0:
        raise FeishuError(f"发送失败: {data}")
