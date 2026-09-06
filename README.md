# vreader

把「视频博主 → 结构化知识」的通用管线。首个频道 `token_bug`：处理抖音博主
「token（词源）」的模型实测视频，自动维护一张「模型 × bug 难度」榜单。

**用法（飞书）**：把抖音分享链接发给机器人 → 自动下载/转写/提取 → 处理完回执 →
发「vr榜单」取回最新榜单；低置信度记录进待确认区，管理员发「vr确认 <video_id>」入榜。

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
  （funasr, CPU，长视频分块）。`extract.json` 的 `asr_model` 记录实际所用引擎。
- **提取**：OpenAI 兼容端点（默认 :8317 cliproxy，`LLM_MODEL`+兜底链；`LLM_BACKEND=claude`
  可切 claude CLI）+ 频道 prompt + 模型别名表（纠 ASR 错写）。
- **状态**：SQLite（WAL、每线程独立连接、原子领取）；崩溃恢复按落盘产物跳过阶段。
- **通知**：outbox 表，与任务终态同事务写入，独立线程退避重试，重启恢复投递。
- **审批**：`record_decisions` 用内容指纹作身份；`auto_ok` 每次按 confidence 重判，
  仅 `approved` 凭指纹继承（重跑不错位、降置信度自动退出主榜）。

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
python -m core.cli --board                  # 打印榜单
python -m core.service                       # 起服务
```

## 纪律（public 仓）

凭据只在 `.env`（gitignore）；内网/部署细节在 `DEPLOY.local.md`（gitignore）；
视频/音频/转写/提取/榜单等他人版权衍生数据全在 `data/`（gitignore），不进仓。
