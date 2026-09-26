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


def test_a_section_of_only_commented_keys_renders_as_an_empty_mapping():
    """`blender.render:` and `project:` have no uncommented keys at all. A bare
    heading followed by comments parses as null and then fails validation, which
    is why the example file could not be loaded as a config as it stood."""
    text = generate_example_yaml()
    for line in ("render: {}", "project: {}"):
        assert re.search(rf"^\s*{re.escape(line)}\s*$", text, re.M), text
    data = yaml.safe_load(text)
    assert data["blender"]["render"] == {}
    assert data["project"] == {}
    # The whole example must load as a config, unedited.
    get_effective_config(None, data)


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
        cfg_file.write_text(yaml.dump({"blender": {"proxy_size": 50}}))
        cfg = get_effective_config(cfg_file)
        assert cfg["blender"]["proxy_size"] == 50
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

    def test_render_defaults_to_empty(self):
        """Nothing under `render:` means nothing is written to the scene, so a
        project that says nothing keeps the source-derived fps/resolution."""
        cfg = get_effective_config(None)
        assert cfg["blender"]["render"] == {}

    def test_render_forwards_arbitrary_nested_keys(self, tmp_path: Path):
        """The section is an open pass-through to Blender's render RNA: keys it
        has never heard of must survive validation as plain dicts/scalars."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(
            yaml.dump(
                {
                    "blender": {
                        "render": {
                            "resolution_x": 1920,
                            "filepath": "//../renders/final.mp4",
                            "ffmpeg": {"codec": "H264", "audio_codec": "AAC"},
                        }
                    }
                }
            )
        )
        cfg = get_effective_config(cfg_file)
        render = cfg["blender"]["render"]
        assert render["resolution_x"] == 1920
        assert render["filepath"] == "//../renders/final.mp4"
        assert render["ffmpeg"] == {"codec": "H264", "audio_codec": "AAC"}

    def test_default_fps_is_gone(self, tmp_path: Path):
        """Folded into `render.fps`. A leftover key must fail loudly rather than
        be silently ignored -- it would read as a working fps setting."""
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"blender": {"default_fps": 24.0}}))
        with pytest.raises(ValidationError):
            get_effective_config(cfg_file)

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
        assert cfg["pipeline"]["to_stage"] == "render"

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

    def test_general_image_markup_default_is_html(self):
        cfg = get_effective_config(None)
        assert cfg["general"]["image_markup"] == "html"

    def test_general_image_markup_rejects_unknown_value(self, tmp_path: Path):
        cfg_file = tmp_path / "cfg.yml"
        cfg_file.write_text(yaml.dump({"general": {"image_markup": "rst"}}))
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


def test_director_prompt_asks_for_a_playback_before_settling_a_range():
    """Every symptom a real run produced was correct as a spec and wrong on
    playback: `timelapse [1,15] x5` reads fine, and plays as the speaker
    announcing that very work at 5x, unintelligible. The director used to be
    asked to reconstruct that playback in its head; the conversation computes
    it and hands it back every turn, so the rule is now to READ it — and the
    one move that makes reading it worth anything is re-sending the range."""
    prompt = get_effective_config(None, {})["director"]["prompt"].lower()
    assert "the playback you are shown" in prompt
    assert "reads fine as a spec" in prompt
    assert "re-send the range" in prompt


def test_director_prompt_states_the_no_op_baseline():
    """A playback needs a state to play FROM. Every delta was already in the
    prompt (silences dropped, cut deletes, timelapse destroys speech) but the
    ground state they are deltas to was not: "outside" appeared zero times, and
    the two mentions of 1x offered it as a CHOICE ("leave it at 1x") rather
    than as what an unmarked line already does. `speech is never dropped by
    default` existed only inside the keep bullet, as an argument against
    widening keeps."""
    prompt = get_effective_config(None, {})["director"]["prompt"].lower()
    assert "with no op at all" in prompt
    assert "outside a range" in prompt


def test_director_prompt_playback_rule_stays_out_of_the_timelapse_bullet():
    """Improvement 16 again (see test_director_prompt_timelapse_bullet_stays_
    cheap_to_choose): weight on THIS bullet pushes the director toward the
    easier option. The playback rule applies to every op's range, so it belongs
    in Rules where it costs the timelapse choice nothing. A first attempt put
    it in this bullet and grew it 1026 -> 1684 characters."""
    assert "play it back" not in _timelapse_bullet().lower()


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
    line = next(ln for ln in prompt.splitlines() if "Judge pacing from the speech figure" in ln)
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
    # The four-part form is 17.0% of the director's real brackets and 20% of the
    # plan's, yet went undocumented while `[4.2s, gap 0.8s]` -- which the legend
    # leads with -- is the rarest at 4.4%.  A shape the prompt never shows is a
    # shape the LLM has to guess at while it is being asked to judge pacing.
    assert format_dur_gap(13.0, 0.8, 62.9) in prompt  # "[13.0s speech, 62.9s silence, gap 0.8s]"
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


def test_director_prompt_documents_silence_lines_and_how_to_address_them():
    # A silence used to be the one thing in the transcript with no number, so
    # the prompt had to teach the "n~" form.  Under the whole-video numbering
    # it has a number like any other line -- and the loop REJECTS "n~" -- so
    # the prompt must teach the number and nothing else.
    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]
    assert "[silent " in prompt
    assert "53~" not in prompt
    assert "either endpoint" not in prompt
    # What acts on it: the two ops that can, named where the line is shown.
    silence = next(ln for ln in prompt.splitlines() if ln.startswith("is the wait"))
    assert "put its number in an op" in silence
    assert "keep" in silence and "timelapse" in silence


def test_director_prompt_silence_example_matches_the_real_formatter():
    """The DIRECTOR_PROMPT's documented example line must be exactly what
    director.silence_lines.SilenceLine renders -- pins the doc example to the
    real formatter so a rendering change (indent, decimal places, wording)
    fails loudly here instead of silently going stale in the prompt.

    The `[silent gap: ...]` annotation the silence line replaces for a
    between-line wait survives for silence INSIDE a line, so the whole-video
    view's prefix for it is pinned too."""
    from nagare_clip.director.display import build_display_view
    from nagare_clip.director.run import SegmentTranscript
    from nagare_clip.director.silence_lines import SilenceLine, silence_body
    from nagare_clip.gap_context.gaps import Gap
    from nagare_clip.order import Segment

    cfg = get_effective_config(None, {})
    prompt = cfg["director"]["prompt"]

    line = SilenceLine(53, 0.0, 29.9, ("a build runs and logs scroll past",))
    # The whole-video view numbers the silence, so it drops the "after line n"
    # the un-numbered transcript line carries -- and that is the body the
    # prompt's example must show.
    body = silence_body(line.duration, line.descriptions)
    assert body == "[silent 29.9s: a build runs and logs scroll past]"
    assert f"54: {body}" in prompt

    gap = Gap(start=0.0, end=12.4, frames=[], description="x")
    view = build_display_view(
        [
            (
                Segment("A", None),
                SegmentTranscript(["x"], 1, [(0.0, 1.0)], None, [(1, gap)], []),
            )
        ]
    )
    prefix = view.render().split("\n")[2].split("x]")[0]
    assert prefix == "    [silent gap: "
    assert prefix.strip() in prompt


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
    ops = parse_director_response('{"ops": [' + example + "]}", num_lines=100)
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
    # Rescuing one silence no longer needs a keep over the lines around it:
    # the silence is addressable on its own ("53~"), so the [N, N+1] mechanic
    # -- which could only ever approximate it -- is gone.
    assert "[N, N+1]" not in prompt
    assert "narrowest range" not in prompt
    # A continuous event may still be spanned whole.
    assert "WHOLE event" in prompt
    assert "jump cuts" in prompt
    # Talking is never a reason to widen one.
    assert "Never widen a keep to mark talking as important" in prompt
    # And a plan-side "feature/retain" direction must not be read as a keep op.
    assert "editorial emphasis, NOT a " in prompt


def test_the_reply_contract_is_stated_by_the_turn_that_needs_it():
    """`loop.REPLY_SHAPE` goes out with every single turn, immediately above the
    reply it governs, and already says that op line numbers are this
    transcript's. DIRECTOR_PROMPT said it again 9,000 characters earlier, to be
    reread every turn for nothing.

    Only the mechanical half moves out. That a reply OWNS its range and
    re-sending REPLACES its ops stays in the prefix as well, deliberately:
    that copy is strategic rather than formatting -- being able to go back and
    rewrite is the move the whole design exists for, and the model should hold
    it before it writes its first op. `test_director_prompt_states_the_turn_
    protocol` pins that one."""
    from nagare_clip.director.loop import REPLY_SHAPE

    prompt = get_effective_config(None, {})["director"]["prompt"]
    for claim in ("REPLACE", "re-send", "this transcript's numbers"):
        assert claim.lower() in REPLY_SHAPE.lower(), claim
    assert '"lines" are this transcript\'s numbers' not in prompt
    # "Output only the JSON object" likewise: every turn ends in REPLY_SHAPE's
    # "one JSON object and nothing else", directly above the reply.
    assert "nothing else" in REPLY_SHAPE
    assert "Output only the JSON object" not in prompt
    # What only the prefix can say -- one op cannot span two segments -- stays.
    assert "inside one [k] block" in prompt


def test_director_prompt_states_each_keep_rule_in_exactly_one_place():
    """An audit of the assembled prompt found the same rules restated across the
    role line, the Timing legend, the Visual-context paragraph, the op menu and
    the generated keep-limit note -- `dropped by default` four times, the
    `[N, N+1]` mechanic three times, the overlay trigger list verbatim twice.

    Repetition is not free here. The one rule that governs where an op's
    boundary goes is stated ONCE, at 55% into the prompt, while the plan's
    concrete line ranges arrive at 82% -- so the prompt's own emphasis is
    inverted relative to what actually needs saying. Restating a mechanic in
    four places also makes it four places to drift: the keep-limit note used to
    assert that "a continuous on-screen event fits well inside" the 8-line cap,
    which is false in exactly the case the whole-event keep rule exists for.

    Each of these belongs to one owner: the keep op bullet owns keep mechanics,
    the Timing legend owns bracket notation, the overlay bullet owns when to
    caption."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    # The [N, N+1] rescue mechanic is gone entirely: a silence line is
    # addressed as "53~", exactly, so an approximation of it in a second
    # vocabulary is one more place to drift.  It must not creep back.
    assert prompt.count("[N, N+1]") == 0
    # "dropped by default" is the Timing legend's job; the keep bullet and the
    # visual-context paragraph point at it rather than restating it.
    assert prompt.count("dropped by default") <= 2
    # The overlay trigger list is the overlay bullet's, not the role line's.
    assert prompt.count("mishaps") == 1


def test_director_prompt_lets_a_described_action_span_its_whole_run():
    """Ending every described gap's keep at [N, N+1] is what lost the
    water-spill cleanup, so the whole-run rule has to survive -- but it now
    lives once, in the keep bullet, rather than being restated in the
    visual-context paragraph. The bullet states it more completely (it names
    the accident/demo/result cases and the jump-cut consequence), and keeping
    one owner is what stopped the two copies drifting apart."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    keep_bullet = next(ln for ln in prompt.splitlines() if ln.startswith("- keep:"))
    assert "across several gaps" in keep_bullet
    assert "WHOLE event in one keep" in keep_bullet
    # And the silence paragraph must not restate it: it names the ops that
    # act on a silence and leaves how wide to the bullet that owns it.
    silence = next(ln for ln in prompt.splitlines() if ln.startswith("is the wait"))
    assert "keep" in silence and "timelapse" in silence
    assert "WHOLE event" not in silence and "jump cuts" not in silence


def test_director_chunk_lines_default():
    """How many display lines one turn of the conversation is asked to review.
    The turn cap is ceil(lines / chunk_lines) * 2, so this number sets both how
    much the director holds at once and how many turns it may spend."""
    cfg = get_effective_config(None, {})
    assert cfg["director"]["chunk_lines"] == 40


def test_director_max_keep_lines_default():
    cfg = get_effective_config(None, {})
    assert cfg["director"]["max_keep_lines"] == 8


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


def test_publish_describe_frames_defaults():
    cfg = get_effective_config(None, {})["publish"]["describe_frames"]
    assert cfg["enabled"] is False  # a vision model has to be configured first
    assert cfg["provider"] == "ollama_chat"
    assert cfg["max_retries"] == 2
    assert cfg["prompt"]


def test_the_frame_description_prompt_asks_for_prose_and_the_empty_regions():
    """It is read by a model and by a human; neither needs a schema."""
    prompt = get_effective_config(None, {})["publish"]["describe_frames"]["prompt"].lower()
    assert "json" not in prompt or "no json" in prompt
    assert "empty" in prompt
    assert "legible" in prompt


def test_the_frame_description_prompt_forbids_writing_a_headline():
    """Writing copy is the copy call's job; this one only looks. A vision model
    handed a frame will happily caption it unless told not to."""
    prompt = get_effective_config(None, {})["publish"]["describe_frames"]["prompt"].lower()
    assert "do not write a headline" in prompt


def test_image_markup_is_general_not_per_stage():
    """It is a property of the VIEWER, not of a stage: two copies is one for a
    human to keep in sync, and one project with two markdown files disagreeing."""
    cfg = get_effective_config(None, {})
    assert cfg["general"]["image_markup"] == "html"
    assert "image_markup" not in cfg["publish"]
    assert "image_markup" not in cfg["render"]


@pytest.mark.parametrize("section", ["publish", "render"])
def test_a_per_stage_image_markup_is_rejected_rather_than_ignored(tmp_path, section):
    path = tmp_path / "c.yml"
    path.write_text(f"{section}:\n  image_markup: markdown\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_publish_pairing_defaults():
    cfg = get_effective_config(None, {})["publish"]["pairing"]
    assert cfg["enabled"] is True  # same text model as the copy call; no new dependency
    # unset = inherit publish's, because it runs on publish's model and must be
    # sampled the way that model requires
    assert cfg["temperature"] is None
    assert cfg["max_retries"] is None
    assert cfg["retry_temp_step"] is None and cfg["retry_temp_cap"] is None
    assert cfg["prompt"]


def test_the_pairing_prompt_owns_the_look_vocabulary_the_copy_prompt_lost():
    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"].lower()
    for word in ("fill", "stroke", "strokewidth", "pointsize", "gravity", "offset"):
        assert word in prompt


def test_the_pairing_prompt_asks_for_an_index_never_a_path():
    """A path is a string a model can invent; an index is bounded."""
    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"].lower()
    assert "index" in prompt
    assert ".jpg" not in prompt and "path" not in prompt


def test_the_pairing_prompts_own_json_example_parses():
    from nagare_clip.publish.pairing import try_parse_pairing_response

    prompt = get_effective_config(None, {})["publish"]["pairing"]["prompt"]
    start = prompt.index("{", prompt.index("JSON shape"))
    depth, end = 0, start
    for i, ch in enumerate(prompt[start:], start=start):
        depth += (ch == "{") - (ch == "}")
        if depth == 0:
            end = i + 1
            break
    got = try_parse_pairing_response(prompt[start:end], num_sets=4, num_frames=24)
    assert got is not None and got[1].frame is not None
    assert got[1].style and got[1].lines


def test_render_defaults():
    cfg = get_effective_config(None, {})
    render = cfg["render"]
    assert render["enabled"] is True
    assert (render["width"], render["height"]) == (1280, 720)
    assert render["line_gap"] == 12
    assert render["fonts"] == {}


def test_render_fonts_come_from_the_file(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text(
        'render:\n  fonts:\n    hook: "Noto-Serif-CJK-JP-Black"\n',
        encoding="utf-8",
    )
    cfg = get_effective_config(path, {})
    assert cfg["render"]["fonts"] == {"hook": "Noto-Serif-CJK-JP-Black"}


def test_an_unknown_render_key_is_rejected(tmp_path):
    path = tmp_path / "c.yml"
    path.write_text("render:\n  colour: red\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_a_background_key_is_rejected_rather_than_ignored(tmp_path):
    """One project-wide background is gone; a set names its own in publish.json."""
    path = tmp_path / "c.yml"
    path.write_text("render:\n  background: frames/a/1.jpg\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        get_effective_config(path, {})


def test_a_config_still_carrying_publish_thumbnail_fails_loudly(tmp_path):
    """No shim: the canvas size must have exactly one place to look."""
    path = tmp_path / "c.yml"
    path.write_text("publish:\n  thumbnail:\n    width: 1920\n", encoding="utf-8")
    with pytest.raises(ValidationError) as e:
        get_effective_config(path, {})
    assert "thumbnail" in str(e.value)


def _plan_prompt() -> str:
    return get_effective_config(None, {})["plan"]["prompt"]


def test_plan_prompt_examples_survive_the_parser():
    """Every documented direction example must parse, or the prompt teaches a
    shape the parser drops."""
    from nagare_clip.plan.plan_llm import try_parse_plan_response
    from nagare_clip.summary.summarize import PartSummary

    parts = [PartSummary("v", (1, 40), f"part {i}") for i in range(1, 4)]
    examples = [ln.strip().rstrip(",") for ln in _plan_prompt().splitlines() if '"index": ' in ln]
    assert examples
    for example in examples:
        parsed = try_parse_plan_response('{"directions": [' + example + "]}", parts)
        assert parsed, f"prompt example dropped: {example}"


def test_plan_prompt_asks_for_a_message_every_run():
    """The run that builds 22 directions from nothing is the one a human most
    needs explained, and it is the run with no conversation to reply to.  A
    message defined only as "your reply to the human" is therefore not written
    at all on a first run — the real one said "No changes requested since last
    plan; repeating all directions unchanged." about a plan it had just built
    from scratch."""
    prompt = _plan_prompt()
    assert '"message"' in prompt
    lowered = prompt.lower()
    assert "every run" in lowered or "always" in lowered
    # it explains the plan; it is not addressed to anyone
    assert "not a reply" in lowered or "not a reply to" in lowered


def test_plan_message_example_does_not_assume_a_conversation():
    """An example anchors harder than an instruction: a message example ending
    in "is that right?" teaches the model to answer a conversation that, on this
    stage, no longer exists."""
    prompt = _plan_prompt()
    message_lines = [ln for ln in prompt.splitlines() if '"message"' in ln]
    assert message_lines
    for line in message_lines:
        assert "?" not in line, f"plan message example asks the human a question: {line}"


def test_plan_prompt_says_nothing_about_the_conversation():
    """plan is a pure function of the summaries; revising against what the human
    said is plan_revise's job.  An edit vocabulary in this prompt would put
    instructions about deleting existing directions in front of a first run that
    has none, and everything added competes with the editorial brief."""
    prompt = _plan_prompt().lower()
    for word in ("conversation", "previous", "human editor"):
        assert word not in prompt, f"plan prompt still talks about the {word}"


def test_plan_prompt_documents_the_line_range_split():
    """A direction may narrow to part of its part — that is what makes the
    human's "[31,83] is really two things" actionable rather than merely heard."""
    prompt = _plan_prompt()
    assert '"lines"' in prompt
    assert "split" in prompt.lower()


def _revise_prompt() -> str:
    return get_effective_config(None, {})["plan_revise"]["prompt"]


def test_revise_prompt_examples_survive_the_parser():
    """The documented JSON shape must parse as written, or the prompt teaches a
    shape the parser drops."""
    from nagare_clip.plan.plan_llm import PartDirection
    from nagare_clip.plan_revise.revise_llm import try_parse_revision
    from nagare_clip.summary.summarize import PartSummary

    parts = [PartSummary("v", (1, 100), f"part {i}") for i in range(1, 26)]
    lines = _revise_prompt().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith('{"delete"'))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip().endswith("}"))
    ops = try_parse_revision("\n".join(lines[start : end + 1]), parts)
    assert ops is not None, "the prompt's own JSON shape does not parse"
    assert ops.delete and ops.update and ops.message
    assert ops.add == [PartDirection("v", (60, 83), "feature — the demonstration itself")], (
        "the prompt's add example is dropped by the parser"
    )


def test_revise_prompt_states_the_carry_through_contract():
    """Output must be proportional to the change: a restated plan grows with the
    project and invites the model to economise, and an omitted direction is then
    indistinguishable from a deletion."""
    prompt = _revise_prompt().lower()
    assert "conversation" in prompt
    assert "do not name" in prompt or "not named" in prompt
    for op in ("delete", "add", "update", "message"):
        assert f'"{op}"' in _revise_prompt()


def test_revise_prompt_never_offers_keep_as_a_direction_word():
    """Same trap as the plan prompt: `keep` is a director op with a mechanical
    cost, and these directions are fed to the director as context."""
    prompt = _revise_prompt()
    assert 'Never use the word "keep" in a direction' in prompt
    remainder = [ln for ln in prompt.splitlines() if 'Never use the word "keep"' not in ln]
    assert not [ln for ln in remainder if "keep" in ln.lower()]


def test_revise_prompt_message_is_a_reply_not_a_plan_summary():
    """Both stages have a "message" and they are different things: plan explains
    the plan it just made, plan_revise answers the human."""
    prompt = _revise_prompt()
    assert '"message"' in prompt
    lowered = prompt.lower()
    assert "reply" in lowered
    assert "the human" in lowered


def test_revise_prompt_documents_the_ids_and_the_split():
    prompt = _revise_prompt()
    assert "id" in prompt.lower()
    assert "split" in prompt.lower()
    assert '"message"' in prompt


def test_director_prompt_says_who_the_editor_is():
    prompt = get_effective_config(None, {})["director"]["prompt"]
    assert "Guide:" in prompt and "Editor:" in prompt
    assert "the editor wins" in prompt


def test_director_prompt_explains_the_plan():
    prompt = get_effective_config(None, {})["director"]["prompt"]
    assert "your first reply is a plan" in prompt
    assert "not op boundaries" in prompt


def test_director_prompt_explains_the_order():
    prompt = get_effective_config(None, {})["director"]["prompt"]
    assert '"order": [[first, last], ...]' in prompt
    assert "shooting order" in prompt
    assert "covering every line exactly once" in prompt


def test_director_prompt_did_not_grow_for_the_silence_lines():
    """Improvement 16: every round that grew this prompt cost something, so a
    feature that adds a paragraph has to pay for it by deleting what it makes
    redundant -- here the gap-rescue mechanics in the Timing legend and the
    visual-context paragraph, and the [N, N+1] rule in the keep bullet.  6289
    characters is what it measured before the silence lines went in.

    The conversation raised the ceiling to 6600 ONCE, deliberately: the turn
    protocol (an approximate range per turn, a reply that owns its range, and
    `done`) is what the model is doing now, and no wording of it is free.  It
    paid what it could -- the "53~" teaching (a silence is a numbered line
    now), the transcript-echo rule the last Rules line already covers, the
    1-based-numbering restatement above the menu, and half the playback rule
    (the preview computes that playback now) -- which is 300 of the 604
    characters the protocol cost.  Anything further must delete, not raise.

    6495 -> 6595: the seam fact (a [k] block was recorded as its own video, so
    it can open with a greeting or end with a sign-off) took 100 of the 104
    characters that were left, inside the ceiling rather than over it.  Four
    characters remain: the next addition here deletes something first.

    6595 -> 6589: the bracket legend's new facts (speech + silence add up to
    the footage; a keep plays the silence; a timelapse plays speech+silence
    over its factor) were paid for by rewording the Timing/pacing lines and
    deleting "Those are the only four forms" and "no timing, no bracket".

    6589 -> 6107, and the ceiling back BELOW 6289, the size before the silence
    lines: the conversation's 311-character raise is repaid in full.  Only
    restatements went -- sentences another part of the assembled message
    already says: "Output only the JSON object" (REPLY_SHAPE, every turn),
    "every segment in playback order" (VIEW_HEADER), "the stretch you really
    reviewed" (the turn's ask), "different footage ... refused" (the loop's
    refusal says both), the cut-overlap rule's second statement of itself,
    the overlay's "not how long it shows" (its duration says so), and the
    reasons trailing "buildup", "mishaps" and "note".  No fact left the
    message; the next addition deletes before it adds.

    6107 -> 6580, and the ceiling raised to 6600 deliberately, the second
    time: the director now decides the playback order.  Its meaning (the view
    stays in shooting order, ranges in the order they play, coverage, a
    silence line travels with its range, a timelapse stays inside one range)
    belongs in this cached prefix, not in REPLY_SHAPE, which every turn re-sends
    UNCACHED -- so REPLY_SHAPE was cut to naming the key.  Nothing else to
    delete paid for it; the next addition deletes before it adds.

    6580 -> 6865, ceiling 6900: the director writes its own plan (the plan
    stage's directions are no longer shown).  The prompt says only that the
    plan exists, stays revisable and marks sections, not op boundaries; what a
    plan covers is in loop.PLAN_REQUEST, sent once.  The prefix as a whole
    SHRANK: the directions list and SECTION_BOUNDARY_NOTE left it.

    -> ceiling 7150: the Speakers paragraph.  The director resumes from its
    directory and a person joins by writing an editor entry; the prompt is the
    only place that says what an Editor: part is and that it wins, since no
    code treats the person differently."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    assert len(prompt) < 7150


def _display_view(n: int = 100):
    """A flat one-segment view, enough for the loop to read a reply against."""
    from nagare_clip.director.display import DisplayLine, DisplaySegment, DisplayView

    return DisplayView(
        lines=[
            DisplayLine(number=i, segment=1, stem="x", source_line=i, is_silence=False, text="t")
            for i in range(1, n + 1)
        ],
        segments=[DisplaySegment(index=1, stem="x", label="x", first=1, last=n)],
    )


def test_director_prompt_says_a_join_is_where_two_recordings_meet():
    """`SEAM_NOTE` went out in f197206 with the per-segment seam block, on the
    reasoning that a whole-video view makes every join visible.  The finished
    cut still carried a sign-off at every join: SEEING a join is not the same
    as KNOWING it is one.  The fact goes back -- a [k] block was recorded as
    its own video, so it can open with a greeting or end with a sign-off --
    adapted to the whole-video view, where there is no seam block to put it in.

    The fact, not the rule: whether to cut one is the model's call, and a
    prompt that decided it for the model would be the editorial judgement this
    stage exists to make."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    bullet = next(line for line in prompt.split("\n") if "sign-off" in line)
    sentence = next(s for s in bullet.split(". ") if "sign-off" in s)
    lowered = sentence.lower()
    assert "[k]" in sentence
    assert "recorded as its own video" in lowered
    assert "greeting" in lowered
    assert not any(word in lowered for word in ("cut", "remove", "delete", "must", "never"))


def test_director_prompt_states_the_turn_protocol():
    """The director is no longer one call over one segment: it is a
    conversation over the whole video. A prompt describing the old shape is
    not merely stale, it contradicts what every user message asks for."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    lowered = prompt.lower()
    assert "one numbering" in lowered  # the whole video, numbered once
    assert '"range"' in prompt and '"reviewed_through"' in prompt
    assert '{"done": true}' in prompt
    # Re-sending a range REPLACES its ops — the one move that makes the
    # playback worth reading.
    assert "replace" in lowered


def test_director_prompt_turn_shape_is_the_one_the_loop_parses():
    """Pin the documented reply to the real reader: a stale shape here is a
    turn the loop rejects, and the retry ladder burns on it."""
    from nagare_clip.director.loop import LoopState, apply_reply

    prompt = get_effective_config(None, {})["director"]["prompt"]
    start = prompt.index('{"range"')
    shape = prompt[start : prompt.index("]}", start) + 2]
    view = _display_view()
    state = LoopState(plan="p")  # the range shape follows the planning turn
    result = apply_reply(view, state, shape)
    assert result.error is None and result.refusal is None
    assert {op.type for op in result.ops} == {"cut", "timelapse", "overlay", "keep", "edit"}
    assert state.reviewed_through == 78


def test_director_prompt_bracket_legend_says_what_each_figure_plays():
    """The speech/silence split is now what intervals renders: the speech figure
    is what plays at 1x with no op, the silence is every second of the line the
    render drops, and the two add up to the line's footage.  Without that last
    fact the director cannot price its own ops: a timelapse plays the WHOLE
    footage over its factor, not the speech figure over it, and a keep plays
    the silence back.  Stated in the Timing legend, which owns the notation,
    and paid for there -- the ceiling above is not raised."""
    prompt = get_effective_config(None, {})["director"]["prompt"]
    timing = next(ln for ln in prompt.splitlines() if ln.startswith("Timing:"))
    assert "add up to the line's footage" in timing
    pacing = next(ln for ln in prompt.splitlines() if "Judge pacing from the speech figure" in ln)
    assert "a timelapse plays speech+silence divided by its factor" in pacing
    assert "a keep plays the silence too" in pacing.lower()


# ---------------------------------------------------------------------------
# reasoning_effort: passed straight to LiteLLM; the old `thinking` key is gone
# ---------------------------------------------------------------------------

_LLM_SECTIONS = [
    ("sentence_split",),
    ("text_filter",),
    ("summary",),
    ("plan",),
    ("plan_revise",),
    ("director",),
    ("guided_edit",),
    ("gap_context",),
    ("publish",),
    ("publish", "describe_frames"),
]


def _section(cfg: dict, path: tuple[str, ...]) -> dict:
    for key in path:
        cfg = cfg[key]
    return cfg


def _nest(path: tuple[str, ...], leaf: dict) -> dict:
    for key in reversed(path):
        leaf = {key: leaf}
    return leaf


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_reasoning_effort_defaults_to_unset(path):
    section = _section(DEFAULTS, path)
    assert section["reasoning_effort"] is None
    assert "thinking" not in section


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_reasoning_effort_value_is_kept_verbatim(tmp_path: Path, path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump(_nest(path, {"reasoning_effort": "xhigh"})))
    assert _section(get_effective_config(cfg_file), path)["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("path", _LLM_SECTIONS, ids=".".join)
def test_old_thinking_key_fails_naming_the_key_and_its_replacement(tmp_path: Path, path):
    """Not a silent alias: `thinking: false` used to be translated, and
    `reasoning_effort` is not -- so a leftover key must stop the run and say
    which key and what replaced it, not be read as a generic typo."""
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump(_nest(path, {"thinking": False})))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    msg = str(e.value)
    assert ".".join((*path, "thinking")) in msg
    assert "reasoning_effort" in msg
    assert "LiteLLM" in msg
    assert "extra" not in msg.lower()


def test_old_thinking_key_reports_every_occurrence(tmp_path: Path):
    cfg_file = tmp_path / "cfg.yml"
    cfg_file.write_text(yaml.dump({"director": {"thinking": True}, "plan": {"thinking": "high"}}))
    with pytest.raises(ValueError) as e:
        get_effective_config(cfg_file)
    assert "director.thinking" in str(e.value)
    assert "plan.thinking" in str(e.value)
