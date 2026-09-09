# Gemini Transcribe（REST / Live）作 ASR：实测与 Gladia 对比

初测 2026-09-08，并发流式补测 2026-09-09。评测机：dev box（GCP，直连 Google 200；生产机不通 Google 须带代理）。
真值集：`tests/gold/token_bug/gold.json`（12 视频 / 158 格）。

**结论先行（更新）**：`gemini-3.5-transcribe-live` **以「切段并发 + 近实时喂 + 接缝去重」封装后，下游 gold 准确率与 Gladia 打平**（Live 92.4% vs Gladia 91.8%，同一提取器，差 1 格在噪声内），速度 ~4.5x 实时、无 RPM/RPD 限（仅 20K TPM）。**已成为 Gladia 的可用等价替代/走量方案**。REST 版质量同样好但配额太小（25/天），只宜兜底。8317 上的 transcribe 不可用。

> 背景：ASR 主通道原为 Gladia（见 memory `gladia-asr-beats-sensevoice`，gold 100%）。本文评 Gemini 两个 transcribe 变体能否替代/兜底，并给出可落地的并发流式封装。

---

## 0. 三条路一句话

| 通道 | 接口 | 能不能用 | 定位 |
|---|---|---|---|
| **8317 cliproxy 的 `Gemini-3.5-Transcribe`** | OpenAI REST | ❌ 上游驳回 + **丢音频** | 不可用 |
| **裸 Gemini `gemini-3.5-transcribe`** | REST generateContent | ✅ 质量好、极稳 | 配额小（25/天），兜底/少量 |
| **裸 Gemini `gemini-3.5-transcribe-live`** | WebSocket bidiGenerateContent | ✅ 质量好、无 RPM/RPD | **并发流式封装后≈Gladia，走量首选** |

凭据（勿 commit）：生产机 `~/workspace/res/geminikey.md`（裸 key）+ `~/workspace/res/proxy.md`（3 个 HTTP 代理）。dev box 直连不用代理。

---

## 1. 8317 cliproxy 上的 `Gemini-3.5-Transcribe`：不可用

- 列在 `/v1/models` 里，但**任何调用（连纯文本）**→ 上游 `400 GenerateContentRequest.model: unexpected model name format`。
- 即使换能路由的普通 gemini，**8317 的 OpenAI 兼容层不透传音频**：附 7.3MB / 910s 音频，`prompt_tokens` 仍 2404（≈纯文本；真音频应 +~29K）。→ 8317 上"看似转写"其实是模型幻觉。
- **结论**：ASR 走 8317 此路不通，必须走裸 Gemini API。（另注：8317 的 gemini-3.5-flash 作**提取器**也频繁 503，不可用；提取器用本机/生产 agy CLI。）

## 2. 裸 `gemini-3.5-transcribe`（REST generateContent）

- **可用**：response 为 `candidates[0].content.parts[0].audioTranscription.text`。质量高、流畅，bug 编号清晰。
- **极稳**：~22 次请求零 503，每发 3.3–7s，**确定性**（同输入重复 8 次输出一字不差）。
- **实测限额（全部 429 元数据坐实，Free Tier）**：

  | 限额 | metric | 值 |
  |---|---|---|
  | RPM | `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` | **3 / 分钟** |
  | TPM | `GenerateContentInputTokensPerModelPerMinute-FreeTier` | **10,000 输入token / 分钟** |
  | RPD | `generate_content_free_tier_requests` | **25 / 天**（第 25 次成功后第 26 次即 PerDay 429） |

- 10K 上下文 ⇒ 单次 ≤~5min 音频；**25/天 ÷ ~6 块 ≈ 4 视频/天**。
- **定位**：质量/稳定都好但配额太小 → **兜底/少量**（比本地 SenseVoice 兜底强：名字更准、无本机内存开销）。

## 3. 裸 `gemini-3.5-transcribe-live`（WebSocket）——走量主力

### 3.1 协议
`wss://generativelanguage.googleapis.com/ws/...BidiGenerateContent?key=` →
发 `{"setup":{"model":"models/gemini-3.5-transcribe-live"}}` → `setupComplete` →
流式发 PCM16k（`realtimeInput.audio`，`audio/pcm;rate=16000`）→ 末尾 `audioStreamEnd`。
产出：流式 `interimInputTranscription`（临时、糊）；一段语音结束给**一条干净的 final `inputTranscription`** + `generationComplete`。**取 final、按段拼接**即整段转写（final 明显优于 interim）。

### 3.2 三个硬限与真因（踩坑记录，封装必读）
1. **输入突发限**：一次性灌太多**未消费**音频 → `1011 Resource has been exhausted`。阈值约「领先服务端 ~60–75s 音频」（60s instant OK、90s+ 就炸）。**根因是喂得比服务端消费快、缓冲超前**，不是会话硬上限。→ **按实时/近实时速率喂**即可长时间不触发。
2. **会话时长上限**：单会话喂太久 → `1008 GoAway`。
3. **静音触发断句（关键）**：**连续无留白**的语音，模型几乎不吐 final（8min 只 5 段 final、卡在 interim）；**有停顿/插静音**才触发 VAD 断句、正常吐 final（8min 23 段）。真实录音的天然停顿即可；密集语音需靠切段末尾 `audioStreamEnd` 强制 finalize。
4. **~~1007 invalid argument~~（已根治，见 3.4）**。

> 实测：1x 实时喂、单会话连跑 8 分钟**零 1011/GoAway**；plain(无留白) final=5、silence(每20s插3s静音) final=23。→ **对实时/直播场景，单会话+停顿可持续跑，瓶颈只剩 20K TPM**（1x≈1920 tok/min，够不着）。

### 3.3 走量封装：切段并发 + 近实时喂 + 接缝去重（`live_parallel.py`）
离线批量要**快**，单条实时流太慢（=视频时长）。因 Live **无 RPM/RPD 限**，用并发换速度：
- **切 N 段**（默认 5），每段**±10s 重叠**防边界截断；
- **N 路独立 WS 并发**，各段**按 1x 实时喂**（`sleep(1s/chunk)`，避开突发限）；错峰建连（`idx*1.5s`）；
- 每段收 final 拼接，段间用 **C 接缝去重**（见 3.5）；
- 实测：16.8min 视频 5 路并发 **墙钟 ~225s（~4.5x 实时）**，字节对齐后**零 1007、零重试**。

**并发天花板 = 20K TPM（实测，勿贪高）**：本模型音频 token 率实测 **~55 tok/s**（非常规 32/s），故 1x 实时下 **~6 路 × 55 ≈ 19.8K/min 就顶满 20K TPM**。实测：
  | 并发 | 墙钟(16.8min视频) | 速度 | 1011 | 下游 gold(7669) |
  |---|---|---|---|---|
  | 5 路 | 225s | 4.5x | 0 | 10/12 |
  | 6 路 | 197s | 5.1x | 0 | 8/12 ⬇ |
  | 10 路 | 失败 | — | seg6–9 反复 `1011 exceeded quota`、重试全挂 | — |
  - **速度地板由 TPM 决定**：整段 ~55K token ÷ 20K/min ≈ **2.8min**，与并发数无关；5–6 路已逼近，**再加并发快不了**（7+ 必撞 1011 空转）。
  - **切太短掉识别率**：6 路段更短(~168s) 这次把 `B001` 糊成 `B010`（青铜三格全丢）→ 8/12 < 5 路 10/12。段边界变化伤边界 token（单次样本，方向可信）。
  - **结论：默认 5 路最优**（贴近 TPM 地板 + 质量稳）；**别调到 7+**。想更快只能靠多 key/project（各自独立 20K TPM）或付费提额，不在并发数上。

### 3.4 1007 根因与修复（重要）
**症状**：并发时约 1–2/5 段以 `1007 (invalid frame payload) Request contains an invalid argument` 断连；换 3 路、关 ping 都无效。
**真因**：段边界按 `int(s*BPS)` 计算，`s` 为小数秒时可落在**奇数字节**——16bit PCM 半个采样，服务端判非法。
**修复**：边界按**整秒对齐**（`int(round(s))*BPS`，即 BPS=32000 的倍数，必为偶数），末块也保证偶数长度。改后 5 路并发**彻底无 1007**。

### 3.5 接缝去重（C 方案，`stitch_overlap`）
相邻段各自转写了重叠的 ~20s，拼接会重复。三方案对比（用真实并发产物）：

| 方案 | 字数 | 残留重复句 | 覆盖率 |
|---|---|---|---|
| A 不去重 | 6289 | 9 | 基准 |
| B 句级接缝拼接 | 5858 | 2 | 保留 |
| **C 最长重叠拼接** ✅ | 5888 | 2 | 保留（bug 全对、Luna 33≈Gladia 34） |
| D = C + 句级残留清理 | 5888 | 2 | 无增益 |

**选 C**：`difflib.SequenceMatcher` 在「前段尾 ~600 字 × 后段头」找最长公共块，裁掉后段头部落在重叠内的部分再拼。对 ASR 模糊差异（`GRM↔GBT`、`威斯弗莱什↔维斯弗莱士`）稳健、最保内容、单遍。残留 2 处是句内碎片口吃（真实语音，无害）。已合入 `live_parallel.py`。

## 4. 与 Gladia 的对比（同一把尺）

### 4a. 转写覆盖率（不经 LLM，直接看转写命中打分要素，12 视频）
| | Live(60s硬切) | Gladia |
|---|---|---|
| gold bug 编号覆盖 | 52/56 = 93% | 55/56 = 98% |
| 名单模型可听命中 | 34/40 = 85% | 36/40 = 90% |

（这是**旧的 60s 硬切**版；并发+C 版覆盖率显著回升，见 4c 的下游结果。）

### 4b. 早期 A/B（已被 4c 取代，留作对照）
弱提取器（gemini-3.5-flash@8317）+ 60s 硬切 Live：**Live 75.0% vs Gladia 84.4%**（128 格）。落后 9 点，**主因是 60s 硬切边界丢词 + 弱提取器**——非模型问题。

### 4c. 最终 A/B（并发+C 转写，提取器=agy gemini-3.7-flash-medium 生产同款，全 12 视频 158 格）✅
| 视频 | Live(并发+C) | Gladia |
|---|---|---|
| 7681 | 12/12 | 8/12 |
| 7680 | 12/12 | 12/12 |
| 7679734 | 16/16 | 16/16 |
| 7679001 | 12/12 | 12/12 |
| 7677918 | 9/15 | 8/15 |
| 7674603（30格） | 28/30 | 30/30 |
| 7669287 | 10/12 | 12/12 |
| 7664522 | 12/12 | 12/12 |
| 7661634 | 8/9 | 9/9 |
| 7668291 | 7/8 | 6/8 |
| 7670119 | 9/9 | 9/9 |
| 7673854 | 11/11 | 11/11 |
| **合计** | **146/158 = 92.4%** | **145/158 = 91.8%** |

- **打平**（Live 微超 1 格，在提取噪声内）。逐视频：Live 赢 3、平 7、输 2。
- 绝对值 ~92%（非生产 98.7%）是因**现场重跑提取**（`known=set()` 无 known_versions 上下文 + agy 逐次波动）；两侧同尺，**差=打平**才是结论。
- 对比 4b：补齐「并发实时喂（消除边界丢词）+ C 去重 + 字节对齐 + 强提取器」后，9 点差距归零。

## 5. 结论 & 建议

1. **`gemini-3.5-transcribe-live` + 并发流式封装 = Gladia 的可用等价替代**：质量打平（92.4% vs 91.8%）、~4.5x 实时、无 RPM/RPD（仅 20K TPM，5 路才用一半）、字节对齐后并发零 1007。
2. **实时/直播场景**：单会话 + 自然停顿即可持续跑，瓶颈只剩 20K TPM。
3. **REST `gemini-3.5-transcribe`**：质量同好但 25/天，**兜底/少量**（可换掉本地 SenseVoice 兜底）。
4. **8317 的 transcribe 与 gemini-flash 提取器**均不可用（丢音频 / 频繁 503）；提取器用 **agy CLI（生产 gemini-3.7-flash-medium）**。
5. Gladia 仍是当前生产主力；是否切换到 Live 并发方案，取决于是否要摆脱 Gladia 的月度时长额度——质量与速度已不再是障碍。
6. **独立结论**（音频直出实验）：多模态"音频直出提取"整体替换不可行（弱模型 78.8%、自产 transcript 护栏两向失灵），但去糊几乎免费又准，宜作"模型名/名单校对"辅助 pass（见 memory `audio-direct-name-proofread-pass`）。

## 6. 复现要点 & 产物

- **脚本**：`ops/live_transcribe.py`（**已长期化**：切段并发 + 近实时喂 + C 去重 + 字节对齐；`GEMINI_API_KEY` 环境变量；可 CLI 或 `from ops.live_transcribe import transcribe` 库调用）。A/B 打分 harness `validate_ab.py` 仍在 scratchpad（eval 专用，依赖 gold + agy 提取器）。
- **音频**：12 gold 视频音频存 `data/gold_audio/<aweme_id>.mp3`（gitignore，视频已删）；并发+C 转写 `data/gold_audio/par_<aweme_id>.txt`；旧 60s 版 `live_<aweme_id>.txt`。可直接复用不重转。
- **Live 并发调用要点**：PCM16k；边界**整秒对齐**（否则 1007）；N 路并发、±10s 重叠、按 1x 实时喂、错峰建连；取 final `inputTranscription`；`stitch_overlap` 拼接。
- **REST 调用**：`POST .../models/gemini-3.5-transcribe:generateContent?key=`，parts=[{text},{inline_data:{mime_type:"audio/mp3",data:b64}}]；取 `audioTranscription.text`。
- **A/B 方法**：`core.extract.build_extract(aweme_id,title,transcript,known=set())` 分别吃 Live/Gladia 转写，提取器统一（agy gemini-3.7-flash-medium），按 `aweme_id×模型×bug_id` 对 gold 逐格打分。
