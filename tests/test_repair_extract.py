"""WP-A：ops/repair_extract.py 受控外科式修复（合成数据，conftest 隔离 data_dir）。"""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from core import db, extract, pipeline

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))
import repair_extract as rx  # noqa: E402

AID = "v42"
TX = ("黄金G001 Kimi K3 第一轮的修改结果 没有问题恭喜加两分 "
      "黄金G001 GLM5.3 第一轮的修改结果 同样正确加两分 "
      "钻石D003 Kimi K3 第一轮的修改结果 还是没做对 "
      "钻石D003 Kimi K3 第二轮的修改结果 这回做对了加两分 "
      "钻石D003 GLM5.3 第一轮的修改结果 做对了加三分")
LLM = [
    {"model_raw": "Kimi K3", "bug_level": "黄金", "bug_id": "G001", "solved_round": 1,
     "evidence_quote": "Kimi K3 第一轮的修改结果 没有问题恭喜加两分", "confidence": 0.9},
    {"model_raw": "GLM5.3", "bug_level": "黄金", "bug_id": "", "solved_round": 1,
     "evidence_quote": "GLM5.3 第一轮的修改结果 同样正确加两分", "confidence": 0.9},
    {"model_raw": "Kimi K3", "bug_level": "钻石", "bug_id": "D003", "solved_round": 1,
     "evidence_quote": "钻石D003 Kimi K3 第一轮的修改结果 还是没做对", "confidence": 0.9},
]
ADD = [
    {"model_raw": "Kimi K3", "model_series": "Kimi", "model_version": "3", "model_variant": "",
     "bug_level": "钻石", "bug_id": "D003", "solved_round": 2, "confidence": 0.9,
     "evidence_quote": "钻石D003 Kimi K3 第二轮的修改结果 这回做对了加两分"},
    {"model_raw": "GLM5.3", "model_series": "GLM", "model_version": "5.3", "model_variant": "",
     "bug_level": "钻石", "bug_id": "D003", "solved_round": 1, "confidence": 0.9,
     "evidence_quote": "钻石D003 GLM5.3 第一轮的修改结果 做对了加三分"},
]


def _spec(**over):
    s = {"aweme_id": AID,
         "edit": [{"match": {"model_raw": "GLM5.3", "bug_level": "黄金"}, "set": {"bug_id": "G001"}}],
         "delete": [{"match": {"model_raw": "Kimi K3", "bug_id": "D003", "solved_round": 1}}],
         "add": ADD, "expect_total": {"Kimi K3": 4, "GLM-5.3": 5}}
    s.update(over)
    return s


@pytest.fixture
def env(conn, data_dir):
    """产物 + DB 已按正常管线落定（含一次 _finish 等价的决策与渲染）。"""
    # 手动把 GLM 黄金 / Kimi 钻石 决策铺好，避开 B（回填）影响：直接组装 extract
    ex = extract.build_extract(aweme_id=AID, title="Kimi K3 对战 GLM5.3", transcript=TX,
                               claude_text=json.dumps({"records": LLM}, ensure_ascii=False))
    for r in ex["records"]:  # 还原事故形态：空 bug_id 未被回填
        if r["model_raw"] == "GLM5.3":
            r["bug_id"] = ""
    ex["result_rev"] = extract.result_rev(AID, ex["records"])
    p = pipeline._paths(AID)
    p["dir"].mkdir(parents=True, exist_ok=True)
    Path(p["transcript"]).write_text(TX, encoding="utf-8")
    Path(p["extract"]).write_text(json.dumps(ex, ensure_ascii=False, indent=2), encoding="utf-8")
    extract.apply_decisions(conn, AID, ex, known=db.list_known_versions(conn))
    pipeline.render_board(conn)
    return p


def _write_spec(tmp_path, spec):
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return f, hashlib.sha256(f.read_bytes()).hexdigest()


def _snap(data_dir, p):
    c = db.connect(data_dir / "vreader.db")
    try:
        rows = {r["record_id"]: r["decision"] for r in db.list_decisions_for_video(c, AID)}
    finally:
        c.close()
    board = (data_dir / "token_bug" / "board.md").read_bytes()
    return Path(p["extract"]).read_bytes(), rows, board


def _run(data_dir, spec_path, sha, apply=False):
    argv = ["--data", str(data_dir), "--spec", str(spec_path), "--expect-spec-sha256", sha]
    return rx.main(argv + (["--apply"] if apply else []))


def test_dry_run_writes_nothing(env, data_dir, tmp_path, capsys):
    f, sha = _write_spec(tmp_path, _spec())
    dbf = data_dir / "vreader.db"
    before = (Path(env["extract"]).read_bytes(), dbf.read_bytes())
    assert _run(data_dir, f, sha) == 0
    assert (Path(env["extract"]).read_bytes(), dbf.read_bytes()) == before
    assert not (data_dir / "backup").exists()
    assert "dry-run" in capsys.readouterr().out


def test_match_must_hit_exactly_one(env, data_dir, tmp_path):
    f, sha = _write_spec(tmp_path, _spec(delete=[{"match": {"model_raw": "Kimi K3"}}]))
    before = _snap(data_dir, env)
    assert _run(data_dir, f, sha, apply=True) != 0
    assert _snap(data_dir, env) == before


def test_dry_run_matches_apply_with_persistent_decision(env, conn, data_dir, tmp_path, monkeypatch):
    rows = db.list_decisions_for_video(conn, AID)
    ex = json.loads(Path(env["extract"]).read_text(encoding="utf-8"))
    kimi_g = next(r for r in ex["records"] if r["model_raw"] == "Kimi K3" and r["bug_level"] == "黄金")
    kimi_d = next(r for r in ex["records"] if r["bug_level"] == "钻石")
    rid_ok = extract.record_id(AID, kimi_g)
    rid_rej = extract.record_id(AID, kimi_d)
    conn.execute("UPDATE record_decisions SET decision=? WHERE record_id=?", (db.APPROVED, rid_ok))
    conn.execute("UPDATE record_decisions SET decision=? WHERE record_id=?", (db.REJECTED_CONFLICT, rid_rej))
    conn.commit()
    assert len(rows) == 3
    captured = {}
    orig = rx._verify

    def spy(expected, actual):
        captured["e"], captured["a"] = expected, actual
        return orig(expected, actual)
    monkeypatch.setattr(rx, "_verify", spy)
    f, sha = _write_spec(tmp_path, _spec())
    assert _run(data_dir, f, sha, apply=True) == 0
    assert captured["e"] == captured["a"]
    assert captured["a"][rid_ok] == db.APPROVED          # 恒存继承
    assert captured["a"][rid_rej] == db.REJECTED_CONFLICT  # 删除后仍恒存（stale 豁免）
    ex2 = json.loads(Path(env["extract"]).read_text(encoding="utf-8"))
    assert len(ex2["records"]) == 4 and "known_versions_used_at_decision" in ex2
    assert list((data_dir / "backup").iterdir())


def test_apply_mismatch_restores_extract_db_and_board_from_fresh_connection(env, data_dir, tmp_path, monkeypatch):
    before = _snap(data_dir, env)
    monkeypatch.setattr(rx, "_verify", lambda e, a: False)
    f, sha = _write_spec(tmp_path, _spec())
    assert _run(data_dir, f, sha, apply=True) != 0
    fresh = sqlite3.connect(str(data_dir / "vreader.db"))
    fresh.row_factory = sqlite3.Row
    rows = {r["record_id"]: r["decision"] for r in
            fresh.execute("SELECT record_id, decision FROM record_decisions WHERE aweme_id=?", (AID,))}
    fresh.close()
    assert rows == before[1]
    assert _snap(data_dir, env) == before


def test_spec_sha_mismatch_refuses(env, data_dir, tmp_path):
    f, _ = _write_spec(tmp_path, _spec())
    before = _snap(data_dir, env)
    assert _run(data_dir, f, "0" * 64, apply=True) != 0
    assert _snap(data_dir, env) == before
    assert not (data_dir / "backup").exists()


def test_expect_total_mismatch_refuses(env, data_dir, tmp_path):
    f, sha = _write_spec(tmp_path, _spec(expect_total={"Kimi K3": 5, "GLM-5.3": 5}))
    before = _snap(data_dir, env)
    assert _run(data_dir, f, sha, apply=True) != 0
    assert _snap(data_dir, env) == before
    assert not (data_dir / "backup").exists()


def test_apply_refuses_when_lock_held(env, data_dir, tmp_path):
    from core import lock
    f, sha = _write_spec(tmp_path, _spec())
    held = lock.DataDirLock(data_dir)
    assert held.acquire()
    try:
        assert _run(data_dir, f, sha, apply=True) == 3
    finally:
        held.release()
