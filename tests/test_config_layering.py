"""Several --config files, deep-merged left to right (a shared base + a per-video file)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nagare_clip.config import (
    DEFAULTS,
    deep_merge,
    get_effective_config,
    write_effective_config,
)


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --- merge semantics ---------------------------------------------------------


def test_deep_merge_replaces_a_list_whole_rather_than_concatenating():
    base = {"text_filter": {"keywords": ["a", "b"], "use_llm": True}}
    override = {"text_filter": {"keywords": ["c"]}}
    assert deep_merge(base, override) == {"text_filter": {"keywords": ["c"], "use_llm": True}}


def test_deep_merge_an_empty_list_clears_the_base_list():
    assert deep_merge({"k": [1, 2]}, {"k": []}) == {"k": []}


def test_deep_merge_replaces_a_dict_with_a_scalar_whole():
    assert deep_merge({"a": {"x": 1}}, {"a": None}) == {"a": None}


# --- get_effective_config over a sequence of files ---------------------------


def test_later_file_wins_and_dicts_merge_recursively(tmp_path: Path):
    base = _write(
        tmp_path / "base.yml",
        {
            "intervals": {"silence_threshold": 2.5, "min_keep": 0.5},
            "text_filter": {"keywords": ["base1", "base2"]},
            "project": {"audience": "makers", "tone": "calm"},
        },
    )
    video = _write(
        tmp_path / "video.yml",
        {
            "intervals": {"silence_threshold": 3.0},
            "text_filter": {"keywords": ["video"]},
            "project": {"tone": "excited"},
        },
    )
    cfg = get_effective_config([base, video])
    assert cfg["intervals"]["silence_threshold"] == 3.0  # later file wins
    assert cfg["intervals"]["min_keep"] == 0.5  # base survives where not overridden
    assert cfg["intervals"]["keep_pre_margin"] == DEFAULTS["intervals"]["keep_pre_margin"]
    assert cfg["text_filter"]["keywords"] == ["video"]  # list replaced, not concatenated
    assert cfg["project"]["audience"] == "makers"
    assert cfg["project"]["tone"] == "excited"


def test_order_matters(tmp_path: Path):
    a = _write(tmp_path / "a.yml", {"intervals": {"silence_threshold": 1.5}})
    b = _write(tmp_path / "b.yml", {"intervals": {"silence_threshold": 4.0}})
    assert get_effective_config([a, b])["intervals"]["silence_threshold"] == 4.0
    assert get_effective_config([b, a])["intervals"]["silence_threshold"] == 1.5


def test_cli_overrides_beat_every_file(tmp_path: Path):
    a = _write(tmp_path / "a.yml", {"intervals": {"silence_threshold": 1.5}})
    b = _write(tmp_path / "b.yml", {"intervals": {"silence_threshold": 4.0}})
    cfg = get_effective_config([a, b], {"intervals": {"silence_threshold": 9.0}})
    assert cfg["intervals"]["silence_threshold"] == 9.0


def test_an_empty_sequence_is_the_defaults():
    assert get_effective_config([]) == DEFAULTS


def test_a_single_path_still_works(tmp_path: Path):
    a = _write(tmp_path / "a.yml", {"intervals": {"silence_threshold": 1.5}})
    assert get_effective_config(a)["intervals"]["silence_threshold"] == 1.5


def test_a_missing_file_among_several_names_its_path(tmp_path: Path):
    a = _write(tmp_path / "a.yml", {})
    missing = tmp_path / "nope.yml"
    with pytest.raises(FileNotFoundError, match="nope.yml"):
        get_effective_config([a, missing])


@pytest.mark.parametrize("which", [0, 1])
def test_a_removed_key_in_any_file_raises_naming_its_dotted_path(tmp_path: Path, which):
    files = [
        _write(tmp_path / "a.yml", {"intervals": {"silence_threshold": 1.5}}),
        _write(tmp_path / "b.yml", {"intervals": {"min_keep": 0.5}}),
    ]
    _write(files[which], {"director": {"thinking": False}})
    with pytest.raises(ValueError, match=r"director\.thinking removed: use `reasoning_effort`"):
        get_effective_config(files)


def test_a_key_valid_only_after_merging_is_validated_once_on_the_result(tmp_path: Path):
    """A typo in the base still fails even when the later file is clean."""
    base = _write(tmp_path / "base.yml", {"intervals": {"silence_treshold": 1.0}})
    video = _write(tmp_path / "video.yml", {"intervals": {"min_keep": 0.5}})
    with pytest.raises(Exception, match="silence_treshold"):
        get_effective_config([base, video])


# --- the effective-config snapshot -------------------------------------------


def test_snapshot_round_trips_to_the_same_config(tmp_path: Path):
    base = _write(tmp_path / "base.yml", {"project": {"audience": "日本の視聴者"}})
    video = _write(tmp_path / "video.yml", {"intervals": {"silence_threshold": 3.0}})
    cfg = get_effective_config([base, video], {"intervals": {"min_keep": 0.25}})
    snap = write_effective_config(cfg, tmp_path / "out", [base, video])
    assert snap == tmp_path / "out" / "effective_config.yml"
    assert get_effective_config(snap) == cfg


def test_snapshot_names_its_source_files_in_order(tmp_path: Path):
    base = _write(tmp_path / "base.yml", {})
    video = _write(tmp_path / "video.yml", {})
    snap = write_effective_config(DEFAULTS, tmp_path, [base, video])
    text = snap.read_text(encoding="utf-8")
    assert text.index(str(base)) < text.index(str(video))


def test_snapshot_redacts_api_keys(tmp_path: Path):
    src = _write(tmp_path / "a.yml", {"director": {"api_key": "sk-secret"}})
    cfg = get_effective_config(src)
    snap = write_effective_config(cfg, tmp_path / "out", [src])
    assert "sk-secret" not in snap.read_text(encoding="utf-8")
    assert cfg["director"]["api_key"] == "sk-secret"  # the in-memory config is untouched
