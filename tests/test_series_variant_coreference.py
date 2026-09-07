"""Step 2 昵称共指 + 变体传递（plan M03）：组合锚定约束是所有输出路径的总闸。"""
from core import models


def _resolve(raw, series, version, variant, src, title=""):
    data = models.load_series()
    anchors = models.build_anchors(src, title, data=data)
    return models.resolve_record(raw, series, version, variant, anchors, data=data)


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
