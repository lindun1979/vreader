"""部署前只读冒烟（LLM-free）：在**数据副本**上验证新代码能读入全部既有 extract.json
（双轨校验）、db.init 幂等 seed known_versions、render_board 正常出榜。不改生产库。

用法：python ops/prod_smoke.py <data_dir_copy>
退出码 0 = 全部通过；非零 = 有 extract 读不回或渲染失败（勿 reload）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    data_dir = Path(argv[0])
    import os
    os.environ["VREADER_DATA_DIR"] = str(data_dir)
    # 让 config 重新按 env 取 DATA_DIR
    from core import config
    config.DATA_DIR = data_dir
    from core import db, extract as ex_mod, pipeline
    from channels.token_bug import board

    dbp = str(data_dir / "vreader.db")
    db.init(dbp)  # 幂等：建 known_versions + seed（不动既有表）
    conn = db.connect(dbp)
    kv = db.list_known_versions(conn)
    print(f"known_versions seeded: {len(kv)}")

    ch_dir = config.channel_dir(pipeline.CHANNEL)
    total = ok = bad = 0
    for ex in board.load_extracts(ch_dir):
        total += 1
        try:
            ex_mod.validate_extract(ex)
            ok += 1
        except ex_mod.ExtractError as e:
            bad += 1
            print(f"  ❌ 读不回 {ex.get('video_id')}: {e}")
    print(f"extract.json: {ok}/{total} 通过双轨校验（{bad} 失败）")

    md = pipeline.render_board(conn)
    rows = md.count("\n| ") - 2  # 表头两行外的数据行（粗估）
    print(f"render_board OK, board.md 约 {max(rows, 0)} 行数据")
    conn.close()

    if bad:
        print("❌ 有 extract 读不回，勿 reload（需排查双轨/冻结名单）")
        return 2
    print("✅ 冒烟通过：既有产物全部可读、榜单正常渲染")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
