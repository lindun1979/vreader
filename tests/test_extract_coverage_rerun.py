"""覆盖复跑功能测试（plan v4 Step 7）：锚点覆盖检查触发条件、条件二跑、状态机合并、
单边强制 pending、缓存键差异、失败/预算降级、溯源快照、claude_text 注入不双跑。

用 monkeypatch 替换 extract._call_llm 为「按调用次数返回预置文本」的 fake，从而在
claude_text is None（真调 LLM）路径上驱动双跑。
"""
import json
import time

import pytest

from core import db, extract as ex

# 两系列都带版本邻接 → build_anchors versions keys = {GLM, Step}
TX = ("青铜这题 GLM-5.3 一次就修对了，逻辑很干净。"
      "字节的 Step 3.7 Flash 也来挑战这道题，第二轮才把它修好。")

GLM_QUOTE = "GLM-5.3 一次就修对了"
STEP_QUOTE = "Step 3.7 Flash 也来挑战这道题，第二轮才把它修好"


def _glm(**o):
    r = {"model_raw": "GLM-5.3", "model_series": "GLM", "model_version": "5.3",
         "model_variant": "", "bug_level": "青铜", "bug_id": "B001", "solved_round": 1,
         "evidence_quote": GLM_QUOTE, "confidence": 0.9}
    r.update(o)
    return r


def _step(**o):
    r = {"model_raw": "Step 3.7 Flash", "model_series": "Step", "model_version": "3.7",
         "model_variant": "Flash", "bug_level": "黄金", "bug_id": "G001", "solved_round": 2,
         "evidence_quote": STEP_QUOTE, "confidence": 0.9}
    r.update(o)
    return r


def _text(records):
    return json.dumps({"records": records}, ensure_ascii=False)


class _FakeLLM:
    """按调用序返回预置文本；记录每次收到的 prompt。responses 元素可为 str 或 Exception。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def __call__(self, prompt, *, timeout=None, deadline=None):
        self.prompts.append(prompt)
        if not self.responses:
            raise AssertionError("_call_llm 调用次数超出预置")
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    @property
    def calls(self):
        return len(self.prompts)


def _patch(monkeypatch, responses):
    fake = _FakeLLM(responses)
    monkeypatch.setattr(ex, "_call_llm", fake)
    return fake


# ---------- 1. 覆盖齐全不触发 ----------

def test_no_rerun_when_coverage_complete(monkeypatch):
    fake = _patch(monkeypatch, [_text([_glm(), _step()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    assert fake.calls == 1
    assert "coverage_rerun_status" not in out and "coverage_runs" not in out
    assert all(not r.get("single_run") for r in out["records"])


# ---------- 2. 触发 + 二跑补齐 ----------

def test_rerun_recovers_dropped_series(monkeypatch):
    # 首跑丢 Step，二跑两者都出
    fake = _patch(monkeypatch, [_text([_glm()]), _text([_glm(), _step()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    assert fake.calls == 2
    assert out["coverage_rerun_status"] == "completed" and out["coverage_runs"] == 2
    by_series = {r["model_series"]: r for r in out["records"]}
    assert by_series["Step"].get("single_run") is True       # 单边（仅二跑）
    assert not by_series["GLM"].get("single_run")            # 两跑一致 → 不标单边
    assert out["coverage_missing"] == []                     # 合并后无缺口
    assert out["coverage_trigger_missing"] == ["Step"]


# ---------- 3. 两跑都缺 ----------

def test_both_runs_miss_no_third_call(monkeypatch):
    fake = _patch(monkeypatch, [_text([_glm()]), _text([_glm()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    assert fake.calls == 2                                    # 恰两次，无第三跑
    assert out["coverage_rerun_status"] == "completed"
    assert out["coverage_trigger_missing"] == ["Step"]
    assert out["coverage_missing"] == ["Step"]               # 两跑仍缺


# ---------- 4. 同槽不同分 → pending_conflict ----------

def test_conflict_scores_go_pending_conflict(monkeypatch, db_path):
    # 首跑只出 GLM(青铜第1轮=score1) → Step 缺触发二跑；二跑 GLM 同槽第2轮(score0/白做)造对立
    # + 补出 Step。GLM 两跑同 attempt_key 不同 score → 不同 rid → 全组 pending_conflict。
    fake = _patch(monkeypatch, [
        _text([_glm(solved_round=1)]),                 # GLM score1，Step 缺
        _text([_glm(solved_round=2), _step()])])       # GLM score0（对立）+ Step
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    assert fake.calls == 2
    db.init(db_path)
    c = db.connect(db_path)
    counts = ex.apply_decisions(c, "v1", out)
    assert counts.get(db.PENDING_CONFLICT, 0) == 2            # 两条对立记录全组冲突
    c.close()


# ---------- 5. 单边强制 pending（且已裁决不重开） ----------

def test_single_run_forced_pending_even_high_conf():
    r = _glm(confidence=0.99)
    r["single_run"] = True
    assert ex._classify(r, is_conflict=False, known=None) == db.PENDING


def test_approved_single_run_not_reopened(monkeypatch, db_path):
    db.init(db_path)
    c = db.connect(db_path)
    fake = _patch(monkeypatch, [_text([_glm()]), _text([_glm(), _step()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    # 先决策：Step 单边 → pending
    ex.apply_decisions(c, "v1", out)
    step = next(r for r in out["records"] if r["model_series"] == "Step")
    rid = ex.record_id("v1", step)
    assert db.get_decision(c, rid)["decision"] == db.PENDING
    # 人工确认后再决策一次，approved 凭指纹恒存不被 single_run 拉回
    db.upsert_decision(c, record_id=rid, aweme_id="v1", decision=db.APPROVED,
                       extractor_version=out["extractor_version"],
                       prompt_hash=out["prompt_hash"], commit=True)
    ex.apply_decisions(c, "v1", out)
    assert db.get_decision(c, rid)["decision"] == db.APPROVED
    c.close()


# ---------- 6. schema 兼容 ----------

def test_merged_envelope_validates_and_roundtrips(monkeypatch, tmp_path):
    _patch(monkeypatch, [_text([_glm()]), _text([_glm(), _step()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    ex.validate_extract(out, transcript=TX, expected_video_id="v1")
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    back = ex.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert back is not None and back["coverage_runs"] == 2


# ---------- 7. 缓存键差异 ----------

def test_rerun_uses_distinct_cache_key(monkeypatch):
    fake = _patch(monkeypatch, [_text([_glm()]), _text([_glm(), _step()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    assert fake.calls == 2
    assert fake.prompts[0].encode() != fake.prompts[1].encode()   # 二跑 prompt 字节不同
    assert out["prompt_full_hash"] != out["prompt_full_hash_rerun"]


# ---------- 8. 二跑失败保留首跑 ----------

def test_rerun_failure_keeps_first_pass_unmarked(monkeypatch):
    fake = _patch(monkeypatch, [_text([_glm()]), ex.ExtractError("agy 超时")])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)   # 不抛
    assert fake.calls == 2
    assert out["coverage_rerun_status"] == "failed"
    assert "coverage_runs" not in out                                # 保持 1（省略）
    assert "coverage_missing" not in out                             # 未验证不写
    assert out["coverage_trigger_missing"] == ["Step"]
    assert all(not r.get("single_run") for r in out["records"])      # 首跑不被误标


# ---------- 9. 预算不足跳过二跑 + reprocess 传 deadline + _call_llm 逐次裁剪 ----------

def test_rerun_skips_when_budget_insufficient(monkeypatch):
    fake = _patch(monkeypatch, [_text([_glm()])])                    # 只预置一次（二跑不应发生）
    tight = time.monotonic() + 10                                    # 远小于 LLM_TIMEOUT+120
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX, deadline=tight)
    assert fake.calls == 1
    assert out["coverage_rerun_status"] == "skipped_budget"
    assert "coverage_runs" not in out
    assert out["coverage_trigger_missing"] == ["Step"]


def test_call_llm_trims_and_raises_near_deadline(monkeypatch):
    # 真 _call_llm：deadline 太近 → 不发起任何 _dispatch，抛 ExtractError
    called = []
    monkeypatch.setattr(ex, "_dispatch",
                        lambda *a, **k: called.append(1) or "x")
    with pytest.raises(ex.ExtractError):
        ex._call_llm("p", deadline=time.monotonic() + 5)
    assert not called                                                # 绝不跨 deadline 发起


def test_reprocess_threads_deadline(monkeypatch, conn, tmp_path, data_dir):
    # reprocess 自建 deadline 并传入 build_extract（保证 _call_llm 裁剪也保护 reprocess）
    from core import config, pipeline, util
    vdir = config.video_dir("token_bug", "v1")
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "transcript.txt").write_text(TX, encoding="utf-8")
    db.insert_task(conn, aweme_id="v1", channel="token_bug",
                   raw_link="x", chat_id="", sender_id="", title="")
    seen = {}

    def fake_build(**kw):
        seen.update(kw)
        return {"schema_rev": 2, "video_id": "v1", "title": "", "extracted_at": "x",
                "extractor_version": ex.EXTRACTOR_VERSION, "prompt_hash": "ph",
                "asr_model": "gladia-v2", "records": [], "no_content": True,
                "dropped_count": 0, "dropped": [], "result_rev": "r"}
    monkeypatch.setattr(ex, "build_extract", fake_build)
    pipeline.reprocess(conn, "v1")
    assert seen.get("deadline") is not None                          # 确有传入 deadline


# ---------- 10. claude_text 注入路径不双跑 ----------

def test_injected_text_never_reruns(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("claude_text 注入不应调 _call_llm")
    monkeypatch.setattr(ex, "_call_llm", boom)
    # 只给 GLM（Step 缺），但因 claude_text 注入 → 不触发二跑
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX,
                           claude_text=_text([_glm()]))
    assert "coverage_rerun_status" not in out
    assert all(not r.get("single_run") for r in out["records"])


# ---------- 11. 溯源快照抗 models.yml 漂移 ----------

def test_anchor_provenance_survives_reread(monkeypatch, tmp_path):
    _patch(monkeypatch, [_text([_glm()]), _text([_glm()])])
    out = ex.build_extract(aweme_id="v1", title="", transcript=TX)
    anchor_snap = list(out["coverage_anchor_series"])
    trig_snap = list(out["coverage_trigger_missing"])
    p = tmp_path / "extract.json"
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    # 读回不重算这两个快照字段（即便 models.yml 之后变化也不影响存量溯源）
    back = ex.load_valid_extract(str(p), transcript=TX, expected_video_id="v1")
    assert back["coverage_anchor_series"] == anchor_snap
    assert back["coverage_trigger_missing"] == trig_snap
