"""手动处理单条视频（建真值集、调试用）。

    python -m core.cli "<抖音分享文本或链接>"
    python -m core.cli --board             # 打印当前榜单（纯读）
    python -m core.cli --render            # 用现有 extract 重渲染 board.md（不重提取）
    python -m core.cli --reprocess <id>    # 用现有 transcript 重跑提取+决策
    python -m core.cli --retranscribe <id> # 删 transcript 强制用 Gladia 重转+提取
"""
from __future__ import annotations

import sys

from . import config, db, lock, pipeline


def _db():
    dbp = str(config.DATA_DIR / "vreader.db")
    db.init(dbp)
    return db.connect(dbp)


def _print_board() -> int:
    """--board 纯读（C6）：直接打印落盘的 board.md，不 init、不渲染、不取写锁。"""
    p = config.channel_dir(pipeline.CHANNEL) / "board.md"
    if p.exists():
        print(p.read_text(encoding="utf-8"))
    else:
        print("（暂无榜单）")
    return 0


def _list_corrections() -> int:
    """--list-corrections 纯读：列校正 op（含 needs_review），走严格只读连接、不 init、不取写锁。"""
    dbp = config.DATA_DIR / "vreader.db"
    if not dbp.exists():
        print("（暂无校正记录）")
        return 0
    conn = db.connect_ro(str(dbp))
    try:
        rows = db.list_all_corrections(conn)
    finally:
        conn.close()
    if not rows:
        print("（暂无校正记录）")
        return 0
    for r in rows:
        line = (f"[{r['op_id'][:8]}] {r['status']} · aweme={r['aweme_id']} · "
                f"{r['old_rid'][:8]}→{r['new_rid'][:8]}")
        if r["last_error"]:
            line += f" · {r['last_error']}"
        print(line)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    if argv[0] == "--board":
        return _print_board()
    if argv[0] == "--list-corrections":
        return _list_corrections()
    # 写路径（处理/重跑）取数据目录 flock，与 serve 及其他 CLI 写实例互斥
    dirlock = lock.DataDirLock(config.DATA_DIR)
    if not dirlock.acquire(blocking=False):
        print("数据目录被 serve 或另一 CLI 实例占用，拒绝写操作。", file=sys.stderr)
        return 3
    conn = _db()
    try:
        if argv[0] == "--render":
            pipeline.render_board(conn)
            print("board 已重渲染")
            return 0
        if argv[0] == "--reprocess":
            if len(argv) < 2:
                print("用法：--reprocess <aweme_id>", file=sys.stderr)
                return 1
            print(pipeline.reprocess(conn, argv[1]))
            return 0
        if argv[0] == "--resolve-correction":
            if len(argv) < 3 or argv[2] not in ("keep-file", "apply-journal"):
                print("用法：--resolve-correction <op_id> <keep-file|apply-journal>", file=sys.stderr)
                return 1
            from . import extract as ex_mod
            ok, msg = ex_mod.resolve_correction(conn, argv[1], argv[2])
            print(msg)
            return 0 if ok else 2
        if argv[0] == "--retranscribe":
            if len(argv) < 2:
                print("用法：--retranscribe <aweme_id>", file=sys.stderr)
                return 1
            status = pipeline.retranscribe(conn, argv[1])
            print(f"status={status} aweme_id={argv[1]}")
            return 0 if status == db.SUCCEEDED else 2
        from . import douyin
        text = argv[0]
        aweme_id = douyin.resolve_aweme_id(text)
        if not db.get_task(conn, aweme_id):
            detail = douyin.fetch_detail(aweme_id)
            title = douyin.meta_from_detail(detail)["title"]
            db.insert_task(conn, aweme_id=aweme_id, channel=pipeline.CHANNEL, raw_link=text,
                           chat_id="", sender_id="", title=title)
        db.set_status(conn, aweme_id, db.RECEIVED)
        task = db.get_task(conn, aweme_id)
        status = pipeline.process_task(conn, task)
        print(f"status={status} aweme_id={aweme_id}")
        return 0 if status == db.SUCCEEDED else 2
    finally:
        conn.close()
        dirlock.release()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
