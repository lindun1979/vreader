from core import routing, service


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


def test_confirm_with_second_arg():
    # 逐条冲突记录码 / rev:版本（放宽 confirm）
    assert routing.classify("vr确认 6961737553342991651 a1b2c3d4") == "confirm"
    assert routing.classify("vr确认 6961737553342991651 rev:abc123") == "confirm"


def test_detail_command():
    assert routing.classify("vr明细 6961737553342991651") == "detail"
    assert routing.classify("/vreader 明细 v123") == "detail"
    assert routing.parse_detail("vr明细 v123") == "v123"


def test_help_command():
    assert routing.classify("vr帮助") == "help"
    assert routing.classify("vr 帮助") == "help"        # 带空格
    assert routing.classify("/vreader 帮助") == "help"


def test_vr_prefix_tolerates_space():
    # vr 与命令词间可选空格：四类命令都认
    assert routing.classify("vr 榜单") == "board"
    assert routing.classify("vr 明细 7681587976547208511") == "detail"
    assert routing.classify("vr 确认 7681587976547208511") == "confirm"
    assert routing.classify("vr 确认 7681587976547208511 a1b2c3d4") == "confirm"
    assert routing.parse_confirm("vr 确认 vid rev:abc") == ("vid", "rev:abc")
    assert routing.parse_detail("vr 明细 vid9") == "vid9"


def test_help_word_in_sentence_not_matched():
    assert routing.classify("能给我点帮助吗") is None


def test_help_handler_returns_usage():
    code, reply = service.handle_help(None, {})
    assert code == 200
    for cmd in ["vr榜单", "vr明细", "vr确认", "vr帮助", "rev:"]:
        assert cmd in reply
    assert "/help" in service._ROUTES


def test_plain_chat_not_forwarded():
    assert routing.classify("今天天气不错") is None
    assert routing.classify("帮我查下明天的日程") is None
    assert routing.classify("") is None


def test_board_word_in_sentence_not_matched():
    # "榜单" 出现在闲聊里不应误触发（需精确命令词）
    assert routing.classify("这个榜单看起来不错啊") is None
