"""run_director over a PARTIAL segment: what the LLM actually receives.

Every other run_director test uses a whole-source segment, which cannot tell a
slice apart from the whole file. These do.
"""

from __future__ import annotations

import json

import nagare_clip.director.director_llm as dl
import nagare_clip.director.run as director_run
from nagare_clip.director.context import Neighbour
from nagare_clip.order import Segment

CFG = {"director": {"enabled": True, "prompt": "P", "max_retries": 0}}


def _edits(tmp_path, stem, count=6):
    path = tmp_path / f"{stem}_edits.txt"
    path.write_text("\n".join(f"{stem}の{i}行目" for i in range(1, count + 1)) + "\n", "utf-8")
    return path


def _json(tmp_path, stem, times):
    path = tmp_path / f"{stem}.json"
    path.write_text(
        json.dumps({"segments": [{"start": s, "end": e, "text": "x"} for s, e in times]}), "utf-8"
    )
    return path


def _capture(monkeypatch):
    seen = {}

    def fake_llm(messages, cfg):
        seen["system"] = messages[0]["content"]
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    return seen


class TestTheSliceItSees:
    def test_only_the_segments_lines_are_sent(self, tmp_path, monkeypatch):
        seen = _capture(monkeypatch)
        director_run.run_director(_edits(tmp_path, "a"), CFG, segment=Segment("a", (3, 5)))
        assert seen["user"] == "3: aの3行目\n4: aの4行目\n5: aの5行目"

    def test_the_lines_outside_the_segment_are_absent(self, tmp_path, monkeypatch):
        seen = _capture(monkeypatch)
        director_run.run_director(_edits(tmp_path, "a"), CFG, segment=Segment("a", (3, 5)))
        assert "aの2行目" not in seen["user"]
        assert "aの6行目" not in seen["user"]

    def test_the_timing_brackets_belong_to_the_segments_own_lines(self, tmp_path, monkeypatch):
        seen = _capture(monkeypatch)
        times = [(0.0, 1.0), (2.0, 3.0), (4.0, 9.0), (10.0, 11.0), (12.0, 13.0), (14.0, 15.0)]
        director_run.run_director(
            _edits(tmp_path, "a"),
            CFG,
            segment=Segment("a", (3, 5)),
            json_path=_json(tmp_path, "a", times),
        )
        # Line 3 runs 4.0-9.0 -> 5.0s. Unsliced times would give line 3 the
        # first entry's 1.0s instead.
        assert seen["user"].splitlines()[0].startswith("3: aの3行目  [5.0s")


class TestTheSeamItSees:
    def test_a_neighbour_segment_of_the_same_source_shows_only_its_own_lines(
        self, tmp_path, monkeypatch
    ):
        seen = _capture(monkeypatch)
        edits = _edits(tmp_path, "a")
        director_run.run_director(
            edits,
            CFG,
            segment=Segment("a", (3, 5)),
            before=Neighbour(Segment("a", (1, 2)), edits),
        )
        assert "- aの2行目" in seen["system"]
        assert "- aの5行目" not in seen["system"]

    def test_the_seam_is_labelled_by_the_segment_not_the_source(self, tmp_path, monkeypatch):
        seen = _capture(monkeypatch)
        edits = _edits(tmp_path, "a")
        director_run.run_director(
            edits,
            CFG,
            segment=Segment("a", (3, 5)),
            before=Neighbour(Segment("a", (1, 2)), edits),
        )
        assert "(a [1-2], its last lines)" in seen["system"]


class TestTheGapsItSees:
    def _gaps(self, tmp_path, *spans):
        path = tmp_path / "a_gaps.json"
        path.write_text(
            json.dumps(
                {
                    "gaps": [
                        {"start": s, "end": e, "frames": [], "description": d} for s, e, d in spans
                    ]
                }
            ),
            "utf-8",
        )
        return path

    def test_a_gap_outside_the_segment_is_not_annotated(self, tmp_path, monkeypatch):
        seen = _capture(monkeypatch)
        times = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (6.0, 7.0), (8.0, 9.0), (10.0, 11.0)]
        director_run.run_director(
            _edits(tmp_path, "a"),
            CFG,
            segment=Segment("a", (3, 5)),
            json_path=_json(tmp_path, "a", times),
            gaps=self._gaps(tmp_path, (1.2, 1.9, "別の場面"), (5.2, 5.9, "この場面")),
        )
        assert "この場面" in seen["user"]
        assert "別の場面" not in seen["user"]


class TestTheSilenceItSees:
    def test_the_speech_silence_split_belongs_to_the_segments_own_lines(
        self, tmp_path, monkeypatch
    ):
        from nagare_clip.audio_silence.cuts_file import write_cuts

        seen = _capture(monkeypatch)
        # Line 4 holds a long internal silence; line 2 (the same offset within
        # the whole source) holds none, so unsliced silences would put the split
        # bracket on the wrong line.
        times = [(0.0, 1.0), (2.0, 3.0), (4.0, 5.0), (10.0, 20.0), (21.0, 22.0), (23.0, 24.0)]
        cuts = tmp_path / "a_cuts.txt"
        write_cuts(cuts, [(11.0, 19.0)])
        director_run.run_director(
            _edits(tmp_path, "a"),
            CFG,
            segment=Segment("a", (3, 5)),
            json_path=_json(tmp_path, "a", times),
            cuts_txt=cuts,
        )
        line4 = seen["user"].splitlines()[1]
        assert line4.startswith("4: ")
        assert "speech" in line4 and "silence" in line4
        assert "silence" not in seen["user"].splitlines()[0]
