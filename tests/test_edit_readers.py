"""Every reader of an ``_edits.txt`` goes through ``edit_lines.parse_edit_lines``.

A reader that split the file itself would count a silence line as a
transcript line and shift every line after it off its segment.
"""

from __future__ import annotations

from pathlib import Path

from nagare_clip.director.run import SegmentInputs, load_segment_transcript
from nagare_clip.order import Segment
from nagare_clip.pipeline import stages

SILENCED = ["あ", "[silent 10.0s: x]", "<keep>い</keep>", "う"]


def test_the_director_reads_speech_lines_only(tmp_path: Path):
    edits = tmp_path / "c_edits.txt"
    edits.write_text("\n".join(SILENCED) + "\n", encoding="utf-8")
    t = load_segment_transcript(SegmentInputs(Segment("c", None), edits))
    assert t.edit_lines == ["あ", "<keep>い</keep>", "う"]


def test_the_order_counts_speech_lines_only(tmp_path: Path, monkeypatch):
    (tmp_path / "text_filter").mkdir()
    (tmp_path / "text_filter" / "c_edits.txt").write_text(
        "\n".join(SILENCED) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(stages, "project_stems", lambda _d: ["c"])

    class _Ctx:
        input_videos_dir = tmp_path
        stems = ("c",)

        def stage_dir(self, name):
            return tmp_path / name

    assert stages._line_counts(_Ctx()) == {"c": 3}
