import time

from core import config, service


def _setup(monkeypatch):
    monkeypatch.setattr(config, "HMAC_SECRET", "s3cret")


def test_valid_signature_accepted(monkeypatch):
    _setup(monkeypatch)
    ts = str(int(time.time()))
    body = b'{"text":"x"}'
    assert service.verify(ts, service.sign(ts, body), body)


def test_wrong_signature_rejected(monkeypatch):
    _setup(monkeypatch)
    ts = str(int(time.time()))
    body = b'{"text":"x"}'
    assert not service.verify(ts, "deadbeef", body)


def test_stale_timestamp_rejected(monkeypatch):
    _setup(monkeypatch)
    ts = str(int(time.time()) - 600)  # 超出 300s 窗口
    body = b'{"text":"x"}'
    assert not service.verify(ts, service.sign(ts, body), body)


def test_body_tamper_rejected(monkeypatch):
    _setup(monkeypatch)
    ts = str(int(time.time()))
    body = b'{"text":"x"}'
    sig = service.sign(ts, body)
    assert not service.verify(ts, sig, b'{"text":"y"}')


def test_empty_secret_rejects_all(monkeypatch):
    monkeypatch.setattr(config, "HMAC_SECRET", "")
    ts = str(int(time.time()))
    assert not service.verify(ts, "anything", b"{}")
