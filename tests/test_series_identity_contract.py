"""身份契约（plan M01）：种子 canonical 字节等价 → 同输入 rid 不变；撞名拒绝；
UNKNOWN→真名后旧 approved 判 stale 且新 rid 独立。"""
import pytest

from core import db, extract as ex_mod, models


def _legacy_rec(canonical):
    return {"model_canonical": canonical, "model_raw": "raw", "bug_level": "黄金",
            "bug_id": "G001", "score": 2, "rounds": 1, "solved": True,
            "evidence_quote": "证据句子够长够长", "confidence": 0.9}


def _v2_rec(canonical, series, version, variant):
    r = _legacy_rec(canonical)
    r.update(model_series=series, model_version=version, model_variant=variant)
    return r


def test_rid_unchanged_legacy_vs_v2_same_canonical():
    # rid 只由 canonical+raw+level+bug_slot+score 决定；v2 增的 series 字段不进 rid
    for s, v, var in [("GLM", "5.3", ""), ("DeepSeek", "4", "Pro"), ("GPT", "5.6", "Luna")]:
        c = models.compose_canonical(s, v, var)
        rid_legacy = ex_mod.record_id("aw1", _legacy_rec(c))
        rid_v2 = ex_mod.record_id("aw1", _v2_rec(c, s, v, var))
        assert rid_legacy == rid_v2, f"{c} rid 变了"


def test_collision_triple_rejected():
    data = {"A": {"format": "X {v}"}, "B": {"format": "X {v}"}}
    with pytest.raises(models.ConfigError):
        models.compose_canonical("A", "1", "", data=data)


def test_unknown_to_realname_old_approved_stale_new_rid_independent(conn):
    aid = "awX"
    # 旧格式（无 schema_rev）：一条 UNKNOWN 记录，人工 approved
    ex_old = {"video_id": aid, "title": "t", "extracted_at": "x",
              "extractor_version": "token_bug/1", "prompt_hash": "p1", "asr_model": "gladia-v2",
              "records": [dict(_legacy_rec("UNKNOWN"), model_raw="糊模型")]}
    ex_mod.apply_decisions(conn, aid, ex_old)
    rid_unknown = ex_mod.record_id(aid, ex_old["records"][0])
    db.upsert_decision(conn, record_id=rid_unknown, aweme_id=aid, decision=db.APPROVED,
                       extractor_version="token_bug/1", prompt_hash="p1",
                       approved_by="admin", approved_at=1.0)
    # 升级后重提：同一条现在锚定为真名（rid 变化）
    ex_new = {"video_id": aid, "title": "t", "extracted_at": "y",
              "extractor_version": "token_bug/2", "prompt_hash": "p2", "asr_model": "gladia-v2",
              "records": [_v2_rec("GLM-5.3", "GLM", "5.3", "")]}
    counts = ex_mod.apply_decisions(conn, aid, ex_new,
                                    known={("GLM", "5.3", "")})
    rid_real = ex_mod.record_id(aid, ex_new["records"][0])
    assert rid_real != rid_unknown
    # 旧 UNKNOWN approved 记录本次缺失 → stale（approved_stale 计数）
    assert db.get_decision(conn, rid_unknown)["decision"] == db.STALE
    assert counts["approved_stale"] == 1
    # 新真名 rid 独立存在，走本次分类（已知版本 + 高 conf → auto_ok）
    assert db.get_decision(conn, rid_real)["decision"] == db.AUTO_OK
