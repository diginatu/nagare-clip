"""publish/pairing: a headline and the photograph it sits on, decided together.

A second, text-only call. It sees the copy sets and the frame *descriptions* --
never an image, and never a path -- and answers with a frame INDEX per set plus
where and in what colour the text goes.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.publish.describe_frames import FrameDescription
from nagare_clip.publish.pairing import (
    SetPairing,
    apply_pairing,
    font_slot_note,
    format_frame_block,
    format_sets_block,
    generate_pairing,
    try_parse_pairing_response,
)
from nagare_clip.publish.publish_llm import PublishCopy
from nagare_clip.render.thumbnail import ThumbLine, ThumbSet

CFG = {"prompt": "pair them", "max_retries": 2}


def _frame(index=1, label="まさかの水漏れ発覚", description="a puddle, lower right", **kw):
    base = {
        "stem": "a",
        "source_time": 55.66 * index,
        "kind": "overlay",
        "label": label,
        "path": f"frames/a/{55.66 * index:.3f}.jpg",
        "hash": f"h{index}",
        "description": description,
    }
    base.update(kw)
    return FrameDescription(**base)


def _sets(n=2):
    return [ThumbSet(lines=[ThumbLine("hook", f"hook{i + 1}")]) for i in range(n)]


def _response(**overrides):
    data = {
        "sets": [
            {
                "set": 1,
                "frame": 2,
                "gravity": "southwest",
                "offset": "+56+62",
                "lines": [{"line": 1, "fill": "#FFD54F", "pointsize": 140}],
            }
        ]
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


# --- what the call is shown --------------------------------------------------


def test_the_frames_are_shown_numbered_with_kind_time_label_and_description():
    block = format_frame_block([_frame(1), _frame(2, label="ポンプ", description="a pump running")])
    assert block.splitlines()[0].startswith("1.")
    assert "overlay" in block and "55.7s" in block
    assert "まさかの水漏れ発覚" in block
    assert "a puddle, lower right" in block
    assert block.splitlines()[1].startswith("2.")


def test_a_frame_with_no_description_still_gets_an_index():
    """An index the model can name beats a gap in the numbering."""
    block = format_frame_block([_frame(1, description=""), _frame(2)])
    assert block.splitlines()[0].startswith("1.")
    assert block.splitlines()[1].startswith("2.")


def test_the_frame_block_never_shows_a_path():
    """A path is a string a model can invent; an index is bounded and checkable."""
    block = format_frame_block([_frame(1)])
    assert ".jpg" not in block
    assert "frames/" not in block


def test_the_sets_are_shown_with_numbered_lines_and_their_roles():
    block = format_sets_block(
        [ThumbSet(lines=[ThumbLine("tag", "水槽DIY"), ThumbLine("hook", "水浸し！")])]
    )
    assert "Set 1" in block
    assert "1" in block and "tag" in block and "水槽DIY" in block
    assert "hook" in block and "水浸し！" in block


# --- parsing -----------------------------------------------------------------


def test_a_pairing_carries_the_frame_index_and_the_styles():
    got = try_parse_pairing_response(_response(), num_sets=2, num_frames=3)
    assert got == {
        1: SetPairing(
            frame=2,
            style={"gravity": "southwest", "offset": "+56+62"},
            lines={1: {"fill": "#FFD54F", "pointsize": 140}},
        )
    }


@pytest.mark.parametrize("bad", ["not json", "[]", '{"nope": 1}'])
def test_a_response_with_no_usable_sets_is_a_hard_failure(bad):
    assert try_parse_pairing_response(bad, num_sets=2, num_frames=3) is None


def test_a_fenced_response_is_unwrapped():
    got = try_parse_pairing_response(f"```json\n{_response()}\n```", num_sets=2, num_frames=3)
    assert got is not None and 1 in got


def test_a_set_index_out_of_range_is_dropped(caplog):
    got = try_parse_pairing_response(
        json.dumps({"sets": [{"set": 9, "frame": 1}, {"set": 1, "frame": 1}]}),
        num_sets=2,
        num_frames=3,
    )
    assert list(got) == [1]
    assert "9" in caplog.text


@pytest.mark.parametrize("frame", [0, 4, -1, "seven", None, True])
def test_a_frame_index_out_of_range_falls_back_rather_than_failing(frame, caplog):
    """An index is bounded and checkable, so a bad one costs the background
    and nothing else -- the set still renders, on the fallback still."""
    got = try_parse_pairing_response(
        json.dumps({"sets": [{"set": 1, "frame": frame, "gravity": "north"}]}),
        num_sets=1,
        num_frames=3,
    )
    assert got[1].frame is None
    assert got[1].style == {"gravity": "north"}  # the rest of the decision survives


def test_only_known_style_keys_survive():
    """The renderer validates values; this stops unknown KEYS entering the
    artifact at the parse boundary, as the copy call always did."""
    got = try_parse_pairing_response(
        json.dumps(
            {
                "sets": [
                    {
                        "set": 1,
                        "frame": 1,
                        "gravity": "north",
                        "blend": "overlay",
                        "lines": [{"line": 1, "fill": "white", "kerning": 4}],
                    }
                ]
            }
        ),
        num_sets=1,
        num_frames=1,
    )
    assert got[1].style == {"gravity": "north"}
    assert got[1].lines == {1: {"fill": "white"}}


def test_a_line_index_out_of_range_is_dropped(caplog):
    got = try_parse_pairing_response(
        json.dumps({"sets": [{"set": 1, "frame": 1, "lines": [{"line": 7, "fill": "white"}]}]}),
        num_sets=1,
        num_frames=1,
    )
    assert got[1].lines == {}


# --- applying ----------------------------------------------------------------


def test_the_index_is_resolved_to_a_path_before_it_reaches_publish_json():
    """The human editing that file wants a filename, not a number."""
    frames = [_frame(1), _frame(2)]
    out = apply_pairing(_sets(1), {1: SetPairing(frame=2, style={}, lines={})}, frames)
    assert out[0].background == frames[1].path


def test_the_style_lands_on_the_set_and_its_lines():
    pairing = {
        1: SetPairing(frame=1, style={"gravity": "southwest"}, lines={1: {"fill": "#FFD54F"}})
    }
    out = apply_pairing(_sets(1), pairing, [_frame(1)])
    assert out[0].style == {"gravity": "southwest"}
    assert out[0].lines[0].style == {"fill": "#FFD54F"}


def test_the_copy_is_never_altered_by_pairing():
    sets = [ThumbSet(lines=[ThumbLine("hook", "水浸し！")])]
    out = apply_pairing(sets, {1: SetPairing(frame=1, style={}, lines={})}, [_frame(1)])
    assert [(line.role, line.text) for line in out[0].lines] == [("hook", "水浸し！")]


def test_a_set_the_pairing_never_mentioned_falls_back_to_the_presets():
    """No background and no look is exactly what a project renders today."""
    out = apply_pairing(_sets(2), {1: SetPairing(frame=1, style={}, lines={})}, [_frame(1)])
    assert out[1].background == ""
    assert out[1].style == {} and out[1].lines[0].style == {}


def test_an_unresolvable_frame_leaves_the_background_empty():
    out = apply_pairing(_sets(1), {1: SetPairing(frame=None, style={}, lines={})}, [_frame(1)])
    assert out[0].background == ""


def test_no_pairing_at_all_returns_the_sets_unchanged():
    sets = _sets(2)
    assert apply_pairing(sets, {}, [_frame(1)]) == sets


# --- the call ----------------------------------------------------------------


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses) or [_response()]
        self.calls: list = []

    def __call__(self, messages, cfg):
        self.calls.append(messages)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]


def _copy(n=2):
    return PublishCopy(titles=["t"], thumbnail_copy=_sets(n))


def test_the_call_sends_the_sets_and_the_frames_and_no_images():
    llm = FakeLLM()
    generate_pairing(
        _copy(),
        [_frame(1), _frame(2, label="ポンプ", description="a pump running, right of centre")],
        CFG,
        call_llm=llm,
    )
    user = llm.calls[0][1]["content"]
    assert isinstance(user, str)  # text-only: no content-part list, no base64
    assert "Set 1" in user and "hook1" in user
    # ... and the frames, which are the whole reason this call is separate
    assert "まさかの水漏れ発覚" in user
    assert "a pump running, right of centre" in user
    assert ".jpg" not in user


def test_the_font_slots_are_named_to_this_call_now():
    llm = FakeLLM()
    generate_pairing(_copy(), [_frame(1)], {**CFG, "fonts": {"sans-bold": "Noto"}}, call_llm=llm)
    assert "sans-bold" in llm.calls[0][0]["content"]


def test_a_hard_failure_is_retried_then_degrades_to_no_pairing():
    llm = FakeLLM("garbage", "still garbage", "nope")
    assert generate_pairing(_copy(), [_frame(1)], CFG, call_llm=llm) == {}
    assert len(llm.calls) == 3


def test_a_failing_call_degrades_to_no_pairing():
    def boom(messages, cfg):
        raise RuntimeError("no model")

    assert generate_pairing(_copy(), [_frame(1)], CFG, call_llm=boom) == {}


def test_no_copy_sets_means_no_call():
    llm = FakeLLM()
    assert generate_pairing(PublishCopy(titles=["t"]), [_frame(1)], CFG, call_llm=llm) == {}
    assert llm.calls == []


def test_no_frames_means_no_call():
    """Nothing to choose between; the sets fall back to the presets."""
    llm = FakeLLM()
    assert generate_pairing(_copy(), [], CFG, call_llm=llm) == {}
    assert llm.calls == []


def test_font_slot_note_lists_the_configured_slots():
    note = font_slot_note({"serif-black": "X", "sans-bold": "Y"})
    assert "sans-bold" in note and "serif-black" in note
