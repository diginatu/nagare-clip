"""render stage: reads publish.json, composites one thumbnail per copy set and
writes render.json + render.md.  Never makes an LLM call, ever."""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import nagare_clip.render.run as render_run
from nagare_clip.pipeline.external import run_magick

HAS_MAGICK = shutil.which("magick") is not None

CFG = {"render": {"enabled": True, "width": 1280, "height": 720, "line_gap": 12, "fonts": {}}}

PUBLISH = {
    "titles": ["候補1"],
    "lead": "リード文。",
    "description": "リード文。",
    "chapters": [],
    "chapters_qualify": False,
    "chapter_issues": [],
    "thumbnail_copy": [
        {"lines": [{"role": "tag", "text": "水槽DIY"}, {"role": "hook", "text": "水浸し！"}]},
        {"lines": [{"role": "hook", "text": "穴あけ不要。"}], "gravity": "southwest"},
    ],
    "thumbnails": [
        {
            "stem": "a",
            "source_time": 1.0,
            "timeline_time": 1.0,
            "kind": "overlay",
            "label": "水浸し！",
            "path": "frames/a/1.000.jpg",
        }
    ],
}


class FakeRun:
    """Records commands; answers measure calls with plausible metrics."""

    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> str:
        self.cmds.append(cmd)
        if cmd[-1] == "info:":
            return "".join("300 90\n" for a in cmd if a.startswith("label:"))
        return ""


def _project(tmp_path, publish=PUBLISH, *, frames=("frames/a/1.000.jpg",)):
    """A publish stage dir with publish.json and its stills, plus a render dir."""
    publish_dir = tmp_path / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)
    (publish_dir / "publish.json").write_text(
        json.dumps(publish, ensure_ascii=False), encoding="utf-8"
    )
    for rel in frames:
        still = publish_dir / rel
        still.parent.mkdir(parents=True, exist_ok=True)
        still.write_bytes(b"jpeg")
    return publish_dir, tmp_path / "render"


def _run(tmp_path, cfg=CFG, *, run=None, publish=PUBLISH, frames=("frames/a/1.000.jpg",)):
    publish_dir, render_dir = _project(tmp_path, publish, frames=frames)
    render_run.run_render(
        publish_dir / "publish.json",
        render_dir / "render.json",
        cfg,
        markdown=render_dir / "render.md",
        run=run if run is not None else FakeRun(),
    )
    return (
        json.loads((render_dir / "render.json").read_text(encoding="utf-8")),
        (render_dir / "render.md").read_text(encoding="utf-8"),
        render_dir,
    )


def test_disabled_writes_an_empty_artifact(tmp_path):
    data, md, _ = _run(tmp_path, {"render": {"enabled": False}})
    assert data == {"renders": [], "skipped": []}
    assert "disabled" in md


def test_a_missing_publish_json_writes_an_empty_artifact(tmp_path, caplog):
    render_run.run_render(
        tmp_path / "publish" / "publish.json",
        tmp_path / "render" / "render.json",
        CFG,
        markdown=tmp_path / "render" / "render.md",
        run=FakeRun(),
    )
    data = json.loads((tmp_path / "render" / "render.json").read_text(encoding="utf-8"))
    assert data == {"renders": [], "skipped": []}
    assert "publish.json" in caplog.text


def test_every_copy_set_in_publish_json_is_rendered(tmp_path):
    data, _, render_dir = _run(tmp_path)
    assert data["renders"] == [
        {"set": 1, "path": "thumbnails/set1.jpg", "background": "frames/a/1.000.jpg"},
        {"set": 2, "path": "thumbnails/set2.jpg", "background": "frames/a/1.000.jpg"},
    ]


def test_the_images_land_under_the_render_stage_dir_not_publish(tmp_path):
    run = FakeRun()
    _, _, render_dir = _run(tmp_path, run=run)
    written = [cmd[-1] for cmd in run.cmds if cmd[-1] != "info:"]
    assert written == [
        str(render_dir / "thumbnails/set1.jpg"),
        str(render_dir / "thumbnails/set2.jpg"),
    ]


def test_the_background_resolves_against_the_publish_dir(tmp_path):
    """The stills sit beside publish.json, not beside the renders."""
    run = FakeRun()
    _run(tmp_path, run=run)
    render_cmd = next(cmd for cmd in run.cmds if cmd[-1] != "info:")
    assert render_cmd[1] == str(tmp_path / "publish" / "frames/a/1.000.jpg")


def test_the_markdown_is_a_contact_sheet_of_the_finished_images(tmp_path):
    _, md, _ = _run(tmp_path)
    first, second = md.index("## Set 1"), md.index("## Set 2")
    assert first < md.index('<img src="thumbnails/set1.jpg"') < second
    assert second < md.index('<img src="thumbnails/set2.jpg"')
    assert "水浸し！" in md
    assert "frames/a/1.000.jpg" in md


def test_markdown_image_markup_uses_markdown_images(tmp_path):
    """One setting, read by both markdown files: it describes the viewer."""
    cfg = {**CFG, "general": {"image_markup": "markdown"}}
    _, md, _ = _run(tmp_path, cfg)
    assert "![Set 1](thumbnails/set1.jpg)" in md
    assert "<img" not in md


def test_no_copy_sets_renders_nothing_without_failing(tmp_path):
    data, md, _ = _run(tmp_path, publish={**PUBLISH, "thumbnail_copy": []})
    assert data["renders"] == []
    assert "_(none)_" in md


def test_a_hand_edited_background_changes_only_that_thumbnail(tmp_path):
    """The whole point of the split: edit one line of publish.json, re-run.

    No LLM call is involved in getting here -- run_render is the entry point
    the stage uses, and the guards below prove the transport is never loaded.
    """
    publish = json.loads(json.dumps(PUBLISH))
    publish["thumbnail_copy"][1]["background"] = "frames/b/55.660.jpg"
    run = FakeRun()
    data, _, _ = _run(
        tmp_path,
        run=run,
        publish=publish,
        frames=("frames/a/1.000.jpg", "frames/b/55.660.jpg"),
    )
    assert [r["background"] for r in data["renders"]] == [
        "frames/a/1.000.jpg",
        "frames/b/55.660.jpg",
    ]


def test_an_absolute_background_outside_the_project_renders(tmp_path):
    """A photograph the camera never rolled on is one line of JSON away."""
    outside = tmp_path / "studio.png"
    outside.write_bytes(b"png")
    publish = json.loads(json.dumps(PUBLISH))
    publish["thumbnail_copy"][0]["background"] = str(outside)
    run = FakeRun()
    data, md, _ = _run(tmp_path, run=run, publish=publish)
    assert data["renders"][0]["background"] == str(outside)
    assert next(cmd for cmd in run.cmds if cmd[-1] != "info:")[1] == str(outside)
    assert str(outside) in md


def test_sets_naming_no_background_render_exactly_as_before(tmp_path):
    """Nothing said -> the first shortlist candidate, for every set."""
    data, _, _ = _run(tmp_path)
    assert {r["background"] for r in data["renders"]} == {"frames/a/1.000.jpg"}


def _skipping(tmp_path, *, background, frames=("frames/a/1.000.jpg",), run=None):
    """One set that cannot render, one that can."""
    publish = json.loads(json.dumps(PUBLISH))
    publish["thumbnail_copy"][0]["background"] = background
    return _run(tmp_path, publish=publish, frames=frames, run=run)


def test_a_set_that_could_not_be_rendered_says_so_in_the_contact_sheet(tmp_path):
    """The human edits publish.json and reads render.md; a typo that makes a
    set vanish from that file, with the reason only in the log, is unreadable."""
    data, md, _ = _skipping(tmp_path, background="frames/z/nope.jpg")
    assert "## Set 1" in md and "水浸し！" in md  # heading and copy still there
    assert "not rendered" in md.lower()
    assert "frames/z/nope.jpg" in md
    assert '<img src="thumbnails/set1.jpg"' not in md
    assert [r["set"] for r in data["renders"]] == [2]


def test_the_skipped_sets_are_recorded_in_render_json(tmp_path):
    data, _, _ = _skipping(tmp_path, background="frames/z/nope.jpg")
    assert data["skipped"] == [{"set": 1, "reason": "background not found: frames/z/nope.jpg"}]


def test_a_set_with_no_background_anywhere_says_that_instead(tmp_path):
    """Nothing named and nothing on the shortlist is a different problem from
    a path that is wrong, and the fix is a different one."""
    data, md, _ = _run(tmp_path, publish={**PUBLISH, "thumbnails": []}, frames=())
    assert data["renders"] == []
    assert [e["set"] for e in data["skipped"]] == [1, 2]
    assert "no background" in md.lower()
    assert "## Set 2" in md and "穴あけ不要。" in md


def test_a_failed_magick_call_is_reported_in_the_contact_sheet(tmp_path, caplog):
    def run(cmd):
        if cmd[-1] == "info:":
            return FakeRun()(cmd)
        raise OSError("magick: unable to write")

    data, md, _ = _run(tmp_path, run=run)
    assert data["renders"] == []
    assert [e["set"] for e in data["skipped"]] == [1, 2]
    assert "not rendered" in md.lower()
    assert "magick" in md.lower()


def test_non_latin_copy_with_no_fonts_configured_says_so_in_the_contact_sheet(tmp_path):
    """ImageMagick's default face has no CJK glyphs and renders them as
    nothing at all -- silently. The one run this actually happened on took a
    person to notice; the file they were reading should have said it."""
    cfg = {"render": {**CFG["render"], "fonts": {}}}
    data, md, _ = _run(tmp_path, cfg)
    assert data["renders"], "the sets still render; this is a warning, not a skip"
    assert "render.fonts" in md
    assert "draws nothing at all" in md


def test_latin_only_copy_with_no_fonts_says_nothing(tmp_path):
    """The default face can draw it, so there is nothing to warn about."""
    publish = json.loads(json.dumps(PUBLISH))
    publish["thumbnail_copy"] = [{"lines": [{"role": "hook", "text": "plain ascii"}]}]
    cfg = {"render": {**CFG["render"], "fonts": {}}}
    _, md, _ = _run(tmp_path, cfg, publish=publish)
    assert "render.fonts" not in md


def test_configured_fonts_mean_no_warning(tmp_path):
    cfg = {"render": {**CFG["render"], "fonts": {"sans-bold": "Noto-Sans-CJK-JP-Bold"}}}
    _, md, _ = _run(tmp_path, cfg)
    assert "render.fonts" not in md


def test_the_render_package_never_reaches_for_the_llm():
    """Zero calls by construction, not by configuration.

    Every provider in this repo goes through ``llm_client.call_llm`` (a hard
    constraint in AGENTS.md), so a stage that names neither cannot call a
    model -- and a future edit that tries to has to trip this first.
    """
    offenders = []
    for path in sorted(Path("src/nagare_clip/render").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
            offenders += [f"{path}: imports {n}" for n in names if "llm_client" in n]
        if "call_llm" in source:
            offenders.append(f"{path}: mentions call_llm")
    assert offenders == []


def test_the_stage_runs_without_ever_loading_the_transport(tmp_path):
    """The static guard above, confirmed on the real code path.

    A fresh interpreter, because other tests import the whole pipeline and
    would leave ``llm_client`` in ``sys.modules`` either way.
    """
    publish_dir, render_dir = _project(tmp_path)
    script = textwrap.dedent(
        r"""
        import json, sys
        from pathlib import Path
        from nagare_clip.render.run import run_render

        publish_dir, render_dir = Path(sys.argv[1]), Path(sys.argv[2])
        run_render(
            publish_dir / "publish.json",
            render_dir / "render.json",
            {"render": {"enabled": True, "width": 1280, "height": 720, "line_gap": 12}},
            markdown=render_dir / "render.md",
            run=lambda cmd: "300 90\n" * sum(a.startswith("label:") for a in cmd),
        )
        assert json.loads((render_dir / "render.json").read_text())["renders"], "no renders"
        assert "nagare_clip.llm_client" not in sys.modules, sorted(sys.modules)
        """
    )
    subprocess.run([sys.executable, "-c", script, str(publish_dir), str(render_dir)], check=True)


# --- the zero-call loop, end to end ------------------------------------------


def _four_set_project(tmp_path):
    """A publish dir with four copy sets and two real stills."""
    publish_dir = tmp_path / "publish"
    (publish_dir / "frames" / "a").mkdir(parents=True)
    for name, gradient in (("9.622.jpg", "navy-skyblue"), ("55.660.jpg", "darkred-orange")):
        subprocess.run(
            [
                "magick",
                "-size",
                "640x360",
                f"gradient:{gradient}",
                str(publish_dir / "frames/a" / name),
            ],
            check=True,
        )
    publish = {
        **PUBLISH,
        "thumbnail_copy": [
            # Identical copy on purpose: then any difference between the four
            # images is the preset, and byte-identity below cannot be a
            # coincidence of the text.
            {"lines": [{"role": "hook", "text": "hook"}], "background": ""}
            for _ in range(4)
        ],
        "thumbnails": [
            {
                "stem": "a",
                "source_time": 9.622,
                "timeline_time": 9.6,
                "kind": "overlay",
                "label": "l",
                "path": "frames/a/9.622.jpg",
            }
        ],
    }
    (publish_dir / "publish.json").write_text(
        json.dumps(publish, ensure_ascii=False), encoding="utf-8"
    )
    return publish_dir, tmp_path / "render"


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_hand_editing_one_background_changes_exactly_one_thumbnail(tmp_path):
    """The acceptance constraint this whole stage split exists for.

    Edit one set's background in publish.json, re-run render: that thumbnail
    changes, the other three come back byte-identical, and no model is called.
    """
    publish_dir, render_dir = _four_set_project(tmp_path)
    args = (publish_dir / "publish.json", render_dir / "render.json", CFG)
    render_run.run_render(*args, run=run_magick, markdown=render_dir / "render.md")
    before = {p.name: p.read_bytes() for p in sorted((render_dir / "thumbnails").iterdir())}
    assert len(before) == 4

    data = json.loads((publish_dir / "publish.json").read_text(encoding="utf-8"))
    data["thumbnail_copy"][1]["background"] = "frames/a/55.660.jpg"
    (publish_dir / "publish.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )

    render_run.run_render(*args, run=run_magick, markdown=render_dir / "render.md")
    after = {p.name: p.read_bytes() for p in sorted((render_dir / "thumbnails").iterdir())}

    assert after["set2.jpg"] != before["set2.jpg"], "the edited set did not change"
    for name in ("set1.jpg", "set3.jpg", "set4.jpg"):
        assert after[name] == before[name], f"{name} changed and should not have"

    manifest = json.loads((render_dir / "render.json").read_text(encoding="utf-8"))
    assert [r["background"] for r in manifest["renders"]] == [
        "frames/a/9.622.jpg",
        "frames/a/55.660.jpg",
        "frames/a/9.622.jpg",
        "frames/a/9.622.jpg",
    ]


@pytest.mark.skipif(not HAS_MAGICK, reason="ImageMagick not installed")
def test_a_project_naming_no_background_renders_the_same_twice(tmp_path):
    """Line-count and preset variation survive, and rendering is deterministic
    -- which is what makes the comparison above mean anything."""
    publish_dir, render_dir = _four_set_project(tmp_path)
    args = (publish_dir / "publish.json", render_dir / "render.json", CFG)
    render_run.run_render(*args, run=run_magick)
    first = {p.name: p.read_bytes() for p in sorted((render_dir / "thumbnails").iterdir())}
    render_run.run_render(*args, run=run_magick)
    second = {p.name: p.read_bytes() for p in sorted((render_dir / "thumbnails").iterdir())}
    assert first == second
    # identical copy, identical background: the four images differ only because
    # preset_for() is round-robin, which is what keeps four options looking
    # like four options.
    assert len(set(first.values())) == 4
