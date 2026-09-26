"""The deterministic order note: a reorder must never be invisible.

No LLM call, in the same spirit as the cut report: the machine states what happened and the human decides whether it
was right.
"""

from __future__ import annotations

import json

import pytest

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def _ctx(tmp_path, stems=("dev", "mix")):
    sources = [
        SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4") for s in stems
    ]
    return PipelineContext(
        cfg=get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=0,
        to_index=len(st.STAGE_NAMES) - 1,
    )


@pytest.fixture
def project(tmp_path):
    in_dir = tmp_path / "in"
    in_dir.mkdir()
    tf = tmp_path / "out" / "text_filter"
    tf.mkdir(parents=True)
    for stem, count in (("dev", 40), ("mix", 97)):
        (in_dir / f"{stem}.mp4").touch()
        (tf / f"{stem}_edits.txt").write_text("x\n" * count, encoding="utf-8")
    return tmp_path


def _plan(tmp_path, order):
    """Write the director's ``order.json``."""
    d = tmp_path / "out" / "director"
    d.mkdir(parents=True, exist_ok=True)
    payload = {} if order is None else {"order": order}
    (d / "order.json").write_text(json.dumps(payload), encoding="utf-8")


def _note(tmp_path):
    return tmp_path / "out" / "llm_report" / "notes" / "order.md"


class TestTheNote:
    def test_shooting_order_writes_no_note(self, project):
        st.write_order_note(_ctx(project))
        assert not _note(project).exists()

    def test_an_identity_order_writes_no_note(self, project):
        # Present-and-identity is not a reorder.
        _plan(project, [{"stem": "dev", "lines": [1, 40]}, {"stem": "mix", "lines": [1, 97]}])
        st.write_order_note(_ctx(project))
        assert not _note(project).exists()

    def test_a_reorder_is_stated(self, project):
        _plan(
            project,
            [
                {"stem": "mix", "lines": [31, 97]},
                {"stem": "dev"},
                {"stem": "mix", "lines": [1, 30]},
            ],
        )
        st.write_order_note(_ctx(project))
        text = _note(project).read_text(encoding="utf-8")
        assert "mix [31-97]" in text
        assert "dev" in text

    def test_the_note_says_where_a_segment_moved_from(self, project):
        # Shooting order is dev then mix, so mix moved from 2 to 1.
        _plan(project, [{"stem": "mix"}, {"stem": "dev"}])
        st.write_order_note(_ctx(project))
        text = _note(project).read_text(encoding="utf-8")
        assert "1. mix  (was 2 in shooting order)" in text
        assert "2. dev  (was 1 in shooting order)" in text

    def test_a_segment_that_did_not_move_is_not_annotated(self, project):
        _plan(
            project,
            [
                {"stem": "dev"},
                {"stem": "mix", "lines": [31, 97]},
                {"stem": "mix", "lines": [1, 30]},
            ],
        )
        st.write_order_note(_ctx(project))
        lines = _note(project).read_text(encoding="utf-8").splitlines()
        assert "1. dev" in lines

    def test_a_rejected_order_is_reported_as_such(self, project):
        _plan(project, [{"stem": "dev"}])  # mix left out entirely
        st.write_order_note(_ctx(project))
        text = _note(project).read_text(encoding="utf-8")
        assert "rejected" in text.lower()
        assert "mix" in text

    def test_a_stale_note_is_removed_when_the_order_goes_back(self, project):
        note = _note(project)
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("stale", encoding="utf-8")
        st.write_order_note(_ctx(project))
        assert not note.exists()
