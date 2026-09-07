# vreader

把「视频博主 → 结构化知识」的通用管线。首个频道 `token_bug`：处理抖音博主
「token（词源）」的模型实测视频，自动维护一张「模型 × bug 难度」榜单。

**用法（飞书）**：把抖音分享链接发给机器人 → 自动下载/转写/提取 → 处理完回执 →
- `vr帮助` 显示用法说明
- `vr榜单` 取回最新榜单
- `vr明细 <video_id>` 看某视频逐条明细（记录码 + 状态 + 完整证据；未知/新版本另显 raw 原文 + 归一三元组）
- `vr确认 <video_id>` 批量确认普通/新版本待确认记录入榜（**新版本首次确认即登记**，此后同版本自动上榜）；
  `vr确认 <video_id> <记录码>` 逐条确认矛盾记录；`vr确认 <video_id> rev:<版本>` 绑版本确认（防确认过时内容）

**模型归一（series-norm）**：`models.yml` 是**系列表**（format 身份模板 + 系列别名 + 两级昵称 + 变体），
版本号不写死——代码对转写/标题做「提及锚定」从源文本数字拼合 canonical（`GRM5.3`、`step3.7 flash`
等新版本无需改表）。未见过的版本首次出现 → `pending_new_version`，`vr确认` 一次入库为已知版本。
昵称（「一哥/火星刺客」）随期指向系列**当时最新**版本，只绑系列不绑版本号。

## 管线

```
分享链接 → 解析 aweme_id → 下载(直连 detail API) → Gladia 云转写(主)
  ｜失败回落 ffmpeg 16k wav → SenseVoice 分块转写
  → LLM 提取(schema+证据校验) → 决策(auto_ok/pending)
  → 渲染 board.md → 飞书回执
```

- **下载**：不用 yt-dlp（其 Douyin extractor 漏传参数已失效），直接调 web detail
  API（匿名 ttwid cookie，无需登录）。见 `core/douyin.py`。
- **ASR**：Gladia 云转写为主（gold 真值集 97 格 100% vs SenseVoice 89.7%，带
  models.yml 热词，`GLADIA_API_KEY` 配 .env），失败自动回落本地 SenseVoiceSmall
  （funasr, CPU，长视频分块）。`extract.json` 的 `asr_model` 记录实际所用引擎；
  单任务回执 + 榜单末尾均会**显式警示**落到兜底 ASR 的视频（名字易糊、结果存疑）。
- **提取**：`LLM_BACKEND` 可选 `agy`（Antigravity CLI，生产主通道，`LLM_MODEL` 走 agy，
  兜底链走 :8317；生产直连不通须配 `AGY_PROXY` 代理）/ `openai`（:8317 cliproxy）/ `claude`
  （本地 CLI）+ 频道 prompt + 模型系列表（LLM 出 series/version/variant，代码提及锚定拼 canonical）。
  **轮次驱动打分**：LLM 出「第几轮做对」(`solved_round`)，代码反推得分；钻石/王者第4轮做对
  = 白做（0分但已解，榜单显示第[4]次），区别于「没做对」。
- **状态**：SQLite（WAL、每线程独立连接、原子领取）；崩溃恢复按落盘产物前推跳过
  阶段（产物齐全时不依赖上游网络）；recover 递增 retry_count 防毒丸崩溃循环。
- **通知**：outbox 表，与任务终态同事务写入（`db.finalize_task`，无静默失败），
  独立线程退避重试，重启恢复投递。
- **审批**：`record_decisions` 用内容指纹作身份（`model_key`＋`bug_slot`＋score，未知
  模型/空 bug_id 不误合并）；四态待确认 `pending`/`pending_unknown`/`pending_conflict`/`pending_new_version`；
  `auto_ok` 每次按 confidence 重判，`approved`/`rejected_conflict` 凭指纹继承恒存
  （重跑不错位、降置信度自动退出主榜、矛盾组唯一结论）。
- **可靠性加固**：flock 单实例执行权；健康三态 healthz（在执行/空闲有活/退避）+ 线程
  守护三分支，不健康返 503（线程死/outbox 积压/db 连错/低磁盘/ASR 卡死）；磁盘门禁
  （拒收+暂停+限频告警）；下载字节/时限硬限、subprocess timeout + killpg 后代、任务
  总预算；产物原子写 + 发布锁。

## 目录

```
core/        douyin/asr/extract/db/pipeline/service/feishu/routing/config/cli
channels/token_bug/   extract_prompt.md · models.yml · board.py
schemas/     token_bug.extract.schema.json
ops/         部署与验证脚本、launchd 模板
tests/       pytest（M2 门禁）；fixtures/gold 为 gitignore 的真实数据
```

## 开发

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 填凭据，chmod 600
pytest -q                                   # 全部单测
python -m core.cli "<抖音分享链接>"          # 手动处理一条（建真值集）
python -m core.cli --board                  # 打印榜单（纯读，不取写锁）
python -m core.cli --render                  # 用现有 extract 重渲染 board.md（不重提取）
python -m core.cli --reprocess <aweme_id>   # 现有 transcript 重跑提取+决策（prompt/别名改动后）
python -m core.cli --retranscribe <aweme_id> # 删 transcript 强制用 Gladia 重转+提取（修兜底 ASR 数据）
python -m core.service                       # 起服务
```

## 纪律（public 仓）

凭据只在 `.env`（gitignore）；内网/部署细节在 `DEPLOY.local.md`（gitignore）；
视频/音频/转写/提取/榜单等他人版权衍生数据全在 `data/`（gitignore），不进仓。
