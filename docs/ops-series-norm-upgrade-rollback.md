# series-norm 升级 / 回滚 / 修复运维手册（plan M11）

> 生产解释器 `~/workspace/vreader/.venv/bin/python`。生产机 **不跑 pytest**（learnings
> tests-write-prod-state）；冒烟只跑只读 healthz + `--board` + eval 副本。
> 停机窗口目标 < 15 分钟。

## 1. 升级步骤（顺序不可乱）

1. **停服**：`launchctl unload ~/Library/LaunchAgents/ai.chivox.vreader.plist`
2. **取维护锁 + 备份快照**（持数据目录 flock 防窗口期 CLI 写入）：

   ```bash
   cd ~/workspace/vreader
   BK=~/vreader-backup/$(date +%Y%m%d-%H%M%S); mkdir -p "$BK"
   # 产物清单（db 旁文件按实际存在与否入清单）
   find data -name 'vreader.db*' -o -name extract.json -o -name board.md > "$BK/manifest-files.txt"
   # 视频目录清单（含尚无 extract.json 的中间态目录）
   ls -d data/token_bug/*/ > "$BK/manifest-dirs.txt"
   # db 文件族 + extract/board 打包
   cp data/vreader.db "$BK"/; [ -f data/vreader.db-wal ] && cp data/vreader.db-wal "$BK"/
   [ -f data/vreader.db-shm ] && cp data/vreader.db-shm "$BK"/
   tar czf "$BK/extracts.tgz" $(find data -name extract.json -o -name board.md)
   ```
   （等价逻辑见 `ops/upgrade_rollback.py:backup`；评测副本也从这一快照取。）
3. **git pull**（**必走 HTTP 代理**，直连 pack 卡死，见 CLAUDE.md 生产部署）。
4. **释放维护锁**，`launchctl load ...plist`。
5. **load 之后**跑冒烟：`curl -s 127.0.0.1:8232/healthz`（期 200）+
   `.venv/bin/python -m core.cli --board`（纯读，不取写锁）。
6. **上游 life-assistant 打 patch**：`git apply docs/skill_router-vreader-help-detail-confirm.patch`
   （先 `git apply --check`），随后 `ops/verify_upstream_routing.py <checkout>` 冒烟。
7. **真人验证**：`vr帮助` + 投一条新版本视频走
   `pending_new_version → vr确认 → 上榜 → 同版本第二条自动`。

## 2. 回滚 = 恢复到停服快照（有界丢弃，非无损）

1. `launchctl unload ...plist` + **取维护锁**。
2. 快照后新增数据先导出留档：
   ```bash
   sqlite3 ~/workspace/vreader/data/vreader.db ".dump record_decisions" > "$BK/post.decisions.sql"
   sqlite3 ~/workspace/vreader/data/vreader.db ".dump known_versions"   > "$BK/post.known.sql"
   ```
3. `git checkout <旧版>`。
4. **按备份清单恢复**（逻辑见 `ops/upgrade_rollback.py:rollback`）：先删目标处
   `vreader.db*` 全族（清残留旁文件），再恢复备份 db 文件族；解包 `extracts.tgz`
   **覆盖恢复**既有 `extract.json` 与 `board.md`。
5. 清除快照后新增内容，**目录与产物分别对照各自清单**：不在 `manifest-dirs.txt`
   的视频目录才整目录删（真正新增视频）；旧目录内新产 `extract.json` 按
   `manifest-files.txt` 缺席判定**只删该文件**，绝不删整目录或其 transcript。
6. board 用第 4 步恢复的备份件，**不在持锁状态下调 CLI `--render`**（`core/cli.py:38`
   写入口会独立申请同一 flock 失败退出；如需重渲染，待锁释放、服务未启时单独跑）。
7. 上游 patch 撤销（life-assistant 仓 `git revert` 该提交 / `git checkout` 还原）。
8. **释放维护锁**。
9. `launchctl load ...plist`（持锁 load 会与 serve 单实例检查冲突，释放必须在 load 之前）。

留档的 `post.*.sql` 由人工决定是否回放。

## 3. 撤销登记 ≠ 撤销批准（与裁决恒存不冲突）

- **只撤销「以后免版本审核」资格**（不动既有 APPROVED）：
  ```sql
  DELETE FROM known_versions WHERE series='GLM' AND version='5.4' AND variant='';
  ```
  `apply_decisions` 恒存继承先于分类（`core/extract.py`），reprocess 后同 rid 仍 APPROVED。
- **确需撤销既有批准**（显式，分开成节）：
  1. 按 extract.json 三元组字段列出受影响 rid（查询模板）：
     grep 各视频 `extract.json` 中 `model_series/version/variant` 命中该三元组的记录，
     用 `core.extract.record_id` 反算其 rid。
  2. 留档 → **显式** `UPDATE record_decisions SET decision='pending_new_version'
     WHERE record_id IN (...)` → 重新裁决 → 服务未启时单独 `--render`。

## 4. 存量 pending_unknown / 迁移清理

部署后先在**副本上演练** reprocess 效果（`ops/eval_gold.py <副本目录>`，只读、
自校验源哈希），确认后逐视频在生产跑 `--reprocess <id>`（需先 `launchctl unload` 停 serve，
CLI 写路径取 flock）。这是运维动作，非自动。

## 5. 评测（发布前门禁）

```bash
cp -r ~/workspace/vreader/data /tmp/vreader-eval-copy   # 隔离副本，不碰生产裁决库
.venv/bin/python ops/eval_gold.py /tmp/vreader-eval-copy --min-extract 96
```
逐格 diff 无上榜回归、提取准确率 ≥ 门槛方可发布。
