"""Shared inputs for the silence-line equivalence tests.

Six lines of WhisperX with long waits after lines 1 (10.0 s), 3 (8.0 s) and
4 (6.0 s) and short ones elsewhere.  No word is stretched past
``SILENCE_MAX_WORD_SPAN``, so a line's raw last-word end and its clamped speech
span end agree (spec Q2: they differ only for a stretched final word).
Every timelapse span divides by its factor to at most two decimals, so the
caption guided_edit rounds and the one ``op_times`` did not round agree.
"""

from __future__ import annotations


def _line(text: str, words: list[tuple[str, float, float]]) -> dict:
    return {
        "start": words[0][1],
        "end": words[-1][2],
        "text": text,
        "words": [{"word": w, "start": s, "end": e} for w, s, e in words],
    }


WHISPERX = {
    "duration": 40.0,
    "segments": [
        _line("あい", [("あ", 0.5, 0.8), ("い", 0.8, 1.1)]),
        _line("うえ", [("う", 11.1, 11.5), ("え", 11.5, 12.0)]),
        _line("おか", [("お", 12.5, 12.9), ("か", 12.9, 13.0)]),
        _line("きく", [("き", 21.0, 21.5), ("く", 21.5, 22.0)]),
        _line("けこ", [("け", 28.0, 28.4), ("こ", 28.4, 29.0)]),
        _line("さし", [("さ", 30.0, 30.5), ("し", 30.5, 31.0)]),
    ],
}

TEXT_FILTER_LINES = ["あい", "うえ", "おか", "きく", "けこ", "さし"]

INTERVALS_CFG = {
    "silence_threshold": 1.0,
    "min_keep": 0.001,
    "keep_pre_margin": 0.0,
    "keep_post_margin": 0.0,
    "min_cut": 0.0,
}

#: name -> the ops of one ``_director.json``.
CASES: dict[str, list[dict]] = {
    "keep_one_silence": [{"type": "keep", "lines": ["1~", "1~"]}],
    "keep_from_silence": [{"type": "keep", "lines": ["1~", 3]}],
    "keep_to_silence": [{"type": "keep", "lines": [2, "3~"]}],
    "keep_silence_to_silence": [{"type": "keep", "lines": ["1~", "3~"]}],
    "speed_in_keep_over_silences": [
        {"type": "keep", "lines": ["3~", "4~"]},
        {"type": "speed", "lines": ["3~", "4~"], "factor": 5.0},
    ],
    "timelapse_one_silence": [
        {"type": "timelapse", "lines": ["1~", "1~"], "factor": 8.0, "text": "準備"}
    ],
    "timelapse_from_silence": [
        {"type": "timelapse", "lines": ["1~", 3], "factor": 7.0, "text": "作業"}
    ],
    "timelapse_to_silence": [
        {"type": "timelapse", "lines": [4, "4~"], "factor": 4.0, "text": "片付け"}
    ],
    "overlay_on_silence": [
        {"type": "overlay", "lines": ["3~", "3~"], "text": "待ち", "duration": 2.0}
    ],
    "two_timelapses": [
        {"type": "timelapse", "lines": ["1~", 2], "factor": 5.0, "text": "一"},
        {"type": "timelapse", "lines": [3, "3~"], "factor": 5.0, "text": "二"},
    ],
    "silence_ops_beside_line_ops": [
        {"type": "timelapse", "lines": ["1~", "1~"], "factor": 8.0, "text": "準備"},
        {"type": "overlay", "lines": [2, 2], "text": "見出し", "duration": 1.5},
        {"type": "cut", "lines": [5, 5]},
        {"type": "keep", "lines": [6, 6]},
    ],
}
