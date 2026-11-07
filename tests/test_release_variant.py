"""D-C：正式版（release_markers）判定表 + 跨变体 .0 写法（7704 混元4正式版回归）。"""
import pytest

from core import models

K = {("Hunyuan", "4.0", "Preview"), ("Hunyuan", "3.0", ""), ("Step", "3.7", "Flash")}

# 合成 7704 转写：正式版 + 糊系列名「混源4.0正式版」+ 旧版本「混元3」
TX_7704 = ("混元4正式版参加史山考核了 比如当初的混元3参加史山考核的时候是取得了6分 "
           "钻石BUG-001混源4.0正式版第一轮的修改结果 钻石bug第001 混元4第三轮的修改结果")
TITLE_7704 = "Hy4正式版挑战祖传代码"


def _resolve(raw, s, v, var, tx, title="", known=K):
    a = models.build_anchors(tx, title)
    return models.resolve_record(raw, s, v, var, a, known=known)[0]


def test_release_markers_not_a_series():
    assert "release_markers" not in models.load_series()
    assert "正式版" in models.release_markers()


@pytest.mark.parametrize("raw,s,v,var", [
    ("混元4", "Hunyuan", "4", ""),            # 第7行：源文本正式版，不补 Preview
    ("会员4正式版", "Hunyuan", "4", "正式版"),  # 第1行：raw 带标记（LLM 把正式版填进变体）
    ("混元4.0正式版", "Hunyuan", "4.0", ""),    # 不得被破歧成 Hunyuan 3.0
    ("混元4", "Hunyuan", "4.0", "Preview"),    # 第4行：LLM 臆测 Preview、无组合锚点
    ("混元4正式版", "Hunyuan", "4", "Preview"),  # 第1行优先于 LLM 变体
])
def test_7704_resolves_to_hunyuan_4_0(raw, s, v, var):
    assert _resolve(raw, s, v, var, TX_7704, TITLE_7704) == "Hunyuan 4.0"


def test_old_version_still_resolves():
    assert _resolve("混元3", "Hunyuan", "3", "", TX_7704, TITLE_7704) == "Hunyuan 3.0"


TX_BOTH = "今天混元4.0 preview和混元4.0正式版同场 混元4.0 preview先上"


def test_row2_raw_variant_word():
    assert _resolve("混元4.0preview", "Hunyuan", "4.0", "", TX_BOTH) == "Hunyuan 4.0 Preview"


def test_row3_llm_variant_with_combo_anchor():
    assert _resolve("混元4.0", "Hunyuan", "4.0", "Preview", TX_BOTH) == "Hunyuan 4.0 Preview"


def test_row5_llm_variant_without_anchor_or_release():
    assert _resolve("混元4", "Hunyuan", "4", "Preview", "混元4 很强") == "UNKNOWN"


def test_row6_release_and_variant_both_mentioned_is_unknown():
    assert _resolve("混元4.0", "Hunyuan", "4.0", "", TX_BOTH) == "UNKNOWN"


def test_row8_unique_known_variant_still_filled():
    assert _resolve("step3.7", "Step", "3.7", "", "step3.7 第一轮做对了") == "Step 3.7 Flash"


def test_marker_does_not_leak_to_other_model_in_same_sentence():
    tx = "GLM5.3正式版和Step3.7 同场 step3.7 第一轮做对了"
    assert _resolve("step3.7", "Step", "3.7", "", tx) == "Step 3.7 Flash"
    assert models.build_anchors(tx)["release"] == {"GLM": {"5.3"}}


@pytest.mark.parametrize("text,hit", [("gaming", False), ("megastep", False), ("Grok GA", True),
                                      ("混元4正式版", True), ("release", True), ("prerelease", False)])
def test_latin_marker_word_boundary(text, hit):
    assert models._has_marker(text) is hit


@pytest.mark.parametrize("known,args,want", [
    ({("S", "5", "")}, ("S", "5", ""), "5"),                                   # 精确
    ({("S", "5.0", "")}, ("S", "5", ""), "5.0"),                               # 同变体孪生
    ({("S", "5.0", "Pro")}, ("S", "5", ""), "5.0"),                            # 跨变体只用孪生
    ({("S", "5.0", "Pro"), ("S", "5", "Max")}, ("S", "5", ""), "5"),           # 两种写法并存 → 不猜
    ({("S", "6", "")}, ("S", "5", ""), "5"),                                   # 都没有
])
def test_known_spelling_truth_table(known, args, want):
    assert models.known_spelling(known, *args) == want


def test_normalize_triple_cross_variant_spelling():
    c, s, v, var = models.normalize_triple("Hunyuan", "4", "", known=K)
    assert (c, v, var) == ("Hunyuan 4.0", "4.0", "")


def test_bulk_confirm_blocks_cross_variant_twin(conn):
    """旧产物里的 Hunyuan 4（无变体）在 Hunyuan|4.0|Preview 已登记时被批量确认拦截，提示改为 4.0。"""
    import json
    from core import config, db, extract
    aid = "x_twin"
    db.register_known_version(conn, "Hunyuan", "4.0", "Preview", "admin", commit=True)
    r = {"model_canonical": "Hunyuan 4", "model_raw": "混元4", "model_series": "Hunyuan",
         "model_version": "4", "model_variant": "", "bug_level": "黄金", "bug_id": "G001",
         "solved_round": 1, "solved": True, "score": 2, "rounds": 1,
         "evidence_quote": "混元4 第一轮做对了", "confidence": 0.9}
    ex = {"schema_rev": 2, "video_id": aid, "title": "混元4", "extracted_at": "x",
          "extractor_version": "token_bug/2", "prompt_hash": "p", "asr_model": "gladia-v2",
          "records": [r], "result_rev": extract.result_rev(aid, [r])}
    p = config.video_dir("token_bug", aid)
    p.mkdir(parents=True, exist_ok=True)
    (p / "extract.json").write_text(json.dumps(ex, ensure_ascii=False), encoding="utf-8")
    rid = extract.record_id(aid, r)
    db.upsert_decision(conn, record_id=rid, aweme_id=aid, decision=db.PENDING_NEW_VERSION,
                       extractor_version="token_bug/2", prompt_hash="p", commit=True)
    assert extract.approve_pending_for_video(conn, aid, "admin") == (0, [])
    assert extract.twin_blocked_pending(conn, aid) == [(rid, "Hunyuan 4", "Hunyuan 4.0")]
