from nagare_clip.sentence_split.llm import (
    build_messages,
    parse_ranges,
    split_window,
)

BUNSETSU = [(0, 2, "あい"), (2, 4, "うえ"), (4, 6, "おか")]


def test_build_messages_numbers_bunsetsu():
    msgs = build_messages(BUNSETSU, "SYS")
    assert msgs[0] == {"role": "system", "content": "SYS"}
    assert msgs[1]["content"] == "0:あい 1:うえ 2:おか"


def test_parse_ranges_valid_contiguous_full_coverage():
    assert parse_ranges('{"sentences":[[0,1],[2,2]]}', 3) == [(0, 1), (2, 2)]


def test_parse_ranges_rejects_gaps_and_bad_coverage():
    assert parse_ranges('{"sentences":[[0,0],[2,2]]}', 3) is None      # gap (skips 1)
    assert parse_ranges('{"sentences":[[1,2]]}', 3) is None            # doesn't start at 0
    assert parse_ranges('{"sentences":[[0,1]]}', 3) is None            # doesn't reach 2
    assert parse_ranges('{"sentences":[[0,2],[1,2]]}', 3) is None      # overlap/not contiguous
    assert parse_ranges('not json', 3) is None
    assert parse_ranges('{"nope":[]}', 3) is None


def test_split_window_returns_ranges_on_valid_response():
    def fake(messages, cfg):
        return '{"sentences":[[0,1],[2,2]]}'
    assert split_window(BUNSETSU, {"max_retries": 0}, call_llm=fake) == [(0, 1), (2, 2)]


def test_split_window_degrades_to_none_after_failures():
    calls = []

    def boom(messages, cfg):
        calls.append(1)
        raise RuntimeError("no server")

    assert split_window(BUNSETSU, {"max_retries": 1}, call_llm=boom) is None
    assert len(calls) == 2  # first try + one retry


def test_split_window_single_bunsetsu_no_call():
    def fail(messages, cfg):
        raise AssertionError("should not be called")
    assert split_window([(0, 2, "あい")], {}, call_llm=fail) == [(0, 0)]
