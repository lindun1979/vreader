"""D-A 批量公布题：同组取最大值（不求和）、证据归属、容量闸、非法 batch、复核与明细展示。"""
import itertools
import json

from channels.token_bug import board
from core import config, db, extract, pipeline

QA = "除了S001之外其他八道题 混元4全部一轮作对"
QB = "剩下的那几道白银混元4也都是一轮作对"
QC = "白银混元4这几道一轮作对"            # 归一化后与 QC2 相同、原文不同（空白差异）
QC2 = "白银混元4这几道 一轮作对"
TX = (f"混元4正式版参加史山考核 白银S001 混元4 依然没有作对 {QA} {QB} {QC2} "
      "青铜混元4一道没做对 钻石D001 混元4第一轮做对")
KNOWN = {("Hunyuan", "4.0", "Preview")}


def _b(level, n, sr=1, q=QA, conf=0.9, **kw):
    r = {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4", "model_variant": "",
         "bug_level": level, "bug_id": "", "solved_round": sr, "batch_count": n,
         "evidence_quote": q, "confidence": conf}
    r.update(kw)
    return r


S001 = {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4", "model_variant": "",
        "bug_level": "白银", "bug_id": "S001", "solved_round": 0,
        "evidence_quote": "白银S001 混元4 依然没有作对", "confidence": 0.9}


def _build(recs, tx=TX):
    return extract.build_extract(aweme_id="v", title="混元4", transcript=tx,
                                 claude_text=json.dumps({"records": recs}, ensure_ascii=False),
                                 known=KNOWN)


def _batch(ex, level):
    return [r for r in ex["records"] if r.get("batch_slot") and r["bug_level"] == level]


def test_duplicate_same_quote_takes_max_not_sum():
    ex = _build([_b("白银", 1), _b("白银", 1)])
    assert len(_batch(ex, "白银")) == 1
    assert ex["batch_merged"][0]["merged"] == 1 and len(set(ex["batch_merged"][0]["quotes"])) == 1


def test_max_n_chooses_evidence_and_records_merge():
    ex = _build([_b("白银", 2, q=QB), _b("白银", 3, q=QA)])
    recs = _batch(ex, "白银")
    assert len(recs) == 3 and {r["evidence_quote"] for r in recs} == {QA}
    assert ex["batch_merged"] == [{"model": "Hunyuan 4.0", "level": "白银", "score": 1, "merged": 1,
                                   "quotes": sorted([QA, QB])}]


def test_tie_on_n_is_order_independent():
    outs = {tuple(sorted(r["evidence_quote"] for r in _batch(_build(list(p)), "白银")))
            for p in itertools.permutations([_b("白银", 2, q=QB), _b("白银", 2, q=QA)])}
    assert len(outs) == 1
    outs2 = {tuple(r["evidence_quote"] for r in _batch(_build(list(p)), "白银"))
             for p in itertools.permutations([_b("白银", 2, q=QC2), _b("白银", 2, q=QC)])}
    assert len(outs2) == 1  # 归一化相同 → 按原文字典序


def test_different_results_not_merged():
    ex = _build([_b("青铜", 2), _b("青铜", 1, sr=0, q="青铜混元4一道没做对")])
    assert sorted(r["batch_slot"] for r in _batch(ex, "青铜")) == ["0-1", "1-1", "1-2"]
    assert "batch_merged" not in ex


def test_one_bad_evidence_uses_good_one():
    ex = _build([_b("白银", 2, q="这句话不在转写里面的胡编证据"), _b("白银", 2, q=QB)])
    recs = _batch(ex, "白银")
    assert len(recs) == 2 and {r["evidence_quote"] for r in recs} == {QB}
    assert any("evidence" in d["reason"] for d in ex["dropped"])


def test_capacity_gate_drops_level_batches_keeps_named():
    ex = _build([S001, _b("白银", 3)])
    assert _batch(ex, "白银") == [] and [r["bug_id"] for r in ex["records"]] == ["S001"]
    assert any(d["reason"] == "batch 超容量" for d in ex["dropped"])


def test_invalid_batch_dropped():
    ex = _build([_b("钻石", 1, q="钻石D001 混元4第一轮做对"), _b("白银", 10), _b("白银", 2, bug_id="S002")])
    assert ex["records"] == []
    assert sum(1 for d in ex["dropped"] if d["reason"].startswith("batch 非法")) == 3


def test_silver_named_fail_plus_batch_no_conflict(conn):
    ex = _build([S001, _b("白银", 2, q=QB)])
    counts = extract.apply_decisions(conn, "v", ex)
    assert counts.get(db.PENDING_CONFLICT, 0) == 0
    assert [r["bug_id"] for r in ex["records"] if not r.get("batch_slot")] == ["S001"]


def test_batch_hidden_until_approved_then_counted(conn):
    ex = _build([_b("青铜", 3)])
    extract.apply_decisions(conn, "v", ex)
    assert "Hunyuan" not in board.render([ex], db.board_visible_ids(conn), extract.record_id)
    db.approve_pending_for_video(conn, "v", "admin")
    md = board.render([ex], db.board_visible_ids(conn), extract.record_id)
    assert "| Hunyuan 4.0 | 3 | 3/3 |" in md


def test_detail_view_groups_batch(conn, data_dir):
    ex = _build([S001, _b("青铜", 3)])
    vd = config.video_dir("token_bug", "v")
    vd.mkdir(parents=True)
    (vd / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    extract.apply_decisions(conn, "v", ex)
    out = extract.detail_view(conn, "v")
    assert "批量 ×3（推断，未念编号）｜Hunyuan 4.0｜青铜｜第1轮做对" in out
    codes = next(ln for ln in out.splitlines() if "记录码：" in ln).split("：")[1].split()
    assert len(codes) == 3 and out.count("批量 ×") == 1


def test_merge_hint_in_receipt_without_basic_regex():
    ex = _build([_b("白银", 2, q=QB), _b("白银", 2, q=QA)])
    w = pipeline._receipt_warnings(ex, set(), transcript="今天没提基础题总数")
    assert any("批量描述被合并" in x for x in w)
