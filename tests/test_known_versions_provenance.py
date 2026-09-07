"""Step 2/3 溯源（plan M08）：两类快照分别保存、线程交错（提取=A、决策=B、A 不改写）、
DELETE 登记后仍能从产物还原两集合并核对摘要、静态 prompt_hash 与 rid 不变。"""
import json
import threading

from core import config, db, extract as ex_mod, pipeline

TX = "青铜这题 glm5.3 一次就修对了，全程逻辑清晰表现不错。"


def _claude():
    return json.dumps({"records": [{
        "model_raw": "glm5.3", "model_series": "GLM", "model_version": "5.3", "model_variant": "",
        "bug_level": "青铜", "bug_id": "B001", "score": 1,
        "evidence_quote": "glm5.3 一次就修对了", "confidence": 0.9}]}, ensure_ascii=False)


def _paths(aid):
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    return {"dir": d, "extract": str(d / "extract.json")}


def test_two_snapshots_saved_separately_and_a_not_rewritten(conn, db_path):
    aid = "prov1"
    p = _paths(aid)
    order = {"built": threading.Event(), "confirmed": threading.Event()}
    result = {}

    def worker():
        c = db.connect(db_path)
        try:
            known_a = db.list_known_versions(c)          # 提取输入集合 A（build 前）
            ex = ex_mod.build_extract(aweme_id=aid, title="t", transcript=TX,
                                      claude_text=_claude(), known=known_a)
            import core.util as util
            util.atomic_write_text(p["extract"], json.dumps(ex, ensure_ascii=False))
            result["A"] = set(known_a)
            result["ex"] = ex
            order["built"].set()
            order["confirmed"].wait(5)                    # 等确认线程登记新版本
            with pipeline.lock.publish_lock:
                known_b = db.list_known_versions(c)       # 决策时点集合 B（锁内读已提交）
                ex_mod.apply_decisions(c, aid, ex, known=known_b)
                pipeline._write_decision_snapshot(ex, p, known_b)
            result["B"] = set(known_b)
        finally:
            c.close()

    def confirmer():
        c = db.connect(db_path)
        try:
            order["built"].wait(5)                        # 确认发生在提取开始后
            db.register_known_version(c, "GLM", "9.9", "", "admin", commit=True)
            order["confirmed"].set()
        finally:
            c.close()

    tw, tc = threading.Thread(target=worker), threading.Thread(target=confirmer)
    tw.start(); tc.start(); tc.join(5); tw.join(5)

    ex = result["ex"]
    A, B = result["A"], result["B"]
    assert ("GLM", "9.9", "") in B and ("GLM", "9.9", "") not in A  # B ⊋ A
    # 提取快照 = A（永不改写），且摘要匹配
    used_a = {tuple(t) for t in ex["known_versions_used"]}
    assert used_a == A
    assert ex["known_versions_snapshot"] == ex_mod._snapshot(A)
    # 决策快照 = B，独立字段
    used_b = {tuple(t) for t in ex["known_versions_used_at_decision"]}
    assert used_b == B
    assert "decided_at" in ex


def test_delete_registration_still_reconstructs_from_artifact(conn):
    aid = "prov2"
    p = _paths(aid)
    known_a = db.list_known_versions(conn)
    ex = ex_mod.build_extract(aweme_id=aid, title="t", transcript=TX,
                              claude_text=_claude(), known=known_a)
    # 决策集合 B 含一个额外登记
    db.register_known_version(conn, "GLM", "9.9", "", "admin", commit=True)
    known_b = db.list_known_versions(conn)
    pipeline._write_decision_snapshot(ex, p, known_b)
    # 删除当前登记（M11 修复路径）后，仍能从 extract.json 本体还原两个集合并核对摘要
    conn.execute("DELETE FROM known_versions WHERE series='GLM' AND version='9.9'")
    conn.commit()
    disk = json.loads(open(p["extract"], encoding="utf-8").read())
    recon_a = {tuple(t) for t in disk["known_versions_used"]}
    recon_b = {tuple(t) for t in disk["known_versions_used_at_decision"]}
    assert ex_mod._snapshot(recon_a) == disk["known_versions_snapshot"]
    assert ("GLM", "9.9", "") in recon_b and ("GLM", "9.9", "") not in recon_a


def test_static_prompt_hash_and_rid_stable(conn):
    aid = "prov3"
    # A 与 B 不同（注入了新版本）不改变静态 prompt_hash，也不改变同事实 rid
    ex_a = ex_mod.build_extract(aweme_id=aid, title="t", transcript=TX, claude_text=_claude(),
                                known=set())
    ex_b = ex_mod.build_extract(aweme_id=aid, title="t", transcript=TX, claude_text=_claude(),
                                known={("GLM", "9.9", "")})
    assert ex_a["prompt_hash"] == ex_b["prompt_hash"]                      # 静态，不含 known
    assert ex_a["prompt_full_hash"] != ex_b["prompt_full_hash"]            # 实际 prompt 含 known
    assert ex_mod.record_id(aid, ex_a["records"][0]) == ex_mod.record_id(aid, ex_b["records"][0])
