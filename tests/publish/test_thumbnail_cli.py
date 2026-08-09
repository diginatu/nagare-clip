"""The standalone re-render CLI: swap a background or hand-edit a colour in
publish.json and rebuild the images, with no LLM call."""

from __future__ import annotations

import json

import pytest

from nagare_clip.publish import thumbnail
from nagare_clip.publish.publish_llm import ThumbLine, ThumbSet

PUBLISH_JSON = {
    "titles": ["A"],
    "thumbnail_copy": [
        {
            "lines": [
                {"role": "tag", "text": "水槽DIY"},
                {"role": "hook", "text": "水浸し！", "fill": "#B08D3E"},
            ],
            "gravity": "northwest",
            "offset": "+56+62",
        },
        {"lines": [{"role": "hook", "text": "穴あけ不要。"}]},
    ],
    "thumbnails": [{"stem": "a", "path": "frames/a/1.000.jpg"}],
    "renders": [],
}


def test_sets_are_read_back_out_of_publish_json():
    sets = thumbnail.sets_from_dict(PUBLISH_JSON)
    assert sets[0] == ThumbSet(
        lines=[
            ThumbLine("tag", "水槽DIY", {}),
            ThumbLine("hook", "水浸し！", {"fill": "#B08D3E"}),
        ],
        style={"gravity": "northwest", "offset": "+56+62"},
    )
    assert sets[1].style == {}


@pytest.mark.parametrize("data", [{}, {"thumbnail_copy": "x"}, {"thumbnail_copy": [1, None]}])
def test_a_file_with_no_usable_sets_reads_as_empty(data):
    assert thumbnail.sets_from_dict(data) == []


def _project(tmp_path):
    stage = tmp_path / "output" / "publish"
    (stage / "frames" / "a").mkdir(parents=True)
    (stage / "frames" / "a" / "1.000.jpg").write_bytes(b"x")
    (stage / "publish.json").write_text(json.dumps(PUBLISH_JSON), encoding="utf-8")
    return stage


def test_the_cli_renders_every_set_without_an_llm_call(tmp_path, monkeypatch):
    stage = _project(tmp_path)
    cmds = []
    monkeypatch.setattr(
        thumbnail,
        "_runner",
        lambda: lambda cmd: (cmds.append(cmd), "300 90\n" * 3)[1],
    )
    assert thumbnail.main(["--publish-dir", str(stage)]) == 0
    assert len([c for c in cmds if c[-1] != "info:"]) == 2


def test_the_cli_background_flag_overrides_the_config(tmp_path, monkeypatch):
    stage = _project(tmp_path)
    (stage / "frames" / "a" / "9.000.jpg").write_bytes(b"x")
    cmds = []
    monkeypatch.setattr(
        thumbnail,
        "_runner",
        lambda: lambda cmd: (cmds.append(cmd), "300 90\n" * 3)[1],
    )
    thumbnail.main(["--publish-dir", str(stage), "--background", "frames/a/9.000.jpg"])
    assert str(stage / "frames/a/9.000.jpg") in cmds[1]


def test_the_cli_reports_a_missing_publish_json(tmp_path, capsys):
    assert thumbnail.main(["--publish-dir", str(tmp_path)]) == 1
    assert "publish.json" in capsys.readouterr().err
