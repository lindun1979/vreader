"""抖音分享链接解析与视频下载。

M0 实测结论（2026-09-04，dev box）：yt-dlp 的 Douyin extractor 请求 web detail
API 时漏传 device_platform/aid 等参数而失败；直接调 detail API 更稳。全程只需匿名
ttwid cookie（bytedance ttwid 注册端点获取），无需登录/账号。
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
import urllib.error

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
# 抖音 WAF 会拦缺少 Accept/Accept-Language 的请求（urllib 默认不发 → 403）。
_BROWSER_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.douyin.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept-Encoding": "identity",
}


def _headers(ttwid: str | None = None) -> dict:
    h = dict(_BROWSER_HEADERS)
    if ttwid:
        h["Cookie"] = f"ttwid={ttwid}"
    return h
_TTWID_REGISTER = "https://ttwid.bytedance.com/ttwid/union/register/"
_TTWID_BODY = json.dumps({
    "region": "cn", "aid": 1768, "needFid": False, "service": "www.ixigua.com",
    "migrate_info": {"ticket": "", "source": "node"},
    "cbUrlProtocol": "https", "union": True,
}).encode()
_DETAIL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_DETAIL_PARAMS = ("device_platform=webapp&aid=6383&channel=channel_pc_web"
                  "&pc_client_type=1&version_code=170400&version_name=17.4.0"
                  "&cookie_enabled=true&platform=PC")

# 分享文本里可能出现的抖音链接
_URL_RE = re.compile(r"https?://[^\s，。、）)\]】]+")
_AWEME_IN_URL = re.compile(r"/(?:video|note)/(\d+)")
_MODAL_ID = re.compile(r"[?&]modal_id=(\d+)")
_DIGITS = re.compile(r"(\d{15,25})")

_ttwid_cache = {"value": None, "ts": 0.0}
_TTWID_TTL = 3600.0


class DownloadError(Exception):
    pass


def _get_ttwid(force: bool = False) -> str:
    now = time.time()
    if not force and _ttwid_cache["value"] and now - _ttwid_cache["ts"] < _TTWID_TTL:
        return _ttwid_cache["value"]
    req = urllib.request.Request(_TTWID_REGISTER, data=_TTWID_BODY,
                                 headers={"Content-Type": "application/json", "User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=12) as resp:
        for k, v in resp.getheaders():
            if k.lower() == "set-cookie" and v.startswith("ttwid="):
                ttwid = v.split(";", 1)[0][len("ttwid="):]
                _ttwid_cache.update(value=ttwid, ts=now)
                return ttwid
    raise DownloadError("ttwid 注册端点未返回 cookie")


def resolve_aweme_id(share_text: str) -> str:
    """从分享文本/链接解析出 aweme_id。短链会跟随重定向。"""
    m = _URL_RE.search(share_text or "")
    url = m.group(0) if m else (share_text or "").strip()
    if not url:
        raise DownloadError("分享文本中未找到链接")
    # 长链直接可解析
    aid = _aweme_from_url(url)
    if aid:
        return aid
    # 短链：跟随重定向拿最终 URL
    try:
        req = urllib.request.Request(url, headers=_headers())
        with urllib.request.urlopen(req, timeout=12) as resp:
            final = resp.geturl()
    except urllib.error.URLError as e:
        raise DownloadError(f"短链重定向失败: {e}") from e
    aid = _aweme_from_url(final)
    if not aid:
        raise DownloadError(f"无法从链接解析 aweme_id: {final}")
    return aid


def _aweme_from_url(url: str) -> str | None:
    for rgx in (_AWEME_IN_URL, _MODAL_ID):
        m = rgx.search(url)
        if m:
            return m.group(1)
    # 兜底：纯数字 id 段
    m = _DIGITS.search(url)
    return m.group(1) if m else None


def fetch_detail(aweme_id: str) -> dict:
    """调 web detail API，返回 aweme_detail dict。带一次 ttwid 刷新重试。"""
    for attempt in range(2):
        ttwid = _get_ttwid(force=attempt > 0)
        url = f"{_DETAIL}?aweme_id={aweme_id}&{_DETAIL_PARAMS}"
        req = urllib.request.Request(url, headers=_headers(ttwid))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            if attempt == 1:
                raise DownloadError(f"detail API 请求失败: {e}") from e
            continue
        detail = (data or {}).get("aweme_detail")
        if detail:
            return detail
    raise DownloadError(f"detail API 未返回 aweme_detail（可能需刷新 cookie/签名）: {aweme_id}")


def meta_from_detail(detail: dict) -> dict:
    video = detail.get("video") or {}
    return {
        "aweme_id": str(detail.get("aweme_id") or ""),
        "title": (detail.get("desc") or "").strip(),
        "duration_s": round((video.get("duration") or 0) / 1000, 1),
        "create_time": detail.get("create_time"),  # epoch 秒
    }


def _play_urls(detail: dict) -> list[str]:
    video = detail.get("video") or {}
    urls: list[str] = []
    for u in (video.get("play_addr") or {}).get("url_list") or []:
        if u not in urls:
            urls.append(u)
    for br in video.get("bit_rate") or []:
        for u in (br.get("play_addr") or {}).get("url_list") or []:
            if u not in urls:
                urls.append(u)
    return urls


def download(aweme_id: str, dest_path: str, *, detail: dict | None = None) -> dict:
    """下载视频到 dest_path，返回元数据。已存在则跳过下载（幂等）。"""
    import os
    detail = detail or fetch_detail(aweme_id)
    meta = meta_from_detail(detail)
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        meta["skipped"] = True
        return meta
    ttwid = _get_ttwid()
    last_err: Exception | None = None
    for u in _play_urls(detail):
        try:
            req = urllib.request.Request(u, headers=_headers(ttwid))
            tmp = dest_path + ".part"
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            if os.path.getsize(tmp) < 1024:
                raise DownloadError("下载内容过小")
            os.replace(tmp, dest_path)
            meta["skipped"] = False
            meta["bytes"] = os.path.getsize(dest_path)
            return meta
        except Exception as e:  # noqa: BLE001 换下一个播放地址
            last_err = e
            continue
    raise DownloadError(f"所有播放地址下载失败: {last_err}")
