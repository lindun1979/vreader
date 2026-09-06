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
  别名表命中优先于 LLM 猜测；模型归一靠**版本号数字**——「国模一哥/一哥」是博主对
  GLM 系【当时最新】版本的动态称呼（5.2 期=GLM-5.2、5.3 期=GLM-5.3），勿把"一哥"绑
  某版本号（见 memory glm-yige-dynamic-reference）。
- ASR：**Gladia 云转写主**（gold 97 格 100% vs SenseVoice 89.7%，~11s/条，带 models.yml
  热词；GLADIA_API_KEY 配 .env，免费 10h/月），失败自动回落本地 SenseVoiceSmall（CPU）。
  SenseVoice 长视频**必须分块**转写（整段喂入峰值 10GB+ 拖垮 16G 机）。
- 存储：SQLite（tasks / notification_outbox / record_decisions），WAL、每线程独立连接。

## 关键不变量（勿破坏）
- 连接绝不跨线程共享（learnings sqlite-shared-connection-threads）。
- 终态状态更新与通知写 outbox 必须同一事务（`db.finalize_task`，失败必达飞书，无静默）。
- record_id = `model_key`（未知模型用 raw 归一）＋`bug_slot`（空 bug_id 用 evidence 指纹）
  ＋难度＋score 的指纹，**不含 confidence**、顺序无关；`auto_ok` 每次按 confidence 重判，
  `approved`/`rejected_conflict` 凭指纹继承恒存（`rejected_conflict` 缺失不 stale）；
  三态待确认 pending/pending_unknown/pending_conflict，board 只渲染 auto_ok+approved。
- 提取输出必须过 schema + evidence_quote 是 transcript 子串，否则丢弃（全丢→no_content
  成功，不当失败重试）；缓存 extract.json 读回也必过 `validate_extract`，坏则留证重建。
- 单实例执行权：数据目录 flock（serve 最先取），CLI 写路径同锁，`--board` 纯读不取锁。
- 执行边界：外网/重型阶段带 subprocess timeout + 下载字节/时限硬限 + 任务总预算；
  健康 healthz 三态判定，不健康返 503（线程死/outbox 积压/db 连错/低磁盘/ASR 卡死）。
- 频道插件化：通用逻辑在 core/，频道特定在 channels/<ch>/。

## 生产部署
见 `DEPLOY.local.md`（gitignore）。生产解释器=`~/workspace/vreader/.venv/bin/python`
（launchd plist 用它；系统 `/usr/local/bin/python3.11` 无 jsonschema 等项目依赖，勿用）。
git 仓部署：`git pull`（**必走 HTTP 代理**，直连 pack 卡死）→ launchctl unload/load。
reprocess/迁移等 CLI 写操作需 flock，**必须先 `launchctl unload` 停 serve** 再跑。
launchd 管理（禁 nohup），登记运维斯 SERVICES.md。

## 测试
`pytest -q`（92 项全绿）。手动处理：`python -m core.cli "<链接>"`；重跑：`--reprocess <id>`。
真值集：`tests/gold/token_bug/gold.json`（用户人工标注 7 视频得分，进仓）；
准确率评测脚本思路见开发记录（提取 vs 真值按 模型×等级 比对）。

## 纪律
public 仓：凭据→.env，内网细节→DEPLOY.local.md，版权数据→data/，均 gitignore。
