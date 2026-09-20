"""The director's playback preview: facts about what its ops will play.

Fixtures are real line timings copied from the water_pump_4 project, so the
numbers asserted here are the ones the model was misjudging.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace

import pytest

from nagare_clip.director.director_llm import (
    DirectorOp,
    line_seconds,
    render_transcript,
    speech_seconds,
    try_parse_director_response,
)
from nagare_clip.director.display import build_display_view
from nagare_clip.director.preview import (
    numbering_for,
    preview_segment,
    preview_turn,
    resolve_placements,
)
from nagare_clip.director.run import SegmentTranscript
from nagare_clip.director.silence_lines import SilenceLine
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.guided_edit.timelapse import caption_duration, expand_timelapse_ops
from nagare_clip.intervals.sync_json import OVERLAY_TAG_RE
from nagare_clip.order import Segment

# PXL_20260426_090431216 lines 45-55: (start, end, silence inside, clean text).
WATER = [
    (399.915, 400.215, 0.0, "OK"),
    (400.215, 409.1, 4.521000000000129, "これでこの状態でここに水を注ぐと溜められるんですよね"),
    (409.1, 412.695, 2.34699999999998, "ちょっとパイプを一旦下ろして"),
    (434.647, 453.932, 13.874999999999886, "ここに水を注ぎます"),
    (453.932, 456.415, 0.76400000000001, "水面ここまで来てるんで"),
    (
        456.415,
        470.379,
        3.4110000000000014,
        "ここなら指で入れて呼び水を保ったままパイプを持ってくれるかなと思ってやってみたら意外とよくできたんで",
    ),
    (
        470.379,
        496.291,
        15.689000000000078,
        "ちょっとこれをカメラ固定してさすがに作業こんな感じでカメラ固定しました",
    ),
    (496.291, 500.953, 2.024000000000001, "今パイプ止めてあって水面ここまで来てます"),
    (500.953, 501.897, 0.22800000000000864, "その状態で"),
    (
        531.779,
        561.567,
        13.402000000000044,
        "この状態で今予備水持ってきたんでこれをちょっと難しいけどちょっとだけまだ難しいんだけどここを"
        "水面に入れてちょっと漏れちゃった漏れないように左手でね少しずつ排水しながら入れていって水面まで持っていくと",
    ),
    (564.787, 566.43, 1.1179999999999382, "で、あとは下ろすだけ。"),
]
WATER_FIRST = 45
PUMP_DESC = "The aquarium setup remains still, then a hand enters from the right holding a clear tube over the tank."
# Anchors are slice-relative, exactly as load_segment_transcript hands them over.
WATER_GAPS = [
    (
        3,
        Gap(
            411.972, 420.333, description="The camera shifts to a closer view inside the aquarium."
        ),
    ),
    (3, Gap(428.423, 434.707, description="The camera view shifts to the pipe opening.")),
    (9, Gap(518.734, 531.862, description=PUMP_DESC)),
]

# PXL_20260429_135641071 lines 5-16.
FISH = [
    (51.81, 57.038, 1.223000000000006, "そんな時にバラタナゴ買っちゃったんですが"),
    (86.667, 87.208, 0.0, "どうしようかな"),
    (88.11, 102.702, 12.150999999999996, "網ですくってむずそう"),
    (123.455, 134.612, 6.69899999999997, "あんまり大きい網がないんでむずいなこれ一旦"),
    (
        172.46,
        190.322,
        4.129000000000019,
        "ちなみに日本バラタナゴ、私基本的に在来種、日本にいる種が結構好きで、なので日本の種を選んでみましたか。",
    ),
    (190.322, 193.185, 1.25, "あのー、暴れないで。"),
    (
        234.931,
        264.87,
        22.664000000000016,
        "これに入れてきたのでこんな感じでちょうど電気が消えちゃった電気自動化してるんすよ",
    ),
    (269.272, 279.949, 7.651999999999987, "動画にほぼ撮れてないな映ってるかどうかわかんないけど"),
    (279.949, 285.177, 2.6129999999999995, "時間的にね遅くなっちゃったのがよくなかったね"),
    (285.177, 292.809, 5.088000000000022, "微妙に水合わせに時間かけちゃって"),
    (292.809, 295.633, 0.12599999999997635, "そんなに水合わせって時間かけなくていいと思うんだけど"),
    (300.946, 327.561, 21.867999999999995, "入った大きめこんな感じで入りました"),
]
FISH_FIRST = 5


def _op(t, a, b, **kw):
    return DirectorOp(type=t, lines=(a, b), **kw)


def _preview(rows, first, ops, gaps=None, **kw):
    return preview_segment(
        [r[3] for r in rows],
        ops,
        seg_times=[(r[0], r[1]) for r in rows],
        silences=[r[2] for r in rows],
        anchored_gaps=gaps or [],
        first_line=first,
        **kw,
    )


def _block(text: str, header: str) -> str:
    """The lines of one op's section, from its header up to the next header."""
    lines = text.split("\n")
    start = lines.index(header)
    out = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("  "):
            break
        out.append(line)
    return "\n".join(out)


class TestBoundaryGaps:
    def test_the_gap_after_a_one_line_timelapse_is_dropped(self):
        op = _op("timelapse", 53, 53, factor=5.0, text="予備水を用意している")
        text = _preview(WATER, WATER_FIRST, [op], WATER_GAPS).text
        block = _block(text, "timelapse [53,53] x5.0 「予備水を用意している」")
        # Named as the transcript names it, so the director can tie the two
        # together — and address it as "53~".
        assert "  the silence after line 53 is outside this op — dropped" in block
        # The vision description of that gap goes with it.
        assert f"    [silent 29.9s after line 53: {PUMP_DESC}]" in block
        # 52 -> 53 is contiguous: its bracket shows no gap, so neither does this.
        assert "after line 52" not in block

    def test_what_the_one_line_timelapse_plays(self):
        op = _op("timelapse", 53, 53, factor=5.0, text="予備水を用意している")
        block = _block(
            _preview(WATER, WATER_FIRST, [op], WATER_GAPS).text,
            "timelapse [53,53] x5.0 「予備水を用意している」",
        )
        assert (
            "  plays 0.9 s of footage in 0.2 s (default for line 53: 0.9 s); "
            "caption on screen 0.2 s" in block
        )

    def test_the_gap_after_line_47(self):
        op = _op("timelapse", 47, 47, factor=4.0, text="パイプを外して呼び水の準備中")
        block = _block(
            _preview(WATER, WATER_FIRST, [op], WATER_GAPS).text,
            "timelapse [47,47] x4.0 「パイプを外して呼び水の準備中」",
        )
        assert "  the silence after line 47 is outside this op — dropped" in block
        # Two descriptions fall in that one wait; the transcript joins them
        # with " / " and so does this.
        assert (
            "    [silent 22.0s after line 47: The camera shifts to a closer view inside the "
            "aquarium. / The camera view shifts to the pipe opening.]" in block
        )
        assert "after line 46" not in block
        assert "  plays 3.6 s of footage in 0.9 s (default for line 47: 1.2 s)" in block

    def test_a_range_ending_one_line_later_plays_that_gap(self):
        op = _op("timelapse", 53, 54, factor=5.0, text="予備水を用意している")
        block = _block(
            _preview(WATER, WATER_FIRST, [op], WATER_GAPS).text,
            "timelapse [53,54] x5.0 「予備水を用意している」",
        )
        assert "after line 53" not in block
        assert "29.9 s gaps between lines" in block
        # ... and pays for it with line 54's 16.4 s of speech.
        assert "    54 16.4 s 「この状態で今予備水持ってきたんでこれをちょっと難…」" in block
        assert "  the silence after line 54 is outside this op — dropped" in block
        assert "  plays 60.6 s of footage in 12.1 s" in block

    def test_a_description_of_silence_inside_the_last_line_is_not_the_gap(self):
        # Anchored at line 54 but its midpoint is inside line 54's own span:
        # that silence is inside the op, not the gap after it.
        gaps = WATER_GAPS + [(10, Gap(540.0, 545.0, description="inside line 54"))]
        op = _op("timelapse", 53, 54, factor=5.0, text="T")
        block = _block(
            _preview(WATER, WATER_FIRST, [op], gaps).text, "timelapse [53,54] x5.0 「T」"
        )
        assert "the silence after line 54 is outside this op" in block
        assert "    [silent 3.2s after line 54]" in block
        assert "inside line 54" not in block

    def test_the_gap_before_a_range(self):
        op = _op("timelapse", 6, 15, factor=12.0, text="暗闇の中、網で悪戦苦闘")
        block = _block(
            _preview(FISH, FISH_FIRST, [op]).text,
            "timelapse [6,15] x12.0 「暗闇の中、網で悪戦苦闘」",
        )
        assert "  the silence after line 5 is outside this op — dropped" in block
        assert "  the silence after line 15 is outside this op — dropped" in block

    def test_a_keep_reports_its_boundary_gaps_too(self):
        block = _block(
            _preview(WATER, WATER_FIRST, [_op("keep", 52, 53)], WATER_GAPS).text, "keep [52,53]"
        )
        assert "the silence after line 53 is outside this op — dropped" in block
        assert "  plays 5.6 s of footage in 5.6 s (default for lines 52-53: 3.6 s)" in block


class TestUnintelligible:
    def test_a_long_timelapse_lists_every_line_it_swallows(self):
        op = _op("timelapse", 6, 15, factor=12.0, text="暗闇の中、網で悪戦苦闘")
        block = _block(
            _preview(FISH, FISH_FIRST, [op]).text,
            "timelapse [6,15] x12.0 「暗闇の中、網で悪戦苦闘」",
        )
        assert "  speech at x12.0 — unintelligible (total 41.1 s over 10 lines):" in block
        assert "    9 13.7 s 「ちなみに日本バラタナゴ、私基本的に在来種、日本に…」" in block
        assert "    6 0.5 s 「どうしようかな」" in block
        assert all(f"    {n} " in block for n in range(6, 16))
        assert "    5 " not in block and "    16 " not in block
        # What it buys, beside what it costs.
        assert (
            "  plays 209.0 s of footage in 17.4 s (default for lines 6-15: 41.1 s); "
            "caption on screen 17.4 s" in block
        )
        assert (
            "  footage inside: 41.1 s speech, 105.7 s gaps between lines, "
            "62.2 s silence within lines" in block
        )

    @pytest.mark.parametrize(("factor", "listed"), [(2.0, True), (1.9, False)])
    def test_a_hand_speed_at_2x_is_unintelligible(self, factor, listed):
        text = _preview(FISH, FISH_FIRST, [_op("speed", 9, 9, factor=factor)]).text
        assert ("unintelligible" in text) is listed

    def test_a_hand_speed_plays_speech_over_its_factor(self):
        text = _preview(FISH, FISH_FIRST, [_op("speed", 9, 9, factor=2.0)]).text
        assert "  plays 13.7 s of footage in 6.9 s (default for line 9: 13.7 s)" in text


class TestOneFigurePerLine:
    def test_line_seconds_is_the_bracket_figure(self):
        rows = WATER + FISH
        times = [(r[0], r[1]) for r in rows]
        sil = [r[2] for r in rows]
        rendered = render_transcript([r[3] for r in rows], times, sil).split("\n")
        figures = line_seconds(times, sil)
        for line, fig in zip(rendered, figures):
            assert re.search(rf"\[{fig:.1f}s[,\] ]", line), (line, fig)

    def test_line_53_reports_its_bracket_not_its_net_speech(self):
        # The bracket folds the 0.2 s silence back in (0.9 s); net speech is 0.7 s.
        assert f"{speech_seconds([WATER[8][:2]], [WATER[8][2]])[0]:.1f}" == "0.7"
        text = _preview(WATER, WATER_FIRST, [_op("keep", 53, 53)]).text
        assert "(default for line 53: 0.9 s)" in text

    def test_caption_seconds_are_caption_duration(self):
        # An earlier keep takes lines 6-8, so the timelapse's own keep is
        # clipped to 9-15 — but the caption was sized from the range as sent.
        ops = [_op("keep", 6, 8), _op("timelapse", 6, 15, factor=12.0, text="T")]
        text = _preview(FISH, FISH_FIRST, ops).text
        padded = [(None, None)] * (FISH_FIRST - 1) + [(r[0], r[1]) for r in FISH]
        expected = caption_duration(ops[1], padded)
        assert expected == pytest.approx(17.41)
        assert f"caption on screen {expected:.1f} s" in _block(text, "timelapse [6,15] x12.0 「T」")


class TestCaptions:
    def test_an_overlay_inside_a_timelapse_shares_the_screen(self):
        ops = [
            _op("timelapse", 6, 15, factor=12.0, text="暗闇の中、網で悪戦苦闘"),
            _op("overlay", 9, 9, text="在来種が好き", duration=3.0),
        ]
        text = _preview(FISH, FISH_FIRST, ops).text
        over = _block(text, "overlay [9,9] 「在来種が好き」")
        assert "  no runtime change; caption on screen 3.0 s from line 9" in over
        assert "  on screen together with the caption of timelapse [6,15] (op 1) for 3.0 s" in over
        tl = _block(text, "timelapse [6,15] x12.0 「暗闇の中、網で悪戦苦闘」")
        assert "  on screen together with the caption of overlay [9,9] (op 2) for 3.0 s" in tl

    def test_captions_apart_are_not_reported(self):
        ops = [
            _op("overlay", 5, 5, text="A", duration=2.0),
            _op("overlay", 16, 16, text="B", duration=2.0),
        ]
        assert "together" not in _preview(FISH, FISH_FIRST, ops).text


class TestCutsAndClipping:
    def test_a_cut_states_what_it_removes(self):
        text = _preview(FISH, FISH_FIRST, [_op("cut", 5, 6)]).text
        assert "  removes lines 5-6: 4.5 s" in _block(text, "cut [5,6]")

    def test_a_cut_removes_the_bracket_figure(self):
        text = _preview(WATER, WATER_FIRST, [_op("cut", 53, 53)]).text
        assert "  removes line 53: 0.9 s" in _block(text, "cut [53,53]")

    def test_a_cut_is_clipped_around_a_keep_even_when_sent_first(self):
        ops = [_op("cut", 5, 7), _op("keep", 6, 8)]
        text = _preview(FISH, FISH_FIRST, ops).text
        cut = _block(text, "cut [5,7]")
        assert "  clipped to [5,5]: lines 6-7 are under keep [6,8] (op 2)" in cut
        assert "  removes line 5: 4.0 s" in cut
        assert "clipped" not in _block(text, "keep [6,8]")

    def test_a_fully_overlapped_op_says_it_has_no_effect(self):
        ops = [_op("keep", 6, 8), _op("keep", 7, 7)]
        block = _block(_preview(FISH, FISH_FIRST, ops).text, "keep [7,7]")
        assert "  not applied: keep op fully overlaps existing span(s)" in block


class TestFooter:
    def test_an_over_width_keep_is_reported_with_the_parsers_reason(self):
        drops: list[str] = []
        ops = try_parse_director_response(
            json.dumps({"ops": [{"type": "keep", "lines": [6, 15]}]}),
            16,
            drops=drops,
            max_keep_lines=4,
            first_line=5,
        )
        assert ops == [] and len(drops) == 1
        text = _preview(FISH, FISH_FIRST, ops, drops=drops).text
        assert f"dropped by the parser (no effect): {drops[0]}" in text

    def test_runtime_default_and_with_ops(self):
        rows = [(0.0, 10.0, 4.0, "a"), (12.0, 15.0, 0.0, "b"), (20.0, 22.0, 0.0, "c")]
        result = _preview(rows, 1, [_op("cut", 2, 2)], label="segment [3]")
        assert result.default_seconds == pytest.approx(11.0)
        assert result.runtime_seconds == pytest.approx(8.0)
        assert result.text.endswith("segment [3]: default 11.0 s → with these ops 8.0 s")

    def test_a_keep_adds_its_gaps_and_silence_to_the_runtime(self):
        rows = [(0.0, 10.0, 4.0, "a"), (12.0, 15.0, 0.0, "b"), (20.0, 22.0, 0.0, "c")]
        result = _preview(rows, 1, [_op("keep", 1, 2)])
        # 0-15 plays whole, then line 3's 2 s.
        assert result.runtime_seconds == pytest.approx(17.0)

    def test_the_whole_video_estimate(self):
        rows = [(0.0, 10.0, 0.0, "a")]
        text = _preview(rows, 1, [], elsewhere_seconds=100.0).text
        assert text.endswith("whole video (estimate): 100.0 s elsewhere + 10.0 s here = 110.0 s")

    def test_untimed_segment_says_so(self):
        result = preview_segment(["a"], [_op("cut", 1, 1)], seg_times=None)
        assert "no line timings" in result.text


# --- apply_ops and the preview agree on every clip -----------------------------

_TAG_OPEN = {
    "keep": re.compile(r"<keep>"),
    "cut": re.compile(r"<cut>"),
    "speed": re.compile(r"<speed factor=\"[^\"]*\">"),
}
_TAG_CLOSE = {"keep": "</keep>", "cut": "</cut>", "speed": "</speed>"}


def _tag_ranges(lines: list[str]) -> set[tuple[str, int, int]]:
    """Every marker's line range in an applied ``_edits.txt``."""
    out: set[tuple[str, int, int]] = set()
    for kind, open_re in _TAG_OPEN.items():
        start = None
        for n, line in enumerate(lines, start=1):
            if open_re.search(line):
                start = n
            if _TAG_CLOSE[kind] in line and start is not None:
                out.add((kind, start, n))
                start = None
    for n, line in enumerate(lines, start=1):
        for _ in OVERLAY_TAG_RE.findall(line):
            out.add(("overlay", n, n))
    return out


TABLE = [
    [_op("cut", 5, 7), _op("keep", 6, 8)],
    [_op("keep", 6, 8), _op("cut", 5, 7)],
    [_op("timelapse", 6, 15, factor=12.0, text="T"), _op("overlay", 9, 9, text="O", duration=2.0)],
    [_op("overlay", 6, 6, text="O", duration=2.0), _op("timelapse", 6, 15, factor=8.0, text="T")],
    [_op("keep", 6, 8), _op("timelapse", 6, 15, factor=12.0, text="T"), _op("cut", 14, 16)],
    [_op("cut", 9, 12), _op("keep", 6, 8), _op("keep", 7, 10), _op("speed", 11, 13, factor=1.5)],
    [_op("keep", 6, 8), _op("keep", 7, 7)],
    [_op("speed", 5, 16, factor=2.0), _op("timelapse", 8, 10, factor=4.0), _op("cut", 5, 6)],
]


@pytest.mark.parametrize("ops", TABLE)
def test_apply_ops_and_the_preview_clip_identically(ops):
    lines = [""] * (FISH_FIRST - 1) + [r[3] for r in FISH]
    seg_times = [(None, None)] * (FISH_FIRST - 1) + [(r[0], r[1]) for r in FISH]

    def no_llm(_m, _c):
        raise AssertionError("span ops make no LLM call")

    applied, _ = apply_ops(
        lines, expand_timelapse_ops(ops, seg_times), {"prompt": ""}, call_llm=no_llm
    )
    placed = resolve_placements(lines, ops, seg_times)
    previewed = {
        (p.kind, p.lines[0], p.lines[1]) for per_op in placed for p in per_op if p.lines is not None
    }
    assert previewed == _tag_ranges(applied)


# --- the conversation's view: display numbering and the whole video ------------


WATER_SILENCES = [
    SilenceLine(
        47,
        412.695,
        434.647,
        (
            "The camera shifts to a closer view inside the aquarium.",
            "The camera view shifts to the pipe opening.",
        ),
    ),
    SilenceLine(53, 501.897, 531.779, (PUMP_DESC,)),
]


def _transcript(rows, first, silence_lines=()):
    return SegmentTranscript(
        edit_lines=[r[3] for r in rows],
        first_line=first,
        seg_times=[(r[0], r[1]) for r in rows],
        silences=[r[2] for r in rows],
        gaps=[],
        silence_lines=list(silence_lines),
    )


WATER_T = _transcript(WATER, WATER_FIRST, WATER_SILENCES)
FISH_T = _transcript(FISH, FISH_FIRST)


def _view():
    """Two segments, so a source line number is never its display number."""
    return build_display_view(
        [(Segment("water", (45, 55)), WATER_T), (Segment("fish", (5, 16)), FISH_T)]
    )


def _numbered(ops, **kw):
    """The water segment's preview under the whole video's numbering."""
    return preview_segment(
        WATER_T.edit_lines,
        ops,
        seg_times=WATER_T.seg_times,
        silences=WATER_T.silences,
        silence_lines=WATER_T.silence_lines,
        first_line=WATER_T.first_line,
        numbering=numbering_for(_view(), 1),
        **kw,
    ).text


class TestDisplayNumbering:
    """Source line 45 is display line 1, and the 29.9 s wait after 53 is 11."""

    def test_the_view_numbers_this_fixture_the_way_the_assertions_assume(self):
        view = _view()
        assert view.from_source(1, 45) == 1
        assert view.from_source(1, 53) == 10
        assert view.from_source(1, 53, silence=True) == 11
        assert view.from_source(2, 5) == 14

    def test_an_op_is_titled_in_display_numbers(self):
        text = _numbered([_op("timelapse", 48, 50, factor=8.0, text="T")])
        assert "timelapse [5,7] x8.0 「T」" in text
        assert "timelapse [48,50]" not in text

    def test_the_lines_an_op_plays_are_display_numbers(self):
        text = _numbered([_op("timelapse", 48, 50, factor=8.0, text="T")])
        assert "(default for lines 5-7:" in text

    def test_speech_made_unintelligible_is_listed_by_display_number(self):
        text = _numbered([_op("timelapse", 48, 50, factor=8.0, text="T")])
        assert "    5 5.4 s 「ここに水を注ぎます」" in text

    def test_a_silence_op_is_titled_by_the_silence_lines_own_number(self):
        op = DirectorOp(type="timelapse", lines=(53, 53), factor=5.0, text="T")
        text = _numbered([replace(op, gap_start=True, gap_end=True)])
        assert "timelapse [11,11] x5.0 「T」" in text
        assert "  covers line 11 (501.9-531.8 s)" in text

    def test_a_dropped_silence_is_named_by_its_display_line(self):
        text = _numbered([_op("keep", 53, 53)])
        assert "  line 11 is outside this op — dropped" in text
        # ...and shown exactly as the transcript shows line 11.
        assert f"    11: {_view().line(11).text}" in text

    def test_a_silence_with_no_line_of_its_own_still_names_a_display_line(self):
        # The 3.2 s after source line 54 (display 12) is under the threshold.
        text = _numbered([_op("keep", 54, 54)])
        assert "  the silence after line 12 is outside this op — dropped" in text
        assert "    [silent 3.2s after line 12]" in text

    def test_a_clip_note_and_its_op_reference_are_display_numbers(self):
        text = _numbered([_op("cut", 48, 50), _op("keep", 49, 50)])
        assert "  clipped to [5,5]: lines 6-7 are under keep [6,7] (op 2)" in text


class TestASilenceNobodyCanHear:
    def test_a_zero_second_gap_inside_a_time_resolved_op_is_not_printed(self):
        # A timelapse over a stretch whose lines run into each other holds a
        # gap after every one of them.  Printing "[silent 0.0s after line 6]"
        # for each is noise the director has to read past every turn: six of
        # the seven gap lines in the real project's first timelapse were this.
        view = _view()
        op = DirectorOp(
            type="timelapse", lines=(47, 53), factor=6.0, text="T", gap_start=True, gap_end=True
        )
        text = preview_segment(
            WATER_T.edit_lines,
            [op],
            seg_times=WATER_T.seg_times,
            silences=WATER_T.silences,
            silence_lines=WATER_T.silence_lines,
            first_line=WATER_T.first_line,
            numbering=numbering_for(view, 1),
        ).text
        assert "0.0s" not in text
        # ...and the waits that ARE there still are.
        assert "11: [silent 29.9s" in text

    def test_a_wait_the_segment_bounds_do_not_show_is_still_printed(self):
        # WhisperX stretches a line's last word to the next line's start:
        # source lines 49 and 50 touch (segment gap 0.0) while the wait the
        # pipeline actually drops — the silence line's own interval — is
        # 6.6 s.  The preview prices what will be dropped, not the bound.
        op = DirectorOp(
            type="timelapse", lines=(49, 50), factor=6.0, text="T", gap_start=True, gap_end=True
        )
        text = preview_segment(
            WATER_T.edit_lines,
            [op],
            seg_times=WATER_T.seg_times,
            silences=WATER_T.silences,
            silence_lines=[SilenceLine(49, 456.415, 463.0)],
            first_line=WATER_T.first_line,
        ).text
        assert "[silent 6.6s after line 49]" in text


class TestWholeVideoRuntime:
    def test_the_footer_is_the_whole_video_not_this_segment(self):
        view = _view()
        ops = {1: [_op("cut", 45, 47)]}
        water = preview_segment(
            WATER_T.edit_lines,
            ops[1],
            seg_times=WATER_T.seg_times,
            silences=WATER_T.silences,
            silence_lines=WATER_T.silence_lines,
            first_line=WATER_T.first_line,
        )
        expected = water.runtime_seconds + FISH_T.default_runtime()
        text = preview_turn(view, [WATER_T, FISH_T], ops, segments=[1])
        assert f"with the ops accepted so far {expected:.1f} s" in text
        # The segment's own runtime is a different number, and it is reported
        # as the segment's, not as the video's.
        assert f"{water.runtime_seconds:.1f} s" != f"{expected:.1f} s"
        assert f"segment [1] water [45-55]: default {water.default_seconds:.1f} s" in text

    def test_a_segment_with_no_accepted_ops_counts_at_its_default(self):
        view = _view()
        text = preview_turn(view, [WATER_T, FISH_T], {}, segments=[1])
        total = WATER_T.default_runtime() + FISH_T.default_runtime()
        assert f"whole video so far: default {total:.1f} s" in text
        assert f"with the ops accepted so far {total:.1f} s" in text

    def test_a_segment_the_turn_did_not_touch_is_priced_but_not_printed(self):
        view = _view()
        ops = {1: [_op("cut", 45, 47)], 2: [_op("cut", 5, 7)]}
        text = preview_turn(view, [WATER_T, FISH_T], ops, segments=[2])
        assert "segment [1] water" not in text
        assert "segment [2] fish" in text
        fish = preview_segment(
            FISH_T.edit_lines,
            ops[2],
            seg_times=FISH_T.seg_times,
            silences=FISH_T.silences,
            first_line=FISH_T.first_line,
        )
        water = preview_segment(
            WATER_T.edit_lines,
            ops[1],
            seg_times=WATER_T.seg_times,
            silences=WATER_T.silences,
            silence_lines=WATER_T.silence_lines,
            first_line=WATER_T.first_line,
        )
        expected = water.runtime_seconds + fish.runtime_seconds
        assert f"with the ops accepted so far {expected:.1f} s" in text

    def test_the_turns_ops_are_shown_in_display_numbers(self):
        view = _view()
        text = preview_turn(view, [WATER_T, FISH_T], {2: [_op("cut", 5, 7)]}, segments=[2])
        assert "cut [14,16]" in text

    def test_the_parsers_drops_are_reported_once(self):
        view = _view()
        text = preview_turn(
            view, [WATER_T, FISH_T], {1: [], 2: []}, segments=[1, 2], drops=["bad op"]
        )
        assert text.count("dropped by the parser (no effect): bad op") == 1
