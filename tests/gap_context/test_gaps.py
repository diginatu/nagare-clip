import json

from nagare_clip.gap_context.gaps import Gap, gaps_from_dict, gaps_to_dict, load_gaps


def test_round_trip():
    gaps = [Gap(start=10.0, end=22.5, frames=["frames/a/10.200.jpg"], description="ビルドが走る")]
    data = gaps_to_dict(gaps)
    assert data == {
        "gaps": [
            {
                "start": 10.0,
                "end": 22.5,
                "frames": ["frames/a/10.200.jpg"],
                "description": "ビルドが走る",
            }
        ]
    }
    assert gaps_from_dict(data) == gaps


def test_duration():
    assert Gap(start=10.0, end=22.5, frames=[], description="x").duration == 12.5


def test_gaps_from_dict_is_lenient():
    data = {
        "gaps": [
            {"start": 1.0, "end": 2.0, "description": "ok"},  # frames optional
            {"start": "nope", "end": 2.0, "description": "bad start"},
            {"start": 1.0, "end": 2.0},  # no description
            {"start": 1.0, "end": 2.0, "description": ""},  # empty description
            "not a dict",
        ]
    }
    gaps = gaps_from_dict(data)
    assert len(gaps) == 1
    assert gaps[0].description == "ok"
    assert gaps[0].frames == []


def test_gaps_from_dict_handles_garbage():
    assert gaps_from_dict(None) == []
    assert gaps_from_dict({"gaps": "nope"}) == []
    assert gaps_from_dict({}) == []


def test_load_gaps_missing_file(tmp_path):
    assert load_gaps(tmp_path / "nope.json") == []
    assert load_gaps(None) == []


def test_load_gaps_invalid_json(tmp_path):
    p = tmp_path / "g.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_gaps(p) == []


def test_load_gaps_reads_a_file(tmp_path):
    p = tmp_path / "g.json"
    p.write_text(
        json.dumps({"gaps": [{"start": 1.0, "end": 5.0, "frames": [], "description": "d"}]}),
        encoding="utf-8",
    )
    assert load_gaps(p) == [Gap(start=1.0, end=5.0, frames=[], description="d")]


def test_gaps_from_dict_rejects_invalid_interval():
    """Test that end <= start is rejected (not coerced)."""
    data = {
        "gaps": [
            {"start": 5.0, "end": 5.0, "description": "equal"},
            {"start": 10.0, "end": 5.0, "description": "reversed"},
            {"start": 1.0, "end": 2.0, "description": "valid"},
        ]
    }
    gaps = gaps_from_dict(data)
    # Only the valid one should be kept
    assert len(gaps) == 1
    assert gaps[0].start == 1.0
    assert gaps[0].end == 2.0


def test_gaps_from_dict_rejects_bool_start_end():
    """Test that bool start/end are rejected, not coerced to 1.0/0.0."""
    data = {
        "gaps": [
            {"start": True, "end": 2.0, "description": "bool start"},
            {"start": 1.0, "end": False, "description": "bool end"},
            {"start": 1.0, "end": 2.0, "description": "valid"},
        ]
    }
    gaps = gaps_from_dict(data)
    # Only the valid one should be kept
    assert len(gaps) == 1
    assert gaps[0].start == 1.0
    assert gaps[0].end == 2.0


def test_gaps_from_dict_collapses_whitespace_in_description():
    """A hand-edited gaps.json (or a description that slipped through from a
    non-compliant vision LLM before this fix) may contain internal newlines;
    they must collapse to single spaces so the description can never inject
    a bogus 'N: ...'-looking line into a downstream numbered transcript."""
    data = {
        "gaps": [
            {
                "start": 1.0,
                "end": 2.0,
                "description": "Line one.\n2: fake injected\n  extra  spaces ",
            },
        ]
    }
    gaps = gaps_from_dict(data)
    assert len(gaps) == 1
    assert "\n" not in gaps[0].description
    assert gaps[0].description == "Line one. 2: fake injected extra spaces"


def test_gaps_from_dict_filters_non_string_frames():
    """Test that non-string entries in frames list are filtered out."""
    data = {
        "gaps": [
            {
                "start": 1.0,
                "end": 2.0,
                "frames": ["valid.jpg", 123, None, "also_valid.png", True, "final.jpg"],
                "description": "mixed frames",
            }
        ]
    }
    gaps = gaps_from_dict(data)
    assert len(gaps) == 1
    assert gaps[0].frames == ["valid.jpg", "also_valid.png", "final.jpg"]
