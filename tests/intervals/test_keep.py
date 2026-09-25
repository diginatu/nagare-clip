"""What the intervals stage drops with no director op, as one function.

``dropped_ranges`` exists so the director's brackets can quote exactly what
``run_intervals`` renders.  The parity test is the point: it runs the real
stage and asserts the complement of its keep intervals IS ``dropped_ranges`` —
so a pass added to one and not the other fails here, not in a finished cut.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.keep import dropped_ranges
from nagare_clip.intervals.run import run_intervals


def _words(text, *spans):
    return [{"word": ch, "start": s, "end": e} for ch, (s, e) in zip(text, spans)]


def _data():
    # Line 1: speech 0-2, a 3 s word gap, speech 5-7.  Line 2: speech 20-21.
    # Real GiNZA is used (loaded once per process): captions are part of what
    # shapes the keep intervals, so the parity below must include them.
    return {
        "duration": 30.0,
        "segments": [
            {
                "start": 0.0,
                "end": 7.0,
                "text": "あいうえおかきく",
                "words": _words(
                    "あいうえおかきく",
                    (0.0, 0.5),
                    (0.5, 1.0),
                    (1.0, 1.5),
                    (1.5, 2.0),
                    (5.0, 5.5),
                    (5.5, 6.0),
                    (6.0, 6.5),
                    (6.5, 7.0),
                ),
            },
            {
                "start": 20.0,
                "end": 21.0,
                "text": "さしすせ",
                "words": _words(
                    "さしすせ", (20.0, 20.25), (20.25, 20.5), (20.5, 20.75), (20.75, 21.0)
                ),
            },
        ],
    }


LINES = ["あいうえおかきく", "さしすせ"]


def _ivl(**over):
    cfg = get_effective_config(None, {})["intervals"]
    cfg.update(over)
    return cfg


def _approx(ranges):
    return [(pytest.approx(s, abs=1e-3), pytest.approx(e, abs=1e-3)) for s, e in ranges]


class TestDroppedRanges:
    def test_a_word_gap_over_the_threshold_is_dropped_without_any_cut_list(self):
        ivl = _ivl(silence_threshold=1.0, keep_pre_margin=0.0, keep_post_margin=0.0, min_cut=0.0)
        dropped = dropped_ranges(_data(), ivl)
        assert (pytest.approx(2.0), pytest.approx(5.0)) in _approx(dropped)

    def test_keep_margins_give_back_part_of_a_dropped_gap(self):
        ivl = _ivl(silence_threshold=1.0, keep_pre_margin=0.5, keep_post_margin=0.3, min_cut=0.0)
        dropped = dropped_ranges(_data(), ivl)
        assert (pytest.approx(2.3), pytest.approx(4.5)) in _approx(dropped)

    def test_the_audio_silence_cuts_are_dropped_too(self):
        ivl = _ivl(
            silence_threshold=100.0,
            min_keep=0.0,
            keep_pre_margin=0.0,
            keep_post_margin=0.0,
            min_cut=0.0,
        )
        dropped = dropped_ranges(_data(), ivl, cut_ranges=[(0.5, 2.0)])
        assert (pytest.approx(0.5), pytest.approx(2.0)) in _approx(dropped)

    def test_a_human_keep_in_the_edit_lines_is_not_dropped(self):
        ivl = _ivl(silence_threshold=1.0, keep_pre_margin=0.0, keep_post_margin=0.0, min_cut=0.0)
        edits = ["あいう<keep>えお</keep>かきく", "さしすせ"]
        dropped = dropped_ranges(_data(), ivl, edit_lines=edits)
        assert not any(s < 4.0 < e for s, e in dropped)


def _run_stage(tmp_path, data, edit_lines, ivl, cuts):
    js = tmp_path / "c.json"
    js.write_text(json.dumps(data), encoding="utf-8")
    edits = tmp_path / "c_edits.txt"
    edits.write_text("\n".join(edit_lines) + "\n", encoding="utf-8")
    cuts_txt = tmp_path / "c_cuts.txt"
    write_cuts(cuts_txt, cuts)
    out = tmp_path / "c_intervals.json"
    run_intervals(edits, js, out, {"intervals": ivl}, cuts_txt=cuts_txt)
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "over",
    [
        {},
        {"silence_threshold": 1.0},
        {"silence_threshold": 1.0, "keep_pre_margin": 0.5, "keep_post_margin": 0.3},
        {"silence_threshold": 0.8, "min_keep": 3.0, "min_cut": 2.5},
    ],
)
def test_parity_the_complement_of_what_run_intervals_keeps(tmp_path, over):
    data = _data()
    ivl = _ivl(**over)
    cuts = [(8.0, 18.0), (24.0, 29.0)]
    out = _run_stage(tmp_path, data, LINES, ivl, cuts)
    keeps = [(k["start"], k["end"]) for k in out["keep_intervals"]]
    gaps, cursor = [], 0.0
    for s, e in keeps:
        if s > cursor:
            gaps.append((cursor, s))
        cursor = e
    if cursor < out["duration_sec"]:
        gaps.append((cursor, out["duration_sec"]))
    got = dropped_ranges(data, ivl, edit_lines=LINES, cut_ranges=cuts)
    assert _approx(got) == gaps
