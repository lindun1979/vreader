"""ops/model_eval.py：出口并行/同出口串行、限流等待重试、失败分类、续跑、检查集打分（不调真 LLM）。"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

from core import extract

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import model_eval as me  # noqa: E402

VID = "7681587976547208511"  # gold 内视频
TX = "白银S005 Kimi K3 第一轮的修改结果 没有问题恭喜加一分"
OUT = json.dumps({"records": [{"model_raw": "Kimi K3", "bug_level": "白银", "bug_id": "S005",
                               "solved_round": 1, "confidence": 0.9,
                               "evidence_quote": "Kimi K3 第一轮的修改结果 没有问题恭喜加一分"}]},
                 ensure_ascii=False)


@pytest.fixture
def snap(tmp_path):
    d = tmp_path / "snap" / "token_bug" / VID
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(TX, encoding="utf-8")
    (d / "extract.json").write_text(json.dumps({"title": "t", "known_versions_used": []}), encoding="utf-8")
    return tmp_path / "snap"


@pytest.fixture
def waits(monkeypatch):
    w = []
    monkeypatch.setattr(me, "_sleep", lambda s: w.append(s))
    monkeypatch.setattr(me, "LANE_GAP", {})
    return w


def _fake(monkeypatch, fn):
    monkeypatch.setattr(extract, "_dispatch", lambda transport, model, prompt, *, timeout: fn(model))


def _meta(run_dir, model, vid=VID):
    return json.loads((run_dir / me._slug(model) / vid / "meta.json").read_text())


def test_throttle_waits_then_succeeds(snap, tmp_path, monkeypatch, waits):
    n = []

    def fn(model):
        n.append(1)
        if len(n) <= 2:
            raise extract.ExtractError("LLM 请求失败: HTTP Error 503: Service Unavailable")
        return OUT
    _fake(monkeypatch, fn)
    rd = me.run(["m"], tmp_path / "run", snap, [VID], lanes={"m": "google"})
    m = _meta(rd, "m")
    assert m["status"] == "ok" and m["attempts"] == 3 and waits == [300, 600] and m["waited_s"] == 900


def test_throttle_exhausted_and_cooldown_fast_fail(snap, tmp_path, monkeypatch, waits):
    def fn(model):
        raise extract.ExtractError("LLM 错误: {'message': 'auth_unavailable: no auth available'}")
    _fake(monkeypatch, fn)
    rd = me.run(["m"], tmp_path / "run", snap, [VID], lanes={"m": "google"})
    m = _meta(rd, "m")
    assert m["status"] == "throttled" and m["attempts"] == 4 and waits == [300, 600, 900]


@pytest.mark.parametrize("reply,status", [
    (lambda: "  ", "empty"),
    (lambda: (_ for _ in ()).throw(extract.ExtractError("LLM 请求超时或中断: timed out")), "timeout"),
    (lambda: (_ for _ in ()).throw(extract.ExtractError("LLM 请求失败: HTTP Error 400: Bad Request")), "error"),
])
def test_non_throttle_failures_not_retried(snap, tmp_path, monkeypatch, waits, reply, status):
    _fake(monkeypatch, lambda model: reply())
    rd = me.run(["m"], tmp_path / "run", snap, [VID], lanes={"m": "x"})
    m = _meta(rd, "m")
    assert m["status"] == status and m["attempts"] == 1 and waits == []


def test_lanes_parallel_same_lane_serial(snap, tmp_path, monkeypatch, waits):
    lock = threading.Lock()
    active = {"g": 0, "o": 0}
    peak = {"g": 0, "o": 0}
    both = threading.Event()

    def fn(model):
        lane = model[0]
        with lock:
            active[lane] += 1
            peak[lane] = max(peak[lane], active[lane])
            if active["g"] and active["o"]:
                both.set()
        both.wait(2)              # 只有两出口同时在跑才会立刻放行
        time.sleep(0.01)
        with lock:
            active[lane] -= 1
        return OUT
    _fake(monkeypatch, fn)
    me.run(["g1", "g2", "o1"], tmp_path / "run", snap, [VID], lanes={"g1": "g", "g2": "g", "o1": "o"})
    assert both.is_set() and peak == {"g": 1, "o": 1}


def test_lane_gap_spaces_requests(snap, tmp_path, monkeypatch, waits):
    monkeypatch.setattr(me, "LANE_GAP", {"google": 30})
    _fake(monkeypatch, lambda model: OUT)
    me.run(["a", "b"], tmp_path / "run", snap, [VID], lanes={"a": "google", "b": "google"})
    assert len(waits) == 1 and 29 < waits[0] <= 30


def test_resume_skips_ok_and_retries_failed(snap, tmp_path, monkeypatch, waits):
    _fake(monkeypatch, lambda model: OUT if model == "a" else "")
    rd = me.run(["a", "b"], tmp_path / "run", snap, [VID], lanes={"a": "x", "b": "x"})
    calls = []
    _fake(monkeypatch, lambda model: calls.append(model) or OUT)
    me.run(["a", "b"], rd, None, [], lanes=None)        # 续跑：快照/视频/出口来自 manifest
    assert calls == ["b"] and _meta(rd, "b")["status"] == "ok"


def test_report_scores_offline(snap, tmp_path, monkeypatch, waits):
    _fake(monkeypatch, lambda model: OUT)
    rd = me.run(["a"], tmp_path / "run", snap, [VID], lanes={"a": "x"})
    _fake(monkeypatch, lambda model: pytest.fail("report 不得调 LLM"))
    [r] = me.report([rd], None, tmp_path / "rep" / "report.md")
    assert r["gold_total"] == 12 and r["videos"][VID]["status"] == "ok"
    assert (tmp_path / "rep" / "scores.json").exists() and (tmp_path / "rep" / "diff-a.md").exists()
    assert "| a | x |" in (tmp_path / "rep" / "report.md").read_text()


def test_report_reads_legacy_layout(snap, tmp_path):
    d = tmp_path / "legacy"
    (d / VID).mkdir(parents=True)
    (d / VID / "run0.txt").write_text(OUT, encoding="utf-8")
    (d / VID / "run0.meta.json").write_text(json.dumps({"elapsed_s": 3.0, "error": None}))
    (d / "summary.json").write_text(json.dumps({"model": "old", "videos": {VID: []}}))
    with pytest.raises(SystemExit):
        me.report([d], None, None)
    [r] = me.report([d], snap, None)
    assert r["model"] == "old" and r["videos"][VID]["status"] == "ok"


def test_score_check_batch():
    truth = {"truth": {"M": {"S001": 0}}, "batch": {"M": {"青铜": {"1": 3}, "黄金": {"2": 1}}}}
    recs = ([{"model_canonical": "M", "bug_id": "S001", "bug_level": "白银", "score": 0}]
            + [{"model_canonical": "M", "bug_level": "青铜", "score": 1, "batch_slot": f"1-{k}"} for k in range(2)]
            + [{"model_canonical": "M", "bug_level": "黄金", "score": 2, "batch_slot": f"2-{k}"} for k in range(3)])
    s = me.score_check(truth, recs)
    assert (s["correct"], s["total"], s["batch_missing"], s["batch_over"]) == (1 + 2 + 1, 5, 1, 2)
    assert sorted((d["bug"], d["truth"], d["got"]) for d in s["diffs"]) == [
        ("青铜#batch(1分)", 3, 2), ("黄金#batch(2分)", 1, 3)]


def test_classify():
    assert me._classify(None, OUT) == "ok"
    assert me._classify("HTTP Error 429: Too Many Requests", "") == "throttled"
    assert me._classify("503 high demand", "") == "throttled"
