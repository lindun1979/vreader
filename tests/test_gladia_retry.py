"""Gladia 网络重试：瞬时超时不应直接回落 SenseVoice（2026-10-11 上传 read timeout 事故）。"""
import urllib.error

import pytest

from core import asr, config, models


@pytest.fixture
def gladia(monkeypatch):
    """ffmpeg/sleep/热词打桩；返回调用记录 calls=[(fn名, proxy)]。"""
    monkeypatch.setattr(asr.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(asr.time, "sleep", lambda s: None)
    monkeypatch.setattr(asr, "_vocabulary", lambda: [])
    monkeypatch.setattr(config, "GLADIA_PROXY", "")
    calls = []
    return calls


def _api_ok(calls):
    def api(url, key, data=None, *, proxy=""):
        calls.append(("api", proxy))
        if data is not None:
            return {"result_url": "https://x/r"}
        return {"status": "done", "result": {"transcription": {
            "utterances": [{"text": "青铜 B001 GLM5.3 做对了"}]}}}
    return api


def test_upload_timeout_retried_then_succeeds(gladia, monkeypatch, tmp_path):
    n = {"up": 0}

    def upload(path, key, *, proxy=""):
        gladia.append(("up", proxy))
        n["up"] += 1
        if n["up"] < 3:
            raise TimeoutError("The read operation timed out")
        return "https://x/a"
    monkeypatch.setattr(asr, "_gladia_upload", upload)
    monkeypatch.setattr(asr, "_gladia_api", _api_ok(gladia))
    text = asr._transcribe_gladia(str(tmp_path / "v.mp4"), "k")
    assert "B001" in text
    assert [c for c in gladia if c[0] == "up"] == [("up", "")] * 3


def test_direct_exhausted_falls_to_proxy_and_sticks(gladia, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "GLADIA_PROXY", "http://u:p@proxy:8080")

    def upload(path, key, *, proxy=""):
        gladia.append(("up", proxy))
        if not proxy:
            raise TimeoutError("timed out")
        return "https://x/a"
    monkeypatch.setattr(asr, "_gladia_upload", upload)
    monkeypatch.setattr(asr, "_gladia_api", _api_ok(gladia))
    asr._transcribe_gladia(str(tmp_path / "v.mp4"), "k")
    ups = [p for f, p in gladia if f == "up"]
    assert ups == ["", "", "", "http://u:p@proxy:8080"]
    # 后续 API 调用粘在代理线路上
    assert all(p == "http://u:p@proxy:8080" for f, p in gladia if f == "api")


def test_all_routes_fail_raises_gladia_error(gladia, monkeypatch, tmp_path):
    def upload(path, key, *, proxy=""):
        gladia.append(("up", proxy))
        raise TimeoutError("timed out")
    monkeypatch.setattr(asr, "_gladia_upload", upload)
    with pytest.raises(asr.GladiaError, match="上传失败"):
        asr._transcribe_gladia(str(tmp_path / "v.mp4"), "k")
    assert len(gladia) == asr._GLADIA_DIRECT_TRIES


def test_4xx_not_retried(gladia, monkeypatch, tmp_path):
    def upload(path, key, *, proxy=""):
        gladia.append(("up", proxy))
        raise urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
    monkeypatch.setattr(asr, "_gladia_upload", upload)
    with pytest.raises(asr.GladiaError):
        asr._transcribe_gladia(str(tmp_path / "v.mp4"), "k")
    assert len(gladia) == 1


def test_poll_transient_error_retried(gladia, monkeypatch, tmp_path):
    monkeypatch.setattr(asr, "_gladia_upload", lambda p, k, *, proxy="": "https://x/a")
    ok = _api_ok(gladia)
    n = {"poll": 0}

    def api(url, key, data=None, *, proxy=""):
        if data is None:
            n["poll"] += 1
            if n["poll"] == 1:
                raise urllib.error.URLError("connection reset")
        return ok(url, key, data, proxy=proxy)
    monkeypatch.setattr(asr, "_gladia_api", api)
    assert "B001" in asr._transcribe_gladia(str(tmp_path / "v.mp4"), "k")


def test_mistral_large_series_and_nickname():
    data = models.load_series()
    assert models.compose_canonical("Mistral Large", "3") == "Mistral Large 3"
    anchors = models.build_anchors("今天欧公子 Mistral Large 3 来挑战", "", data=data)
    assert models.resolve_record("Mistral Large 3", "Mistral Large", "3", "", anchors,
                                 data=data)[0] == "Mistral Large 3"
    # 校正命令输入宽容：mistral → 正式键
    assert models._canon_series_name(data, "mistral") == "Mistral Large"
