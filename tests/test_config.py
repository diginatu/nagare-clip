"""Tests for the centralised config module."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from nagare_clip.config import (
    DEFAULTS,
    deep_merge,
    generate_example_yaml,
    get_effective_config,
    load_config,
)


def _leaf_paths(d: dict[str, Any], prefix: str = "") -> Iterator[str]:
    """Yield the dotted path of every non-dict leaf in *d*."""
    for key, value in d.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            yield from _leaf_paths(value, path + ".")
        else:
            yield path


def _top_section_lines(text: str, top: str) -> list[str]:
    """Lines of the ``top:`` top-level section (until the next column-0 key or EOF)."""
    lines = text.splitlines()
    start = next(
        (i for i, ln in enumerate(lines) if re.match(rf"^{re.escape(top)}\s*:", ln)),
        None,
    )
    if start is None:
        return []
    end = next(
        (j for j in range(start + 1, len(lines)) if re.match(r"^\w+\s*:", lines[j])),
        len(lines),
    )
    return lines[start:end]


def test_example_file_matches_generator():
    """config.example.yml must be exactly what the generator emits (regenerate
    with `make config-example` to fix drift)."""
    on_disk = Path("config.example.yml").read_text(encoding="utf-8")
    assert generate_example_yaml() == on_disk


def test_generated_example_is_valid_yaml_covering_all_defaults():
    """Every DEFAULTS leaf appears in the generated example as a real key or a
    commented `# leaf:` line in its own top-level section."""
    text = generate_example_yaml()
    data = yaml.safe_load(text) or {}

    def has_real(path: str) -> bool:
        cur: Any = data
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return False
            cur = cur[part]
        return True

    for path in _leaf_paths(DEFAULTS):
        if has_real(path):
            continue
        top, leaf = path.split(".")[0], path.split(".")[-1]
        block = _top_section_lines(text, top)
        assert any(re.match(rf"^\s*#\s*{re.escape(leaf)}\s*:", ln) for ln in block), (
            f"DEFAULTS leaf {path!r} missing from generated example section {top!r}"
        )


class TestLoadConfig:
    def test_returns_empty_for_none(self):
        assert load_config(None) == {}

    def test_reads_yaml_file(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.0}}))
        result = load_config(cfg_file)
        assert result == {"intervals": {"silence_threshold": 2.0}}

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yml")

    def test_empty_file_returns_empty(self, tmp_path: Path):
        cfg_file = tmp_path / "empty.yml"
        cfg_file.write_text("")
        assert load_config(cfg_file) == {}

    def test_non_dict_yaml_returns_empty(self, tmp_path: Path):
        cfg_file = tmp_path / "list.yml"
        cfg_file.write_text("- a\n- b\n")
        assert load_config(cfg_file) == {}


class TestDeepMerge:
    def test_basic(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        assert deep_merge(base, override) == {"a": 1, "b": 3, "c": 4}

    def test_preserves_unset_keys(self):
        base = {"a": 1, "b": {"x": 10, "y": 20}}
        override = {"b": {"x": 99}}
        result = deep_merge(base, override)
        assert result == {"a": 1, "b": {"x": 99, "y": 20}}

    def test_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        override = {"a": {"b": {"c": 99}}}
        result = deep_merge(base, override)
        assert result["a"]["b"]["c"] == 99
        assert result["a"]["b"]["d"] == 2

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        deep_merge(base, override)
        assert base["a"]["b"] == 1

    def test_override_replaces_non_dict_with_dict(self):
        base = {"a": 1}
        override = {"a": {"nested": True}}
        result = deep_merge(base, override)
        assert result == {"a": {"nested": True}}


class TestGetEffectiveConfig:
    def test_defaults_only(self):
        cfg = get_effective_config(None)
        assert cfg == DEFAULTS
        # Verify it's a copy, not the same object
        assert cfg is not DEFAULTS

    def test_config_overrides_defaults(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.5}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["intervals"]["silence_threshold"] == 2.5
        # Other defaults intact
        assert cfg["intervals"]["min_keep"] == 1.0
        assert cfg["intervals"]["caption"]["max_bunsetu"] == 12

    def test_cli_overrides_config(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": 2.5}}))
        cli = {"intervals": {"silence_threshold": 3.0}}
        cfg = get_effective_config(cfg_file, cli)
        assert cfg["intervals"]["silence_threshold"] == 3.0

    def test_full_precedence(self, tmp_path: Path):
        """CLI > config > defaults."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "intervals": {
                        "silence_threshold": 2.5,
                        "min_keep": 0.5,
                    }
                }
            )
        )
        cli = {"intervals": {"silence_threshold": 3.0}}
        cfg = get_effective_config(cfg_file, cli)
        # CLI wins
        assert cfg["intervals"]["silence_threshold"] == 3.0
        # Config wins over default
        assert cfg["intervals"]["min_keep"] == 0.5
        # Default remains
        assert cfg["intervals"]["keep_pre_margin"] == 1.0

    def test_partial_config(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"default_fps": 24.0}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["default_fps"] == 24.0
        # All other sections still have defaults
        assert cfg["intervals"]["silence_threshold"] == 1.5
        assert cfg["general"]["log_level"] == "INFO"
        assert cfg["blender"]["caption_style"]["font_size"] == 50

    def test_nested_caption_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"caption": {"max_bunsetu": 20}}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["intervals"]["caption"]["max_bunsetu"] == 20
        # Other caption defaults intact
        assert cfg["intervals"]["caption"]["max_duration"] == 4.0

    def test_caption_style_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"caption_style": {"font_size": 72}}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["caption_style"]["font_size"] == 72
        assert cfg["blender"]["caption_style"]["alignment_x"] == "CENTER"

    def test_unknown_top_level_section_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"custom_section": {"key": "value"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_typo_leaf_key_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        # 'silence_threshld' is a typo of silence_threshold
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshld": 2.0}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_wrong_type_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"intervals": {"silence_threshold": "not-a-number"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_pipeline_to_stage_valid(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"pipeline": {"to_stage": "intervals"}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["pipeline"]["to_stage"] == "intervals"

    def test_pipeline_stage_defaults_are_names(self):
        cfg = get_effective_config(None)
        assert cfg["pipeline"]["from_stage"] == "transcription"
        assert cfg["pipeline"]["to_stage"] == "publish"

    def test_transcription_language_default(self):
        cfg = get_effective_config(None)
        assert cfg["transcription"]["language"] == "ja"

    def test_transcription_language_config_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"transcription": {"language": "en"}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["transcription"]["language"] == "en"
        # Other transcription defaults intact
        assert cfg["transcription"]["compute_type"] == "float16"

    def test_retry_defaults_present(self):
        cfg = get_effective_config(None)
        s2 = cfg["text_filter"]
        assert s2["retry_on_invalid"] is True
        assert s2["retry_min_batch_size"] == 1

    def test_text_filter_keywords_default_empty(self):
        cfg = get_effective_config(None)
        assert cfg["text_filter"]["keywords"] == []
        assert "summary_llm" not in cfg["text_filter"]

    def test_text_filter_keywords_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"keywords": ["Kubernetes"]}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["text_filter"]["keywords"] == ["Kubernetes"]
        # Other text_filter defaults intact
        assert cfg["text_filter"]["batch_size"] == 10

    def test_removed_summary_llm_section_rejected(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"text_filter": {"summary_llm": {"enabled": True}}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_publish_image_markup_default_is_html(self):
        cfg = get_effective_config(None)
        assert cfg["publish"]["image_markup"] == "html"

    def test_publish_image_markup_rejects_unknown_value(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"publish": {"image_markup": "rst"}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

    def test_gap_context_context_lines_default_is_one(self):
        cfg = get_effective_config(None)
        assert cfg["gap_context"]["context_lines"] == 1

    def test_gap_context_context_lines_override(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"gap_context": {"context_lines": 4}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["gap_context"]["context_lines"] == 4
        # Independent of guided_edit's same-named knob
        assert cfg["guided_edit"]["context_lines"] == 1

    def test_gap_context_context_lines_rejects_a_negative(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"gap_context": {"context_lines": -1}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)


def test_blender_style_allows_extra_keys(tmp_path):
    """caption_style / speed_mark are open-ended Blender TextStrip pass-throughs:
    a `font` path and an arbitrary RNA attr must survive validation."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(
        yaml.dump(
            {
                "blender": {
                    "caption_style": {"font": "/abs/font.ttf", "shadow_blur": 0.5},
                    "speed_mark": {"box_margin": 0.05},
                }
            }
        )
    )
    cfg = get_effective_config(cfg_file)
    assert cfg["blender"]["caption_style"]["font"] == "/abs/font.ttf"
    assert cfg["blender"]["caption_style"]["shadow_blur"] == 0.5
    assert cfg["blender"]["speed_mark"]["box_margin"] == 0.05


def test_caption_style_new_defaults_present():
    cfg = get_effective_config(None)
    cs = cfg["blender"]["caption_style"]
    assert cs["use_shadow"] is True
    assert cs["wrap_width"] == 0.90


def test_intervals_unknown_key_rejected(tmp_path):
    """A non-open-ended section still rejects unknown keys."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"blender": {"caption": {"bogus": 1}}}))
    # blender has no 'caption' key at all -> rejected
    with pytest.raises(ValidationError):
        get_effective_config(cfg_file)


def test_speed_mark_defaults_present():
    cfg = get_effective_config(None, {})
    sm = cfg["blender"]["speed_mark"]
    assert sm["enabled"] is True
    assert sm["template"] == "x{factor}"
    assert sm["font_size"] == 35
    assert sm["alignment_x"] == "RIGHT"
    assert sm["anchor_y"] == "TOP"
    assert sm["location_x"] == 0.95
    assert sm["location_y"] == 0.95


def test_director_defaults_present():
    cfg = get_effective_config(None, {})
    d = cfg["director"]
    assert d["enabled"] is False
    assert d["provider"] == "ollama_chat"
    assert d["api_base"] == ""
    assert "model" in d
    assert d["response_format"] == "json"
    assert isinstance(d["prompt"], str) and d["prompt"]


def test_director_prompt_offers_no_speed_op():
    """Improvement 16: `speed` is off the director's menu. Three prompt rounds
    bounded the mild 1.3-2.0 band and it kept coming back (57.4% -> 1.4% ->
    49.0% -> 29.9% of the finished video), because a middle option is always
    cheaper to choose than a timelapse. Neither a bullet nor a JSON-shape
    example may offer it -- an example is what an LLM copies over the prose,
    and the parser would drop the op it teaches."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    assert not [ln for ln in prompt.splitlines() if ln.startswith("- speed:")]
    assert '"type": "speed"' not in prompt


def test_every_op_type_in_the_json_shape_is_one_the_director_may_emit():
    """Each JSON-shape example must survive the real LLM-path parser. An
    example for an off-menu type would be silently dropped at runtime while
    still teaching the director to emit it."""
    from nagare_clip.director.director_llm import MENU_TYPES, try_parse_director_response

    prompt = get_effective_config(None, {})["director"]["prompt"]
    examples = [ln.strip().rstrip(",") for ln in prompt.splitlines() if '{"type": "' in ln]
    assert len(examples) == len(MENU_TYPES)
    for example in examples:
        ops = try_parse_director_response('{"ops": [' + example + "]}", num_lines=100)
        assert ops, f"prompt example is dropped by the parser: {example}"
        assert ops[0].type in MENU_TYPES


def _timelapse_bullet() -> str:
    """The single DIRECTOR_PROMPT line describing the `timelapse` op."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    return next(ln for ln in prompt.splitlines() if ln.startswith("- timelapse:"))


def test_director_prompt_keeps_the_two_mode_choice():
    """With `speed` off the menu the listening/timelapse choice lives entirely
    in the timelapse bullet, and it must still read as a choice between two
    modes (see docs/superpowers/specs/2026-08-01-speed-two-mode-choice-design.md
    and 2026-08-09-director-no-speed-op-design.md)."""
    bullet = _timelapse_bullet().lower()
    assert "two modes" in bullet
    assert "listening" in bullet
    assert "1x" in bullet
    assert "cut the weakest parts" in bullet


def test_director_prompt_timelapse_states_its_price():
    """A timelapse loses intelligible audio; saying so is what forces an honest
    choice instead of a mild speed-up that splits the difference."""
    bullet = _timelapse_bullet().lower()
    assert "4.0" in bullet
    assert "unintelligible" in bullet


def test_director_prompt_timelapse_is_self_contained():
    """The whole point of the op: one op does the arrangement three ops used to.
    The prompt must not ask for a companion keep/speed/overlay, or the director
    will emit the ops the desugaring already creates."""
    bullet = _timelapse_bullet().lower()
    assert 'do not add a separate "keep" or "overlay"' in bullet
    assert "consecutive" in bullet  # how to change the caption partway through


def test_director_prompt_timelapse_ties_factor_to_target_runtime():
    """A flat factor instruction becomes the director's default regardless of
    wording (see improvement 11: keep width, overlay duration and the old bare
    speed factor all clustered on the one number the prompt showed), so the
    bullet states a target on-screen runtime and lets the span's own length
    set the number -- rather than showing a factor to copy."""
    bullet = _timelapse_bullet().lower()
    assert "about a minute on screen" in bullet
    assert "longer span" in bullet


def test_director_prompt_timelapse_bullet_stays_cheap_to_choose():
    """Improvement 16: the run that nearly eliminated the mild band is the run
    where this bullet was at its simplest; the two rounds that grew it (a
    target length, three worked examples, motion and repetition to weigh) both
    saw the band come back, because making the right option harder to choose
    pushes the director to the easier one. The worked-example table is gone
    and must not creep back."""
    bullet = _timelapse_bullet().lower()
    assert "8x" not in bullet and "16x" not in bullet
    assert "lookup table" not in bullet


def test_director_prompt_timelapse_example_parses_and_is_fast():
    """Pin the documented example to the real parser: a stale one (missing text,
    or a factor under the floor) would be silently dropped at runtime."""
    from nagare_clip.director.director_llm import TIMELAPSE_MIN_FACTOR, parse_director_response

    prompt = get_effective_config(None, {})["director"]["prompt"]
    example = next(
        line.strip().rstrip(",") for line in prompt.splitlines() if '"type": "timelapse"' in line
    )
    ops = parse_director_response('{"ops": [' + example + "]}", num_lines=100)
    assert len(ops) == 1
    assert ops[0].type == "timelapse"
    assert ops[0].factor is not None and ops[0].factor >= TIMELAPSE_MIN_FACTOR
    assert ops[0].text


def test_director_prompt_timing_line_does_not_offer_speed_for_long_duration():
    """The Timing paragraph must not offer speeding up as the remedy for a
    long line duration -- that contradicts the two-mode speed rule elsewhere
    in the prompt (speed is a LISTENING/TIMELAPSE choice, not a dial). A long
    speech duration should route to cutting instead; a long stretch of manual
    work remains a timelapse candidate."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    line = next(ln for ln in prompt.splitlines() if "Use these numbers to judge pacing" in ln)
    lowered = line.lower()
    assert "candidates for cutting or speeding up" not in lowered
    assert "cutting" in lowered
    assert "timelapse" in lowered


@pytest.mark.parametrize("stage", ["director", "plan"])
def test_prompt_documents_duration_and_gap_bracket(stage):
    """The director/plan inputs carry a `[4.2s, gap 0.8s]` bracket per line/part
    (rendered by timing.format_dur_gap).  The prompt must explain that notation,
    and the example it shows must be exactly what the renderer emits — otherwise
    the LLM is told to read a format we never produce."""
    from nagare_clip.timing import format_dur_gap

    prompt = get_effective_config(None, {})[stage]["prompt"]
    assert format_dur_gap(4.2, 0.8) in prompt  # "[4.2s, gap 0.8s]"
    assert format_dur_gap(4.2, None) in prompt  # "[4.2s]" — last line/part, no gap
    assert format_dur_gap(13.0, None, 62.9) in prompt  # "[13.0s speech, 62.9s silence]"
    lowered = prompt.lower()
    assert "duration" in lowered
    assert "gap" in lowered
    # A negligible gap is omitted from the bracket; the prompt must say so
    # rather than implying only the last line/part lacks a gap.
    assert "negligible" in lowered


def test_guided_edit_defaults_present():
    cfg = get_effective_config(None, {})
    g = cfg["guided_edit"]
    assert g["enabled"] is False
    assert g["provider"] == "ollama_chat"
    assert g["api_base"] == ""
    assert "model" in g
    assert isinstance(g["prompt"], str) and g["prompt"]


def test_director_override_keeps_other_defaults(tmp_path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"director": {"enabled": True, "model": "gpt-oss:120b"}}))
    cfg = get_effective_config(cfg_file)
    assert cfg["director"]["enabled"] is True
    assert cfg["director"]["model"] == "gpt-oss:120b"
    assert cfg["director"]["temperature"] == 0.2


def test_llm_report_defaults():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    assert cfg["general"]["llm_report"] is True
    assert cfg["general"]["llm_report_dir"] == "output/llm_report"


def test_llm_sections_default_provider_and_empty_api_base():
    from nagare_clip.config import DEFAULTS

    sections = [
        DEFAULTS["text_filter"],
        DEFAULTS["summary"],
        DEFAULTS["plan"],
        DEFAULTS["director"],
        DEFAULTS["guided_edit"],
    ]
    for sec in sections:
        assert sec["provider"] == "ollama_chat"
        assert sec["api_base"] == ""


def test_general_langfuse_defaults_true():
    cfg = get_effective_config(None, {})
    assert cfg["general"]["langfuse"] is True


def test_sentence_split_defaults_present():
    from nagare_clip.config import get_effective_config

    cfg = get_effective_config(None, {})
    sp = cfg["sentence_split"]
    assert sp["enabled"] is False
    assert sp["window_segments"] == 20
    assert sp["max_retries"] == 2
    assert sp["response_format"] == "json"
    assert isinstance(sp["prompt"], str) and sp["prompt"]


def test_section_named_env_var_does_not_override_default(monkeypatch):
    """A top-level section name as an env var must NOT leak into config
    (no environment config source; precedence is CLI > file > defaults only)."""
    monkeypatch.setenv("SUMMARY", '{"model":"LEAKED"}')
    cfg = get_effective_config(None)
    assert cfg["summary"]["model"] == "gpt-oss:120b"


def test_scalar_env_var_colliding_with_section_does_not_crash(monkeypatch):
    """A non-JSON env var whose name collides with a section must not make
    get_effective_config raise."""
    monkeypatch.setenv("PLAN", "short")
    cfg = get_effective_config(None)  # must not raise
    assert cfg["plan"]["enabled"] is False


def test_gap_context_defaults():
    cfg = get_effective_config(None, {})
    g = cfg["gap_context"]
    assert g["enabled"] is False
    assert g["min_gap"] == 3.0
    assert g["frame_width"] == 960
    assert g["max_retries"] == 2
    assert g["provider"] == "ollama_chat"
    assert g["prompt"]


def test_gap_context_rejects_unknown_key():
    with pytest.raises(ValidationError):
        get_effective_config(None, {"gap_context": {"nonesuch": 1}})


def test_gap_context_prompt_documents_the_markers_describe_parses():
    # describe.describe_gap parses a leading STATIC:/ACTION: marker off the
    # vision reply; the default prompt must instruct the model to emit them.
    cfg = get_effective_config(None, {})
    prompt = cfg["gap_context"]["prompt"]
    assert "STATIC:" in prompt
    assert "ACTION:" in prompt


def test_director_prompt_documents_gap_annotations():
    # The director must be told what the `[silent gap ...]` annotation line means
    # and that a `keep` op spanning the adjacent lines rescues the moment.
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    assert "[silent gap" in prompt
    assert "keep" in prompt


def test_director_prompt_gap_example_matches_the_real_formatter():
    """The DIRECTOR_PROMPT's documented example line must be exactly what
    gap_context.context.annotate_numbered_transcript renders for the
    corresponding Gap -- pins the doc example to the real formatter so a
    rendering change (indent, decimal places, wording) fails loudly here
    instead of silently going stale in the prompt."""
    from nagare_clip.gap_context.context import annotate_numbered_transcript
    from nagare_clip.gap_context.gaps import Gap

    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]

    gap = Gap(start=0.0, end=12.4, frames=[], description="a build runs and logs scroll past")
    rendered = annotate_numbered_transcript("1: x", [(1, gap)])
    annotation_line = rendered.split("\n")[1]

    assert annotation_line == "    [silent gap 12.4s: a build runs and logs scroll past]"
    assert annotation_line in prompt


def test_director_prompt_overlay_example_carries_a_duration():
    """The prompt's overlay example must satisfy the real parser: an overlay
    op without a positive duration is dropped, so a stale example would teach
    the director to emit ops that never reach the timeline."""
    from nagare_clip.director.director_llm import parse_director_response

    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    example = next(
        line.strip().rstrip(",") for line in prompt.splitlines() if '"type": "overlay"' in line
    )
    ops = parse_director_response('{"ops": [' + example + "]}", num_lines=10)
    assert len(ops) == 1
    assert ops[0].type == "overlay"
    assert ops[0].duration is not None and ops[0].duration > 0


def test_director_prompt_frames_goal_as_tighten_and_stage():
    """The prompt's opening framing must ask for staging, not just removal --
    otherwise the director has no license to add overlays or keep a moment
    (see docs/superpowers/specs/2026-07-28-director-prompt-stage-not-trim-design.md)."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    opening = prompt.split("\n\n")[0].lower()
    assert "tighten" in opening
    assert "stage" in opening


def test_director_prompt_treats_long_gap_as_keep_candidate_when_speech_announces_event():
    """A long gap must not be an unconditional 'dead air' rule -- the prompt
    must tell the director to read the surrounding speech and keep a gap that
    the speech says is meaningful (an accident, a cleanup, a wait for a result)."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"].lower()
    assert "fallback" in prompt
    assert "accident" in prompt
    assert "watchable moment" in prompt


def test_director_prompt_prefers_timelapse_for_visible_work_reserves_cut_for_digressions():
    """Repetition that is VISIBLE WORK should be timelapsed rather than deleted --
    the tighten-rather-than-delete instinct from the 2026-07-28 rewrite -- while
    cut stays reserved for spans that leave the throughline entirely."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"].lower()
    assert "payoff" in prompt
    assert "throughline" in prompt
    assert "buildup" in prompt
    assert "visible work" in prompt


def test_director_prompt_does_not_offer_a_fast_option_for_repeated_speech():
    """The prefer-timelapse-over-cut paragraph must route repeated SPEECH to 1x
    or a cut.  Left ungated it reads as a licence to shave talking with a mild
    speed-up, which is how a real run put 57% of the finished video under speed
    with 85% of that footage carrying captions.  It must not name `speed`
    either: with the op off the menu, mentioning it is naming an option that no
    longer exists."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    # The paragraph is one prompt line ending in ":" above the bullet list --
    # select it alone, so a bullet's own wording cannot satisfy these for it.
    para = next(ln for ln in prompt.splitlines() if "Prefer a timelapse over a cut" in ln).lower()
    assert "speech" in para
    assert "speed" not in para
    assert "1x" in para
    assert "cut the weakest passes" in para


def test_director_prompt_documents_overlay_density_target():
    """Overlay needs a sense of when/how often -- a loose numeric target that
    an editorial brief (future feature) can override, not a hard rule."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    assert "3-5 minutes" in prompt
    assert "editorial brief" in prompt.lower()


def test_plan_prompt_never_offers_keep_as_a_direction_word():
    """`keep` means two different things one stage apart: "this part earns its
    place" (plan) vs. "restore every silence in this range" (a director op).
    plan.json is fed to the director as context, so the director copies the word
    across and pays the mechanical price.  The plan vocabulary must not contain
    it — every occurrence in the prompt must be the rule forbidding it."""
    cfg = get_effective_config(None, {})
    prompt = cfg["plan"]["prompt"]

    assert "feature" in prompt  # the editorial-sense replacement
    assert 'Never use the word "keep" in a direction' in prompt

    # Drop the rule (a single line) — no other line may mention the word.
    remainder = [line for line in prompt.splitlines() if 'Never use the word "keep"' not in line]
    assert not [line for line in remainder if "keep" in line.lower()]


def test_plan_prompt_example_directions_are_not_director_op_names():
    """The plan's own JSON example is what the LLM imitates most closely."""
    from nagare_clip.director.director_llm import VALID_TYPES

    cfg = get_effective_config(None, {})
    examples = [line for line in cfg["plan"]["prompt"].splitlines() if '"direction":' in line]
    assert examples
    for line in examples:
        verb = line.split('"direction": "')[1].split(" ")[0].strip('",')
        assert verb not in VALID_TYPES, f"plan example uses director op name {verb!r}"


def test_director_prompt_scales_keep_width_to_what_is_on_screen():
    """A keep's width must follow what is on screen: narrow to rescue one silent
    gap, but wide enough to carry a continuous event (an accident, a cleanup)
    without chopping the payoff into jump cuts.  Widening one to mark *talking*
    as important stays forbidden — that abuse took a 22.3-minute cut to 53.9."""
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    # Rescuing one gap keeps its narrow example...
    assert "narrowest range" in prompt
    assert "[N, N+1]" in prompt
    # ...but a continuous event may be spanned whole.
    assert "WHOLE event" in prompt
    assert "jump cuts" in prompt
    # Talking is never a reason to widen one.
    assert "Never widen a keep to mark talking as important" in prompt
    # And a plan-side "feature/retain" direction must not be read as a keep op.
    assert "editorial emphasis, NOT a " in prompt


def test_director_prompt_lets_a_described_action_span_its_whole_run():
    """The visual-context paragraph is where a described gap's keep width is
    decided; ending every one at [N, N+1] is what lost the water-spill cleanup."""
    cfg = get_effective_config(None, {})
    assert "continues across several gaps" in cfg["director"]["prompt"]


def test_director_max_keep_lines_default():
    cfg = get_effective_config(None, {})
    assert cfg["director"]["max_keep_lines"] == 8


def test_director_max_prior_captions_default():
    """How many of the captions already shown earlier in the finished video the
    director is handed. Generous by default — a modern context window swallows
    100 short lines — but tunable for long projects."""
    cfg = get_effective_config(None, {})
    assert cfg["director"]["max_prior_captions"] == 100


def test_text_filter_prompt_repeated_phrase_example_is_valid_patch_syntax():
    """Real-run failure mode: told to de-duplicate repeated phrases but shown
    no marker example, the filter LLM rewrites the line bare and the safety
    check drops it.  The default prompt must state the rule AND show a worked
    marker example — and the example must round-trip through the real patch
    applier."""
    from nagare_clip.text_filter.llm_filter import apply_patches_to_lines

    cfg = get_effective_config(None, {})
    prompt = cfg["text_filter"]["prompt"]
    assert "repeated" in prompt
    assert "映ってる映ってるね" in prompt  # example input line
    assert "{{映ってる->}}映ってるね" in prompt  # example output line
    assert apply_patches_to_lines(["{{映ってる->}}映ってるね"]) == ["映ってるね"]


def test_publish_thumbnail_defaults():
    cfg = get_effective_config(None, {})
    thumb = cfg["publish"]["thumbnail"]
    assert thumb["enabled"] is True
    assert thumb["background"] == ""
    assert (thumb["width"], thumb["height"]) == (1280, 720)
    assert thumb["line_gap"] == 12
    assert thumb["fonts"] == {}


def test_publish_thumbnail_fonts_come_from_the_file(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        'publish:\n  thumbnail:\n    fonts:\n      hook: "Noto-Serif-CJK-JP-Black"\n',
        encoding="utf-8",
    )
    cfg = get_effective_config(path, {})
    assert cfg["publish"]["thumbnail"]["fonts"] == {"hook": "Noto-Serif-CJK-JP-Black"}


def test_an_unknown_thumbnail_key_is_rejected(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("publish:\n  thumbnail:\n    colour: red\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})
