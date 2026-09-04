"""手动处理单条视频（建真值集、调试用）。

    python -m core.cli "<抖音分享文本或链接>"
    python -m core.cli --board            # 打印当前榜单
    python -m core.cli --reprocess <id>   # 用现有 transcript 重跑提取+决策
"""
from __future__ import annotations

import sys

from . import config, db, pipeline


def _db():
    dbp = str(config.DATA_DIR / "vreader.db")
    db.init(dbp)
    return db.connect(dbp)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    conn = _db()
    try:
        if argv[0] == "--board":
            print(pipeline.render_board(conn))
            return 0
        # 直接同步处理一条（不经队列/服务），便于建真值集
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
