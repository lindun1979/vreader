"""WP-E2：ops/eval_extract_repeat.py 的独立性检查、落盘与模板切换（不调真 LLM）。"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from core import extract, models

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import eval_extract_repeat as er  # noqa: E402

VID = "7681587976547208511"  # gold 内视频
TX = "白银S005 Kimi K3 第一轮的修改结果 没有问题恭喜加一分"
OUT = json.dumps({"records": [{"model_raw": "Kimi K3", "bug_level": "白银", "bug_id": "S005",
                               "solved_round": 1, "confidence": 0.9,
                               "evidence_quote": "Kimi K3 第一轮的修改结果 没有问题恭喜加一分"}]},
                 ensure_ascii=False)


@pytest.fixture(autouse=True)
def _agy_backend(monkeypatch):
    """这些用例 mock 的是 _call_agy：固定评测通道为 agy（不依赖本机 .env 的 LLM_BACKEND）。"""
    from core import config
    monkeypatch.setattr(config, "LLM_BACKEND", "agy")


@pytest.fixture
def copy_dir(tmp_path):
    d = tmp_path / "copy" / "token_bug" / VID
    d.mkdir(parents=True)
    (d / "transcript.txt").write_text(TX, encoding="utf-8")
    (d / "extract.json").write_text(json.dumps({"title": "t", "known_versions_used": []}),
                                    encoding="utf-8")
    return tmp_path / "copy"


def _run(copy_dir, out, monkeypatch, reply, runs=3, prompt=None):
    seen = []

    def fake(prompt_text, *, timeout=300, model=None):
        seen.append(prompt_text)
        return reply(len(seen))
    monkeypatch.setattr(extract, "_call_agy", fake)
    argv = ["--data", str(copy_dir), "--videos", VID, "--runs", str(runs), "--out", str(out)]
    if prompt:
        argv += ["--prompt", str(prompt)]
    return er.main(argv), seen


def test_nonce_unique_and_files_written(copy_dir, tmp_path, monkeypatch):
    out = tmp_path / "out"
    rc, seen = _run(copy_dir, out, monkeypatch, lambda i: OUT + " " * i)
    assert rc == 0
    nonces = [json.loads((out / VID / f"run{i}.meta.json").read_text())["nonce"] for i in range(3)]
    assert len(set(nonces)) == 3 and all(n in p for n, p in zip(nonces, seen))
    for i in range(3):
        assert (out / VID / f"run{i}.txt").exists()
    s = json.loads((out / "summary.json").read_text())
    assert s["suspect_cache"] == [] and s["videos"][VID][0]["records"] == 1


def test_identical_outputs_flag_suspect_cache(copy_dir, tmp_path, monkeypatch):
    rc, _ = _run(copy_dir, tmp_path / "out", monkeypatch, lambda i: OUT)
    assert rc != 0
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert s["suspect_cache"] == [VID]


def test_prompt_file_changes_dispatched_prompt(copy_dir, tmp_path, monkeypatch):
    t1, t2 = tmp_path / "a.md", tmp_path / "b.md"
    t1.write_text("模板A {TRANSCRIPT}", encoding="utf-8")
    t2.write_text("模板B {TRANSCRIPT}", encoding="utf-8")
    _, s1 = _run(copy_dir, tmp_path / "o1", monkeypatch, lambda i: OUT + " " * i, runs=1, prompt=t1)
    _, s2 = _run(copy_dir, tmp_path / "o2", monkeypatch, lambda i: OUT + " " * i, runs=1, prompt=t2)
    m1 = json.loads((tmp_path / "o1" / VID / "run0.meta.json").read_text())
    m2 = json.loads((tmp_path / "o2" / VID / "run0.meta.json").read_text())
    assert m1["prompt_sha256"] != m2["prompt_sha256"]
    assert s1[0].startswith("模板A") and s2[0].startswith("模板B")


def test_build_prompt_template_none_unchanged():
    known = {("GLM", "5.3", "")}
    data = models.load_series()
    legacy = (extract._prompt_template()
              .replace("{SERIES_LIST}", "\n".join(f"- {s}" for s in data))
              .replace("{SERIES_TABLE}", extract._series_table(data, known))
              .replace("{TITLE}", "标题")
              .replace("{TRANSCRIPT}", TX))
    assert extract._build_prompt(TX, "标题", known=known) == legacy


def test_raw_stats_counts_dup_and_ellipsis():
    recs = [{"model_series": "GLM", "model_version": "5.3", "bug_level": "黄金", "bug_id": "G1",
             "evidence_quote": "a...b"},
            {"model_series": "GLM", "model_version": "5.3", "bug_level": "黄金", "bug_id": "g1",
             "evidence_quote": "ab"}]
    st = er._raw_stats(json.dumps({"records": recs}))
    assert st["dup_cells"] == 1 and st["ellipsis_evidence"] == 1


# ---- 逐轮门槛 --gate（plan batch-bugs WP-1 M8）----

def _gold13(monkeypatch, tmp_path, outcome):
    """13 个合成 gold 视频 × 3 轮；outcome(vid_idx, run) → 'ok' | 'wrong' | 'batch' | 'fail'。"""
    gold = [{"aweme_id": f"g{i}", "truth": {"Kimi K3": {"S005": 1}}} for i in range(13)]
    monkeypatch.setattr(er.eval_gold, "load_gold", lambda: gold)
    data = tmp_path / "copy"
    for g in gold:
        d = data / "token_bug" / g["aweme_id"]
        d.mkdir(parents=True)
        (d / "transcript.txt").write_text(TX + " 白银S005 Kimi K3 第一轮的修改结果 没有问题", encoding="utf-8")
        (d / "extract.json").write_text(json.dumps({"title": "t", "known_versions_used": []}), encoding="utf-8")
    rec_ok = {"model_raw": "Kimi K3", "bug_level": "白银", "bug_id": "S005", "solved_round": 1,
              "confidence": 0.9, "evidence_quote": "Kimi K3 第一轮的修改结果 没有问题恭喜加一分"}
    seq = []

    def fake_agy(prompt_text, *, timeout=300, model=None):
        n = len(seq)
        seq.append(1)
        vi, run = divmod(n, 3)
        kind = outcome(vi, run)
        if kind == "fail":
            raise extract.ExtractError("agy CLI 超时")
        recs = [dict(rec_ok)]
        if kind == "wrong":
            recs[0]["solved_round"] = 0
        if kind == "batch":
            recs.append({"model_raw": "Kimi K3", "bug_level": "青铜", "bug_id": "", "solved_round": 1,
                         "batch_count": 1, "confidence": 0.9,
                         "evidence_quote": "Kimi K3 第一轮的修改结果 没有问题恭喜加一分"})
        return json.dumps({"records": recs}, ensure_ascii=False) + " " * n
    monkeypatch.setattr(extract, "_call_agy", fake_agy)
    return data


def _gate_run(data, tmp_path, gate="correct>=13,named_over<=3,batch_over<=0", videos="gold"):
    return er.main(["--data", str(data), "--videos", videos, "--runs", "3",
                    "--out", str(tmp_path / "out"), "--gate", gate])


def test_gate_all_runs_pass(monkeypatch, tmp_path):
    data = _gold13(monkeypatch, tmp_path, lambda v, r: "ok")
    assert _gate_run(data, tmp_path) == 0
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert [pr["correct"] for pr in s["per_run"]] == [13, 13, 13]


def test_gate_one_run_below_threshold(monkeypatch, tmp_path):
    data = _gold13(monkeypatch, tmp_path, lambda v, r: "wrong" if (v, r) == (4, 1) else "ok")
    assert _gate_run(data, tmp_path) == 5


def test_gate_extra_batch_fails(monkeypatch, tmp_path):
    data = _gold13(monkeypatch, tmp_path, lambda v, r: "batch" if (v, r) == (0, 2) else "ok")
    assert _gate_run(data, tmp_path) == 5
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert s["per_run"][2]["batch_over"] == 1


def test_gate_call_failure_fails(monkeypatch, tmp_path):
    data = _gold13(monkeypatch, tmp_path, lambda v, r: "fail" if (v, r) == (7, 0) else "ok")
    assert _gate_run(data, tmp_path) == 5


def test_gate_missing_video_fails(monkeypatch, tmp_path):
    data = _gold13(monkeypatch, tmp_path, lambda v, r: "ok")
    import shutil
    shutil.rmtree(data / "token_bug" / "g12")
    assert _gate_run(data, tmp_path) == 5
    s = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert all(pr["videos_missing"] == 1 for pr in s["per_run"])


def test_openai_transport_uses_cliproxy_model(copy_dir, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(extract, "_call_openai",
                        lambda p, *, timeout=300, model=None: seen.append(model) or OUT + " " * len(seen))
    rc = er.main(["--data", str(copy_dir), "--videos", VID, "--runs", "2", "--out", str(tmp_path / "o"),
                  "--transport", "openai", "--model", "gemini-flash-latest"])
    assert rc == 0 and seen == ["gemini-flash-latest"] * 2
    s = json.loads((tmp_path / "o" / "summary.json").read_text())
    assert s["transport"] == "openai" and s["model"] == "gemini-flash-latest"
