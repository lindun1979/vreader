"""频道无关的提取执行：调 claude CLI、schema 校验、证据子串校验、记录指纹与决策。

术语纠错在这里做（SenseVoice 不支持 hotword）：prompt 注入模型别名表 + 提取后按
别名表二次归一。record_id 用事实字段指纹，不含 confidence（confidence 每次重判）。
"""
from __future__ import annotations

import hashlib
import json
import os
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


# bug_id 首字母 → 等级映射（一致性校验；未知前缀不判冲突，避免误伤新命名）
_PREFIX_LEVEL = {"B": "青铜", "S": "白银", "G": "黄金", "D": "钻石", "K": "王者", "W": "王者"}


def _bug_norm(r: dict) -> str:
    return (r.get("bug_id", "") or "").strip().upper()


def _model_key(r: dict) -> str:
    """未知模型用 raw 归一区分，避免不同未知模型误合并同一 rid（C2）。"""
    mc = r.get("model_canonical", "")
    if mc == "UNKNOWN":
        return "UNKNOWN:" + _normalize_quote(r.get("model_raw", ""))
    return mc


def _bug_slot(r: dict) -> str:
    """空 bug_id 用 evidence 指纹占位，避免空 bug_id 相互碰撞合并（C2）。"""
    bn = _bug_norm(r)
    if bn:
        return bn
    return "e:" + hashlib.sha256(_normalize_quote(r.get("evidence_quote", "")).encode()).hexdigest()[:8]


def _attempt_key(aweme_id: str, r: dict) -> str:
    """同一次对战尝试的键（不含 score）：同 attempt 出现不同 score = 矛盾组。"""
    return f"{aweme_id}|{_model_key(r)}|{r['bug_level']}|{_bug_slot(r)}"


def _record_id(aweme_id: str, r: dict) -> str:
    # 身份 = attempt_key + score（编码 solved/rounds）；不含 confidence；顺序无关。
    key = f"{_attempt_key(aweme_id, r)}|{r.get('score')}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _bug_prefix_conflict(r: dict) -> bool:
    """bug_id 已知前缀却指向别的等级（如 G005 标成钻石）→ 冲突（降 pending）。
    未知前缀不判冲突（可能是新命名规则）。"""
    bn = _bug_norm(r)
    if not bn:
        return False
    mapped = _PREFIX_LEVEL.get(bn[0])
    return mapped is not None and mapped != r.get("bug_level")


def _providers() -> list[tuple[str, str | None]]:
    """按后端返回 (transport, model) 尝试链（主在前，兜底在后）。
    agy 后端：LLM_MODEL 走 agy 主通道，LLM_MODEL_FALLBACK 走 :8317 兜底。"""
    if config.LLM_BACKEND == "claude":
        return [("claude", None)]
    if config.LLM_BACKEND == "agy":
        return [("agy", config.LLM_MODEL),
                *[("openai", m) for m in config.LLM_MODEL_FALLBACK]]
    return [("openai", m) for m in [config.LLM_MODEL, *config.LLM_MODEL_FALLBACK]]


def _dispatch(transport: str, model: str | None, prompt: str, *, timeout: int) -> str:
    if transport == "agy":
        return _call_agy(prompt, timeout=timeout, model=model)
    if transport == "claude":
        return _call_claude(prompt, timeout=timeout)
    return _call_openai(prompt, timeout=timeout, model=model)


def _call_llm(prompt: str, *, timeout: int | None = None) -> str:
    timeout = timeout or config.LLM_TIMEOUT
    # 主通道 + 兜底链；每个尝试重试一次（偶发超时/空返回/授权不可用）
    last: Exception | None = None
    for transport, model in _providers():
        for _ in range(2):
            try:
                out = _dispatch(transport, model, prompt, timeout=timeout)
                if out.strip():
                    return out
                last = ExtractError("LLM 返回空")
            except ExtractError as e:
                last = e
                # 授权不可用/服务不可用 → 直接切下一个通道
                if any(s in str(e) for s in ("auth_unavailable", "503", "unavailable")):
                    break
    raise last or ExtractError("LLM 调用失败")


def _call_agy(prompt: str, *, timeout: int = 300, model: str | None = None) -> str:
    """调 agy（Antigravity CLI）print 模式，返回模型文本。生产直连不通，按
    config.AGY_PROXY 注入 HTTP 代理 env（agy 是 Go 二进制，认标准 HTTPS_PROXY）。"""
    env = dict(os.environ)
    if config.AGY_PROXY:
        env["HTTPS_PROXY"] = config.AGY_PROXY
        env["HTTP_PROXY"] = config.AGY_PROXY
        env.setdefault("NO_PROXY", "127.0.0.1,localhost")
    cmd = [config.AGY_BIN, "-p", prompt, "--model", model or config.LLM_MODEL,
           "--print-timeout", f"{timeout}s"]
    rc, out, err = _run_cli(cmd, timeout=timeout + 30, env=env, name="agy CLI")
    if rc != 0:
        raise ExtractError(f"agy CLI 退出码 {rc}: {err[:300]}")
    return out or ""


def _run_cli(cmd: list[str], *, timeout: int, env: dict | None = None,
             name: str = "CLI") -> tuple[int, str, str]:
    """跑外部 CLI（自立进程组）；超时 killpg 杀整棵执行树后代，避免遗留 ffmpeg 等孤儿。"""
    import signal
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, env=env, start_new_session=True)
    except FileNotFoundError as e:
        raise ExtractError(f"{name} 未找到: {cmd[0]}") from e
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # 杀整组（含后代）
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        raise ExtractError(f"{name} 超时") from e
    return proc.returncode, out or "", err or ""


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
    rc, out, err = _run_cli(
        [config.CLAUDE_BIN, "-p", prompt, "--output-format", "json"],
        timeout=timeout, name="claude CLI")
    if rc != 0:
        raise ExtractError(f"claude CLI 退出码 {rc}: {err[:300]}")
    try:
        env = json.loads(out)
    except json.JSONDecodeError as e:
        raise ExtractError(f"claude CLI 输出非 JSON: {out[:200]}") from e
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


def validate_extract(extract: dict, *, transcript: str | None = None,
                     expected_video_id: str | None = None) -> None:
    """统一产物校验入口（C3）：读回缓存/reprocess 前必过，否则视为不可信。
    校验：envelope schema + video_id 匹配 + 逐记录 schema + derive 一致 +
    canonical∈词表∪UNKNOWN + evidence 归一非空(≥4)且（给了 transcript 时）为其子串。
    失败抛 ExtractError。"""
    errs = sorted(_validator.iter_errors(extract), key=lambda e: list(e.path))
    if errs:
        raise ExtractError(f"envelope schema: {errs[0].message}")
    if expected_video_id is not None and extract.get("video_id") != expected_video_id:
        raise ExtractError(f"video_id 不匹配: {extract.get('video_id')} != {expected_video_id}")
    models = _load_models()
    norm_tx = _normalize_quote(transcript) if transcript is not None else None
    for r in extract["records"]:
        rerrs = sorted(_record_validator.iter_errors(r), key=lambda e: list(e.path))
        if rerrs:
            raise ExtractError(f"record schema: {rerrs[0].message}")
        solved, rounds = derive_solved_rounds(r.get("bug_level"), r.get("score"))
        if solved is None or r.get("solved") != solved or r.get("rounds") != rounds:
            raise ExtractError(
                f"solved/rounds 与 score 不一致: {r.get('bug_level')} score={r.get('score')}")
        if r["model_canonical"] not in models and r["model_canonical"] != "UNKNOWN":
            raise ExtractError(f"model_canonical 非法（不在词表也非 UNKNOWN）: {r['model_canonical']}")
        q = _normalize_quote(r.get("evidence_quote", ""))
        if len(q) < 4:
            raise ExtractError("evidence 归一后过短(<4)")
        if norm_tx is not None and q not in norm_tx:
            raise ExtractError("evidence 非转写子串")


def load_valid_extract(extract_path: str, *, transcript: str | None = None,
                       expected_video_id: str | None = None) -> dict | None:
    """读回缓存 extract.json 并校验（C3 留证重建）：不存在/空/坏 → 返回 None，坏文件
    另存为 .bad.<ts> 保留证据（下游据此重新提取）。校验通过才返回 dict。"""
    p = Path(extract_path)
    if not p.exists() or p.stat().st_size == 0:
        return None
    try:
        ex = json.loads(p.read_text(encoding="utf-8"))
        validate_extract(ex, transcript=transcript, expected_video_id=expected_video_id)
        return ex
    except (json.JSONDecodeError, ExtractError, OSError) as e:
        try:
            p.rename(p.with_suffix(f".json.bad.{int(time.time())}"))
        except OSError:
            pass
        print(f"[extract] 缓存 extract.json 校验失败，留证重建: {e}", flush=True)
        return None


def build_extract(*, aweme_id: str, title: str, transcript: str,
                  claude_text: str | None = None, asr_model: str | None = None) -> dict:
    """把 LLM 输出组装为 extract 对象并做全部校验；不写库。校验失败抛 ExtractError。
    asr_model：显式指定该转写的 ASR 引擎（reprocess 不重转时传原引擎，避免误标默认值）；
    None 时取 asr 模块最近一次实际转写引擎。"""
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
            dropped.append({"record": r, "reason": f"score 非整数: {score}"})
            continue
        solved, rounds = derive_solved_rounds(level, score)
        if solved is None:
            dropped.append({"record": r, "reason": f"{level} score={score} 越界"})
            continue
        r["solved"], r["rounds"] = solved, rounds
        # 逐记录校验：坏记录丢弃（不入榜），不拖垮整条视频
        rerrs = sorted(_record_validator.iter_errors(r), key=lambda e: list(e.path))
        if rerrs:
            dropped.append({"record": r, "reason": rerrs[0].message})
            continue
        # 证据必须是转写子串（归一化后）
        if _normalize_quote(r.get("evidence_quote", "")) not in norm_tx:
            dropped.append({"record": r, "reason": "evidence 非转写子串"})
            continue
        records.append(r)

    extract = {
        "video_id": aweme_id,
        "title": title,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "extractor_version": EXTRACTOR_VERSION,
        "prompt_hash": prompt_hash(),
        "asr_model": asr_model or _asr_model_id(),
        "records": records,
        "dropped_count": len(dropped),
        "dropped": dropped,
    }
    extract["result_rev"] = result_rev(aweme_id, records)
    # 空 records 不再当失败重试（agy M-02/cf5#11）：显式 no_content 终态成功；
    # dropped 明细落盘供人工审计（dup≠dropped：dup 是重复合并，dropped 是校验丢弃）。
    if not records:
        extract["no_content"] = True
    # 兜底整体校验（envelope）
    errs = sorted(_validator.iter_errors(extract), key=lambda e: list(e.path))
    if errs:
        raise ExtractError(f"extract schema 校验失败: {errs[0].message}")
    return extract


def _asr_model_id() -> str:
    try:
        from . import asr
        return asr.model_id()
    except Exception:
        return "SenseVoiceSmall"


def _dedup_records(aweme_id: str, records: list[dict]) -> tuple[dict[str, dict], int]:
    """规则1 完全重复（同 rid）：留 confidence 最高，其余计 dup。顺序无关。
    返回 (rid→record, dup_count)。"""
    by_rid: dict[str, dict] = {}
    dup = 0
    for r in records:
        rid = _record_id(aweme_id, r)
        if rid not in by_rid:
            by_rid[rid] = r
        else:
            dup += 1
            if r.get("confidence", 0) > by_rid[rid].get("confidence", 0):
                by_rid[rid] = r
    return by_rid, dup


def _conflict_rids(aweme_id: str, by_rid: dict[str, dict]) -> set[str]:
    """规则2 矛盾组：同 attempt_key 出现多个不同 score（rid 不同）→ 全组成员冲突。"""
    groups: dict[str, list[str]] = {}
    for rid, r in by_rid.items():
        groups.setdefault(_attempt_key(aweme_id, r), []).append(rid)
    out: set[str] = set()
    for members in groups.values():
        if len(members) > 1:
            out.update(members)
    return out


def _classify(r: dict, is_conflict: bool) -> str:
    """新记录的初始决策（裁决恒存的不走此处）。"""
    if is_conflict:
        return db.PENDING_CONFLICT
    if r.get("model_canonical") == "UNKNOWN":
        return db.PENDING_UNKNOWN                 # 2c' 模型未知 → 待补别名表
    if not _bug_norm(r):
        return db.PENDING                          # 规则6 空 bug_id 一律不 auto_ok
    if _bug_prefix_conflict(r):
        return db.PENDING                          # 2c' bug_id 前缀与等级不一致 → 降级
    if r.get("confidence", 0) >= config.CONFIDENCE_THRESHOLD:
        return db.AUTO_OK
    return db.PENDING


def apply_decisions(conn, aweme_id: str, extract: dict) -> dict:
    """归并七规则重判决策（C2）。auto_ok/pending 每次按本次重算；APPROVED 与
    rejected_conflict 凭指纹继承（裁决恒存，规则3）；消失记录置 stale（跳过裁决）。"""
    ev, ph = extract["extractor_version"], extract["prompt_hash"]
    by_rid, dup = _dedup_records(aweme_id, extract["records"])
    conflict = _conflict_rids(aweme_id, by_rid)
    counts: dict[str, int] = {}
    keep_ids = set(by_rid)
    # 规则5：曾 approved 但本次缺失的记录会被 stale，单独计数用于 ⚠️ 提示
    before = {row["record_id"]: row["decision"] for row in db.list_decisions_for_video(conn, aweme_id)}
    for rid, r in by_rid.items():
        prev = db.get_decision(conn, rid)
        if prev and prev["decision"] in db.PERSISTENT_DECISIONS:
            counts[prev["decision"]] = counts.get(prev["decision"], 0) + 1  # 恒存继承
            continue
        decision = _classify(r, rid in conflict)
        db.upsert_decision(conn, record_id=rid, aweme_id=aweme_id, decision=decision,
                           extractor_version=ev, prompt_hash=ph, commit=False)
        counts[decision] = counts.get(decision, 0) + 1
    counts["stale"] = db.mark_stale(conn, aweme_id, keep_ids, commit=False)
    conn.commit()
    counts["approved_stale"] = sum(
        1 for rid, dec in before.items() if dec == db.APPROVED and rid not in keep_ids)
    # 便捷别名：pending 汇总（三态之和），供回执文案
    counts["dup"] = dup
    counts.setdefault("auto_ok", 0)
    counts["pending"] = (counts.get(db.PENDING, 0) + counts.get(db.PENDING_UNKNOWN, 0)
                         + counts.get(db.PENDING_CONFLICT, 0))
    return counts


def record_id(aweme_id: str, r: dict) -> str:  # 供测试/board 用
    return _record_id(aweme_id, r)


def result_rev(aweme_id: str, records: list[dict]) -> str:
    """本次结果版本 = 全部 rid 排序后的哈希（C9）；确认命令绑此版本防错认旧内容。"""
    ids = sorted(_record_id(aweme_id, r) for r in records)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:12]


def confirm_conflict_member(conn, aweme_id: str, rid: str, approver: str) -> tuple[bool, str]:
    """逐条确认冲突组成员（规则4/C2.4）：确认该成员 APPROVED，同组其余未裁决成员置
    rejected_conflict（同事务）。组内已有 APPROVED → 拒绝（反向改判走 3-15）。
    rid 可为完整 16 位或 ≥6 位前缀。返回 (成功?, 文案)。"""
    p = _paths_extract(aweme_id)
    if p is None:
        return False, f"找不到该视频的提取结果（{aweme_id}）"
    ex = json.loads(Path(p).read_text(encoding="utf-8"))
    by_rid, _ = _dedup_records(aweme_id, ex.get("records", []))
    matches = [x for x in by_rid if x == rid or x.startswith(rid)]
    if not matches:
        return False, f"没有匹配 {rid} 的记录"
    if len(matches) > 1:
        return False, f"{rid} 前缀不唯一，请多给几位"
    target = matches[0]
    ak = _attempt_key(aweme_id, by_rid[target])
    group = [x for x in by_rid if _attempt_key(aweme_id, by_rid[x]) == ak]
    for g in group:  # 组内已有 APPROVED → 一律拒绝再确认
        d = db.get_decision(conn, g)
        if d and d["decision"] == db.APPROVED:
            return False, "该冲突组已有裁决，不能再确认（反向改判待实现 vr改判）"
    import time as _t
    db.upsert_decision(conn, record_id=target, aweme_id=aweme_id, decision=db.APPROVED,
                       extractor_version=ex.get("extractor_version", ""),
                       prompt_hash=ex.get("prompt_hash", ""),
                       approved_by=approver, approved_at=_t.time(), commit=False)
    for g in group:
        if g == target:
            continue
        d = db.get_decision(conn, g)
        if not d or d["decision"] not in db.PERSISTENT_DECISIONS:
            db.upsert_decision(conn, record_id=g, aweme_id=aweme_id,
                               decision=db.REJECTED_CONFLICT,
                               extractor_version=ex.get("extractor_version", ""),
                               prompt_hash=ex.get("prompt_hash", ""), commit=False)
    conn.commit()
    return True, f"已确认该冲突成员入榜，同组其余 {len(group) - 1} 条标记为落败。"


def _paths_extract(aweme_id: str) -> str | None:
    p = config.video_dir("token_bug", aweme_id) / "extract.json"
    return str(p) if p.exists() else None


_DECISION_LABEL = {
    db.AUTO_OK: "已上榜", db.APPROVED: "已确认上榜", db.PENDING: "待确认",
    db.PENDING_UNKNOWN: "待确认(模型未知)", db.PENDING_CONFLICT: "待确认(矛盾)",
    db.REJECTED_CONFLICT: "冲突落败", db.STALE: "已过期", db.EXPIRED: "已过期",
}


def detail_view(conn, aweme_id: str) -> str:
    """/detail：视频提取明细（含 rid 短码/decision/完整证据），供人工审阅与逐条确认。"""
    p = _paths_extract(aweme_id)
    if p is None:
        return f"找不到该视频的提取结果（{aweme_id}）。"
    ex = json.loads(Path(p).read_text(encoding="utf-8"))
    by_rid, _ = _dedup_records(aweme_id, ex.get("records", []))
    lines = [f"📋 {aweme_id} 明细（result_rev={ex.get('result_rev', '?')}）"]
    if not by_rid:
        lines.append("（无有效记录）")
    for rid, r in by_rid.items():
        d = db.get_decision(conn, rid)
        label = _DECISION_LABEL.get(d["decision"], d["decision"]) if d else "?"
        lines.append(
            f"[{rid[:8]}] {r['model_canonical']} · {r['bug_level']} · score={r.get('score')} · "
            f"conf={r.get('confidence')} → {label}\n    证据：{r.get('evidence_quote', '')}")
    return "\n".join(lines)
