# AGENTS.md — vreader

把「视频博主 → 结构化知识」的通用管线。首个频道 `token_bug`：处理抖音博主
「token（词源）」的模型实测视频，自动维护一张「模型 × bug 难度」榜单。
详细规划见 `docs/plans/vreader-mvp-plan-v6.md`（经 6 轮对抗评审收敛）。

**状态：已上线生产（launchd `ai.chivox.vreader`，运维斯登记）。** 主转写为 Gladia、失败时回落到
本地 SenseVoiceSmall，生产提取主通道为 agy。研发测试全量 335 项。

## 纪律（public 仓，硬禁令）

凭据只在 `.env`（gitignore）；内网/部署细节在 `DEPLOY.local.md`（gitignore）；
视频/音频/转写/提取/榜单等他人版权衍生数据全在 `data/`（gitignore），不进仓。

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
  **分段证据**（省略号分隔的有序多段原文，每段 ≥8 字、段间隔 ≤800 归一化字符）兜底接受但
  **强制人工复核**（confidence 封顶 0.6，不 auto_ok）；prompt 仍要求连续摘录。某等级本视频只有
  一个 bug_id 时空 bug_id 回填（让同格 attempt_key 对齐 → 矛盾组走 pending_conflict，不自动裁决）。
  **单段证据近似找回**（WP-B）：精确与分段都失败时，单段且归一化 ≥20 字的证据用编辑距离找回，
  编辑数 1..min(3, L//20) 且最优命中唯一（多处不重叠等距命中 → 丢弃）；quote 改写为**原始转写连续切片**，
  LLM 原文留 envelope `evidence_repaired`，confidence 封顶 0.6 强制复核。多段证据不做近似。
  回执带丢弃原因聚合 + 证据错字修正计数 + 名单×bug 疑似漏格告警（只告警，不改决策；见 `pipeline._receipt_warnings`）。
- **`.0` 孪生写法按已登记写法归一**（WP-A）：`norm_version` 仍不去 `.0`（3.0≠3 的全局语义不改），
  但 `models.known_spelling` 在精确写法未登记、而其孪生（`5`↔`5.0`，仅整数与单个 `.0`）已登记时沿用已登记写法
  （提取 `resolve_record` / 校正 `normalize_triple` 两入口）；两写法都已登记按精确写法。批量确认拒绝
  「孪生已登记的 pending_new_version」（不批准不登记，回执逐条给 `=系列/版本/变体` 校正命令，`twin_blocked_pending`）。
- 单实例执行权：数据目录 flock（serve 最先取），CLI 写路径同锁，`--board` 纯读不取锁。
- 执行边界：外网/重型阶段带 subprocess timeout + 下载字节/时限硬限 + 任务总预算；
  健康 healthz 三态判定，不健康返 503（线程死/outbox 积压/db 连错/低磁盘/ASR 卡死）。
- 频道插件化：通用逻辑在 core/，频道特定在 channels/<ch>/。

## 用法（飞书）

把抖音分享链接发给机器人 → 自动下载/转写/提取 → 处理完回执。同一视频重发：处理中/已完成 →
回「已在处理/已完成」；**已 terminal_failed → 重置为 received（retry_count 清零）重新排队**
（`db.requeue_failed_task`，条件 UPDATE 只动 terminal_failed 行）。
- `vr帮助` 显示用法说明
- `vr榜单` 取回最新榜单
- `vr明细 <video_id>` 看某视频逐条明细（记录码 + 状态 + 完整证据；未知/新版本另显 raw 原文 + 归一三元组）
- `vr确认 <video_id>` 批量确认普通/新版本待确认记录入榜（**新版本首次确认即登记**，此后同版本自动上榜）；
  `vr确认 <video_id> <记录码>` 逐条确认矛盾记录；`vr确认 <video_id> rev:<版本>` 绑版本确认（防确认过时内容）
- `vr确认 <video_id> <记录码>[@<result_rev>]=<系列>/<版本>[/<变体>]` **校正模型名并确认**（见下「校正命令」）

## 管线与架构

```
分享链接 → 解析 aweme_id → 下载(移动端 feed 接口取详情) → Gladia 云转写(主)
  ｜失败回落 ffmpeg 16k wav → SenseVoice 分块转写
  → LLM 提取(schema+证据校验) → 决策(auto_ok/pending)
  → 渲染 board.md → 飞书回执
```

- **入口**：飞书（复用 life-assistant 飞书号，skill_router HMAC 透传到本服务 127.0.0.1:8232）。
- **下载**：**不用 yt-dlp**（Douyin extractor 漏参已坏）。详情走移动端 feed 接口
  `aweme.snssdk.com/aweme/v1/feed/?aweme_id=`（无签名/无 cookie，字段结构同 web aweme_detail；
  返回列表混推荐视频，**必须按 aweme_id 严格匹配**）。web detail API（`/aweme/v1/web/aweme/detail/`）
  2026-10 起被 ArgusSecurityPlugin 全量 403（强制 uifid + a_bogus 签名），已弃。feed 是未公开老接口，
  若再被封，备选：生产机已装 Playwright chromium，可无头打开视频页截取详情。播放地址下载仍带 ttwid。
- **ASR**：**Gladia 云转写主**（gold 97 格 100% vs SenseVoice 89.7%，~11s/条，带 models.yml
  热词；`GLADIA_API_KEY` 配 .env，免费 10h/月），失败自动回落本地 SenseVoiceSmall（funasr, CPU）。
  Gladia 每个请求（上传/建任务/轮询）网络失败先直连重试 3 次、再走 `GLADIA_PROXY` 代理一次
  （切到代理后粘住），4xx（鉴权/参数）不重试；全挂才回落 SenseVoice。
  SenseVoice 长视频**必须分块**转写（整段喂入峰值 10GB+ 拖垮 16G 机）。`extract.json` 的 `asr_model`
  记录实际所用引擎；单任务回执 + 榜单末尾均会**显式警示**落到兜底 ASR 的视频（名字易糊、结果存疑）。
  （评估过的等价替代：Gemini `gemini-3.5-transcribe-live` 并发流式，gold 92.4%≈Gladia 91.8%、
  无 RPM/RPD 限、~4.5x 实时；工具 `ops/live_transcribe.py`，**未接入主管线**、生产 wss 需代理；
  详见 `docs/asr-gemini-transcribe-vs-gladia-eval.md`。当前生产仍 Gladia。）
- **提取 LLM**：`LLM_BACKEND` 可选 `agy`（Antigravity CLI）/ `openai`（:8317 cliproxy）/ `claude`（本地 CLI）。
  生产机 **agy gemini-3.7-flash-medium 主**（生产链路 gold 97 格 96/97≈99%；直连不通须配 `AGY_PROXY`
  代理，见 [[agy-on-prod-via-proxy]]），兜底 :8317 cliproxy 的 gemini-3.5-flash-lite（gold 86.6%）；
  配 `LLM_MODEL`/`LLM_MODEL_FALLBACK`。**研发机调 agy 必须走与生产相同的美国代理**（`.env` 的
  `AGY_PROXY`），否则双地登录触发 Google 风控（2026-09-25 两机 agy 同时被要求验证账号）。
- **轮次驱动打分**（2026-09 改，用户确认）：LLM 出 `solved_round`（第几轮做对；钻石/王者可到
  第4轮=白做0分但已解，0=没做对），代码 `derive_from_round` 反推 score/solved/rounds
  ——修掉钻石/王者 score=0 二义。board 第4轮做对显示第[4]次、计解出、0分；博主追加轮（如王者
  第5轮）做对同理=已解0分（`MAX_ROUND=9` 防垃圾值）。标题作对战名单提召回。
- **完整性加固**（2026-09）：证据须为转写原文（分段证据规则见不变量）；回执附丢弃原因聚合、
  未识别/缺编号计数、名单×bug「疑似漏提」与标题系列无记录告警。
- **模型归一（series-norm）**，见 `core/models.py` + `docs/plans/vreader-series-norm-plan-v5.md`：
  `models.yml` 是**系列表**（format 身份模板 + 系列别名 + 两级昵称 + 变体 + seed 版本），版本号不写死。
  LLM 出 `model_raw/series/version/variant`（**不出 canonical，均不可信**）；代码对
  transcript+title 做「提及锚定」（`build_anchors`）产出 `(series,version[,variant])` 锚点，
  `resolve_record` 六步（raw 拆词纠错 → 版本锚定核验 → 系列纠正 → 共指兜底 → 组合锚定总闸
  → `compose_canonical` round-trip 撞名校验）拼出 canonical。version 靠源文本数字，不靠昵称。
  **共指兜底扩展（偏离 plan 严格「同视频共指」，因真实 ASR 常不口播版本/糊系列名）**：
  本视频无锚点时退**已知版本集**唯一兜底（裸「豆包」→Doubao Seed 2.1）；本视频锚定歧义时
  用 known 破歧（ASR 糊出「豆包2」+「豆包2.1」→取已知 2.1）；version 口播但变体未口播且
  (series,version) 仅一个已知变体时补全（「step3.7」→Step 3.7 Flash）。**均只用 known 破歧/
  兜底、绝不覆盖已口播版本**（多个已知版本且无口播 → UNKNOWN，不臆断；见测试守卫）。
  「一哥/火星刺客」随期指系列当时最新版本，只绑系列（见 [[glm-yige-dynamic-reference]]）。
  未见过版本首次出现 → `pending_new_version`，`vr确认` 同事务 `register_known_version`
  入 `known_versions` 表（三入口：批量/冲突赢家/rev）。extract 带 `schema_rev=2`；旧产物
  （无 schema_rev）canonical 须 ∈ 冻结 `LEGACY_CANONICALS`，`validate_extract` 双轨判。
  `MiMo` 与 `MiniMax` 均支持 `Flash` 变体（如 `MiMo/2.6/Flash` → `MiMo 2.6 Flash`、
  `MiniMax/3.1/Flash` → `MiniMax M3.1 Flash`，确认时登记版本，不走 seed 预置）。
  模型分类（用户 2026-09 拍板，勿改）：**Claude Opus 4.8 ≠ Opus 5.0**（去 version_map 4.8→5）、
  **Fable 5 ≠ Fable 5.1**、**GPT 5.6 有 Sol/Terra/Luna 三变体**（均已进 models.yml seed）。
- **状态**：SQLite（tasks / notification_outbox / record_decisions；WAL、每线程独立连接、原子领取）；
  崩溃恢复按落盘产物前推跳过阶段（产物齐全时不依赖上游网络）；recover 递增 retry_count 防毒丸崩溃循环。
- **通知**：outbox 表，与任务终态同事务写入，独立线程退避重试，重启恢复投递。
- **可靠性加固**：flock 单实例执行权；健康三态 healthz（在执行/空闲有活/退避）+ 线程
  守护三分支；磁盘门禁（拒收+暂停+限频告警）；下载字节/时限硬限、subprocess timeout + killpg
  后代、任务总预算；产物原子写 + 发布锁。

## 校正命令（V-M16，见 `docs/plans/vreader-confirm-correction-command-plan-v8.md`）
`vr确认 <video_id> <记录码>[@<result_rev>]=<系列>/<版本>[/<变体>]` —— 对 pending/pending_unknown/
pending_new_version 记录人工指定三元组，一步「改 extract 四字段 → 登记 known_version →
决策 APPROVED → 上榜」。`@result_rev` **可选**（带则校验防陈旧明细，不带则直接改——同视频连改
不受 rev 变化牵制）。系列/变体名对输入宽容（精确→忽略大小写→别名，如 `preview`/`预览版`→Preview、
`deepseek`/`ds`→DeepSeek）；canonical 由 format 定（DeepSeek 带 V，如 `DeepSeek V4.1 Flash`）。
改名会改 rid，双存储（文件/DB）非原子，用 `correction_operations` journal + source/target sha CAS
恢复（serve 启动 + 命令入口各恢复一次；命令内限当前视频）。停机期被 reprocess 改写 → 恢复转
`needs_review`，人工 CLI 解决：`--list-corrections`（只读 `db.connect_ro`）查看，
`--resolve-correction <op_id> <keep-file|apply-journal>`（停 serve、取 flock）二选一。
`keep-file` 保留全局版本登记、仅回退产物。含空格系列名（Claude Opus 等）不支持，走手改。
**新变体无条件接受**（V-M17，用户 2026-10 定：人工标定优先于清单）：已知系列下 models.yml 未声明的变体
（如 `gemini/4/Argon`）原样接受（首次输入定大小写，此后忽略大小写复用），同事务登记进 `known_versions`；
`models.load_series()` 每次合并「known_versions 中 yml 外的变体」（只读短连接），变体名本身作别名
→ 此后自动锚定/归一，prompt/热词/读回校验同步可见。**未知系列仍拒**（须改 models.yml 定 format）；
自动提取/批量确认不会引入新变体。删学到变体的最后一条 known_versions 行会让引用它的 extract 判坏，见回滚手册。

## 生产部署
见 `DEPLOY.local.md`（gitignore）。生产解释器=`~/workspace/vreader/.venv/bin/python`
（launchd plist 用它；系统 `/usr/local/bin/python3.11` 无 jsonschema 等项目依赖，勿用）。
git 仓部署：`git pull`（**必走 HTTP 代理**，直连 pack 卡死）→ `bash ops/prod.sh restart` →
`bash ops/prod.sh health`。reprocess/迁移等 CLI 写操作需 flock，**必须先 `launchctl unload` 停 serve** 再跑。
launchd 管理（禁 nohup），登记运维斯 SERVICES.md。

## 服务拓扑
<!-- topology:v1 -->
- 产物形态：服务
- 用户界面：飞书（复用 life-assistant 飞书号，skill_router 命中抖音链接 / `vr` 前缀命令后 HMAC 透传）
- 部署宿主：office（重内存 ASR 兜底与 agy 登录态都在生产机；研发机不部署）

| 提供 ref | 进程管理 | 健康端点 |
|---|---|---|
| office:vreader | launchd | http://127.0.0.1:8232/healthz（POST，不健康 503） |

| 提供 ref | 依赖 ref | 硬/软 | 挂了的表现 |
|---|---|---|---|
| office:vreader | remote:douyin-feed-api | 硬 | 取不到视频详情，新链接全部处理失败 |
| office:vreader | remote:feishu-open-api | 硬 | 回执/榜单发不出，outbox 积压超 1h 后 healthz 503 |
| office:vreader | remote:gladia-api | 软 | 回落本地 SenseVoice：变慢、模型名易糊（回执与榜单显式警示） |
| office:vreader | remote:singreality-proxy | 软 | agy 提取与 Gladia 代理兜底失效，提取回落 cliproxy |
| office:vreader | office:cliproxy-api | 软 | agy 正常时无感；agy 也挂则提取失败 |

## 开发与测试

`pytest -q`（335 项全绿，**只在研发机跑**；生产机不跑 pytest）。

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填凭据，chmod 600
pytest -q
python -m core.cli "<抖音分享链接>"          # 手动处理一条（建真值集）
python -m core.cli --board                  # 打印榜单（纯读，不取写锁）
python -m core.cli --render                  # 用现有 extract 重渲染 board.md（不重提取）
python -m core.cli --reprocess <aweme_id>   # 现有 transcript 重跑提取+决策（prompt/别名改动后）
python -m core.cli --retranscribe <aweme_id> # 删 transcript 强制用 Gladia 重转+提取（修兜底 ASR 数据）
python -m core.service                       # 起服务
python ops/eval_gold.py <data副本> --min-extract 96        # gold 准确率评测（只读）
python ops/repair_extract.py --data <data> --spec <spec> --expect-spec-sha256 <sha> [--apply]
                                             # 单视频外科式修复（默认 dry-run；apply 需停 serve）
python ops/replay_extract_fixture.py --fixture ... --sha256 ... --expect-file ... --expect-sha256 ...
                                             # 提取回放（不调 LLM）
python ops/eval_extract_repeat.py --data <副本> --videos gold --runs N --out <dir> [--prompt <模板>]
                                             # prompt 重复评测（agy，nonce 防缓存；研发机须带 AGY_PROXY）
```

- 真值集：`tests/gold/token_bug/gold.json`（用户人工标注 13 视频得分 + aweme_id 映射，进仓）；
  `eval_gold.py` 比较键 aweme_id×canonical×bug_id，缺格/多报/重复/冲突全计错，双口径 + 逐格 diff。
- `repair_extract.py` apply 取 flock、备份 `data/backup/repair-<id>-<ts>/`、实际决策≠预期自动恢复。
- 升级/回滚运维手册：`docs/ops-series-norm-upgrade-rollback.md`（快照清单 + 有界回滚 +
  撤销登记≠撤销批准）；上游透传补丁：`docs/skill_router-vreader-help-detail-confirm.patch`
  （验收 `ops/verify_upstream_routing.py <life-assistant-checkout>`）。

## 目录

```
core/        douyin/asr/extract/db/pipeline/service/feishu/routing/config/cli
channels/token_bug/   extract_prompt.md · models.yml · board.py
schemas/     token_bug.extract.schema.json
ops/         部署与验证脚本、launchd 模板
tests/       pytest；gold/ 真值集与合成回放 fixture 进仓，`fixtures/*.private.*`（真实转写）gitignore
```

## 迭代记录
- **2026-10-10 抖音 403 修复**：web detail API 被 Argus 全量拦截（`Uifid Not Found` → 补 uifid 后
  `Signature Not Found`），10-07~10-10 间生效；`fetch_detail` 改走移动端 feed 接口（严格 id 匹配）。
  同时修 ingest 去重：terminal_failed 任务重发即重新排队（此前任何已存在 id 都拒收，与失败通知
  「可重新发链接重试」矛盾）。生产已部署，用户重发失败视频端到端成功。
- **2026-10-18 新变体无条件接受（V-M17）**：`vr确认 … =gemini/4/Argon` 被「系列 Gemini 无变体 Argon」
  拒（以往每遇新变体都要改 yml+部署）。改为人工校正可登记新变体，存 known_versions、load_series 合并。
- **2026-10-24 版本 `.0` 孪生重名 + 证据错字漏提**：`Step 5 Preview` 与 `Step 5.0 Preview`
  （以及 `Hunyuan 4`/`4.0 Preview`）在榜单上重复——`norm_version` 不去 `.0`，批量确认又登记了孪生写法；
  另有记录因 LLM 证据抄错一个字被丢弃。规划已评审定稿（研发机本地 `docs/plans/version-dot0-evidence-fuzzy-plan-v5.md`，
  该目录 gitignore）。已实施：WP-A 孪生写法按已登记写法归一（见不变量）；WP-B 单段证据近似找回（强制人工复核；
  回放 fixture 7689 因此多找回 1 条「这这/这这这」并转 pending_conflict，私有 expect 已同步）。
  生产外科修复两个视频（7699 Step 5→5.0 两条 + 补回白银 S001；7694 混元 4→4.0 三条）并撤销
  `Step|5|Preview`、`Hunyuan|4|Preview` 两条孪生登记。
- **2026-10-27 `vr帮助` 补「系列名怎么写」**：校正命令最常见的坑是系列名照抄榜面（带版本字母/空格），
  如 `Kimi K3.1 Pro` 应写 `=Kimi/3.1/Pro`（`K` 是 models.yml 的 version_letter）；写错时消息在**路由层**
  就被判成普通聊天、静默回生活助手菜单（`_CONFIRM` 参数正则不允许空格，上下游两份），所以文案只防写错、
  不治静默失败。含空格系列（Claude Opus / Mistral Large）仍不支持校正，走手改。
