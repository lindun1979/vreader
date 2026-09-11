"""Step 2 昵称共指 + 变体传递（plan M03）：组合锚定约束是所有输出路径的总闸。"""
from core import models


def _resolve(raw, series, version, variant, src, title="", known=None):
    data = models.load_series()
    anchors = models.build_anchors(src, title, data=data)
    return models.resolve_record(raw, series, version, variant, anchors, data=data, known=known)


def _seed_known():
    import tempfile
    from core import db
    p = tempfile.mktemp(suffix=".db")
    db.init(p)
    c = db.connect(p)
    k = db.list_known_versions(c)
    c.close()
    return k


def test_bare_nickname_unique_combo_adopts_variant():
    # 视频里只有 GLM-5.3 Flash → 裸「一哥」整体采用该组合（含 variant）
    assert _resolve("一哥", "GLM", "", "", "视频里只有 glm5.3 flash 出场") == \
        ("GLM-5.3 Flash", "GLM", "5.3", "Flash")


def test_bare_nickname_ambiguous_unknown():
    # 多个锚定组合 → UNKNOWN（禁默认最新）
    assert _resolve("一哥", "GLM", "", "", "glm5.2 和 glm5.3 都出场")[0] == "UNKNOWN"


def test_variant_nickname_no_compatible_candidate_version_empty():
    # 小一哥→Flash，源文本只有 GLM 5.2（无 Flash 锚）→ UNKNOWN（LLM version 为空）
    assert _resolve("小一哥", "GLM", "", "", "源文本只有 GLM 5.2 出现")[0] == "UNKNOWN"


def test_variant_nickname_no_compatible_candidate_version_nonempty():
    # 同上但 LLM 预填 version=5.2 → 组合 (5.2,Flash) 无锚 → 仍 UNKNOWN（击穿修复）
    assert _resolve("小一哥", "GLM", "5.2", "Flash", "源文本只有 GLM 5.2 出现")[0] == "UNKNOWN"


def test_flash_anchor_empty_variant_output_unknown():
    # 源文本只有 Flash 锚点，LLM 填 version=5.3 variant='' → (5.3,'') 无锚 → UNKNOWN
    assert _resolve("glm", "GLM", "5.3", "", "源文本只有 glm5.3 flash")[0] == "UNKNOWN"


def test_variant_nickname_unique_flash_adopted():
    # 小一哥→Flash，源文本有 glm5.3 flash → 采用 (5.3,Flash)
    assert _resolve("小一哥", "GLM", "", "", "源文本有 glm5.3 flash 出场") == \
        ("GLM-5.3 Flash", "GLM", "5.3", "Flash")


def test_llm_prefilled_flash_without_anchor_unknown():
    # 反例1：源文本只有 GLM 5.2，LLM raw=小一哥 series=GLM version=5.2 variant=Flash
    # → (5.2,Flash) 无锚 → UNKNOWN，不产生 GLM-5.2 Flash
    assert _resolve("小一哥", "GLM", "5.2", "Flash", "源文本只有 GLM 5.2")[0] == "UNKNOWN"


def test_order_independence():
    # 打乱记录顺序结果不变（锚点集合与顺序无关）：两次不同 src 词序，同一锚定组合
    a = _resolve("一哥", "GLM", "", "", "先 glm5.3 flash 再别的")
    b = _resolve("一哥", "GLM", "", "", "别的先说 然后 glm5.3 flash")
    assert a == b == ("GLM-5.3 Flash", "GLM", "5.3", "Flash")


# ---------- 已知版本集共指（真实 ASR：裸名单版本模型 / 变体未口播）----------

def test_known_unique_fallback_bare_name_no_version_in_video():
    # 「豆包」版本从不口播，本视频无锚点 → 已知版本集唯一 → Doubao Seed 2.1
    known = _seed_known()
    assert _resolve("豆包豆姐", "", "", "", "豆包豆姐上场解题", known=known) == \
        ("Doubao Seed 2.1", "Doubao Seed", "2.1", "")


def test_known_fallback_refuses_when_raw_has_unanchored_version():
    # 防御性拦截：raw 明明带版本号（豆包2.3），但系列名被 ASR 糊成「豆报」锚不上 →
    # 不再静默贴最近已知版本 2.1，而是走待确认（UNKNOWN）。根因：muse spark→muse bug 把 1.3 贴成 1.2。
    known = _seed_known()
    assert _resolve("豆包2.3", "", "", "", "豆报2.3 上场解题", known=known)[0] == "UNKNOWN"


def test_known_disambiguates_ambiguous_in_video():
    # 本视频锚定歧义（ASR 把「豆包2.1」也糊出个「豆包2」）→ 已知版本破歧到 2.1
    known = _seed_known()
    assert _resolve("豆包", "", "", "", "豆包2 又说 豆包2.1 都出场", known=known) == \
        ("Doubao Seed 2.1", "Doubao Seed", "2.1", "")


def test_known_variant_reconcile_version_spoken_variant_unspoken():
    # 「steep3.7」版本口播 3.7、变体未口播；Step 仅一个已知变体 Flash → 补全为 Flash
    known = _seed_known()
    assert _resolve("steep3.7", "", "", "", "steep3.7 修复了这个 bug", known=known) == \
        ("Step 3.7 Flash", "Step", "3.7", "Flash")


def test_known_fallback_refuses_when_multiple_known_versions():
    # 反防：裸「格洛克」+ 无版本口播，Grok 有 2 个已知版本（4.5/4.6）→ UNKNOWN，不臆断
    known = _seed_known()
    assert _resolve("格洛克", "", "", "", "格洛克上场了", known=known)[0] == "UNKNOWN"


def test_known_never_overrides_spoken_version():
    # 版本口播 4.5 → 用口播值，不被 known 里的 4.6 篡改
    known = _seed_known()
    assert _resolve("grok4.5", "", "", "", "grok4.5 出场", known=known) == \
        ("Grok 4.5", "Grok", "4.5", "")


# ---------- 2026-09 用户新增模型：Opus 4.8 / Fable 5 / GPT Sol·Terra ----------

def test_gpt_three_variants_distinct():
    known = _seed_known()
    assert _resolve("GPT5.6soul", "", "", "", "gpt5.6soul 出场", known=known)[0] == "GPT 5.6 Sol"
    assert _resolve("GPT5.6terra", "", "", "", "gpt5.6terra 出场", known=known)[0] == "GPT 5.6 Terra"
    assert _resolve("GPT5.6luna", "", "", "", "gpt5.6luna 出场", known=known)[0] == "GPT 5.6 Luna"


def test_bug_id_glued_model_anchors():
    # ASR 常把 bug 编号与模型连写（S001GPT5.6soul）→ 仍能锚定 GPT 5.6 Sol
    known = _seed_known()
    assert _resolve("GPT5.6soul", "GPT", "5.6", "Sol",
                    "S001GPT5.6soul的修复结果 G003GPT5.6SOAP第二轮", known=known)[0] == "GPT 5.6 Sol"


def test_opus_4_8_and_5_distinct():
    known = _seed_known()
    assert _resolve("Opus4.8", "", "", "", "opus4.8 上场", known=known)[0] == "Claude Opus 4.8"
    assert _resolve("opus5", "", "", "", "opus5 上场", known=known)[0] == "Claude Opus 5"


def test_fable_5_and_5_1_distinct():
    known = _seed_known()
    assert _resolve("飞包5", "", "", "", "飞包5 出场", known=known)[0] == "Fable 5"
    assert _resolve("fable5.1", "", "", "", "fable5.1 出场", known=known)[0] == "Fable 5.1"
