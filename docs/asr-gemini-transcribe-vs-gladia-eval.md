# Gemini Transcribe（REST / Live）作 ASR：实测与 Gladia 对比

日期：2026-09-08。评测机：dev box（GCP，直连 Google 200；生产机不通 Google 须带代理）。
真值集：`tests/gold/token_bug/gold.json`（12 视频 / 158 格）。结论先行：**模型质量够，但都不能现状替换 Gladia**——REST 版配额太小、Live 版当前分块封装掉召回。Gladia 仍是主力 ASR。

> 相关背景：ASR 主通道选型见 [Gladia 完胜 SenseVoice] 的既有结论（gold 97 格 100%）。本文只评 Gemini 两个 transcribe 变体能否替代/兜底。

---

## 0. 三条路一句话

| 通道 | 接口 | 能不能用 | 定位 |
|---|---|---|---|
| **8317 cliproxy 的 `Gemini-3.5-Transcribe`** | OpenAI REST | ❌ 上游驳回 + **丢音频** | 不可用 |
| **裸 Gemini `gemini-3.5-transcribe`** | REST generateContent | ✅ 质量好、极稳 | 配额小（25/天），只宜兜底/少量 |
| **裸 Gemini `gemini-3.5-transcribe-live`** | WebSocket bidiGenerateContent | ✅ 质量好、配额无限 | 走量首选，但需更好分块 |

凭据（勿 commit）：生产机 `~/workspace/res/geminikey.md`（裸 key）+ `~/workspace/res/proxy.md`（3 个 HTTP 代理）。

---

## 1. 8317 cliproxy 上的 `Gemini-3.5-Transcribe`：不可用

- 列在 `/v1/models` 里，但**任何调用（连纯文本）**→ 上游 `400 GenerateContentRequest.model: unexpected model name format`。
- 即使换能路由的普通 gemini，**8317 的 OpenAI 兼容层不透传音频**：附 7.3MB / 910s 音频，`prompt_tokens` 仍 2404（≈纯文本；真音频应 +~29K）。→ 8317 上"看似转写"其实是模型幻觉。
- **结论**：ASR 走 8317 此路不通，必须走裸 Gemini API。

## 2. 裸 `gemini-3.5-transcribe`（REST generateContent）

- **可用**：response 为 `candidates[0].content.parts[0].audioTranscription.text`。质量高、流畅，bug 编号清晰。
- **极稳**：连打 8 发 + 整段 6 块，**共 ~22 次请求零 503**，每发 3.3–7s，**确定性**（同输入重复 8 次输出一字不差）。与 flash 系（频繁 503、80–200s）天壤之别。
- **实测限额（全部 429 元数据坐实，Free Tier）**：

  | 限额 | metric | 值 |
  |---|---|---|
  | RPM | `GenerateRequestsPerMinutePerProjectPerModel-FreeTier` | **3 / 分钟** |
  | TPM | `GenerateContentInputTokensPerModelPerMinute-FreeTier` | **10,000 输入token / 分钟** |
  | RPD | `generate_content_free_tier_requests` | **25 / 天**（第 25 次成功后第 26 次即 PerDay 429） |

- 10K 上下文 ⇒ 单次 ≤~5min 音频，长视频要切块（16.8min≈6 块）。**25/天 ÷ ~6 ≈ 4 视频/天**。
- **定位**：质量/稳定性都好，但配额太小 → **兜底 / 少量**（比本地 SenseVoice 兜底强：名字更准、无本机内存开销）。

## 3. 裸 `gemini-3.5-transcribe-live`（WebSocket）

- **接口**：`wss://generativelanguage.googleapis.com/ws/...BidiGenerateContent?key=`。发 `{"setup":{"model":"models/gemini-3.5-transcribe-live"}}` → `setupComplete` → 流式发 PCM16k（`realtimeInput.audio`, `audio/pcm;rate=16000`）→ `audioStreamEnd`。
- **产出**：流式 `interimInputTranscription`（临时、糊），一段结束给**一条干净的 final `inputTranscription`** + `generationComplete`。**取 final、按段拼接**即整段转写。final 质量明显优于 interim（60s 段实测 `GLM-5.2 / DeepSeek V4 Flash / GPT-5.6 Luna / B001` 全对，比 Gladia 的 `GBT/DeepSick/GRM`、REST 版的 GRM 都干净）。
- **限额**：dashboard 标 RPM/RPD **无限制**、TPM **20K**；实测无每日次数上限。
- **坑（封装必读）**：
  1. **输入突发限**：整段 instant 喂 → `1011 Resource has been exhausted`（60s instant OK，240s 就炸）。**当前解法：60s 分块、每块独立会话、块间 8s**。
  2. **近实时**：约 7–11min / 视频（比 REST 的 3min/4s 慢得多）。
  3. **并行会 OOM dev box**（3 路并行被杀），走量转写要**串行**或严格限并发。

## 4. 与 Gladia 的对比（同一把尺）

### 4a. 转写覆盖率（不经 LLM，直接看转写是否命中打分要素，12 视频）
| | Live | Gladia |
|---|---|---|
| gold bug 编号覆盖 | 52/56 = **93%** | 55/56 = **98%** |
| 名单模型可听命中 | 34/40 = **85%** | 36/40 = **90%** |

### 4b. 下游 gold 准确率 A/B（**提取器固定为同一个 gemini-3.5-flash@8317**，隔离 ASR 变量；11 个双侧都成功的视频、128 格）
| ASR → 同一提取器 | gold 准确率 |
|---|---|
| **Live-ASR 转写** | 96/128 = **75.0%** |
| **Gladia 转写** | 108/128 = **84.4%** |

- **Gladia 领先 ~9 点**，但逐视频有波动：11 个里 Live 持平/反超 5 个、落后 6 个。
- ⚠️ **两个绝对值都偏低是提取器的锅**：这里用弱的 gemini-3.5-flash，不是生产 agy gemini-3.7（生产同尺 98.7%）。**只能看 Live-vs-Gladia 的差**，不代表上线质量。
- **Live 落后的主因是分块 harness 丢词（60s 硬切边界），不是模型差**——覆盖率缺口（93% vs 98%）与之吻合；未分块单段 Live 输出质量其实很好。

## 5. 结论 & 建议

1. **Gladia 仍是主力 ASR**（gold 100%、免费 10h/月、无每日次数硬顶、~11s/条）。
2. **REST `gemini-3.5-transcribe`**：优秀的**兜底/少量**通道（换掉本地 SenseVoice 兜底），受 25/天限制。
3. **Live `gemini-3.5-transcribe-live`**：**走量储备方向**——模型质量够、配额无限，但要先把分块做好（**VAD 感知切分 / 更大重叠 / 或单会话真·实时流**）以补回边界丢的 ~5–10% 召回，才能追平 Gladia。
4. **另一条独立结论**（见音频直出实验）：多模态"音频直出提取"整体替换不可行（弱模型 78.8%、自产 transcript 护栏两向失灵），但**去糊几乎免费又准**，宜作"模型名/名单校对"辅助 pass（见 memory `audio-direct-name-proofread-pass`）。

## 6. 复现要点 & 产物

- **音频**：12 gold 视频音频存 `data/gold_audio/<aweme_id>.mp3`（gitignore，视频已删）；Live 转写存 `data/gold_audio/live_<aweme_id>.txt`。调切法可直接复用，不用重转。
- **REST 调用**：`POST .../v1beta/models/gemini-3.5-transcribe:generateContent?key=`，parts=[{text},{inline_data:{mime_type:"audio/mp3",data:b64}}]；取 `audioTranscription.text`。
- **Live 调用**：见 §3；PCM16k、60s 分块、每块独立 WS 会话、块间 8s、取 final `inputTranscription` 拼接、串行。
- **A/B 方法**：`core.extract.build_extract(aweme_id,title,transcript,known=set())` 分别吃 Live/Gladia 转写，提取器覆盖为同一模型，按 `aweme_id×模型×bug_id` 对 gold 逐格打分。
