"""频道无关的提取执行：调 claude CLI、schema 校验、证据子串校验、记录指纹与决策。

术语纠错在这里做（SenseVoice 不支持 hotword）：prompt 注入模型别名表 + 提取后按
别名表二次归一。record_id 用事实字段指纹，不含 confidence（confidence 每次重判）。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from . import config, db

EXTRACTOR_VERSION = "token_bug/1"
_CH_DIR = config.ROOT / "channels" / "token_bug"
_PROMPT_PATH = _CH_DIR / "extract_prompt.md"
_MODELS_PATH = _CH_DIR / "models.yml"
_SCHEMA_PATH = config.ROOT / "schemas" / "token_bug.extract.schema.json"

_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_validator = Draft202012Validator(_SCHEMA)
_record_validator = Draft202012Validator(_SCHEMA["properties"]["records"]["items"])


class ExtractError(Exception):
    pass


def _load_models() -> dict[str, list[str]]:
    return yaml.safe_load(_MODELS_PATH.read_text(encoding="utf-8")) or {}


def _prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def prompt_hash() -> str:
    """prompt 模板 + 模型表的联合哈希，用于榜单可解释性与决策版本。"""
    h = hashlib.sha256()
    h.update(_prompt_template().encode())
    h.update(_MODELS_PATH.read_text(encoding="utf-8").encode())
    return h.hexdigest()[:12]


def _build_prompt(transcript: str, title: str = "") -> str:
    models = _load_models()
    model_list = "\n".join(f"- {k}" for k in models)
    alias_table = "\n".join(
        f"- {k}: {', '.join(v or [])}" for k, v in models.items())
    tpl = _prompt_template()
    return (tpl.replace("{MODEL_LIST}", model_list)
               .replace("{ALIAS_TABLE}", alias_table)
               .replace("{TITLE}", title or "（无标题）")
               .replace("{TRANSCRIPT}", transcript))


def _canonicalize(model_raw: str, canonical: str, models: dict[str, list[str]]) -> str:
    """归一化模型名。**别名表命中优先于 LLM 的 canonical**（别名表是人工校准的
    ASR 纠错，比 LLM 对乱码的猜测更可信，如 raw='manflash' 应是 Gemini 而非 LLM 猜的
    MiniMax）。别名未命中时才信任 LLM 的 in-list canonical，否则 UNKNOWN。"""
    low = (model_raw or "").strip().lower()
    for k, aliases in models.items():
        if low == k.lower() or low in {a.lower() for a in (aliases or [])}:
            return k
    if canonical in models:
        return canonical
    return "UNKNOWN"


# 每个等级的满分（第1轮做对得满分；机会数=满分）。得分反推轮次：rounds=满分-score+1。
MAX_SCORE = {"青铜": 1, "白银": 1, "黄金": 2, "钻石": 3, "王者": 3}


def derive_solved_rounds(level: str, score: int) -> tuple[bool, int | None]:
    """由等级+得分确定性反推 (solved, rounds)。得分越界返回 (None,None) 表示无效。"""
    maxs = MAX_SCORE.get(level)
    if maxs is None or score < 0 or score > maxs:
        return (None, None)  # type: ignore[return-value]
    if score == 0:
        return (False, None)
    return (True, maxs - score + 1)


def _record_id(aweme_id: str, r: dict) -> str:
    # 含 bug_id 区分同级不同 bug；含 score（编码 solved+rounds）；不含 confidence。
    key = (f"{aweme_id}|{r['model_canonical']}|{r['bug_level']}|{r.get('bug_id','')}"
           f"|{r.get('score')}")
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _call_llm(prompt: str, *, timeout: int | None = None) -> str:
    timeout = timeout or config.LLM_TIMEOUT
    if config.LLM_BACKEND == "claude":
        return _call_claude(prompt, timeout=timeout)
    # 主模型 + 兜底链；每个模型重试一次（偶发超时/空返回/授权不可用）
    last: Exception | None = None
    for model in [config.LLM_MODEL, *config.LLM_MODEL_FALLBACK]:
        for _ in range(2):
            try:
                out = _call_openai(prompt, timeout=timeout, model=model)
                if out.strip():
                    return out
                last = ExtractError("LLM 返回空")
            except ExtractError as e:
                last = e
                # 授权不可用/服务不可用 → 直接切下一个模型
                if any(s in str(e) for s in ("auth_unavailable", "503", "unavailable")):
                    break
    raise last or ExtractError("LLM 调用失败")


def _call_openai(prompt: str, *, timeout: int = 300, model: str | None = None) -> str:
    """调 OpenAI 兼容端点（:8317 cliproxy 的 oc-qwen3.8-flash 等）。"""
    import urllib.request
    import urllib.error
    body = json.dumps({
        "model": model or config.LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        config.LLM_BASE_URL.rstrip("/") + "/chat/completions", data=body,
        method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise ExtractError(f"LLM 请求失败: {e}") from e
    except json.JSONDecodeError as e:
        raise ExtractError(f"LLM 响应非 JSON: {e}") from e
    if data.get("error"):
        raise ExtractError(f"LLM 错误: {data['error']}")
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError) as e:
        raise ExtractError(f"LLM 响应缺 content: {str(data)[:200]}") from e


def _call_claude(prompt: str, *, timeout: int = 300) -> str:
    """调 claude CLI，返回助手文本。"""
    try:
        proc = subprocess.run(
            [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ExtractError(f"claude CLI 未找到: {config.CLAUDE_BIN}") from e
    except subprocess.TimeoutExpired as e:
        raise ExtractError("claude CLI 超时") from e
    if proc.returncode != 0:
        raise ExtractError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:300]}")
    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ExtractError(f"claude CLI 输出非 JSON: {proc.stdout[:200]}") from e
    return env.get("result") or env.get("text") or ""


def _parse_records_json(text: str) -> list[dict]:
    """从助手文本里抠出 JSON 对象并取 records。"""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise ExtractError(f"提取输出中无 JSON 对象: {text[:200]}")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise ExtractError(f"提取 JSON 解析失败: {e}") from e
    recs = obj.get("records")
    if not isinstance(recs, list):
        raise ExtractError("提取 JSON 缺 records 数组")
    return recs


def _normalize_quote(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def build_extract(*, aweme_id: str, title: str, transcript: str,
                  claude_text: str | None = None) -> dict:
    """把 LLM 输出组装为 extract 对象并做全部校验；不写库。校验失败抛 ExtractError。"""
    models = _load_models()
    if claude_text is None:
        claude_text = _call_llm(_build_prompt(transcript, title))
    raw_records = _parse_records_json(claude_text)

    norm_tx = _normalize_quote(transcript)
    records, dropped = [], []
    for r in raw_records:
        r = dict(r)
        r["model_canonical"] = _canonicalize(r.get("model_raw", ""), r.get("model_canonical", ""), models)
        # 由得分反推 solved/rounds（确定性，不靠 LLM 算）
        level, score = r.get("bug_level"), r.get("score")
        if not isinstance(score, int):
            dropped.append((r, f"score 非整数: {score}"))
            continue
        solved, rounds = derive_solved_rounds(level, score)
        if solved is None:
            dropped.append((r, f"{level} score={score} 越界"))
            continue
        r["solved"], r["rounds"] = solved, rounds
        # 逐记录校验：坏记录丢弃（不入榜），不拖垮整条视频
        rerrs = sorted(_record_validator.iter_errors(r), key=lambda e: list(e.path))
        if rerrs:
            dropped.append((r, rerrs[0].message))
            continue
        # 证据必须是转写子串（归一化后）
        if _normalize_quote(r.get("evidence_quote", "")) not in norm_tx:
            dropped.append((r, "evidence 非转写子串"))
            continue
        records.append(r)

    if not records:
        reason = dropped[0][1] if dropped else "无记录"
        raise ExtractError(f"无有效记录（丢弃 {len(dropped)} 条，首因: {reason}）")

    extract = {
        "video_id": aweme_id,
        "title": title,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "extractor_version": EXTRACTOR_VERSION,
        "prompt_hash": prompt_hash(),
        "asr_model": _asr_model_id(),
        "records": records,
        "dropped_count": len(dropped),
    }
    # 兜底整体校验（envelope）
    errs = sorted(_validator.iter_errors({k: v for k, v in extract.items() if k != "dropped_count"}),
                  key=lambda e: list(e.path))
    if errs:
        raise ExtractError(f"extract schema 校验失败: {errs[0].message}")
    return extract


def _asr_model_id() -> str:
    try:
        from . import asr
        return asr.model_id()
    except Exception:
        return "SenseVoiceSmall"


def apply_decisions(conn, aweme_id: str, extract: dict) -> dict:
    """按本次 extract 重判 auto_ok/pending（auto_ok 永不沿用），继承 approved，
    消失记录置 stale。返回 {auto_ok, pending, approved_kept, stale} 计数。"""
    ev, ph = extract["extractor_version"], extract["prompt_hash"]
    keep_ids: set[str] = set()
    counts = {"auto_ok": 0, "pending": 0, "approved_kept": 0}
    for r in extract["records"]:
        rid = _record_id(aweme_id, r)
        keep_ids.add(rid)
        prev = db.get_decision(conn, rid)
        if prev and prev["decision"] == db.APPROVED:
            counts["approved_kept"] += 1  # 仅 approved 凭指纹继承，不动
            continue
        # auto_ok/pending 每次按本次 confidence 重新判定
        ok = r.get("confidence", 0) >= config.CONFIDENCE_THRESHOLD
        decision = db.AUTO_OK if ok else db.PENDING
        db.upsert_decision(conn, record_id=rid, aweme_id=aweme_id, decision=decision,
                           extractor_version=ev, prompt_hash=ph, commit=False)
        counts[decision] += 1
    stale = db.mark_stale(conn, aweme_id, keep_ids)  # 含 commit
    conn.commit()
    counts["stale"] = stale
    return counts


def record_id(aweme_id: str, r: dict) -> str:  # 供测试/board 用
    return _record_id(aweme_id, r)
