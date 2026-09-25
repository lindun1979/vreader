"""WP-D：回执透明化（丢弃原因聚合）+ 名单×bug 疑似漏格告警。"""
import json

from core import config, db, extract, pipeline


def _r(model, level, bug, series=None):
    return {"model_canonical": model, "model_series": series or model.split(" ")[0],
            "bug_level": level, "bug_id": bug}


def _full(models, bugs):
    return [_r(m, lv, b) for m in models for lv, b in bugs]


BUGS3 = [("黄金", "G006"), ("钻石", "D003"), ("王者", "K005")]
M3 = ["GPT 6 Sol", "Grok 4.7", "Claude Opus 5.5"]


def test_missing_two_cells():
    recs = [r for r in _full(M3, BUGS3)
            if not (r["model_canonical"] == "Claude Opus 5.5" and r["bug_id"] in ("G006", "K005"))]
    for r in recs:
        if r["model_canonical"].startswith("Claude"):
            r["model_series"] = "Claude Opus"
    w = pipeline._receipt_warnings({"records": recs}, set())
    assert w == ["疑似漏提：Claude Opus 5.5 × 黄金 G006；Claude Opus 5.5 × 王者 K005（按名单推断）"]


def test_full_matrix_no_warning():
    assert pipeline._receipt_warnings({"records": _full(M3, BUGS3), "dropped_count": 0}, set()) == []


def test_unknown_and_empty_bug_counted_not_in_matrix():
    recs = _full(["GPT 6 Sol"], BUGS3) + [
        {"model_canonical": "UNKNOWN", "model_series": "", "bug_level": "黄金", "bug_id": "G009"},
        _r("GPT 6 Sol", "钻石", ""),
    ]
    w = pipeline._receipt_warnings({"records": recs}, set())
    assert "未识别模型 1 条" in w and "缺 bug 编号 1 条" in w
    assert not any("疑似漏提" in x for x in w)  # UNKNOWN 记录的 bug 不扩充矩阵


def test_truncate_over_5():
    recs = [_r("A 1", "黄金", f"G00{i}") for i in range(1, 6)] + [_r("B 1", "钻石", "D001")]
    w = pipeline._receipt_warnings({"records": recs}, set())
    assert len(w) == 1 and w[0].count("×") == 5 and "等 6 格" in w[0]


def test_dropped_reasons_aggregated_sorted():
    ex = {"records": _full(["GPT 6 Sol"], BUGS3), "dropped_count": 3, "dropped": [
        {"reason": "evidence 非转写子串"}, {"reason": "黄金 solved_round=9 越界"},
        {"reason": "evidence 非转写子串"}]}
    w = pipeline._receipt_warnings(ex, set())
    assert w == ["丢弃 3 条：evidence 非转写子串 2 条、黄金 solved_round=9 越界 1 条"]


def test_title_series_absent_reported():
    w = pipeline._receipt_warnings({"records": _full(["GPT 6 Sol"], BUGS3)}, {"Grok", "GPT"})
    assert w == ["标题提到 Grok 但无任何记录（疑似漏提）"]


def test_bare_title_series_without_version_is_reported_when_absent():
    from core import models
    a = models.build_anchors("", "AI大乱斗 Grok 对战 GPT 6 Sol")
    ts = set(a["versions"]) | a["series"]
    assert "Grok" in ts
    w = pipeline._receipt_warnings({"records": _full(["GPT 6 Sol"], BUGS3)}, ts)
    assert "标题提到 Grok 但无任何记录（疑似漏提）" in w
    assert not any("GPT" in x for x in w)


def test_model_in_all_bugs_not_reported():
    w = pipeline._receipt_warnings({"records": _full(M3, BUGS3)}, {"GPT", "Grok"})
    assert w == []


# ---- 真实路径：_finish → notification_outbox ----
TX = ("黄金G006 Kimi K3 第一轮的修改结果 没有问题恭喜加两分 "
      "黄金G006 GLM5.3 第一轮的修改结果 同样正确加两分 "
      "钻石D003 Kimi K3 第一轮的修改结果 做对了加三分")


def _setup(conn, data_dir, monkeypatch, recs, aid="v1"):
    monkeypatch.setattr(config, "GLADIA_API_KEY", "")
    db.insert_task(conn, aweme_id=aid, channel="token_bug", raw_link="x",
                   chat_id="c", sender_id="s", title="Kimi K3 对战 GLM5.3")
    ex = extract.build_extract(aweme_id=aid, title="Kimi K3 对战 GLM5.3", transcript=TX,
                               claude_text=json.dumps({"records": recs}, ensure_ascii=False))
    p = pipeline._paths(aid)
    p["dir"].mkdir(parents=True, exist_ok=True)
    return ex, p


def _outbox(conn):
    return conn.execute("SELECT content FROM notification_outbox").fetchone()["content"]


def _rec(raw, level, bug, ev, sr=1):
    return {"model_raw": raw, "bug_level": level, "bug_id": bug, "solved_round": sr,
            "evidence_quote": ev, "confidence": 0.9}


def test_finish_receipt_has_drop_missing_and_detail(conn, data_dir, monkeypatch):
    recs = [_rec("Kimi K3", "黄金", "G006", "Kimi K3 第一轮的修改结果 没有问题恭喜加两分"),
            _rec("GLM5.3", "黄金", "G006", "GLM5.3 第一轮的修改结果 同样正确加两分"),
            _rec("Kimi K3", "钻石", "D003", "Kimi K3 第一轮的修改结果 做对了加三分"),
            _rec("GLM5.3", "钻石", "D003", "GLM5.3 第一轮...这句不在转写里面啊啊啊")]
    ex, p = _setup(conn, data_dir, monkeypatch, recs)
    pipeline._finish(conn, "v1", "c", ex, p)
    msg = _outbox(conn)
    assert "⚠️ 丢弃 1 条：evidence 非转写子串 1 条" in msg
    assert "⚠️ 疑似漏提：GLM-5.3 × 钻石 D003（按名单推断）" in msg
    assert msg.count("vr明细 v1") == 1


def test_finish_receipt_unchanged_without_warnings(conn, data_dir, monkeypatch):
    recs = [_rec("Kimi K3", "黄金", "G006", "Kimi K3 第一轮的修改结果 没有问题恭喜加两分"),
            _rec("GLM5.3", "黄金", "G006", "GLM5.3 第一轮的修改结果 同样正确加两分")]
    ex, p = _setup(conn, data_dir, monkeypatch, recs)
    pipeline._finish(conn, "v1", "c", ex, p)
    counts = {"auto_ok": 0, "pending": 0}
    for r in db.list_decisions_for_video(conn, "v1"):
        counts["auto_ok" if r["decision"] == db.AUTO_OK else "pending"] += 1
    expect = (f"✅ 已处理：Kimi K3 对战 GLM5.3（v1，rev {ex['result_rev']}）\n"
              f"提取 2 条记录（自动上榜 {counts['auto_ok']}，待确认 {counts['pending']}）")
    if counts["pending"]:
        expect += "\n👉 查看明细：vr明细 v1\n👉 批量确认：vr确认 v1"
    assert _outbox(conn) == expect


def test_finish_no_content_carries_drop_line_and_detail(conn, data_dir, monkeypatch):
    recs = [_rec("Kimi K3", "黄金", "G006", "完全不存在于转写中的一句伪证据文本")]
    ex, p = _setup(conn, data_dir, monkeypatch, recs)
    assert ex.get("no_content")
    pipeline._finish(conn, "v1", "c", ex, p)
    msg = _outbox(conn)
    assert "⚠️ 丢弃 1 条：evidence 非转写子串 1 条" in msg and "👉 查看明细：vr明细 v1" in msg
