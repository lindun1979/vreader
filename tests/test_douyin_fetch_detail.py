"""fetch_detail 走移动端 feed：只取 aweme_id 严格匹配的那条，推荐视频不当目标。"""
import json

import pytest

from core import douyin


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _feed(*ids):
    return {"aweme_list": [{"aweme_id": i, "desc": f"d{i}", "video": {}} for i in ids]}


def test_returns_matching_item_among_recommendations(monkeypatch):
    monkeypatch.setattr(douyin.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(_feed("999", "123", "888")))
    assert douyin.fetch_detail("123")["desc"] == "d123"


def test_raises_when_target_absent(monkeypatch):
    monkeypatch.setattr(douyin.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(_feed("999", "888")))
    monkeypatch.setattr(douyin.time, "sleep", lambda s: None)
    with pytest.raises(douyin.DownloadError, match="feed 未返回目标视频"):
        douyin.fetch_detail("123", attempts=2)
