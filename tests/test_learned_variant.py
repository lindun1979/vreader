"""V-M17：人工校正引入的新变体无条件接受——登记即持久生效（读回不判坏）、此后自动识别；
未知系列/非法名/自动路径的未登记变体仍拒。"""
import json

import pytest

from core import config, db, extract as ex_mod, models, routing

EV = "王者bugK001双子星做对了呀"
TX = " ".join([EV, "其他内容没有问题"])


def _rec(canonical="UNKNOWN", series="", version="", variant="", *, raw="Gemini 4 Argon"):
    solved, score, rounds = ex_mod.derive_from_round("王者", 1)
    return {"model_canonical": canonical, "model_raw": raw, "model_series": series,
            "model_version": version, "model_variant": variant, "bug_level": "王者",
            "bug_id": "K001", "solved_round": 1, "score": score, "rounds": rounds,
            "solved": solved, "evidence_quote": EV, "confidence": 0.95}


def _setup_unknown(conn, aid):
    recs = [_rec()]
    ex = {"schema_rev": 2, "video_id": aid, "title": "t", "extracted_at": "x",
          "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
          "records": recs, "result_rev": ex_mod.result_rev(aid, recs)}
    d = config.video_dir("token_bug", aid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    (d / "transcript.txt").write_text(TX, encoding="utf-8")
    ex_mod.apply_decisions(conn, aid, ex, known=db.list_known_versions(conn))
    return ex_mod.record_id(aid, recs[0])


def _corr(rid, series, version, variant):
    return routing.Correction(rid=rid[:8], result_rev="", series=series,
                              version=version, variant=variant)


def test_new_variant_correction_accepted_and_persists(conn):
    aid = "vargon"
    rid = _setup_unknown(conn, aid)
    ok, msg = ex_mod.correct_and_confirm(conn, aid, _corr(rid, "gemini", "4", "Argon"), "admin")
    assert ok, msg
    assert "新变体 Gemini Argon" in msg
    assert ("Gemini", "4", "Argon") in db.list_known_versions(conn)
    p = config.video_dir("token_bug", aid) / "extract.json"
    # 读回校验通过（不被判坏 .bad 重建）
    ex = ex_mod.load_valid_extract(str(p), transcript=TX, expected_video_id=aid)
    assert ex is not None and ex["records"][0]["model_canonical"] == "Gemini 4 Argon"
    assert ex_mod.record_id(aid, ex["records"][0]) in db.board_visible_ids(conn)
    assert "Argon" in models.load_series()["Gemini"]["variants"]


def test_learned_variant_reused_case_insensitively(conn):
    rid = _setup_unknown(conn, "va1")
    assert ex_mod.correct_and_confirm(conn, "va1", _corr(rid, "Gemini", "4", "Argon"), "a")[0]
    assert models.normalize_triple("Gemini", "4.1", "argon")[0] == "Gemini 4.1 Argon"


def test_learned_variant_auto_recognized_later(conn):
    rid = _setup_unknown(conn, "va2")
    assert ex_mod.correct_and_confirm(conn, "va2", _corr(rid, "Gemini", "4", "Argon"), "a")[0]
    data = models.load_series()
    anchors = models.build_anchors("王者 gemini 4 argon 第一轮做对", data=data)
    known = db.list_known_versions(conn)
    got = models.resolve_record("gemini 4 argon", "Gemini", "4", "Argon", anchors,
                                data=data, known=known)
    assert got == ("Gemini 4 Argon", "Gemini", "4", "Argon")


def test_unregistered_variant_still_unknown_on_auto_path(conn):
    data = models.load_series()
    anchors = models.build_anchors("gemini 4 neon", data=data)
    got = models.resolve_record("gemini 4 neon", "Gemini", "4", "Neon", anchors,
                                data=data, known=db.list_known_versions(conn))
    assert got[0] == "UNKNOWN"


def test_unknown_series_still_rejected(conn):
    rid = _setup_unknown(conn, "va3")
    ok, msg = ex_mod.correct_and_confirm(conn, "va3", _corr(rid, "Foo", "1", "Bar"), "a")
    assert not ok and "未知系列" in msg


@pytest.mark.parametrize("bad", ["Ar gon", "x" * 25, "a/b"])
def test_invalid_new_variant_name_rejected(bad):
    with pytest.raises(models.ConfigError):
        models.normalize_triple("Gemini", "4", bad, allow_new_variant=True)


def test_new_variant_not_accepted_without_flag():
    with pytest.raises(models.ConfigError):
        models.normalize_triple("Gemini", "4", "Argon")


def test_load_series_without_db_is_plain_yml():
    assert "Argon" not in models.load_series()["Gemini"].get("variants", {})
