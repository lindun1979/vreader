"""Step 2 提及锚定 + 系列纠错（plan M02）。version/raw 拆词数字都必须过锚定。"""
from core import models


def _resolve(raw, series, version, variant, src, title=""):
    data = models.load_series()
    anchors = models.build_anchors(src, title, data=data)
    return models.resolve_record(raw, series, version, variant, anchors, data=data)


def test_grm5_3_anchors_to_glm():
    assert _resolve("GRM5.3", "GLM", "5.3", "", "博主让 GRM5.3 出场解题") == \
        ("GLM-5.3", "GLM", "5.3", "")


def test_gbt5_5_only_anchors_if_adjacent_present():
    # 原文无 gbt5.5 邻接 → 该候选版本被拒；无其它锚定 → UNKNOWN
    assert _resolve("gbt5.5", "GPT", "5.5", "", "今天没有 GPT 出场，只有 GLM5.3")[0] == "UNKNOWN"
    # 原文真有 gbt5.6 luna 邻接 → 锚定成功
    assert _resolve("gbt5.6luna", "GPT", "5.6", "Luna", "gbt5.6luna 强势登场")[0] == "GPT 5.6 Luna"


def test_borrowed_version_rejected_falls_to_coref_unique():
    # LLM 给 GLM version=4.6（借了 Grok 的数字）→ 4.6 无 GLM 锚 → 置空进共指；
    # GLM 唯一锚定组合 (5.3,'') → 落 GLM-5.3
    assert _resolve("", "GLM", "4.6", "", "GLM 5.3 和 Grok 4.6 对战") == \
        ("GLM-5.3", "GLM", "5.3", "")


def test_borrowed_version_rejected_ambiguous_unknown():
    # 借数字被拒 + 共指候选多于一个（5.2 与 5.3 都锚定）→ UNKNOWN
    assert _resolve("", "GLM", "4.6", "", "GLM 5.2 和 GLM 5.3 和 Grok 4.6 三方混战")[0] == "UNKNOWN"


def test_fabricated_raw_rejected():
    # 编造 raw=GRM5.4 + 真实 evidence（原文无 GRM5.4 邻接）→ 5.4 无锚 → 共指落唯一 5.3
    # 关键：编造的 5.4 不被采用（不产生 GLM-5.4）
    out = _resolve("GRM5.4", "GLM", "5.4", "", "原文只提到 GLM5.3")
    assert out[0] != "GLM-5.4"
    assert out == ("GLM-5.3", "GLM", "5.3", "")


def test_title_only_mention_anchors():
    assert _resolve("", "Grok", "4.6", "", "对战正式开始", "中年K3大战青年Grok4.6") == \
        ("Grok 4.6", "Grok", "4.6", "")


def test_opus_4_8_is_distinct_model():
    # Opus 4.8 与 Opus 5 是不同模型（用户 2026-09 确认，version_map 已去 4.8→5）
    assert _resolve("opus", "Claude Opus", "4.8", "", "测试 opus 4.8 解题") == \
        ("Claude Opus 4.8", "Claude Opus", "4.8", "")
    assert _resolve("opus", "Claude Opus", "5", "", "测试 opus 5 解题") == \
        ("Claude Opus 5", "Claude Opus", "5", "")


def test_series_corrected_discards_llm_version():
    # raw=grm5.3 纠正系列为 GLM（LLM 误判 GPT）→ 弃 LLM version，用 raw 数字 5.3
    assert _resolve("grm5.3", "GPT", "5.6", "Luna", "grm5.3 vs gbt5.6 luna") == \
        ("GLM-5.3", "GLM", "5.3", "")


def test_llm_nonempty_version_must_pass_anchor():
    # LLM 非空 version 无豁免：给 GLM version=9.9 无锚 → 共指落唯一 5.3
    assert _resolve("glm", "GLM", "9.9", "", "只有 glm5.3 出现") == ("GLM-5.3", "GLM", "5.3", "")
