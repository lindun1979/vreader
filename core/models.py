"""模型系列表（models.yml v2）加载 + 身份拼合/反解 + 提及锚定。

身份契约（plan M01）：
- canonical = series.format.format(v=版本) + (" "+variant if variant)，是**持久身份键**。
  format 一经建立冻结；改 format = 显式迁移事件（本模块不允许顺手改）。
- compose_canonical 拼出后立即经**反解器 round-trip**：反解不出唯一 (series,version,variant)
  或与输入不符（撞名）→ 抛 ConfigError fail fast，不落盘。
- 无 schema_rev 的旧产物 canonical 必须 ∈ LEGACY_CANONICALS ∪ {UNKNOWN}（冻结名单）。

提及锚定（plan M02，供 extract 层用）：对 transcript+title 做确定性扫描，产出
`(series, version[, variant])` 锚点集；LLM 的 version/raw 拆词数字都必须命中锚点。
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from . import config

_MODELS_PATH = config.ROOT / "channels" / "token_bug" / "models.yml"

# 冻结的 legacy 21+ canonical 名单（无 schema_rev 旧产物只认这些 ∪ UNKNOWN；
# 恰等于 seed 拼合结果，故 legacy 记录确认无需登记）。
LEGACY_CANONICALS = frozenset({
    "GLM-5.3", "GLM-5.2", "GLM-5.3 Flash",
    "DeepSeek V4", "DeepSeek V4 Pro", "DeepSeek V4 Flash",
    "Qwen3.8", "Qwen3.8 Flash", "Kimi K3", "Hunyuan 3.0",
    "MiMo 2.5 Pro", "Doubao Seed 2.1", "Step 3.7 Flash",
    "Muse Spark 1.2", "Longcat 2.0", "Fable 5.1",
    "Claude Opus 5", "Claude Sonnet 4.5", "GPT 5.6 Luna",
    "Gemini 3.7 Flash", "Grok 4.5", "Grok 4.6",
})


class ConfigError(Exception):
    """models.yml 配置错误（撞名 format、非法变体等），加载期/拼合期 fail fast。"""


_CACHE: tuple[float, dict] | None = None


def load_series() -> dict:
    """读 models.yml v2（按 mtime 缓存）。返回 {series: {format, aliases, ...}}。"""
    global _CACHE
    mtime = _MODELS_PATH.stat().st_mtime
    if _CACHE is None or _CACHE[0] != mtime:
        data = yaml.safe_load(_MODELS_PATH.read_text(encoding="utf-8")) or {}
        _validate_series(data)
        _CACHE = (mtime, data)
    return _CACHE[1]


def _validate_series(data: dict) -> None:
    for series, cfg in data.items():
        if "{v}" not in (cfg.get("format") or ""):
            raise ConfigError(f"系列 {series} 的 format 缺 {{v}} 占位: {cfg.get('format')!r}")


# ---------- 版本归一 ----------

_FULLWIDTH = str.maketrans("０１２３４５６７８９．", "0123456789.")


def norm_version(s: str) -> str:
    """点→. / 全角→半角 / 去空格与前导 v；**不砍尾部 .0**（3.0≠3 是不同版本键）。"""
    v = (s or "").strip().translate(_FULLWIDTH)
    v = v.replace("点", ".").replace(" ", "")
    v = re.sub(r"^[vV]", "", v)
    return v


def _apply_version_map(series: str, version: str, data: dict) -> str:
    vm = (data.get(series) or {}).get("version_map") or {}
    return vm.get(version, version)


# ---------- 反解器（round-trip 撞名校验）----------

_VER_RE = r"\d+(?:\.\d+)*"


def _series_variants(cfg: dict) -> list[str]:
    return list((cfg.get("variants") or {}).keys())


def _reverse_regexes(data: dict) -> list[tuple[str, re.Pattern]]:
    """由全部系列 format 生成 (series, 锚定到整串的反解正则)。加载期构建。"""
    out = []
    for series, cfg in data.items():
        prefix, suffix = cfg["format"].split("{v}", 1)
        vars_ = _series_variants(cfg)
        var_grp = (f"(?P<var>(?:{'|'.join(' ' + re.escape(v) for v in vars_)}))?"
                   if vars_ else "")
        pat = (rf"^{re.escape(prefix)}(?P<v>{_VER_RE}){re.escape(suffix)}{var_grp}$")
        out.append((series, re.compile(pat)))
    return out


def _reverse_parse_all(canonical: str, data: dict) -> list[tuple[str, str, str]]:
    """返回所有能拼出 canonical 的 (series, version, variant)。撞名时长度>1。"""
    hits = []
    for series, rx in _reverse_regexes(data):
        m = rx.match(canonical)
        if m:
            var = (m.group("var") or "").strip() if "var" in m.groupdict() else ""
            hits.append((series, m.group("v"), var))
    return hits


def compose_canonical(series: str, version: str, variant: str = "",
                      *, data: dict | None = None) -> str:
    """拼合 canonical，并 round-trip 反解校验（撞名或反解不唯一 → ConfigError）。
    version 会经 norm_version + version_map；variant 必须在该系列 variants 清单内。"""
    data = data if data is not None else load_series()
    cfg = data.get(series)
    if cfg is None:
        raise ConfigError(f"未知系列: {series}")
    if variant and variant not in _series_variants(cfg):
        raise ConfigError(f"系列 {series} 无变体 {variant}")
    v = _apply_version_map(series, norm_version(version), data)
    if not v:
        raise ConfigError(f"空版本号无法拼合: {series}")
    canonical = cfg["format"].format(v=v) + (f" {variant}" if variant else "")
    hits = _reverse_parse_all(canonical, data)
    if hits != [(series, v, variant)]:
        raise ConfigError(
            f"拼合撞名/反解不唯一: {canonical!r} → {hits}（期望 [({series!r},{v!r},{variant!r})]）")
    return canonical


# ---------- 提及锚定（plan M02，供 extract 层）----------

_CJK_SEP = r"[\s\-_·]*"  # 系列名与数字间允许的连接符（空格/连字符/间隔号）


def _norm_source(text: str) -> str:
    """源文本归一：小写 + 全角数字/点转半角（锚定扫描用；不动汉字）。"""
    return (text or "").lower().translate(_FULLWIDTH)


def _series_alias_tokens(cfg: dict) -> list[str]:
    """系列名糊法 token（aliases + 系列级 nicknames），供版本锚定。"""
    return [a.lower() for a in (cfg.get("aliases") or [])] + \
           [n.lower() for n in (cfg.get("nicknames") or [])]


def _variant_alias_map(cfg: dict) -> dict[str, list[str]]:
    """{variant: [词形...]}（变体 aliases + 变体级 nicknames）。"""
    out = {}
    for var, vcfg in (cfg.get("variants") or {}).items():
        vcfg = vcfg or {}
        out[var] = [a.lower() for a in (vcfg.get("aliases") or [])] + \
                   [n.lower() for n in (vcfg.get("nicknames") or [])]
    return out


def build_anchors(transcript: str, title: str = "",
                  *, data: dict | None = None) -> dict:
    """构建本视频锚点集（不依赖 LLM 输出）。返回：
      {"versions": {series: set(version)},           # (series, version) 邻接锚点
       "combos":   {series: set((version, variant))}, # (series, version, variant) 组合锚点
       "series":   set(series)}                        # 裸系列 token（无邻接数字）出现
    version 存**原始提及数字**（未过 version_map），核验完再由调用方映射。
    """
    data = data if data is not None else load_series()
    src = _norm_source(transcript + "\n" + title)
    versions: dict[str, set] = {}
    combos: dict[str, set] = {}
    series_seen: set = set()

    for series, cfg in data.items():
        aliases = _series_alias_tokens(cfg)
        if not aliases:
            continue
        vletter = (cfg.get("version_letter") or "").lower()
        vlet_re = rf"(?:{re.escape(vletter)}{_CJK_SEP})?" if vletter else ""
        alias_alt = "|".join(sorted((re.escape(a) for a in aliases), key=len, reverse=True))
        var_map = _variant_alias_map(cfg)
        # 变体词形 → variant 名（用于组合锚点）
        var_word_to_name = {}
        for var, words in var_map.items():
            for w in words:
                var_word_to_name[w] = var
        var_words = sorted(var_word_to_name, key=len, reverse=True)
        var_alt = "|".join(re.escape(w) for w in var_words) if var_words else None

        # 裸系列出现（alias token 作为词出现）
        if re.search(rf"(?<![a-z0-9])(?:{alias_alt})", src):
            series_seen.add(series)

        # 版本锚点：alias + 连接符 + [版本字母] + 数字（+ 可选紧邻变体词）
        if var_alt:
            ver_pat = rf"(?<![a-z0-9])(?:{alias_alt}){_CJK_SEP}{vlet_re}(?P<v>{_VER_RE})(?:{_CJK_SEP}(?P<var>{var_alt}))?"
        else:
            ver_pat = rf"(?<![a-z0-9])(?:{alias_alt}){_CJK_SEP}{vlet_re}(?P<v>{_VER_RE})"
        for m in re.finditer(ver_pat, src):
            v = m.group("v")
            versions.setdefault(series, set()).add(v)  # 版本邻接（步骤2 核验用，含变体提及）
            var = m.groupdict().get("var")
            # 组合锚点（M03 总闸用）：带变体词 → 只加 (v,variant)；无变体词 → 加 (v,'')。
            # 关键：源文本只出现「GLM-5.3 Flash」时不凭空产生基础款 (5.3,'') 锚点。
            if var:
                combos.setdefault(series, set()).add((v, var_word_to_name[var]))
            else:
                combos.setdefault(series, set()).add((v, ""))

        # 变体词形单独邻接版本（如 "step3.7 flash" 已被上式吞；此处兜 "flash 3.7 glm" 等
        # 少见倒序不处理——组合锚点以上式的紧邻捕获为准）。

    return {"versions": versions, "combos": combos, "series": series_seen}


# ---------- 每记录解析：raw 纠错 + 版本锚定 + 共指兜底（plan M02/M03）----------

def _series_token_index(data: dict) -> tuple[list[tuple[str, str, str]], dict]:
    """返回 (series_tokens, generic_variant_words)。
    series_tokens: [(token, series, variant)]（variant='' 表只定系列）——含系列 aliases、
      系列级 nicknames、以及**仅属单一系列**的变体词（如 manflash/小一哥/luna）。
    generic_variant_words: {word: variant_name}——跨 ≥2 系列的通用变体词（flash/pro），
      只在系列确定后用于定变体。"""
    # 统计每个变体词出现于几个系列
    word_series: dict[str, set] = {}
    word_variant: dict[str, str] = {}
    for series, cfg in data.items():
        for var, words in _variant_alias_map(cfg).items():
            for w in words:
                word_series.setdefault(w, set()).add(series)
                word_variant[w] = var
    generic = {w: word_variant[w] for w, ss in word_series.items() if len(ss) >= 2}
    tokens: list[tuple[str, str, str]] = []
    for series, cfg in data.items():
        for a in _series_alias_tokens(cfg):
            tokens.append((a, series, ""))
        for var, words in _variant_alias_map(cfg).items():
            for w in words:
                if w not in generic:  # 仅单一系列的变体词才能定系列
                    tokens.append((w, series, var))
    tokens.sort(key=lambda t: len(t[0]), reverse=True)  # 长 token 优先
    return tokens, generic


def _first_version_in(raw: str) -> str:
    """从 raw 抽首个版本数字（如 grm5.4→5.4）；无则 ''。"""
    m = re.search(_VER_RE, _norm_source(raw))
    return m.group(0) if m else ""


def _match_series_from_raw(raw: str, data: dict) -> tuple[str | None, str]:
    """raw 拆词纠错：命中系列 token → (series, variant)；再用通用变体词补 variant。
    未命中 → (None, '')。"""
    src = _norm_source(raw)
    tokens, generic = _series_token_index(data)
    series = None
    variant = ""
    for tok, s, var in tokens:
        if not tok:
            continue
        # 拉丁 token：前不接字母数字、后不接字母（允许后接版本数字，如 grm5.3）；
        # 含 CJK 的 token 直接子串匹配
        if re.search(r"[a-z0-9]", tok):
            hit = re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z])", src) is not None
        else:
            hit = tok in src
        if hit:
            series, variant = s, var
            break
    if series is not None and not variant:
        for w, var in generic.items():
            if var in _series_variants(data[series]) and re.search(
                    rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", src):
                variant = var
                break
    return series, variant


def _coref(series: str, variant: str, anchors: dict) -> tuple[str | None, str]:
    """共指兜底（version 为空）：从本视频该系列锚定组合集中选。
    - 已绑 variant：筛 variant 相符者，唯一→采用，否则 UNKNOWN。
    - 未绑 variant：锚定组合恰一个→整体采用（含 variant），否则 UNKNOWN。
    返回 (version|None, variant)。"""
    combos = anchors["combos"].get(series, set())
    if not combos:
        return None, variant
    if variant:
        cand = [c for c in combos if c[1] == variant]
    else:
        cand = list(combos)
    if len(cand) == 1:
        return cand[0][0], cand[0][1]
    return None, variant


def resolve_record(model_raw: str, model_series: str, model_version: str,
                   model_variant: str, anchors: dict, *, data: dict | None = None
                   ) -> tuple[str, str, str, str]:
    """把 LLM 的 (raw, series, version, variant) 解析为 (canonical, series, version, variant)。
    canonical=='UNKNOWN' 时后三者为 ''。plan M02/M03 六步。version 存**已过 version_map**
    的归一值，与 known_versions 三元组一致。"""
    data = data if data is not None else load_series()
    UNK = ("UNKNOWN", "", "", "")

    # 步骤1：raw 拆词纠错（命中优先于 LLM 的 series）
    raw_series, raw_variant = _match_series_from_raw(model_raw, data)
    llm_series = model_series if model_series in data else None
    rd = _first_version_in(model_raw)
    if raw_series is not None and raw_series != llm_series:
        # 系列**被纠正**（raw 与 LLM 系列不一致）→ 弃用 LLM 的 version/variant
        series = raw_series
        variant = raw_variant
        cand_versions = [rd] if rd else []
    elif raw_series is not None:
        # raw 与 LLM 系列一致：保留 LLM version；raw 的变体昵称（如小一哥）覆盖 LLM 变体
        series = raw_series
        variant = raw_variant or (model_variant or "").strip()
        cand_versions = []
        if model_version:
            cand_versions.append(norm_version(model_version))
        if rd and rd not in cand_versions:
            cand_versions.append(rd)
    else:
        series = llm_series
        variant = (model_variant or "").strip()
        cand_versions = []
        if model_version:
            cand_versions.append(norm_version(model_version))
        if rd and rd not in cand_versions:
            cand_versions.append(rd)

    if series is None:
        return UNK
    if variant and variant not in _series_variants(data[series]):
        return UNK  # 未知变体不自动登记 → UNKNOWN

    # 步骤2：版本锚定核验（候选数字必须与该系列邻接出现过）
    anchored_versions = anchors["versions"].get(series, set())
    version = None
    for cv in cand_versions:
        if cv and cv in anchored_versions:
            version = cv
            break

    # 步骤4：共指兜底
    if version is None:
        version, variant = _coref(series, variant, anchors)
        if version is None:
            return UNK

    # M03 总闸：最终 (version, variant) 组合必须 ∈ 锚定组合集
    if (version, variant) not in anchors["combos"].get(series, set()):
        return UNK

    # 步骤6：拼合（round-trip 撞名校验）+ version_map 归一
    try:
        canonical = compose_canonical(series, version, variant, data=data)
    except ConfigError:
        return UNK
    stored_version = _apply_version_map(series, norm_version(version), data)
    return canonical, series, stored_version, variant
