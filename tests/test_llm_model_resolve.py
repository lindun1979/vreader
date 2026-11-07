"""D-E：agy `latest:<glob>` 解析、失效恢复、实际模型记录与降级分类（mock _run_cli，不调真 agy）。"""
import json
import sys
from pathlib import Path

import pytest

from core import config, extract

LIST = ("gemini-4-argon-medium\tGemini 4 Argon (Medium)\n"
        "gemini-3.10-flash-medium\tGemini 3.10 Flash (Medium)\n"
        "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
        "gemini-x-flash-medium\tbroken\n"
        "gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n")
REC = json.dumps({"records": []})
_ORIG_CALL_OPENAI = extract._call_openai


class FakeCli:
    """记录每次调用；按 argv 返回 `agy models` 列表或 -p 结果。"""

    def __init__(self, listing=LIST, invalid=(), print_out=REC, models_rc=0):
        self.calls = []
        self.listing, self.invalid, self.print_out, self.models_rc = listing, set(invalid), print_out, models_rc

    def __call__(self, cmd, *, timeout, env=None, name="CLI", cwd=None):
        self.calls.append({"cmd": cmd, "cwd": cwd})
        if cmd[1:] == ["models"]:
            return self.models_rc, self.listing, ""
        model = cmd[cmd.index("--model") + 1]
        if model in self.invalid:
            return 1, "", f'error: invalid model selection (--model "{model}")'
        return 0, self.print_out, ""

    def n_models(self):
        return sum(1 for c in self.calls if c["cmd"][1:] == ["models"])

    def used_models(self):
        return [c["cmd"][c["cmd"].index("--model") + 1] for c in self.calls if "--model" in c["cmd"]]


@pytest.fixture(autouse=True)
def _agy(monkeypatch):
    extract._reset_latest_cache()
    monkeypatch.setattr(config, "LLM_BACKEND", "agy")
    monkeypatch.setattr(config, "LLM_MODEL", "latest:gemini-*-flash-medium")
    monkeypatch.setattr(config, "LLM_MODEL_FALLBACK", ["lite"])
    monkeypatch.setattr(extract, "_call_openai", lambda p, *, timeout, model: REC)
    yield
    extract._reset_latest_cache()


def _fake(monkeypatch, **kw):
    f = FakeCli(**kw)
    monkeypatch.setattr(extract, "_run_cli", f)
    return f


def test_pick_latest_numeric_order_and_skip_non_numeric():
    ids = [ln.split("\t")[0] for ln in LIST.splitlines()]
    assert extract._pick_latest(ids, "gemini-*-flash-medium", set()) == "gemini-3.10-flash-medium"
    assert extract._pick_latest(list(reversed(ids)), "gemini-*-flash-medium", set()) == "gemini-3.10-flash-medium"
    assert extract._pick_latest(ids, "gemini-*-flash-medium", {"gemini-3.10-flash-medium"}) == "gemini-3.8-flash-medium"
    assert extract._pick_latest(["a-3.8-x", "a-03.8-x"], "a-*-x", set()) == "a-3.8-x"  # 并列取字典序大


def test_explicit_model_passthrough(monkeypatch):
    f = _fake(monkeypatch)
    assert extract.resolve_agy_model("gemini-3.8-flash-medium") == "gemini-3.8-flash-medium"
    assert f.n_models() == 0


def test_resolved_model_passed_to_agy_and_recorded(monkeypatch):
    f = _fake(monkeypatch)
    text, llm_model, reason = extract._call_llm("p")
    assert f.used_models() == ["gemini-3.10-flash-medium"]
    assert llm_model == "agy:gemini-3.10-flash-medium" and reason is None


def test_cache_within_ttl_and_reresolve_after_expiry(monkeypatch):
    f = _fake(monkeypatch)
    extract._call_llm("p")
    extract._call_llm("p")
    assert f.n_models() == 1
    g = "gemini-*-flash-medium"
    m, t = extract._latest_cache[g]
    extract._latest_cache[g] = (m, t - extract._LATEST_TTL - 1)
    extract._call_llm("p")
    assert f.n_models() == 2


def test_restart_clears_cache(monkeypatch):
    f = _fake(monkeypatch)
    extract._call_llm("p")
    extract._reset_latest_cache()
    extract._call_llm("p")
    assert f.n_models() == 2


def test_invalid_selected_model_forces_reresolve_once(monkeypatch):
    f = _fake(monkeypatch, invalid={"gemini-3.10-flash-medium"})
    text, llm_model, reason = extract._call_llm("p")
    assert f.used_models() == ["gemini-3.10-flash-medium", "gemini-3.8-flash-medium"]
    assert llm_model == "agy:gemini-3.8-flash-medium" and reason is None


def test_all_agy_invalid_falls_back_with_reason(monkeypatch):
    f = _fake(monkeypatch, invalid={"gemini-3.10-flash-medium", "gemini-3.8-flash-medium"})
    text, llm_model, reason = extract._call_llm("p")
    assert llm_model == "openai:lite" and reason == "invalid_model"
    assert len(f.used_models()) == 2  # 每次调用至多一次强制重新解析


def test_resolve_failure_keeps_last_good(monkeypatch):
    f = _fake(monkeypatch)
    extract._call_llm("p")
    g = "gemini-*-flash-medium"
    m, t = extract._latest_cache[g]
    extract._latest_cache[g] = (m, t - extract._LATEST_TTL - 1)
    f.models_rc = 1
    _, llm_model, reason = extract._call_llm("p")
    assert llm_model == "agy:gemini-3.10-flash-medium" and reason is None


def test_never_resolved_goes_fallback(monkeypatch):
    _fake(monkeypatch, models_rc=1)
    _, llm_model, reason = extract._call_llm("p")
    assert llm_model == "openai:lite" and reason == "resolve_failed"


def test_no_output_produced_is_error(monkeypatch):
    _fake(monkeypatch, print_out="jetski: no output produced — a tool required the command permission")
    _, llm_model, reason = extract._call_llm("p")
    assert llm_model == "openai:lite" and reason == "empty_output"


def test_agy_cwd_is_empty_dir_not_repo(monkeypatch):
    f = _fake(monkeypatch)
    extract._call_llm("p")
    cwds = {c["cwd"] for c in f.calls}
    assert None not in cwds and all(Path(c).is_dir() and not list(Path(c).iterdir()) for c in cwds)
    assert all(Path(c).resolve() != config.ROOT.resolve() for c in cwds)


@pytest.mark.parametrize("msg,cls", [
    ('error: invalid model selection (--model "x")', "invalid_model"),
    ("agy CLI 超时", "timeout"), ("LLM 返回空", "empty_output"),
    ("Please sign in to view available models", "auth"), ("boom", "other")])
def test_classify_failure(msg, cls):
    assert extract._classify_failure(extract.ExtractError(msg)) == cls


def test_build_extract_writes_llm_fields(monkeypatch):
    monkeypatch.setattr(extract, "_call_llm", lambda p: (REC, "agy:m", None))
    ex = extract.build_extract(aweme_id="v", title="t", transcript="x" * 20)
    assert ex["llm_model"] == "agy:m" and "llm_fallback_reason" not in ex
    monkeypatch.setattr(extract, "_call_llm", lambda p: (REC, "openai:lite", "timeout"))
    ex = extract.build_extract(aweme_id="v", title="t", transcript="x" * 20)
    assert ex["llm_fallback_reason"] == "timeout"
    extract.validate_extract(ex, transcript="x" * 20, expected_video_id="v")


def test_eval_repeat_model_flag(monkeypatch, tmp_path):
    sys.path.insert(0, str(config.ROOT / "ops"))
    import eval_extract_repeat as er
    vid = "7681587976547208511"
    d = tmp_path / "copy" / "token_bug" / vid
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text("白银S005 Kimi K3 第一轮的修改结果 没有问题", encoding="utf-8")
    (d / "extract.json").write_text(json.dumps({"title": "t", "known_versions_used": []}), encoding="utf-8")
    seen = []
    monkeypatch.setattr(extract, "_call_agy",
                        lambda p, *, timeout=300, model=None: seen.append(model) or REC + str(len(seen)))
    er.main(["--data", str(tmp_path / "copy"), "--videos", vid, "--runs", "2",
             "--out", str(tmp_path / "out"), "--model", "gemini-4-argon-medium"])
    assert seen == ["gemini-4-argon-medium"] * 2
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert s["model"] == "gemini-4-argon-medium"
    meta = json.loads((tmp_path / "out" / vid / "run0.meta.json").read_text())
    assert meta["model"] == "gemini-4-argon-medium"


def test_openai_read_timeout_falls_back(monkeypatch):
    """cliproxy 读响应超时（socket TimeoutError）须转 ExtractError，走兜底链并分类 timeout。"""
    import urllib.request
    monkeypatch.setattr(config, "LLM_BACKEND", "openai")
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-flash-latest")
    monkeypatch.setattr(config, "LLM_MODEL_FALLBACK", ["gemini-3.5-flash"])
    monkeypatch.setattr(extract, "_call_openai", _ORIG_CALL_OPENAI)

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": REC}}]}).encode()

    def fake_urlopen(req, timeout):
        if json.loads(req.data)["model"] == "gemini-flash-latest":
            raise TimeoutError("timed out")
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text, llm_model, reason = extract._call_llm("p")
    assert llm_model == "openai:gemini-3.5-flash" and reason == "timeout"
