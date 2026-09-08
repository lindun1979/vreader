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

from jsonschema import Draft202012Validator

from . import config, db, models as models_mod

EXTRACTOR_VERSION = "token_bug/2"
SCHEMA_REV = 2
_CH_DIR = config.ROOT / "channels" / "token_bug"
_PROMPT_PATH = _CH_DIR / "extract_prompt.md"
_MODELS_PATH = _CH_DIR / "models.yml"
_SCHEMA_PATH = config.ROOT / "schemas" / "token_bug.extract.schema.json"
_SCHEMA_V2_PATH = config.ROOT / "schemas" / "token_bug.extract.v2.schema.json"

# 旧轨（无 schema_rev）：既有 schema；v2 轨：新 schema（带 series 三字段 + schema_rev）
_SCHEMA = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
_validator = Draft202012Validator(_SCHEMA)
_record_validator = Draft202012Validator(_SCHEMA["properties"]["records"]["items"])
_SCHEMA_V2 = json.loads(_SCHEMA_V2_PATH.read_text(encoding="utf-8"))
_validator_v2 = Draft202012Validator(_SCHEMA_V2)
_record_validator_v2 = Draft202012Validator(_SCHEMA_V2["properties"]["records"]["items"])


class ExtractError(Exception):
    pass


def _prompt_template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def prompt_hash() -> str:
    """prompt 模板 + 模型表的**静态**联合哈希（不含 known_versions；本量入 rid 语义）。"""
    h = hashlib.sha256()
    h.update(_prompt_template().encode())
    h.update(_MODELS_PATH.read_text(encoding="utf-8").encode())
    return h.hexdigest()[:12]


def prompt_full_hash(prompt: str) -> str:
    """实际发送 prompt 的哈希（含注入的 known_versions；溯源用，不入 rid）。"""
    return hashlib.sha256(prompt.encode()).hexdigest()[:12]


def _series_table(data: dict, known: set[tuple[str, str, str]]) -> str:
    """把系列表渲染为 prompt 注入文本：每系列一段（系列名 + 别名 + 昵称 + 变体 + 已知版本）。"""
    known_by_series: dict[str, list[str]] = {}
    for s, v, var in sorted(known):
        try:
            known_by_series.setdefault(s, []).append(
                models_mod.compose_canonical(s, v, var, data=data))
        except models_mod.ConfigError:
            continue
    lines = []
    for series, cfg in data.items():
        parts = [f"- {series}（模板 {cfg['format']}）"]
        aliases = cfg.get("aliases") or []
        nicks = (cfg.get("nicknames") or []) + [
            n for vc in (cfg.get("variants") or {}).values() for n in ((vc or {}).get("nicknames") or [])]
        if aliases:
            parts.append(f"  别名: {', '.join(aliases)}")
        if nicks:
            parts.append(f"  昵称(随期变版本,勿绑版本号): {', '.join(nicks)}")
        variants = list((cfg.get("variants") or {}).keys())
        if variants:
            parts.append(f"  变体: {', '.join(variants)}")
        kv = known_by_series.get(series)
        if kv:
            parts.append(f"  已知版本: {', '.join(kv)}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _build_prompt(transcript: str, title: str = "",
                  known: set[tuple[str, str, str]] | None = None) -> str:
    data = models_mod.load_series()
    series_table = _series_table(data, known or set())
    series_names = "\n".join(f"- {s}" for s in data)
    tpl = _prompt_template()
    return (tpl.replace("{SERIES_LIST}", series_names)
               .replace("{SERIES_TABLE}", series_table)
               .replace("{TITLE}", title or "（无标题）")
               .replace("{TRANSCRIPT}", transcript))


# 每个等级的满分（第1轮做对得满分；机会数=满分）。得分反推轮次：rounds=满分-score+1。
MAX_SCORE = {"青铜": 1, "白银": 1, "黄金": 2, "钻石": 3, "王者": 3}


def derive_solved_rounds(level: str, score: int) -> tuple[bool, int | None]:
    """【旧轨/legacy 用】由等级+得分反推 (solved, rounds)。得分越界返回 (None,None)。
    注意：score=0 一律判 not solved——无法区分「第 maxs+1 轮才解出(白做0分)」与「没做对」，
    这正是 v2 轮次驱动要修的问题（见 derive_from_round）。"""
    maxs = MAX_SCORE.get(level)
    if maxs is None or score < 0 or score > maxs:
        return (None, None)  # type: ignore[return-value]
    if score == 0:
        return (False, None)
    return (True, maxs - score + 1)


def derive_from_round(level: str, solved_round: int) -> tuple[bool, int | None, int | None]:
    """【v2 轮次驱动】由 (等级, 第几轮做对) 反推 (solved, score, rounds)。
    solved_round: 0=没做对；1..maxs=第 n 轮解出（score=maxs-n+1）；maxs+1=最后一轮才解出
    （"白做"，已解但 0 分）。越界（<0 或 >maxs+1，或非整数）→ (None,None,None) 表示无效。
    规则（用户 2026-09 确认）：钻石/王者 1轮=3 2轮=2 3轮=1 4轮=0(已解)；黄金 1轮=2 2轮=1
    3轮=0(已解)；青铜/白银 1轮=1 2轮=0(已解)；0=没做对。"""
    maxs = MAX_SCORE.get(level)
    if maxs is None or not isinstance(solved_round, int) or solved_round < 0 or solved_round > maxs + 1:
        return (None, None, None)  # type: ignore[return-value]
    if solved_round == 0:
        return (False, 0, None)
    return (True, max(0, maxs - solved_round + 1), solved_round)


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
    """统一产物校验入口（C3/M06 双轨）：读回缓存/reprocess 前必过，否则视为不可信。
    判轨单位是**整个文件**（envelope 定轨，M13）：
    - 有 schema_rev → **只接受 2**（未知修订一律拒），按 v2 全套校验（每条须带 series
      三字段且 compose(series,version,variant)==canonical，或 UNKNOWN 三字段空）。
    - 无 schema_rev（旧产物）→ canonical 必须 ∈ 冻结 LEGACY_CANONICALS ∪ {UNKNOWN}。
    公共：video_id 匹配 + 逐记录 schema + derive 一致 + evidence 归一非空(≥4)且子串。
    失败抛 ExtractError。"""
    rev = extract.get("schema_rev")
    if rev is not None and rev != SCHEMA_REV:
        raise ExtractError(f"未知 schema_rev: {rev}（只接受 {SCHEMA_REV}）")
    v2 = rev is not None
    validator = _validator_v2 if v2 else _validator
    rec_validator = _record_validator_v2 if v2 else _record_validator
    errs = sorted(validator.iter_errors(extract), key=lambda e: list(e.path))
    if errs:
        raise ExtractError(f"envelope schema: {errs[0].message}")
    if expected_video_id is not None and extract.get("video_id") != expected_video_id:
        raise ExtractError(f"video_id 不匹配: {extract.get('video_id')} != {expected_video_id}")
    data = models_mod.load_series() if v2 else None
    norm_tx = _normalize_quote(transcript) if transcript is not None else None
    for r in extract["records"]:
        rerrs = sorted(rec_validator.iter_errors(r), key=lambda e: list(e.path))
        if rerrs:
            raise ExtractError(f"record schema: {rerrs[0].message}")
        if v2:  # 轮次驱动：solved/score/rounds 必须与 solved_round 一致
            solved, score, rounds = derive_from_round(r.get("bug_level"), r.get("solved_round"))
            if (solved is None or r.get("solved") != solved
                    or r.get("score") != score or r.get("rounds") != rounds):
                raise ExtractError(
                    f"solved/score/rounds 与 solved_round 不一致: {r.get('bug_level')} "
                    f"solved_round={r.get('solved_round')}")
        else:  # legacy：得分驱动
            solved, rounds = derive_solved_rounds(r.get("bug_level"), r.get("score"))
            if solved is None or r.get("solved") != solved or r.get("rounds") != rounds:
                raise ExtractError(
                    f"solved/rounds 与 score 不一致: {r.get('bug_level')} score={r.get('score')}")
        _validate_canonical(r, v2, data)
        q = _normalize_quote(r.get("evidence_quote", ""))
        if len(q) < 4:
            raise ExtractError("evidence 归一后过短(<4)")
        if norm_tx is not None and q not in norm_tx:
            raise ExtractError("evidence 非转写子串")


def _validate_canonical(r: dict, v2: bool, data: dict | None) -> None:
    mc = r["model_canonical"]
    if not v2:
        if mc not in models_mod.LEGACY_CANONICALS and mc != "UNKNOWN":
            raise ExtractError(f"legacy canonical 非法（不在冻结名单也非 UNKNOWN）: {mc}")
        return
    s, v, var = r.get("model_series", ""), r.get("model_version", ""), r.get("model_variant", "")
    if mc == "UNKNOWN":
        if s or v or var:
            raise ExtractError("UNKNOWN 记录 series/version/variant 必须为空")
        return
    if not (s and v):
        raise ExtractError(f"v2 记录缺 series/version: {mc}")
    try:
        composed = models_mod.compose_canonical(s, v, var, data=data)
    except models_mod.ConfigError as e:
        raise ExtractError(f"v2 记录三元组无法拼合: {e}") from e
    if composed != mc:
        raise ExtractError(f"compose(series,version,variant)≠canonical: {composed} != {mc}")


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
                  claude_text: str | None = None, asr_model: str | None = None,
                  known: set[tuple[str, str, str]] | None = None) -> dict:
    """把 LLM 输出组装为 v2 extract 对象并做全部校验；不写库。校验失败抛 ExtractError。
    LLM 输出 model_raw + model_series/version/variant（不出 canonical）；代码经**提及锚定**
    解析出 canonical + 归一三元组（M02/M03）。
    known：本次注入 prompt 的已知版本三元组集合（溯源 known_versions_used，M08）；None=空。
    asr_model：显式指定该转写的 ASR 引擎（reprocess 不重转时传原引擎）。"""
    known = known or set()
    data = models_mod.load_series()
    anchors = models_mod.build_anchors(transcript, title, data=data)
    prompt = _build_prompt(transcript, title, known=known)
    if claude_text is None:
        claude_text = _call_llm(prompt)
    raw_records = _parse_records_json(claude_text)

    norm_tx = _normalize_quote(transcript)
    records, dropped = [], []
    for r in raw_records:
        r = dict(r)
        # 提及锚定解析（LLM 的 raw/series/version/variant 不可信，须过源文本锚定）
        canonical, series, version, variant = models_mod.resolve_record(
            r.get("model_raw", ""), r.get("model_series", ""),
            r.get("model_version", ""), r.get("model_variant", ""), anchors,
            data=data, known=known)
        r["model_canonical"] = canonical
        r["model_series"], r["model_version"], r["model_variant"] = series, version, variant
        # 轮次驱动（v2）：LLM 出「第几轮做对」，代码反推 solved/score/rounds（确定性）
        level, sr = r.get("bug_level"), r.get("solved_round")
        if not isinstance(sr, int):
            dropped.append({"record": r, "reason": f"solved_round 非整数: {sr}"})
            continue
        solved, score, rounds = derive_from_round(level, sr)
        if solved is None:
            dropped.append({"record": r, "reason": f"{level} solved_round={sr} 越界"})
            continue
        r["solved"], r["score"], r["rounds"] = solved, score, rounds
        # 逐记录校验：坏记录丢弃（不入榜），不拖垮整条视频
        rerrs = sorted(_record_validator_v2.iter_errors(r), key=lambda e: list(e.path))
        if rerrs:
            dropped.append({"record": r, "reason": rerrs[0].message})
            continue
        # 证据必须是转写子串（归一化后）
        if _normalize_quote(r.get("evidence_quote", "")) not in norm_tx:
            dropped.append({"record": r, "reason": "evidence 非转写子串"})
            continue
        records.append(r)

    used = sorted([list(t) for t in known])
    extract = {
        "schema_rev": SCHEMA_REV,
        "video_id": aweme_id,
        "title": title,
        "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "extractor_version": EXTRACTOR_VERSION,
        "prompt_hash": prompt_hash(),
        "prompt_full_hash": prompt_full_hash(prompt),
        "known_versions_used": used,
        "known_versions_snapshot": _snapshot(known),
        "asr_model": asr_model or _asr_model_id(),
        "records": records,
        "dropped_count": len(dropped),
        "dropped": dropped,
    }
    extract["result_rev"] = result_rev(aweme_id, records)
    # 空 records 不再当失败重试（agy M-02/cf5#11）：显式 no_content 终态成功。
    if not records:
        extract["no_content"] = True
    errs = sorted(_validator_v2.iter_errors(extract), key=lambda e: list(e.path))
    if errs:
        raise ExtractError(f"extract schema 校验失败: {errs[0].message}")
    return extract


def _snapshot(known: set[tuple[str, str, str]]) -> str:
    """已知版本集摘要（sha256 前 12 位，顺序无关）。"""
    payload = "|".join("/".join(t) for t in sorted(known))
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


_LEVEL_WORDS = ["青铜", "白银", "黄金", "钻石", "王者"]
_HOTWORD_CAP = 100


def gladia_hotwords(conn=None) -> list[str]:
    """Gladia 热词（预算与运维）：系列裸名 + 等级词**固定保留**（不参与截断），剩余名额
    给拼合 canonical——按 known_versions.created_at 降序、同时间按三元组字典序，总量截断至
    100。conn=None 时开只读短连接读已提交版本集（读消费点，M05）；库缺失则仅返回固定词。"""
    data = models_mod.load_series()
    fixed = list(data.keys()) + _LEVEL_WORDS
    close = False
    if conn is None:
        try:
            conn = db.connect(str(config.DATA_DIR / "vreader.db"))
            close = True
        except Exception:  # noqa: BLE001
            conn = None
    canon: list[str] = []
    if conn is not None:
        try:
            rows = db.known_versions_rows(conn)
            rows = sorted(rows, key=lambda r: (-r["created_at"], r["series"],
                                               r["version"], r["variant"]))
            for r in rows:
                try:
                    canon.append(models_mod.compose_canonical(
                        r["series"], r["version"], r["variant"], data=data))
                except models_mod.ConfigError:
                    pass
        except Exception:  # noqa: BLE001 库无表/损坏 → 仅固定词
            pass
        finally:
            if close:
                conn.close()
    budget = max(0, _HOTWORD_CAP - len(fixed))
    return fixed + canon[:budget]


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


def _record_triple(r: dict) -> tuple[str, str, str] | None:
    """v2 记录的 (series,version,variant)；旧记录（无 model_series 字段）→ None。"""
    if "model_series" not in r:
        return None
    return (r.get("model_series", ""), r.get("model_version", ""), r.get("model_variant", ""))


def _classify(r: dict, is_conflict: bool,
              known: set[tuple[str, str, str]] | None) -> str:
    """新记录的初始决策（裁决恒存的不走此处）。known=None 时不做新版本判定
    （旧记录/未传集合场景）。已知版本只免除「新版本」这一项，低置信/空bug_id/冲突照旧。"""
    if is_conflict:
        return db.PENDING_CONFLICT
    if r.get("model_canonical") == "UNKNOWN":
        return db.PENDING_UNKNOWN                 # 2c' 模型未知 → 待补别名表
    tri = _record_triple(r)
    if known is not None and tri is not None and tri not in known:
        return db.PENDING_NEW_VERSION              # M04 合法三元组但版本未登记 → 首次确认
    if not _bug_norm(r):
        return db.PENDING                          # 规则6 空 bug_id 一律不 auto_ok
    if _bug_prefix_conflict(r):
        return db.PENDING                          # 2c' bug_id 前缀与等级不一致 → 降级
    if r.get("confidence", 0) >= config.CONFIDENCE_THRESHOLD:
        return db.AUTO_OK
    return db.PENDING


def apply_decisions(conn, aweme_id: str, extract: dict,
                    known: set[tuple[str, str, str]] | None = None) -> dict:
    """归并七规则重判决策（C2）。auto_ok/pending 每次按本次重算；APPROVED 与
    rejected_conflict 凭指纹继承（裁决恒存，规则3）；消失记录置 stale（跳过裁决）。
    known：决策时点已提交的已知版本集合（M04/M05，由入口在 publish_lock 内读好传入）；
    None = 不做新版本判定。"""
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
        decision = _classify(r, rid in conflict, known)
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
                         + counts.get(db.PENDING_CONFLICT, 0)
                         + counts.get(db.PENDING_NEW_VERSION, 0))
    return counts


def record_id(aweme_id: str, r: dict) -> str:  # 供测试/board 用
    return _record_id(aweme_id, r)


def result_rev(aweme_id: str, records: list[dict]) -> str:
    """本次结果版本 = 全部 rid 排序后的哈希（C9）；确认命令绑此版本防错认旧内容。"""
    ids = sorted(_record_id(aweme_id, r) for r in records)
    return hashlib.sha256("|".join(ids).encode()).hexdigest()[:12]


def _maybe_register(conn, r: dict, approver: str) -> str | None:
    """若记录带有效三元组且尚未登记 → register_known_version（同事务），返回其 canonical；
    否则返回 None。UNKNOWN / 缺 series|version 不登记（M04：只登记有效三元组）。"""
    tri = _record_triple(r)
    if not tri or r.get("model_canonical") == "UNKNOWN" or not (tri[0] and tri[1]):
        return None
    if tri in db.list_known_versions(conn):
        return None
    db.register_known_version(conn, tri[0], tri[1], tri[2], approver, commit=False)
    return r["model_canonical"]


def approve_pending_for_video(conn, aweme_id: str, approver: str) -> tuple[int, list[str]]:
    """批量确认入口（M04）：把本视频 PENDING / PENDING_NEW_VERSION 置 APPROVED，且对
    **本次实际被批准且带有效三元组**的新版本记录同事务 register_known_version（绝不遍历
    extract 全量）。返回 (批准数, 新登记的 canonical 列表)。extract 缺失/损坏 → 直接失败
    返回 (0, [])，不产生任何 APPROVED（M05）。"""
    p = _paths_extract(aweme_id)
    if p is None:
        return 0, []
    try:
        ex = json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0, []
    by_rid, _ = _dedup_records(aweme_id, ex.get("records", []))
    ev, ph = ex.get("extractor_version", ""), ex.get("prompt_hash", "")
    now = time.time()
    approved = 0
    registered: list[str] = []
    try:
        for rid, r in by_rid.items():
            d = db.get_decision(conn, rid)
            if not d or d["decision"] not in (db.PENDING, db.PENDING_NEW_VERSION):
                continue
            was_new = d["decision"] == db.PENDING_NEW_VERSION
            db.upsert_decision(conn, record_id=rid, aweme_id=aweme_id, decision=db.APPROVED,
                               extractor_version=ev, prompt_hash=ph,
                               approved_by=approver, approved_at=now, commit=False)
            approved += 1
            if was_new:
                reg = _maybe_register(conn, r, approver)
                if reg:
                    registered.append(reg)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return approved, registered


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
    # 修掉「冲突里的新版本永不登记」漏洞（M04）：冲突成员分类为 pending_conflict（非
    # pending_new_version），故按「三元组未登记」判定，胜者若带新版本则同事务登记。
    registered = _maybe_register(conn, by_rid[target], approver)
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
    msg = f"已确认该冲突成员入榜，同组其余 {len(group) - 1} 条标记为落败。"
    if registered:
        msg += f"\n已登记新版本：{registered}（此后该版本不再因新版本待确认）"
    return True, msg


def _paths_extract(aweme_id: str) -> str | None:
    p = config.video_dir("token_bug", aweme_id) / "extract.json"
    return str(p) if p.exists() else None


_DECISION_LABEL = {
    db.AUTO_OK: "已上榜", db.APPROVED: "已确认上榜", db.PENDING: "待确认",
    db.PENDING_UNKNOWN: "待确认(模型未知)", db.PENDING_CONFLICT: "待确认(矛盾)",
    db.PENDING_NEW_VERSION: "待确认(新版本)",
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
            f"conf={r.get('confidence')} → {label}")
        # UNKNOWN/新版本：展示 raw 原文 + 归一三元组，让管理员看清一次确认的影响
        if d and d["decision"] in (db.PENDING_UNKNOWN, db.PENDING_NEW_VERSION):
            tri = "/".join(x for x in (r.get("model_series", ""), r.get("model_version", ""),
                                       r.get("model_variant", "")) if x)
            lines.append(f"    raw原文：{r.get('model_raw', '')}"
                         + (f" · 归一：{tri}" if tri else " · 归一：未能锚定"))
        lines.append(f"    证据：{r.get('evidence_quote', '')}")
    return "\n".join(lines)
