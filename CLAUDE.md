# CLAUDE.md — vreader

视频博主 → 结构化知识平台。首个频道 token_bug（抖音「token（词源）」模型实测榜单）。
详细规划见 `docs/plans/vreader-mvp-plan-v6.md`（经 6 轮对抗评审收敛）。

**状态：已上线生产（launchd `ai.chivox.vreader`，运维斯登记）。** 两次真人端到端通过，
对用户真值集准确率 91%（gemini-2.5-flash）。

## 架构速记
- 入口：飞书（复用 life-assistant 飞书号，skill_router HMAC 透传到本服务 127.0.0.1:8232）。
- 管线：解析 aweme_id → 下载 → ffmpeg wav → SenseVoice(分块) → LLM 提取 → 决策 → board.md。
- 下载：**不用 yt-dlp**（Douyin extractor 漏参已坏），直连 web detail API + 匿名 ttwid。
- 提取 LLM：生产机 **agy（Antigravity CLI）gemini-3.7-flash-medium 主**（生产链路 gold 97 格
  96/97≈99%；LLM_BACKEND=agy，直连不通须配 AGY_PROXY 代理，见 [[agy-on-prod-via-proxy]]），
  兜底 :8317 cliproxy 的 gemini-3.5-flash-lite（gold 86.6%）；配 LLM_MODEL/LLM_MODEL_FALLBACK。
  得分驱动（LLM 只提 score，代码反推 solved/rounds）。标题作对战名单提召回。
- ASR：**Gladia 云转写主**（gold 97 格 100% vs SenseVoice 89.7%，~11s/条，带 models.yml
  热词；GLADIA_API_KEY 配 .env，免费 10h/月），失败自动回落本地 SenseVoiceSmall（CPU）。
  SenseVoice 长视频**必须分块**转写（整段喂入峰值 10GB+ 拖垮 16G 机）。
- 存储：SQLite（tasks / notification_outbox / record_decisions），WAL、每线程独立连接。

## 关键不变量（勿破坏）
- 连接绝不跨线程共享（learnings sqlite-shared-connection-threads）。
- 终态状态更新与通知写 outbox 必须同一事务（失败必达飞书，无静默）。
- record_id = 事实字段指纹（含模型/难度/solved/rounds，**不含 confidence**）；
  auto_ok 每次按 confidence 重判，仅 approved 凭指纹继承。
- 提取输出必须过 schema + evidence_quote 是 transcript 子串，否则不入榜。
- 频道插件化：通用逻辑在 core/，频道特定在 channels/<ch>/。

## 生产部署
见 `DEPLOY.local.md`（gitignore）。生产 python=`/usr/local/bin/python3.11`。
launchd 管理（禁 nohup），登记运维斯 SERVICES.md。

## 测试
`pytest -q`（45 项全绿）。手动处理：`python -m core.cli "<链接>"`。
真值集：`tests/gold/token_bug/gold.json`（用户人工标注 6 视频 97 格得分，进仓）；
准确率评测脚本思路见开发记录（提取 vs 真值按 模型×等级 比对）。

## 纪律
public 仓：凭据→.env，内网细节→DEPLOY.local.md，版权数据→data/，均 gitignore。
