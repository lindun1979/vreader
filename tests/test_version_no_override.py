"""D-D：已口播版本不得被已知版本集或 LLM 版本覆盖。"""
from core import models


def _resolve(raw, s, v, var, tx, known=None):
    a = models.build_anchors(tx, "")
    return models.resolve_record(raw, s, v, var, a, known=known)[0]


def test_raw_version_beats_llm_version_when_both_anchored():
    tx = "GLM5.2 先上 GLM5.3 第一轮做对了"
    assert _resolve("GLM5.3", "GLM", "5.2", "", tx) == "GLM-5.3"


def test_known_tiebreak_cannot_replace_spoken_version():
    # 本视频锚点 {3,4} 歧义，known 只含 3.0：raw 的 4.0 不得被破歧成 3.0
    known = {("Hunyuan", "3.0", "")}
    tx = "混元3 当年6分 混元4 这次 混源4.0正式版"
    got = _resolve("混元4.0正式版", "Hunyuan", "4.0", "", tx, known)
    assert got != "Hunyuan 3.0" and got in {"Hunyuan 4", "Hunyuan 4.0"}


def test_unanchored_raw_number_not_enforced():
    # raw 里的数字不是本系列锚定版本（如「Flash1017」版本后缀）→ 不触发版本闸
    known = {("DeepSeek", "4.1", "Flash")}
    tx = "DeepSick4.1Flash1017参加史山考核 DeepSick Flash1017 正在验证中"
    assert _resolve("DeepSick Flash1017", "DeepSeek", "4.1", "Flash", tx, known) == "DeepSeek V4.1 Flash"


def test_muse_spark_guard_still_holds():
    known = {("Muse Spark", "1.2", "")}
    tx = "muse bug1.3 第一轮做对了"
    assert _resolve("muse bug1.3", "Muse Spark", "1.3", "", tx, known) != "Muse Spark 1.2"


def test_bare_doubao_still_falls_back_to_known():
    known = {("Doubao Seed", "2.1", "")}
    assert _resolve("豆包", "Doubao Seed", "", "", "豆包第一轮做对了", known) == "Doubao Seed 2.1"


def test_suffix_number_in_raw_is_not_spoken_version():
    # 真实 7698：转写里 1017 也被锚成「版本」，但 raw 中 1017 不紧跟系列名 → 用 LLM 的 4.1
    known = {("DeepSeek", "4.1", "Flash")}
    tx = "DeepSick4.1Flash 参加史山考核 deepsick1017 今天 DeepSick Flash1017 正在验证中"
    assert models.build_anchors(tx)["versions"]["DeepSeek"] >= {"4.1", "1017"}
    assert _resolve("DeepSick Flash1017", "DeepSeek", "4.1", "Flash", tx, known) == "DeepSeek V4.1 Flash"
