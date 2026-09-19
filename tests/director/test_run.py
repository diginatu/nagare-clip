"""director.run: disabled no-op and enabled op-generation paths."""

from __future__ import annotations

import json

import nagare_clip.director.run as director_run
from nagare_clip.director.context import Neighbour
from nagare_clip.director.director_llm import DirectorResult, generate_director_ops, ops_to_dict
from nagare_clip.order import Segment


def test_disabled_writes_empty_ops(tmp_path):
    """When director.enabled is False, write empty ops."""
    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あ\nい\n", encoding="utf-8")
    cfg = {"director": {"enabled": False}}
    result = director_run.run_director(edits_txt=edits, cfg=cfg, segment=Segment("clip", None))

    assert result.ops == []
    assert result.ok is True


def test_enabled_writes_parsed_ops(monkeypatch, tmp_path):
    """When director.enabled is True, generate and write ops."""

    def fake_llm(_messages, _cfg):
        return '{"ops": [{"type": "cut", "lines": [1, 2], "note": "boring"}]}'

    monkeypatch.setattr(
        director_run,
        "generate_director_ops",
        lambda lines, c, overview_context="", **kw: generate_director_ops(
            lines, c, call_llm=fake_llm, overview_context=overview_context
        ),
    )

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = {"director": {"enabled": True}}
    result = director_run.run_director(edits_txt=edits, cfg=cfg, segment=Segment("clip", None))

    assert ops_to_dict(result.ops)["ops"] == [{"type": "cut", "lines": [1, 2], "note": "boring"}]


def test_overview_context_injected_for_stem(monkeypatch, tmp_path):
    """When summary and plan are provided, build overview context and pass it to generate_director_ops."""
    # summary + plan artifacts referencing stem "clip"
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "summary": "Project overview text",
                "parts": [{"stem": "clip", "lines": [1, 2], "summary": "the part"}],
            }
        ),
        encoding="utf-8",
    )
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps({"directions": [{"stem": "clip", "lines": [1, 2], "direction": "keep tight"}]}),
        encoding="utf-8",
    )

    captured = {}

    def fake(lines, c, overview_context="", **kw):
        captured["ctx"] = overview_context
        return DirectorResult([])

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        cfg=cfg,
        summary=summary,
        plan=plan,
        segment=Segment("clip", None),
    )

    assert "Project overview text" in captured["ctx"]
    assert "the part" in captured["ctx"]
    assert "keep tight" in captured["ctx"]


def test_json_passes_seg_times(monkeypatch, tmp_path):
    """When --json is provided, extract segment times and pass to generate_director_ops."""
    js = tmp_path / "clip.json"
    js.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 1.0, "end": 3.0, "text": "あい"},
                    {"start": 4.0, "end": 6.5, "text": "うえ"},
                ]
            }
        ),
        encoding="utf-8",
    )

    captured = {}

    def fake(lines, c, overview_context="", seg_times=None, **kw):
        captured["seg_times"] = seg_times
        return DirectorResult([])

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = {"director": {"enabled": True}}
    director_run.run_director(
        edits_txt=edits,
        cfg=cfg,
        json_path=js,
        segment=Segment("clip", None),
    )

    assert captured["seg_times"] == [(1.0, 3.0), (4.0, 6.5)]


def test_missing_json_passes_none_seg_times(monkeypatch, tmp_path):
    """When --json is not provided or missing, pass None for seg_times."""
    captured = {}

    def fake(lines, c, overview_context="", seg_times=None, **kw):
        captured["seg_times"] = seg_times
        return DirectorResult([])

    monkeypatch.setattr(director_run, "generate_director_ops", fake)

    edits = tmp_path / "clip_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = {"director": {"enabled": True}}
    director_run.run_director(edits_txt=edits, cfg=cfg, segment=Segment("clip", None))

    assert captured["seg_times"] is None


def test_run_director_annotates_the_transcript_from_the_gaps_file(tmp_path, monkeypatch):
    import nagare_clip.director.director_llm as dl

    edits = tmp_path / "a_edits.txt"
    edits.write_text("いち\nに\n", encoding="utf-8")
    jsonp = tmp_path / "a.json"
    jsonp.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 10.0}, {"start": 20.0, "end": 25.0}]}),
        encoding="utf-8",
    )
    gapsp = tmp_path / "a_gaps.json"
    gapsp.write_text(
        json.dumps(
            {"gaps": [{"start": 10.0, "end": 20.0, "frames": [], "description": "デモが動く"}]}
        ),
        encoding="utf-8",
    )

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        segment=Segment("a", None),
        json_path=jsonp,
        gaps=gapsp,
    )
    assert "[silent gap: デモが動く]" in seen["user"]


def test_run_director_without_a_gaps_file_is_unchanged(tmp_path, monkeypatch):
    import nagare_clip.director.director_llm as dl

    edits = tmp_path / "a_edits.txt"
    edits.write_text("いち\nに\n", encoding="utf-8")

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        segment=Segment("a", None),
        gaps=tmp_path / "missing_gaps.json",
    )
    assert seen["user"] == "1: いち\n2: に"


def test_run_director_reads_cuts_txt_for_silence_brackets(tmp_path, monkeypatch):
    """cuts_txt spans inside a segment must surface as 'Ns speech, Ms silence'
    in the LLM user content."""
    import nagare_clip.director.director_llm as dl
    from nagare_clip.audio_silence.cuts_file import write_cuts

    edits = tmp_path / "v_edits.txt"
    edits.write_text("hello\n", encoding="utf-8")
    jsn = tmp_path / "v.json"
    jsn.write_text(
        json.dumps({"segments": [{"start": 0.0, "end": 75.8, "text": "hello"}]}),
        encoding="utf-8",
    )
    cuts = tmp_path / "v_cuts.txt"
    write_cuts(cuts, [(10.0, 72.9)])

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        {"director": {"enabled": True, "prompt": "p", "max_retries": 0}},
        segment=Segment("v", None),
        json_path=jsn,
        cuts_txt=cuts,
    )
    assert "speech" in seen["user"] and "silence" in seen["user"]


def test_project_brief_appended_to_director_prompt(monkeypatch, tmp_path):
    """The brief precedes the summary/plan overview block in the system prompt."""
    from nagare_clip.director import director_llm as dl

    edits = tmp_path / "v_edits.txt"
    edits.write_text("あ\n", encoding="utf-8")

    seen: dict = {}

    def fake_llm(messages, cfg):
        seen["system"] = messages[0]["content"]
        return '{"ops": []}'

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    director_run.run_director(
        edits,
        {
            "director": {"enabled": True, "prompt": "P", "max_retries": 0},
            "project": {"audience": "DIY viewers"},
        },
        segment=Segment("v", None),
    )
    assert seen["system"] == (
        "P\n\nEditorial brief (applies to the whole project; follow it when deciding "
        "what to keep, cut, tighten and emphasise):\n- Audience: DIY viewers"
    )

    seen.clear()
    director_run.run_director(
        edits,
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        segment=Segment("v", None),
    )
    assert seen["system"] == "P"


# --- timeline position + captions already committed upstream ------------------


def _summary_file(tmp_path, stems=("a", "b", "c")):
    path = tmp_path / "summary.json"
    path.write_text(
        json.dumps(
            {
                "summary": "Project overview text",
                "parts": [{"stem": s, "lines": [1, 2], "summary": f"part of {s}"} for s in stems],
            }
        ),
        encoding="utf-8",
    )
    return path


def _director_file(tmp_path, stem, *ops):
    path = tmp_path / f"{stem}_director.json"
    path.write_text(json.dumps({"ops": list(ops)}), encoding="utf-8")
    return path


def _capture_ctx(monkeypatch):
    captured = {}

    def fake(lines, c, overview_context="", **kw):
        captured["ctx"] = overview_context
        return DirectorResult([])

    monkeypatch.setattr(director_run, "generate_director_ops", fake)
    return captured


def _run(tmp_path, cfg_extra=None, **kwargs):
    edits = tmp_path / "b_edits.txt"
    edits.write_text("あい\nうえ\n", encoding="utf-8")
    cfg = {"director": {"enabled": True, **(cfg_extra or {})}}
    director_run.run_director(
        edits_txt=edits,
        cfg=cfg,
        summary=_summary_file(tmp_path),
        segment=Segment("b", None),
        **kwargs,
    )


def test_all_stems_puts_the_video_in_the_finished_timeline(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    _run(tmp_path, all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)])
    assert 'This segment ("b") — segment 2 of 3:' in captured["ctx"]
    assert "Earlier in the finished video (already edited):\n- 1. a" in captured["ctx"]


def test_the_captions_already_shown_reach_the_context(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    _run(
        tmp_path,
        all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        prior_captions=["前回の装置", "配管作業"],
    )
    assert (
        "Captions already shown earlier in the finished video:\n- 前回の装置\n- 配管作業"
        in captured["ctx"]
    )


def test_no_prior_captions_renders_no_caption_block(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    _run(
        tmp_path,
        all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        prior_captions=[],
    )
    assert "Captions already shown" not in captured["ctx"]


def test_max_prior_captions_config_caps_the_list(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    _run(
        tmp_path,
        cfg_extra={"max_prior_captions": 1},
        all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        prior_captions=["古い", "新しい"],
    )
    assert "- 新しい" in captured["ctx"]
    assert "- 古い" not in captured["ctx"]


# --- seam context: the neighbouring videos' lines at the two joins ------------


def _edits_file(tmp_path, stem, *lines):
    path = tmp_path / f"{stem}_edits.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_the_neighbours_lines_reach_the_context(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    before = _edits_file(tmp_path, "a", "また続きになります", "今日は終わりじゃあねー")
    after = _edits_file(tmp_path, "c", "こんにちはデジナです", "紹介していきます")
    _run(
        tmp_path,
        all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        before=Neighbour(Segment("a", None), before),
        after=Neighbour(Segment("c", None), after),
    )
    assert (
        "Immediately BEFORE this segment in the finished video (a, its last lines):"
        in (captured["ctx"])
    )
    assert "- 今日は終わりじゃあねー" in captured["ctx"]
    assert "Immediately AFTER this segment (c, its first lines):" in captured["ctx"]
    assert "- こんにちはデジナです" in captured["ctx"]


def test_seam_lines_defaults_to_three(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    before = _edits_file(tmp_path, "a", "1行目", "2行目", "3行目", "4行目")
    _run(
        tmp_path,
        all_segments=[Segment("a", None), Segment("b", None)],
        before=Neighbour(Segment("a", None), before),
    )
    assert "1行目" not in captured["ctx"]
    assert "- 2行目" in captured["ctx"]
    assert "- 4行目" in captured["ctx"]


def test_seam_lines_config_sets_how_many(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    before = _edits_file(tmp_path, "a", "古い行", "最後の行")
    _run(
        tmp_path,
        cfg_extra={"seam_lines": 1},
        all_segments=[Segment("a", None), Segment("b", None)],
        before=Neighbour(Segment("a", None), before),
    )
    assert "- 最後の行" in captured["ctx"]
    assert "古い行" not in captured["ctx"]


def test_seam_lines_zero_disables_the_block(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    before = _edits_file(tmp_path, "a", "じゃあねー")
    _run(
        tmp_path,
        cfg_extra={"seam_lines": 0},
        all_segments=[Segment("a", None), Segment("b", None)],
        before=Neighbour(Segment("a", None), before),
    )
    assert "Immediately BEFORE" not in captured["ctx"]


def test_a_missing_neighbour_file_degrades_to_nothing(monkeypatch, tmp_path):
    """A single-source re-run may have no neighbour on disk; that must not fail."""
    captured = _capture_ctx(monkeypatch)
    after = _edits_file(tmp_path, "c", "つづきです")
    _run(
        tmp_path,
        all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)],
        before=Neighbour(Segment("a", None), tmp_path / "nope_edits.txt"),
        after=Neighbour(Segment("c", None), after),
    )
    assert "Immediately BEFORE" not in captured["ctx"]
    assert "- つづきです" in captured["ctx"]


def test_no_neighbours_renders_no_seam_block(monkeypatch, tmp_path):
    captured = _capture_ctx(monkeypatch)
    _run(tmp_path, all_segments=[Segment("a", None), Segment("b", None), Segment("c", None)])
    assert "Immediately" not in captured["ctx"]
