from core import routing


def test_douyin_short_link_routes_to_ingest():
    assert routing.classify("看看 https://v.douyin.com/abc123/ 复制打开抖音") == "ingest"


def test_douyin_full_link_routes_to_ingest():
    assert routing.classify("https://www.douyin.com/video/6961737553342991651") == "ingest"


def test_board_command():
    assert routing.classify("vr榜单") == "board"
    assert routing.classify("/vreader 榜单") == "board"


def test_confirm_command():
    assert routing.classify("vr确认 6961737553342991651") == "confirm"
    assert routing.classify("/vreader 确认 v123") == "confirm"


def test_plain_chat_not_forwarded():
    assert routing.classify("今天天气不错") is None
    assert routing.classify("帮我查下明天的日程") is None
    assert routing.classify("") is None


def test_board_word_in_sentence_not_matched():
    # "榜单" 出现在闲聊里不应误触发（需精确命令词）
    assert routing.classify("这个榜单看起来不错啊") is None
