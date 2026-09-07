"""Step 1 身份守卫：compose_canonical 字节等价 + norm_version + 撞名拒绝（plan M01）。"""
import re

import pytest

from core import models


LEGACY_TRIPLES = [
    ("GLM", "5.3", ""), ("GLM", "5.2", ""), ("GLM", "5.3", "Flash"),
    ("DeepSeek", "4", ""), ("DeepSeek", "4", "Pro"), ("DeepSeek", "4", "Flash"),
    ("Qwen", "3.8", ""), ("Qwen", "3.8", "Flash"), ("Kimi", "3", ""),
    ("Hunyuan", "3.0", ""), ("MiMo", "2.5", "Pro"), ("Doubao Seed", "2.1", ""),
    ("Step", "3.7", "Flash"), ("Muse Spark", "1.2", ""), ("Longcat", "2.0", ""),
    ("Fable", "5.1", ""), ("Claude Opus", "5", ""), ("Claude Sonnet", "4.5", ""),
    ("GPT", "5.6", "Luna"), ("Gemini", "3.7", "Flash"), ("Grok", "4.5", ""),
    ("Grok", "4.6", ""),
]


def test_all_legacy_canonicals_compose_byte_equal():
    got = {models.compose_canonical(s, v, var) for s, v, var in LEGACY_TRIPLES}
    assert got == set(models.LEGACY_CANONICALS)


def test_opus_version_map_only_5_0_collapses():
    # 4.8 与 5.0 是不同模型（用户确认）；仅 5.0→5 归一
    assert models.compose_canonical("Claude Opus", "4.8") == "Claude Opus 4.8"
    assert models.compose_canonical("Claude Opus", "5.0") == "Claude Opus 5"
    assert models.compose_canonical("Claude Opus", "5") == "Claude Opus 5"


def test_new_taxonomy_canonicals_compose():
    for c in ["Claude Opus 4.8", "Fable 5", "GPT 5.6 Sol", "GPT 5.6 Terra"]:
        # round-trip 反解唯一（无撞名）
        hits = models._reverse_parse_all(c, models.load_series())
        assert len(hits) == 1, f"{c} 反解不唯一: {hits}"


def test_norm_version_rules():
    assert models.norm_version("5点3") == "5.3"
    assert models.norm_version("v4") == "4"
    assert models.norm_version("V5.0") == "5.0"   # 不砍尾部 .0
    assert models.norm_version("３.７") == "3.7"    # 全角
    assert models.norm_version(" 4.6 ") == "4.6"


def test_variant_not_in_series_rejected():
    with pytest.raises(models.ConfigError):
        models.compose_canonical("GLM", "5.3", "Pro")  # GLM 无 Pro 变体


def test_reverse_parse_unique_for_each_legacy():
    data = models.load_series()
    for s, v, var in LEGACY_TRIPLES:
        c = models.compose_canonical(s, v, var, data=data)
        hits = models._reverse_parse_all(c, data)
        assert hits == [(s, v, var)], f"{c} 反解不唯一: {hits}"


def test_composition_collision_rejected():
    """构造两个 format 撞名的系列 → compose round-trip 反解到 2 个 → ConfigError。"""
    data = {
        "Alpha": {"format": "X {v}"},
        "Beta": {"format": "X {v}"},
    }
    with pytest.raises(models.ConfigError):
        models.compose_canonical("Alpha", "1", "", data=data)


def test_variant_collides_with_other_series_base_rejected():
    """seriesA 的 'Foo {v} Pro' 与 seriesB 'Foo {v}Pro'... 用真实撞名：
    A format 'M {v}' 变体 Pro → 'M 2 Pro'；B format 'M {v} Pro'（把 Pro 写进模板）
    → 'M 2 Pro' 也能被 B 反解。断言拒绝。"""
    data = {
        "A": {"format": "M {v}", "variants": {"Pro": {}}},
        "B": {"format": "M {v} Pro"},
    }
    with pytest.raises(models.ConfigError):
        models.compose_canonical("A", "2", "Pro", data=data)
