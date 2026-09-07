"""轮次驱动打分（用户 2026-09 确认）：LLM 出 solved_round，代码反推 score/solved/rounds。
第4轮做对（0分但已解）≠ 没做对（0分），且在榜单计为已解、显示第[4]次。"""
import json

from channels.token_bug import board
from core import extract as ex


def test_derive_diamond_king_rounds():
    # 钻石/王者：1轮3 2轮2 3轮1 4轮0(已解) 没做对0
    assert ex.derive_from_round("钻石", 1) == (True, 3, 1)
    assert ex.derive_from_round("钻石", 2) == (True, 2, 2)
    assert ex.derive_from_round("王者", 3) == (True, 1, 3)
    assert ex.derive_from_round("王者", 4) == (True, 0, 4)   # 白做：已解但 0 分
    assert ex.derive_from_round("钻石", 0) == (False, 0, None)  # 没做对
    assert ex.derive_from_round("王者", 5) == (None, None, None)  # 越界


def test_derive_gold_and_bronze():
    assert ex.derive_from_round("黄金", 1) == (True, 2, 1)
    assert ex.derive_from_round("黄金", 2) == (True, 1, 2)
    assert ex.derive_from_round("黄金", 3) == (True, 0, 3)   # 黄金第3轮=白做
    assert ex.derive_from_round("青铜", 1) == (True, 1, 1)
    assert ex.derive_from_round("青铜", 2) == (True, 0, 2)   # 青铜第2轮=白做
    assert ex.derive_from_round("白银", 0) == (False, 0, None)


def _claude(solved_round, level="王者", bug="K005"):
    return json.dumps({"records": [{
        "model_raw": "格洛克4.5", "model_series": "Grok", "model_version": "4.5", "model_variant": "",
        "bug_level": level, "bug_id": bug, "solved_round": solved_round,
        "evidence_quote": "格洛克4.5 第四轮才做对这个王者bug",
        "confidence": 0.9}]}, ensure_ascii=False)


TX = "王者bugK005 格洛克4.5 第四轮才做对这个王者bug，白做了不得分。"


def test_round4_solve_vs_never_distinct_in_build():
    # 第4轮做对：solved True, score 0, rounds 4
    ex4 = ex.build_extract(aweme_id="v", title="Grok4.5", transcript=TX, claude_text=_claude(4))
    r4 = ex4["records"][0]
    assert (r4["solved"], r4["score"], r4["rounds"]) == (True, 0, 4)
    # 没做对：solved False, score 0, rounds None
    exN = ex.build_extract(aweme_id="v", title="Grok4.5", transcript=TX, claude_text=_claude(0))
    rN = exN["records"][0]
    assert (rN["solved"], rN["score"], rN["rounds"]) == (False, 0, None)
    # 两者 rid 相同（score 都是 0）——身份稳定，靠 solved/rounds 区分展示
    assert ex.record_id("v", r4) == ex.record_id("v", rN)


def test_board_shows_round4_as_solved():
    ex4 = ex.build_extract(aweme_id="v", title="Grok4.5", transcript=TX, claude_text=_claude(4))
    r4 = ex4["records"][0]
    visible = {ex.record_id("v", r4)}
    md = board.render([ex4], visible, ex.record_id)
    # 王者列：第4轮做对 → 计为解出 1/1、标注第[4]次
    assert "1/1·第[4]次" in md


def test_board_never_solved_not_counted():
    exN = ex.build_extract(aweme_id="v", title="Grok4.5", transcript=TX, claude_text=_claude(0))
    rN = exN["records"][0]
    visible = {ex.record_id("v", rN)}
    md = board.render([exN], visible, ex.record_id)
    assert "0/1" in md  # 没做对 → 0/1
