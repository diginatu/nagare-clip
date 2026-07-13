import base64

import pytest

from nagare_clip.gap_context.describe import GapFrames, build_messages, describe_gap
from nagare_clip.gap_context.gaps import Gap
from nagare_clip.llm_report import Recorder


@pytest.fixture
def gf(tmp_path):
    frames = []
    for name in ("10.200.jpg", "15.000.jpg"):
        p = tmp_path / name
        p.write_bytes(b"\xff\xd8\xff-fake-jpeg")
        frames.append(p)
    return GapFrames(
        start=10.0,
        end=20.0,
        frames=frames,
        relpaths=["frames/a/10.200.jpg", "frames/a/15.000.jpg"],
    )


CFG = {"prompt": "SYSTEM PROMPT", "max_retries": 2, "temperature": 0.2, "model": "m"}


def test_build_messages_has_system_prompt_and_image_parts(gf):
    messages = build_messages(gf, CFG)
    assert messages[0] == {"role": "system", "content": "SYSTEM PROMPT"}
    parts = messages[1]["content"]
    assert parts[0]["type"] == "text"
    assert "10.0" in parts[0]["text"] and "20.0" in parts[0]["text"]  # time range
    assert "10.0s" in parts[0]["text"]  # duration
    images = [p for p in parts if p["type"] == "image_url"]
    assert len(images) == 2
    expected = base64.b64encode(b"\xff\xd8\xff-fake-jpeg").decode()
    assert images[0]["image_url"]["url"] == f"data:image/jpeg;base64,{expected}"


def test_build_messages_includes_neighbour_lines_when_given(gf):
    parts = build_messages(gf, CFG, before="ここでビルドします", after="できました")[1]["content"]
    assert "ここでビルドします" in parts[0]["text"]
    assert "できました" in parts[0]["text"]


def test_build_messages_omits_neighbour_lines_when_absent(gf):
    text = build_messages(gf, CFG)[1]["content"][0]["text"]
    assert "before" not in text.lower()
    assert "after" not in text.lower()


def test_describe_gap_returns_a_described_gap(gf):
    calls = []

    def fake_llm(messages, cfg):
        calls.append(cfg)
        return "  ビルドが走りログが流れている。  "

    gap = describe_gap(gf, CFG, unit="a_gap01", call_llm=fake_llm)
    assert gap == Gap(
        start=10.0,
        end=20.0,
        frames=["frames/a/10.200.jpg", "frames/a/15.000.jpg"],
        description="ビルドが走りログが流れている。",
    )
    assert len(calls) == 1


def test_describe_gap_retries_an_empty_response_then_succeeds(gf):
    responses = iter(["   ", "静止画面。"])

    def fake_llm(messages, cfg):
        return next(responses)

    gap = describe_gap(gf, CFG, unit="a_gap01", call_llm=fake_llm)
    assert gap is not None and gap.description == "静止画面。"


def test_describe_gap_retries_nudge_temperature_up(gf):
    temps = []

    def fake_llm(messages, cfg):
        temps.append(cfg.get("temperature"))
        raise ConnectionError("boom")

    assert describe_gap(gf, CFG, unit="a_gap01", call_llm=fake_llm) is None
    assert temps == [0.2, pytest.approx(0.4), pytest.approx(0.6)]  # 3 attempts


def test_describe_gap_returns_none_when_all_attempts_fail(gf):
    def fake_llm(messages, cfg):
        raise ConnectionError("boom")

    assert describe_gap(gf, CFG, unit="a_gap01", call_llm=fake_llm) is None


def test_describe_gap_skips_a_missing_frame_file(tmp_path):
    good = tmp_path / "a.jpg"
    good.write_bytes(b"jpeg")
    gf = GapFrames(
        start=1.0,
        end=5.0,
        frames=[tmp_path / "gone.jpg", good],
        relpaths=["frames/a/gone.jpg", "frames/a/a.jpg"],
    )
    seen = {}

    def fake_llm(messages, cfg):
        seen["parts"] = messages[1]["content"]
        return "d"

    gap = describe_gap(gf, CFG, unit="u", call_llm=fake_llm)
    images = [p for p in seen["parts"] if p["type"] == "image_url"]
    assert len(images) == 1
    assert gap is not None and gap.frames == ["frames/a/a.jpg"]


def test_describe_gap_returns_none_when_no_frame_is_readable(tmp_path):
    gf = GapFrames(start=1.0, end=5.0, frames=[tmp_path / "gone.jpg"], relpaths=["x.jpg"])

    call_count = [0]

    def fake_llm(messages, cfg):
        call_count[0] += 1
        return "should not reach here"

    result = describe_gap(gf, CFG, unit="u", call_llm=fake_llm)
    assert result is None
    assert call_count[0] == 0


def test_describe_gap_recorder_excludes_base64_payloads(tmp_path):
    """Verify that LLM report records frame PATHs, never base64 payloads."""
    # Create a frame with recognizable content
    frame_content = b"RECOGNIZABLE_FRAME_CONTENT_FOR_TESTING"
    frame_path = tmp_path / "frames" / "test_frame.jpg"
    frame_path.parent.mkdir(parents=True)
    frame_path.write_bytes(frame_content)

    gf = GapFrames(
        start=1.0,
        end=5.0,
        frames=[frame_path],
        relpaths=["frames/test_frame.jpg"],
    )

    def fake_llm(messages, cfg):
        return "A description"

    # Use a real Recorder
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    rec = Recorder("gap_context", report_dir, enabled=True)

    gap = describe_gap(gf, CFG, unit="test_unit", call_llm=fake_llm, recorder=rec)
    assert gap is not None

    # Read the generated markdown report
    report_file = report_dir / "gap_context" / "test_unit.md"
    assert report_file.exists()
    report_text = report_file.read_text(encoding="utf-8")

    # Assert the base64 payload does NOT appear
    base64_payload = base64.b64encode(frame_content).decode("ascii")
    assert base64_payload not in report_text, "base64 payload should not be in report"
    assert "data:image/jpeg;base64," not in report_text, "data-URI prefix should not be in report"

    # Assert the frame's relpath DOES appear
    assert "frames/test_frame.jpg" in report_text, "frame relpath should be in report"
