"""``python -m nagare_clip.director.preview_cli``: previews existing result sets."""

from __future__ import annotations

import json

import pytest
import yaml

from nagare_clip.director.preview_cli import main

TIMES = {
    "a": [(0.0, 2.0), (30.0, 34.0), (35.0, 40.0)],
    "b": [(0.0, 3.0), (4.0, 6.0)],
}


def _ops(*ops):
    return json.dumps({"ops": list(ops)}, ensure_ascii=False)


@pytest.fixture
def project(tmp_path):
    out = tmp_path / "out"
    (tmp_path / "in").mkdir()
    for sub in ("text_filter", "sentence_split", "director"):
        (out / sub).mkdir(parents=True)
    for stem, times in TIMES.items():
        (tmp_path / "in" / f"{stem}.mp4").touch()
        (out / "text_filter" / f"{stem}_edits.txt").write_text(
            "\n".join(f"{stem}の{i}行目" for i in range(1, len(times) + 1)) + "\n",
            encoding="utf-8",
        )
        (out / "sentence_split" / f"{stem}.json").write_text(
            json.dumps({"segments": [{"start": s, "end": e} for s, e in times]}),
            encoding="utf-8",
        )
    (out / "director" / "a_director.json").write_text(
        _ops({"type": "timelapse", "lines": [1, 1], "factor": 4.0, "text": "待つ"}),
        encoding="utf-8",
    )
    config = tmp_path / "nagare_config.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "pipeline": {
                    "input_videos_dir": str(tmp_path / "in"),
                    "output_dir": str(out),
                },
                "director": {"max_keep_lines": 1},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run(capsys, *argv):
    assert main(list(argv)) == 0
    return capsys.readouterr().out


def _listing(root):
    return sorted((p.relative_to(root), p.stat().st_mtime_ns) for p in root.rglob("*"))


def test_every_segment_in_playback_order(project, capsys):
    out = _run(capsys, "--config", str(project / "nagare_config.yml"))
    assert out.index("=== [1] a ===") < out.index("=== [2] b ===")
    assert "timelapse [1,1] x4.0 「待つ」" in out
    assert "the silence after line 1 is outside this op — dropped" in out
    # b has no ops on disk; it still plays, and the whole video adds up.
    assert "no b_director.json" in out
    assert "segment [2]: default 5.0 s → with these ops 5.0 s" in out
    assert "whole video (estimate): 5.0 s elsewhere + 9.5 s here = 14.5 s" in out


def test_writes_nothing(project, capsys):
    before = _listing(project)
    _run(capsys, "--config", str(project / "nagare_config.yml"))
    assert _listing(project) == before


def test_source_narrows_what_is_printed_not_the_estimate(project, capsys):
    out = _run(capsys, "--config", str(project / "nagare_config.yml"), "--source", "b.mp4")
    assert "=== [1] a ===" not in out and "=== [2] b ===" in out
    assert "whole video (estimate): 9.5 s elsewhere + 5.0 s here = 14.5 s" in out


def test_another_director_dir(project, capsys):
    old = project / "old"
    old.mkdir()
    (old / "a_director.json").write_text(_ops({"type": "cut", "lines": [2, 2]}), encoding="utf-8")
    out = _run(capsys, "--config", str(project / "nagare_config.yml"), "--director-dir", str(old))
    assert "cut [2,2]" in out and "timelapse" not in out


def test_parser_drops_come_from_the_llm_report(project, capsys):
    reports = project / "reports"
    reports.mkdir()
    response = _ops(
        {"type": "timelapse", "lines": [1, 1], "factor": 4.0, "text": "待つ"},
        {"type": "keep", "lines": [2, 3]},
    )
    (reports / "a.md").write_text(
        f"# director — a\n\n## Attempt 1/1\n\n### Response\n```\n{response}\n```\n"
        "**Tokens:** prompt 1\n",
        encoding="utf-8",
    )
    out = _run(capsys, "--config", str(project / "nagare_config.yml"), "--report-dir", str(reports))
    assert (
        "dropped by the parser (no effect): keep op spans 2 lines [2, 3] > max_keep_lines=1" in out
    )
    assert "no LLM report for b" in out


def test_brackets_are_priced_with_the_projects_intervals_settings(project, capsys, monkeypatch):
    # The preview must price a line as the stage would: same intervals: section.
    import nagare_clip.director.preview_cli as cli

    config = project / "nagare_config.yml"
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["intervals"] = {"keep_pre_margin": 0.123}
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    seen = []
    real = cli.load_segment_transcript

    def spy(inputs):
        seen.append(inputs)
        return real(inputs)

    monkeypatch.setattr(cli, "load_segment_transcript", spy)
    _run(capsys, "--config", str(config))
    assert seen
    assert all(i.intervals_cfg["keep_pre_margin"] == 0.123 for i in seen)
