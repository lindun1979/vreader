"""D-A 批量记录的人工修复路径：repair_extract 整组 delete + add（batch_slot / manual_triple / rid 去重门禁）。"""
import hashlib
import json
import sys
from pathlib import Path

from core import db, extract, pipeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import repair_extract as rx  # noqa: E402

AID = "9000000000000000001"
QU = "会员这次白银另外两道也都是一轮作对"   # 系列名被 ASR 糊成「会员」，且无版本锚点
QH = "混元4正式版青铜这道一轮作对"
TX = f"混元4正式版参加史山考核 {QH} {QU}"
KNOWN = {("Hunyuan", "4.0", "Preview")}
LLM = [
    {"model_raw": "会员", "bug_level": "白银", "bug_id": "", "solved_round": 1, "batch_count": 2,
     "evidence_quote": QU, "confidence": 0.9},
    {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4", "model_variant": "",
     "bug_level": "青铜", "bug_id": "", "solved_round": 1, "batch_count": 1,
     "evidence_quote": QH, "confidence": 0.9},
]


def _add(slot, level="白银", q=QU, manual=True):
    a = {"model_raw": "混元4", "model_series": "Hunyuan", "model_version": "4.0", "model_variant": "",
         "bug_level": level, "bug_id": "", "batch_slot": slot, "solved_round": 1,
         "evidence_quote": q, "confidence": 0.6}
    if manual:
        a["manual_triple"] = True
    return a


def _spec(**over):
    s = {"aweme_id": AID,
         "delete": [{"match": {"model_canonical": "UNKNOWN", "bug_level": "白银", "batch_slot": "1-1"}},
                    {"match": {"model_canonical": "UNKNOWN", "bug_level": "白银", "batch_slot": "1-2"}}],
         "add": [_add("1-1"), _add("1-2")],
         "expect_total": {"Hunyuan 4.0": 3}}
    s.update(over)
    return s


def _env(conn):
    ex = extract.build_extract(aweme_id=AID, title="Hy4正式版", transcript=TX,
                               claude_text=json.dumps({"records": LLM}, ensure_ascii=False), known=KNOWN)
    assert sorted((r["model_canonical"], r.get("batch_slot")) for r in ex["records"]) == [
        ("Hunyuan 4.0", "1-1"), ("UNKNOWN", "1-1"), ("UNKNOWN", "1-2")]
    p = pipeline._paths(AID)
    Path(p["dir"]).mkdir(parents=True, exist_ok=True)
    Path(p["transcript"]).write_text(TX, encoding="utf-8")
    Path(p["extract"]).write_text(json.dumps(ex, ensure_ascii=False, indent=2), encoding="utf-8")
    extract.apply_decisions(conn, AID, ex, known=db.list_known_versions(conn))
    pipeline.render_board(conn)
    return p


def _run(data_dir, tmp_path, spec, apply=False, name="spec.json"):
    f = tmp_path / name
    f.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    sha = hashlib.sha256(f.read_bytes()).hexdigest()
    argv = ["--data", str(data_dir), "--spec", str(f), "--expect-spec-sha256", sha]
    return rx.main(argv + (["--apply"] if apply else []))


def _ex(p):
    return json.loads(Path(p["extract"]).read_text(encoding="utf-8"))


def test_manual_triple_group_rename_dry_run_then_apply(conn, data_dir, tmp_path, capsys):
    p = _env(conn)
    before = Path(p["extract"]).read_bytes()
    assert _run(data_dir, tmp_path, _spec()) == 0
    out = capsys.readouterr().out
    assert "[manual_triple]" in out and Path(p["extract"]).read_bytes() == before
    assert _run(data_dir, tmp_path, _spec(), apply=True) == 0
    recs = _ex(p)["records"]
    silver = [r for r in recs if r["bug_level"] == "白银"]
    assert sorted(r["batch_slot"] for r in silver) == ["1-1", "1-2"]
    assert {r["model_canonical"] for r in recs} == {"Hunyuan 4.0"}
    rids = [extract.record_id(AID, r) for r in recs]
    assert len(set(rids)) == 3
    decs = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(conn, AID)}
    assert all(decs[x] == db.PENDING_NEW_VERSION for x in rids)
    # 修复后 vr确认：登记版本并批准
    n, registered = extract.approve_pending_for_video(conn, AID, "admin")
    assert n == 3 and registered == ["Hunyuan 4.0"]
    assert ("Hunyuan", "4.0", "") in db.list_known_versions(conn)


def test_unanchored_version_needs_manual_triple(conn, data_dir, tmp_path):
    p = _env(conn)
    before = Path(p["extract"]).read_bytes()

    def spec(manual):
        adds = [_add("1-1", manual=manual), _add("1-2", manual=manual)]
        for x in adds:
            x["model_raw"], x["model_version"] = "会员", "5"   # 转写里没有任何「5」版本锚点
        return _spec(add=adds, expect_total={"Hunyuan 4.0": 1, "Hunyuan 5": 2})
    assert _run(data_dir, tmp_path, spec(False), apply=True, name="a.json") == 4
    assert Path(p["extract"]).read_bytes() == before
    assert _run(data_dir, tmp_path, spec(True), apply=True, name="b.json") == 0
    assert {r["model_canonical"] for r in _ex(p)["records"] if r["bug_level"] == "白银"} == {"Hunyuan 5"}


def test_evidence_must_be_transcript(conn, data_dir, tmp_path):
    p = _env(conn)
    before = Path(p["extract"]).read_bytes()
    spec = _spec(add=[_add("1-1", q="完全不在转写里的一句胡编证据"), _add("1-2")])
    assert _run(data_dir, tmp_path, spec, apply=True) == 4
    assert Path(p["extract"]).read_bytes() == before


def test_collision_with_existing_slot_refused(conn, data_dir, tmp_path):
    p = _env(conn)
    before = Path(p["extract"]).read_bytes()
    spec = _spec(add=[_add("1-1"), _add("1-2"), _add("1-1", level="青铜", q=QH)])  # 青铜 1-1 已存在
    spec["expect_total"] = {"Hunyuan 4.0": 4}
    assert _run(data_dir, tmp_path, spec, apply=True) == 4
    assert Path(p["extract"]).read_bytes() == before


def test_reapply_same_spec_refused(conn, data_dir, tmp_path):
    p = _env(conn)
    assert _run(data_dir, tmp_path, _spec(), apply=True) == 0
    after = Path(p["extract"]).read_bytes()
    assert _run(data_dir, tmp_path, _spec(), apply=True) == 4  # match 不到 UNKNOWN 了
    assert Path(p["extract"]).read_bytes() == after


def test_midway_failure_restores_backup(conn, data_dir, tmp_path, monkeypatch):
    p = _env(conn)
    before_ex = Path(p["extract"]).read_bytes()
    before_dec = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(conn, AID)}

    def boom(*a, **k):
        raise RuntimeError("模拟渲染崩溃")
    monkeypatch.setattr(rx.pipeline, "render_board", boom)
    monkeypatch.setattr(rx, "_restore", lambda bdir, dbp, exp: (
        rx._backup_db(bdir / "vreader.db", dbp, readonly=False),
        Path(exp).write_bytes((bdir / "extract.json").read_bytes())))
    assert _run(data_dir, tmp_path, _spec(), apply=True) == 6
    assert Path(p["extract"]).read_bytes() == before_ex
    c = db.connect(data_dir / "vreader.db")
    try:
        assert {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(c, AID)} == before_dec
    finally:
        c.close()


def test_lock_conflict_refused(conn, data_dir, tmp_path):
    from core import lock
    _env(conn)
    held = lock.DataDirLock(data_dir)
    assert held.acquire()
    try:
        assert _run(data_dir, tmp_path, _spec(), apply=True) == 3
    finally:
        held.release()
