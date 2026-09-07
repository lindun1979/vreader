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
  **轮次驱动**（2026-09 改，用户确认）：LLM 出 `solved_round`（第几轮做对；钻石/王者可到
  第4轮=白做0分但已解，0=没做对），代码 `derive_from_round` 反推 score/solved/rounds
  ——修掉钻石/王者 score=0 二义（第4轮做对 vs 没做对无法区分）。board 第4轮做对显示第[4]次、
  计解出、0分。标题作对战名单提召回。
- 模型归一（**series-norm**，见 `core/models.py` + `docs/plans/vreader-series-norm-plan-v5.md`）：
  `models.yml` 是**系列表**（format 身份模板 + 系列别名 + 两级昵称 + 变体 + seed 版本）。
  LLM 出 `model_raw/series/version/variant`（**不出 canonical，均不可信**）；代码对
  transcript+title 做「提及锚定」（`build_anchors`）产出 `(series,version[,variant])` 锚点，
  `resolve_record` 六步（raw 拆词纠错 → 版本锚定核验 → 系列纠正 → 共指兜底 → 组合锚定总闸
  → `compose_canonical` round-trip 撞名校验）拼出 canonical。version 靠源文本数字，不靠昵称
  **共指兜底扩展（偏离 plan 严格「同视频共指」，因真实 ASR 常不口播版本/糊系列名）**：
  本视频无锚点时退**已知版本集**唯一兜底（裸「豆包」→Doubao Seed 2.1）；本视频锚定歧义时
  用 known 破歧（ASR 糊出「豆包2」+「豆包2.1」→取已知 2.1）；version 口播但变体未口播且
  (series,version) 仅一个已知变体时补全（「step3.7」→Step 3.7 Flash）。**均只用 known 破歧/
  兜底、绝不覆盖已口播版本**（多个已知版本且无口播 → UNKNOWN，不臆断；见测试守卫）。
  （「一哥/火星刺客」随期指系列当时最新版本，只绑系列；见 [[glm-yige-dynamic-reference]]）。
  未见过版本首次出现 → `pending_new_version`，`vr确认` 同事务 `register_known_version`
  入 `known_versions` 表（三入口：批量/冲突赢家/rev）。extract 带 `schema_rev=2`；旧产物
  （无 schema_rev）canonical 须 ∈ 冻结 `LEGACY_CANONICALS`，`validate_extract` 双轨判。
  模型分类（用户 2026-09 拍板，勿改）：**Claude Opus 4.8 ≠ Opus 5.0**（去 version_map 4.8→5）、
  **Fable 5 ≠ Fable 5.1**、**GPT 5.6 有 Sol/Terra/Luna 三变体**（均已进 models.yml seed）。
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
  四态待确认 pending/pending_unknown/pending_conflict/pending_new_version，board 只渲染 auto_ok+approved。
- known_versions 读消费点（prompt 注入、Gladia 热词、决策判定）开只读短连接读已提交集合；
  决策时点集合 B 由入口在 `publish_lock` 内读好传 `apply_decisions(known=...)`；提取输入集合 A
  写入 `known_versions_used`（**永不改写**），决策快照写 `known_versions_used_at_decision`（M08）。
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
`pytest -q`（177 项全绿，**只在研发机跑**；生产机不跑 pytest）。手动处理：
`python -m core.cli "<链接>"`；重跑提取：`--reprocess <id>`；强制用 Gladia 重转（修此前落
兜底 ASR 的数据，删 transcript 走全量管线）：`--retranscribe <id>`。
真值集：`tests/gold/token_bug/gold.json`（用户人工标注 7 视频得分 + aweme_id 映射，进仓）；
准确率评测（只读，隔离副本）：`python ops/eval_gold.py <data副本> --min-extract 96`
（比较键 aweme_id×canonical×bug_id，缺格/多报/重复/冲突全计错，双口径 + 逐格 diff）。
升级/回滚运维手册：`docs/ops-series-norm-upgrade-rollback.md`（快照清单 + 有界回滚 +
撤销登记≠撤销批准）；上游透传补丁：`docs/skill_router-vreader-help-detail-confirm.patch`
（验收 `ops/verify_upstream_routing.py <life-assistant-checkout>`）。

## 纪律
public 仓：凭据→.env，内网细节→DEPLOY.local.md，版权数据→data/，均 gitignore。
