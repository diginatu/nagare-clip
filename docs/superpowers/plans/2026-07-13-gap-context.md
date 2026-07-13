# gap_context Stage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `gap_context` pipeline stage that snapshots frames from long silent gaps, asks a vision LLM what is happening on screen, and feeds those descriptions to the `summary` and `director` stages so a visually interesting silence can be kept instead of blindly cut.

**Architecture:** A new stage between `sentence_split` and `summary`. The pipeline adapter (not the stage `run()`) extracts JPEG frames with ffmpeg **inside the existing whisperx Docker image** — matching the `audio_silence` pattern, keeping `pipeline/external.py` the only subprocess site. The stage then makes one multimodal LLM call per gap through the existing `llm_client.call_llm` (LiteLLM handles provider-specific image encoding) and writes a time-based `{stem}_gaps.json`. Consumers anchor gaps to transcript lines themselves via `timing.segment_times`.

**Tech Stack:** Python 3 + uv, pydantic-settings config models, LiteLLM (`llm_client.call_llm`), ffmpeg via `docker compose run whisperx`, pytest. Spec: `docs/superpowers/specs/2026-07-13-gap-context-design.md`. Branch: `gap-context`.

## Global Constraints

- **Naming:** stages are identified by functional name only, never numbers. The new stage's canonical identifier is `gap_context` everywhere (config section, `src/nagare_clip/gap_context/`, `output/gap_context/`, `--from-stage`/`--to-stage`).
- **No new media deps:** all ffmpeg work goes through the whisperx Docker image via `pipeline/external.py`. No host ffmpeg binary, no Python image/audio libraries. Base64 encoding uses the stdlib (`base64`, `mimetypes`).
- **No provider-specific clients:** every LLM call goes through `nagare_clip.llm_client.call_llm` (LiteLLM).
- **Graceful degradation:** no failure in this stage may abort the pipeline. Frame failure → skip frame; zero frames → skip gap; LLM failure after retries → skip gap; stage disabled → `{"gaps": []}`.
- **Byte-identical when absent:** with no/empty gaps file, the `summary` and `director` prompts must be byte-identical to today. Both consumers get an explicit regression test.
- **Config:** `config.example.yml` is generated — run `make config-example` after touching `config.py`; never hand-edit it.
- **TDD (repo policy):** write the failing test first, run it, see it fail for the right reason, then implement. Where a test is added against code that already exists, mutate the implementation, watch the test fail, revert.
- **Python:** always `uv run` (e.g. `uv run pytest`). Validate with `make check` before the final commit.

---

## File Structure

**Create:**
- `src/nagare_clip/gap_context/__init__.py` — empty package marker
- `src/nagare_clip/gap_context/snapshot.py` — pure gap selection + frame timestamps + frame paths
- `src/nagare_clip/gap_context/gaps.py` — the `Gap` dataclass and the `{stem}_gaps.json` (de)serialisation contract
- `src/nagare_clip/gap_context/describe.py` — multimodal message construction + the per-gap vision call with retries
- `src/nagare_clip/gap_context/context.py` — pure consumer-side helpers: anchor gaps to lines, render the summary block, annotate the director transcript
- `src/nagare_clip/gap_context/run.py` — `run_gap_context()` typed entry point
- `docs/stages/gap_context.md` — deep-dive stage doc
- `tests/gap_context/{__init__.py,test_snapshot.py,test_gaps.py,test_describe.py,test_context.py,test_run.py}`

**Modify:**
- `src/nagare_clip/config.py` — `GAP_CONTEXT_PROMPT`, `GapContextConfig`, field on `NagareClipConfig`, a new paragraph in `DIRECTOR_PROMPT`
- `src/nagare_clip/llm_client.py` — widen `messages` typing to `list[dict[str, Any]]`
- `src/nagare_clip/llm_report.py` — add `gap_context` to `STAGE_ORDER`
- `src/nagare_clip/pipeline/external.py` — `build_snapshot_cmd()`
- `src/nagare_clip/pipeline/stages.py` — `STAGE_NAMES` + `_gap_context_run` / `_gap_context_required` + `STAGES` entry
- `src/nagare_clip/summary/summarize.py` — `segment_video(gap_block=...)`, `build_summary(gap_blocks_by_stem=...)`
- `src/nagare_clip/summary/run.py` — `run_summary(gaps_paths=...)`
- `src/nagare_clip/director/director_llm.py` — `generate_director_ops(gap_annotations=...)`
- `src/nagare_clip/director/run.py` — `run_director(gaps=...)`
- `config.example.yml` (generated), `AGENTS.md`, `README.md`, `plan.md`

---

### Task 1: Config section + prompts

**Files:**
- Modify: `src/nagare_clip/config.py`
- Modify: `config.example.yml` (generated — do not hand-edit)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `cfg["gap_context"]` dict with keys `enabled, provider, api_base, model, api_key, temperature, thinking, timeout, max_retries, retry_temp_step, retry_temp_cap, min_gap, frame_width, prompt`. Every later task reads this. Also `config.GAP_CONTEXT_PROMPT` and an extended `config.DIRECTOR_PROMPT`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
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
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        get_effective_config(None, {"gap_context": {"nonesuch": 1}})


def test_director_prompt_documents_gap_annotations():
    # The director must be told what the `[silent gap ...]` annotation line means
    # and that a `keep` op spanning the adjacent lines rescues the moment.
    from nagare_clip.config import DIRECTOR_PROMPT

    assert "[silent gap" in DIRECTOR_PROMPT
    assert "keep" in DIRECTOR_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k gap_context -v`
Expected: FAIL — `KeyError: 'gap_context'` (and the DIRECTOR_PROMPT assertion fails on `[silent gap`).

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/config.py`, after `GUIDED_EDIT_PROMPT` (~line 201) add:

```python
GAP_CONTEXT_PROMPT = (
    "You are watching frames sampled from a SILENT gap in a video (no one is "
    "speaking). Describe what is happening ON SCREEN during the gap.\n"
    "\n"
    "The frames are in chronological order (start, middle, end of the gap).\n"
    "Focus on ACTION and CHANGE between the frames: is something happening "
    "(a demo running, code being typed, a game being played, a result "
    "appearing), or is the screen essentially static/dead air?\n"
    "\n"
    "Rules:\n"
    "- Answer in ONE or TWO short sentences of plain text. No JSON, no "
    "markdown, no preamble.\n"
    "- If nothing meaningful happens, say so plainly (e.g. 'Static screen, "
    "no visible activity.').\n"
    "- Describe only what you can see; do not speculate about the audio."
)
```

Inside `DIRECTOR_PROMPT`, immediately after the existing `"Timing: ..."` paragraph (i.e. after the line ending `'dead air (already dropped by default unless you "keep" them).\n'`), insert:

```python
    "\n"
    "Visual context: an indented line like\n"
    "    [silent gap 12.4s: a build runs and logs scroll past]\n"
    "may follow a numbered line. It describes what is VISIBLE on screen "
    "during the silence after that line (nobody is speaking). Such gaps are "
    "dropped by default. If the gap shows something worth watching, emit a "
    '"keep" op spanning that line and the next one — a keep over lines '
    "[N, N+1] preserves the silence between them. Annotation lines are not "
    "numbered; never reference them as op lines.\n"
```

Add the model after `GuidedEditConfig` (before `CaptionConfig`):

```python
class GapContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    section_comment: ClassVar[str] = (
        "gap_context stage: runs per video between sentence_split and summary. Long\n"
        "silent spans from the audio_silence cut list are snapshotted (up to 3 frames\n"
        "each, via ffmpeg in the whisperx image) and described by a VISION LLM, so the\n"
        "summary and director stages can see what happens on screen while nobody is\n"
        "speaking (and keep a gap worth watching). Output {stem}_gaps.json is a\n"
        "reviewable intermediate. Disabled by default (writes an empty gap list = no-op)."
    )
    enabled: bool = Field(False, description="Enable the gap_context vision LLM")
    provider: str = Field(
        "ollama_chat",
        description="LiteLLM provider prefix: ollama_chat | openai | gemini | anthropic",
    )
    api_base: str = Field(
        "",
        description="Base URL; empty -> Ollama localhost default; leave empty for cloud providers",
    )
    model: str = Field(
        "qwen2.5vl:7b",
        description='A VISION-capable model (passed to LiteLLM as "<provider>/<model>")',
    )
    api_key: str = Field("", description="API key for the provider (or set the provider's env var)")
    temperature: float = Field(0.2)
    thinking: bool | str = Field(False)
    timeout: int = Field(300)
    max_retries: int = Field(
        2, description="Extra attempts on LLM error / empty response (0 = single attempt)"
    )
    retry_temp_step: float = Field(0.2)
    retry_temp_cap: float = Field(0.8)
    min_gap: float = Field(
        3.0, description="Only silent spans at least this long (seconds) get snapshots"
    )
    frame_width: int = Field(
        960, description="Downscale width (px) of the extracted JPEG frames; height is auto"
    )
    prompt: str = _commented(
        GAP_CONTEXT_PROMPT, sample='"..."', description="System prompt (has a sensible default)"
    )
```

Register it on the root model (`NagareClipConfig`), between `sentence_split` and `summary` to mirror stage order:

```python
    sentence_split: SentenceSplitConfig = Field(default_factory=SentenceSplitConfig)
    gap_context: GapContextConfig = Field(default_factory=GapContextConfig)
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
```

- [ ] **Step 4: Regenerate the example config and run the tests**

Run: `make config-example && uv run pytest tests/test_config.py -v`
Expected: PASS (including `test_example_file_matches_generator`, which now sees the new section in both places).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/config.py config.example.yml tests/test_config.py
git commit -m "feat(gap_context): config section + vision prompt; document gap annotations in director prompt"
```

---

### Task 2: Pure snapshot helpers + Docker frame command

**Files:**
- Create: `src/nagare_clip/gap_context/__init__.py` (empty)
- Create: `src/nagare_clip/gap_context/snapshot.py`
- Modify: `src/nagare_clip/pipeline/external.py`
- Test: `tests/gap_context/__init__.py` (empty), `tests/gap_context/test_snapshot.py`, `tests/pipeline/test_external.py`

**Interfaces:**
- Consumes: `cfg["gap_context"]["min_gap"]`, `["frame_width"]` (Task 1); `audio_silence.cuts_file.read_cuts(path) -> list[tuple[float, float]]` (existing).
- Produces:
  - `snapshot.select_gaps(ranges: list[tuple[float, float]], min_gap: float) -> list[tuple[float, float]]`
  - `snapshot.frame_times(start: float, end: float) -> list[float]`
  - `snapshot.frame_relpath(stem: str, t: float) -> str` — e.g. `"frames/talk1/12.600.jpg"`, relative to the stage dir
  - `external.build_snapshot_cmd(project_root: Path, relative: str, time_s: float, out_container_path: str, width: int) -> list[str]`

- [ ] **Step 1: Write the failing tests**

`tests/gap_context/test_snapshot.py`:

```python
from nagare_clip.gap_context.snapshot import frame_relpath, frame_times, select_gaps


def test_select_gaps_keeps_only_spans_at_or_above_min_gap():
    ranges = [(1.0, 2.0), (10.0, 13.0), (20.0, 32.0)]
    assert select_gaps(ranges, 3.0) == [(10.0, 13.0), (20.0, 32.0)]


def test_select_gaps_sorts_by_start():
    assert select_gaps([(20.0, 30.0), (5.0, 10.0)], 3.0) == [(5.0, 10.0), (20.0, 30.0)]


def test_select_gaps_empty_when_none_long_enough():
    assert select_gaps([(1.0, 2.0)], 3.0) == []


def test_frame_times_start_mid_end_inside_the_span():
    # 0.2s inset keeps the frames off the boundary (where the previous/next
    # word may still be on screen).
    assert frame_times(10.0, 20.0) == [10.2, 15.0, 19.8]


def test_frame_times_dedupes_on_a_short_span():
    # A 3.0s span: start 3.2 / mid 4.5 / end 5.8 are all distinct...
    assert len(frame_times(3.0, 6.0)) == 3
    # ...but a span so short the inset frames collide yields fewer, still >= 1.
    times = frame_times(10.0, 10.3)
    assert times == [10.15]


def test_frame_relpath_is_stable_and_stem_scoped():
    assert frame_relpath("talk1", 12.6) == "frames/talk1/12.600.jpg"
```

Append to `tests/pipeline/test_external.py`:

```python
def test_build_snapshot_cmd(tmp_path):
    from nagare_clip.pipeline.external import build_snapshot_cmd

    cmd = build_snapshot_cmd(
        tmp_path, "talk1.mp4", 12.6, "/output/gap_context/frames/talk1/12.600.jpg", 960
    )
    assert cmd[:3] == ["docker", "compose", "-f"]
    assert "--entrypoint" in cmd and cmd[cmd.index("--entrypoint") + 1] == "ffmpeg"
    # input-side -ss (fast seek), before -i
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "12.600"
    assert cmd[cmd.index("-i") + 1] == "talk1.mp4"
    assert cmd[cmd.index("-vf") + 1] == "scale=960:-2"
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[-1] == "/output/gap_context/frames/talk1/12.600.jpg"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/gap_context/test_snapshot.py tests/pipeline/test_external.py -v`
Expected: FAIL — `ModuleNotFoundError: nagare_clip.gap_context` and `ImportError: cannot import name 'build_snapshot_cmd'`.

- [ ] **Step 3: Write minimal implementation**

`src/nagare_clip/gap_context/__init__.py`: empty file.

`src/nagare_clip/gap_context/snapshot.py`:

```python
"""Pure helpers: which silent gaps get snapshots, and at which timestamps.

No I/O, no Docker — the pipeline adapter runs ffmpeg with what these return.
"""

from __future__ import annotations

# Keep frames off the exact boundary: the previous/next word may still be
# on screen (and WhisperX often stretches a word across the pause edge).
_INSET = 0.2


def select_gaps(
    ranges: list[tuple[float, float]], min_gap: float
) -> list[tuple[float, float]]:
    """Silent spans at least *min_gap* seconds long, sorted by start."""
    return sorted((s, e) for s, e in ranges if e - s >= min_gap)


def frame_times(start: float, end: float) -> list[float]:
    """Up to 3 timestamps inside the span: start+inset, midpoint, end-inset.

    Rounded to milliseconds and de-duplicated (a very short span collapses to
    a single midpoint frame), so the caller always gets at least one time.
    """
    mid = round((start + end) / 2, 3)
    candidates = [round(start + _INSET, 3), mid, round(end - _INSET, 3)]
    out: list[float] = []
    for t in candidates:
        if start <= t <= end and t not in out:
            out.append(t)
    return sorted(out) if out else [mid]


def frame_relpath(stem: str, t: float) -> str:
    """Frame path relative to the gap_context stage dir."""
    return f"frames/{stem}/{t:.3f}.jpg"
```

In `src/nagare_clip/pipeline/external.py`, after `build_silencedetect_cmd`:

```python
def build_snapshot_cmd(
    project_root: Path,
    relative: str,
    time_s: float,
    out_container_path: str,
    width: int,
) -> list[str]:
    """One JPEG frame at *time_s*, via ffmpeg inside the whisperx image."""
    return [
        *_compose_prefix(project_root),
        "--entrypoint",
        "ffmpeg",
        "whisperx",
        "-hide_banner",
        "-nostats",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{time_s:.3f}",
        "-i",
        relative,
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "4",
        out_container_path,
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gap_context/test_snapshot.py tests/pipeline/test_external.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check the new tests**

Temporarily change `select_gaps`'s `>=` to `>` and `_INSET` to `0.0`; run the tests and confirm `test_select_gaps_keeps_only_spans_at_or_above_min_gap` (boundary case would need a 3.0s span — add one if it does not fail) and `test_frame_times_start_mid_end_inside_the_span` fail. Revert both.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/gap_context src/nagare_clip/pipeline/external.py tests/gap_context tests/pipeline/test_external.py
git commit -m "feat(gap_context): pure gap selection/frame timing + ffmpeg snapshot command"
```

---

### Task 3: The `{stem}_gaps.json` contract

**Files:**
- Create: `src/nagare_clip/gap_context/gaps.py`
- Test: `tests/gap_context/test_gaps.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `gaps.Gap` dataclass: `start: float`, `end: float`, `frames: list[str]`, `description: str`; property `duration -> float`
  - `gaps.gaps_to_dict(gaps: list[Gap]) -> dict` → `{"gaps": [{"start","end","frames","description"}]}`
  - `gaps.gaps_from_dict(data: Any) -> list[Gap]` — lenient
  - `gaps.load_gaps(path: Path | None) -> list[Gap]` — missing/unreadable/malformed file → `[]`

- [ ] **Step 1: Write the failing test**

`tests/gap_context/test_gaps.py`:

```python
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
            {"start": 1.0, "end": 2.0, "description": "ok"},          # frames optional
            {"start": "nope", "end": 2.0, "description": "bad start"},
            {"start": 1.0, "end": 2.0},                                 # no description
            {"start": 1.0, "end": 2.0, "description": ""},              # empty description
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gap_context/test_gaps.py -v`
Expected: FAIL — `ModuleNotFoundError: nagare_clip.gap_context.gaps`.

- [ ] **Step 3: Write minimal implementation**

`src/nagare_clip/gap_context/gaps.py`:

```python
"""The {stem}_gaps.json contract: described silent gaps, purely time-based.

Consumers (summary, director) anchor these to transcript lines themselves, so
no line numbers are baked into the artifact.  Reading is lenient: a malformed
entry is dropped (logged), never raised, so a hand-edited file cannot break the
pipeline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Gap:
    start: float
    end: float
    frames: list[str] = field(default_factory=list)  # relative to the stage dir
    description: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


def gaps_to_dict(gaps: list[Gap]) -> dict[str, Any]:
    return {
        "gaps": [
            {
                "start": g.start,
                "end": g.end,
                "frames": list(g.frames),
                "description": g.description,
            }
            for g in gaps
        ]
    }


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def gaps_from_dict(data: Any) -> list[Gap]:
    if not isinstance(data, dict) or not isinstance(data.get("gaps"), list):
        return []
    out: list[Gap] = []
    for raw in data["gaps"]:
        if not isinstance(raw, dict):
            logger.warning("gap_context: dropping non-object gap entry")
            continue
        start = _coerce_float(raw.get("start"))
        end = _coerce_float(raw.get("end"))
        description = raw.get("description")
        if start is None or end is None or end <= start:
            logger.warning("gap_context: dropping gap with bad start/end: %r", raw)
            continue
        if not isinstance(description, str) or not description.strip():
            logger.warning("gap_context: dropping gap without a description: %r", raw)
            continue
        frames = raw.get("frames")
        frames = [f for f in frames if isinstance(f, str)] if isinstance(frames, list) else []
        out.append(
            Gap(start=start, end=end, frames=frames, description=description.strip())
        )
    return out


def load_gaps(path: Path | None) -> list[Gap]:
    """Read a gaps file. Missing/unreadable/invalid ⇒ ``[]`` (logged)."""
    if path is None or not Path(path).is_file():
        return []
    try:
        return gaps_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.warning("gap_context: could not read %s: %s", path, e)
        return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/gap_context/test_gaps.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/gap_context/gaps.py tests/gap_context/test_gaps.py
git commit -m "feat(gap_context): {stem}_gaps.json dataclass + lenient (de)serialisation"
```

---

### Task 4: Vision call (multimodal messages + retries)

**Files:**
- Create: `src/nagare_clip/gap_context/describe.py`
- Modify: `src/nagare_clip/llm_client.py` (typing only)
- Modify: `src/nagare_clip/llm_report.py` (`STAGE_ORDER`)
- Test: `tests/gap_context/test_describe.py`

**Interfaces:**
- Consumes: `snapshot` helpers (Task 2), `Gap` (Task 3), `cfg["gap_context"]` (Task 1), existing `llm_retry.retry_attempts` / `cfg_for_attempt`, `llm_client.with_trace_meta`, `llm_report.Recorder` + outcome constants.
- Produces:
  - `describe.GapFrames` dataclass: `start: float`, `end: float`, `frames: list[Path]` (absolute), `relpaths: list[str]`
  - `describe.build_messages(gf: GapFrames, cfg: dict, *, before: str = "", after: str = "") -> list[dict[str, Any]]`
  - `describe.describe_gap(gf: GapFrames, cfg: dict, *, unit: str, before: str = "", after: str = "", call_llm=..., recorder=NULL_RECORDER) -> Gap | None` — `None` when every attempt failed.

- [ ] **Step 1: Write the failing test**

`tests/gap_context/test_describe.py`:

```python
import base64

import pytest

from nagare_clip.gap_context.describe import GapFrames, build_messages, describe_gap
from nagare_clip.gap_context.gaps import Gap


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

    def fake_llm(messages, cfg):  # pragma: no cover - must not be called
        raise AssertionError("LLM must not be called without frames")

    assert describe_gap(gf, CFG, unit="u", call_llm=fake_llm) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gap_context/test_describe.py -v`
Expected: FAIL — `ModuleNotFoundError: nagare_clip.gap_context.describe`.

- [ ] **Step 3: Write minimal implementation**

`src/nagare_clip/gap_context/describe.py`:

```python
"""One vision-LLM call per silent gap: frames in, a plain-text description out.

The frames are sent as base64 data-URI ``image_url`` content parts; LiteLLM
converts them to whatever the configured provider expects, so no
provider-specific client is needed.  The response is plain text (no JSON to
fail on): an empty/whitespace answer counts as a failure and is retried.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nagare_clip.gap_context.gaps import Gap
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_client import with_trace_meta
from nagare_clip.llm_report import LLM_ERROR, NULL_RECORDER, OK, UNPARSEABLE, Recorder
from nagare_clip.llm_retry import cfg_for_attempt, retry_attempts

logger = logging.getLogger(__name__)

CallLLM = Callable[[list[dict[str, Any]], dict[str, Any]], str]


@dataclass
class GapFrames:
    """A selected gap plus the frames actually extracted for it."""

    start: float
    end: float
    frames: list[Path] = field(default_factory=list)  # absolute, on disk
    relpaths: list[str] = field(default_factory=list)  # recorded in gaps.json

    @property
    def duration(self) -> float:
        return self.end - self.start


def _image_part(path: Path) -> dict[str, Any] | None:
    """Base64 data-URI content part; ``None`` when the file cannot be read."""
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.warning("gap_context: could not read frame %s: %s", path, e)
        return None
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _header_text(gf: GapFrames, before: str, after: str) -> str:
    lines = [
        f"Silent gap: {gf.start:.1f}s - {gf.end:.1f}s ({gf.duration:.1f}s long).",
        f"{len(gf.frames)} frame(s) sampled in chronological order.",
    ]
    if before:
        lines.append(f"Spoken line before the gap: {before}")
    if after:
        lines.append(f"Spoken line after the gap: {after}")
    return "\n".join(lines)


def _content_parts(
    gf: GapFrames, before: str, after: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """User content parts + the relpaths of the frames that made it in."""
    images: list[dict[str, Any]] = []
    kept: list[str] = []
    for i, path in enumerate(gf.frames):
        part = _image_part(path)
        if part is None:
            continue
        images.append(part)
        if i < len(gf.relpaths):
            kept.append(gf.relpaths[i])
    if not images:
        return [], []
    header = _header_text(
        GapFrames(start=gf.start, end=gf.end, frames=gf.frames[: len(images)], relpaths=kept),
        before,
        after,
    )
    return [{"type": "text", "text": header}, *images], kept


def build_messages(
    gf: GapFrames, cfg: dict[str, Any], *, before: str = "", after: str = ""
) -> list[dict[str, Any]]:
    """System prompt + one multimodal user message (header text, then frames)."""
    parts, _ = _content_parts(gf, before, after)
    return [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": parts},
    ]


def _report_messages(messages: list[dict[str, Any]], relpaths: list[str]) -> list[dict[str, str]]:
    """Flatten for the LLM report: frame PATHS, never base64 payloads."""
    system = str(messages[0].get("content", ""))
    parts = messages[1].get("content", [])
    header = parts[0]["text"] if parts and parts[0].get("type") == "text" else ""
    user = header + "\n\nFrames:\n" + "\n".join(f"- {p}" for p in relpaths)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def describe_gap(
    gf: GapFrames,
    cfg: dict[str, Any],
    *,
    unit: str,
    before: str = "",
    after: str = "",
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
) -> Gap | None:
    """Describe one gap. Returns ``None`` when it should be skipped."""
    parts, relpaths = _content_parts(gf, before, after)
    if not parts:
        logger.warning(
            "gap_context: no readable frame for gap %.1f-%.1f; skipping", gf.start, gf.end
        )
        return None
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": parts},
    ]
    report_messages = _report_messages(messages, relpaths)

    cfg = with_trace_meta(cfg, stage=recorder.stage, unit=unit)
    attempts = retry_attempts(cfg)
    for attempt in range(attempts):
        attempt_cfg = cfg_for_attempt(cfg, attempt)
        try:
            response = call_llm(messages, attempt_cfg)
        except Exception as e:  # noqa: BLE001 - recoverable
            logger.warning(
                "gap_context: LLM call failed (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                unit,
                exc_info=True,
            )
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=report_messages,
                error=str(e),
                outcome=LLM_ERROR,
                reason="LLM call failed",
                cfg=attempt_cfg,
            )
            continue
        description = (response or "").strip()
        if not description:
            recorder.attempt(
                unit=unit,
                attempt=attempt,
                total=attempts,
                messages=report_messages,
                response=response,
                outcome=UNPARSEABLE,
                reason="empty description",
                cfg=attempt_cfg,
            )
            logger.warning(
                "gap_context: empty description (attempt %d/%d) for %s",
                attempt + 1,
                attempts,
                unit,
            )
            continue
        recorder.attempt(
            unit=unit,
            attempt=attempt,
            total=attempts,
            messages=report_messages,
            response=response,
            outcome=OK,
            cfg=attempt_cfg,
        )
        recorder.flush_unit(unit, outcome=OK)
        return Gap(start=gf.start, end=gf.end, frames=relpaths, description=description)

    recorder.flush_unit(unit, outcome=LLM_ERROR, reason=f"all {attempts} attempt(s) failed")
    logger.warning("gap_context: all %d attempt(s) failed for %s; gap dropped", attempts, unit)
    return None
```

In `src/nagare_clip/llm_client.py`, widen the signature so a multimodal message (whose `content` is a list of parts) type-checks — the body is unchanged:

```python
def call_llm(messages: list[dict[str, Any]], cfg: dict[str, Any]) -> str:
```

In `src/nagare_clip/llm_report.py`, add the stage to the index ordering:

```python
STAGE_ORDER = ["text_filter", "gap_context", "summary", "plan", "director", "guided_edit"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gap_context/test_describe.py tests/test_llm_report.py tests/test_llm_client.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check**

Temporarily make `describe_gap` accept an empty description (drop the `if not description` guard); run `uv run pytest tests/gap_context/test_describe.py -k empty -v` and confirm `test_describe_gap_retries_an_empty_response_then_succeeds` fails. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/gap_context/describe.py src/nagare_clip/llm_client.py src/nagare_clip/llm_report.py tests/gap_context/test_describe.py
git commit -m "feat(gap_context): per-gap vision call with base64 frames + bounded retries"
```

---

### Task 5: `run_gap_context()` entry point

**Files:**
- Create: `src/nagare_clip/gap_context/run.py`
- Test: `tests/gap_context/test_run.py`

**Interfaces:**
- Consumes: `GapFrames`, `describe_gap` (Task 4); `gaps_to_dict` (Task 3); `timing.segment_times` (existing); `cfg["gap_context"]` (Task 1).
- Produces: `run.run_gap_context(gap_frames: list[GapFrames], output: Path, cfg: dict, *, stem: str, json_path: Path | None = None, recorder: Recorder = NULL_RECORDER) -> None` — writes `{stem}_gaps.json`. The frame extraction itself happens in the pipeline adapter (Task 6), which passes the `GapFrames` in.

- [ ] **Step 1: Write the failing test**

`tests/gap_context/test_run.py`:

```python
import json

from nagare_clip.gap_context.describe import GapFrames
from nagare_clip.gap_context.run import run_gap_context

BASE_CFG = {
    "gap_context": {
        "enabled": True,
        "prompt": "P",
        "max_retries": 0,
        "temperature": 0.2,
        "model": "m",
    }
}


def _frames(tmp_path, *names):
    out = []
    for n in names:
        p = tmp_path / n
        p.write_bytes(b"jpeg")
        out.append(p)
    return out


def test_disabled_writes_an_empty_gap_list(tmp_path):
    out = tmp_path / "a_gaps.json"
    run_gap_context([], out, {"gap_context": {"enabled": False}}, stem="a")
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_writes_a_described_gap(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    monkeypatch.setattr(run_mod, "_call_llm", lambda messages, cfg: "画面でビルドが走っている")
    gf = GapFrames(
        start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["frames/a/10.200.jpg"]
    )
    out = tmp_path / "a_gaps.json"
    run_gap_context([gf], out, BASE_CFG, stem="a")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == {
        "gaps": [
            {
                "start": 10.0,
                "end": 20.0,
                "frames": ["frames/a/10.200.jpg"],
                "description": "画面でビルドが走っている",
            }
        ]
    }


def test_a_failing_gap_is_dropped_not_fatal(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    def boom(messages, cfg):
        raise ConnectionError("down")

    monkeypatch.setattr(run_mod, "_call_llm", boom)
    gf = GapFrames(start=1.0, end=9.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    out = tmp_path / "a_gaps.json"
    run_gap_context([gf], out, BASE_CFG, stem="a")
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_neighbour_lines_are_passed_to_the_llm(tmp_path, monkeypatch):
    import nagare_clip.gap_context.run as run_mod

    seen = {}

    def fake(messages, cfg):
        seen["text"] = messages[1]["content"][0]["text"]
        return "d"

    monkeypatch.setattr(run_mod, "_call_llm", fake)
    jp = tmp_path / "a.json"
    jp.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 10.0, "text": "ここでビルドします"},
                    {"start": 20.0, "end": 25.0, "text": "できました"},
                ]
            }
        ),
        encoding="utf-8",
    )
    gf = GapFrames(start=10.0, end=20.0, frames=_frames(tmp_path, "f.jpg"), relpaths=["f.jpg"])
    run_gap_context([gf], tmp_path / "a_gaps.json", BASE_CFG, stem="a", json_path=jp)
    assert "ここでビルドします" in seen["text"]
    assert "できました" in seen["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gap_context/test_run.py -v`
Expected: FAIL — `ModuleNotFoundError: nagare_clip.gap_context.run`.

- [ ] **Step 3: Write minimal implementation**

`src/nagare_clip/gap_context/run.py`:

```python
"""gap_context stage: describe the long silent gaps of one video.

Frame extraction (ffmpeg in the whisperx image) happens in the pipeline
adapter, which hands the extracted ``GapFrames`` here.  When
``gap_context.enabled`` is false (default) an empty gap list is written, so the
summary/director prompts stay byte-identical and the pipeline behaves exactly
as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from nagare_clip.gap_context.describe import GapFrames, describe_gap
from nagare_clip.gap_context.gaps import Gap, gaps_to_dict
from nagare_clip.llm_client import call_llm as _call_llm
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.timing import segment_times

logger = logging.getLogger(__name__)


def _neighbour_lines(
    gf: GapFrames, segments: list[dict[str, Any]], seg_times
) -> tuple[str, str]:
    """Text of the last line ending at/before the gap and the first starting at/after it."""
    before = after = ""
    for (start, end), seg in zip(seg_times, segments, strict=False):
        text = seg.get("text", "") if isinstance(seg, dict) else ""
        if not isinstance(text, str):
            continue
        if end is not None and end <= gf.start + 0.01:
            before = text.strip()
        if after == "" and start is not None and start >= gf.end - 0.01:
            after = text.strip()
    return before, after


def run_gap_context(
    gap_frames: list[GapFrames],
    output: Path,
    cfg: dict,
    *,
    stem: str,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    gc_cfg = cfg["gap_context"]

    gaps: list[Gap] = []
    if not gc_cfg.get("enabled", False):
        logging.info("gap_context: disabled, writing empty gap list")
    else:
        segments: list[dict[str, Any]] = []
        seg_times: list = []
        if json_path and Path(json_path).is_file():
            try:
                data = json.loads(Path(json_path).read_text(encoding="utf-8"))
                segments = data.get("segments", []) or []
                seg_times = segment_times(data)
            except (ValueError, OSError):
                logging.warning("gap_context: could not read --json %s", json_path)
        logging.info("gap_context: describing %d gap(s) for %s", len(gap_frames), stem)
        for i, gf in enumerate(gap_frames):
            before, after = ("", "")
            if segments and seg_times:
                before, after = _neighbour_lines(gf, segments, seg_times)
            gap = describe_gap(
                gf,
                gc_cfg,
                unit=f"{stem}_gap{i + 1:02d}",
                before=before,
                after=after,
                call_llm=_call_llm,
                recorder=recorder,
            )
            if gap is not None:
                gaps.append(gap)
        logging.info("gap_context: %d gap(s) described for %s", len(gaps), stem)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(gaps_to_dict(gaps), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("gap_context: wrote %s", output)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gap_context/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/gap_context/run.py tests/gap_context/test_run.py
git commit -m "feat(gap_context): run_gap_context() writes {stem}_gaps.json"
```

---

### Task 6: Pipeline registration + frame extraction adapter

**Files:**
- Modify: `src/nagare_clip/pipeline/stages.py`
- Test: `tests/pipeline/test_stages.py`

**Interfaces:**
- Consumes: `build_snapshot_cmd` (Task 2), `select_gaps` / `frame_times` / `frame_relpath` (Task 2), `GapFrames` (Task 4), `run_gap_context` (Task 5), `read_cuts` (existing), `run_command` (existing).
- Produces: stage `"gap_context"` in `STAGE_NAMES` (between `sentence_split` and `summary`) and in `STAGES`; required output `output/gap_context/{stem}_gaps.json`. Frames land at `output/gap_context/frames/{stem}/{t}.jpg`; the container path is `/output/gap_context/frames/{stem}/{t}.jpg` (the compose service already mounts the output dir at `/output`, as the transcription stage's `--output_dir /output/transcription` shows).

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_stages.py` (follow the file's existing fixture/context-building style; the version below constructs a `PipelineContext` directly):

```python
def test_gap_context_is_between_sentence_split_and_summary():
    from nagare_clip.pipeline.stages import STAGE_NAMES, STAGES

    assert STAGE_NAMES.index("gap_context") == STAGE_NAMES.index("sentence_split") + 1
    assert STAGE_NAMES.index("gap_context") == STAGE_NAMES.index("summary") - 1
    assert [s.name for s in STAGES] == STAGE_NAMES


def test_gap_context_required_outputs(gap_ctx):
    from nagare_clip.pipeline.stages import STAGES

    stage = next(s for s in STAGES if s.name == "gap_context")
    assert stage.required_outputs(gap_ctx) == [
        gap_ctx.stage_dir("gap_context") / "talk1_gaps.json"
    ]


def test_gap_context_extracts_a_frame_per_gap_time_and_runs_the_stage(gap_ctx, monkeypatch):
    """Long cut spans -> one docker frame command per frame time; short ones ignored."""
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("1.000 - 2.000\n10.000 - 20.000\n", encoding="utf-8")

    commands = []

    def fake_run_command(cmd, **kwargs):
        commands.append(cmd)
        # Materialise the file ffmpeg would have written (last arg = container path).
        rel = cmd[-1].removeprefix("/output/")
        p = gap_ctx.output_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"jpeg")

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured["gap_frames"] = gap_frames
        captured["stem"] = kwargs["stem"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", fake_run_command)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    # Only the 10s gap qualifies (min_gap 3.0): 3 frames.
    assert len(commands) == 3
    gfs = captured["gap_frames"]
    assert len(gfs) == 1
    assert (gfs[0].start, gfs[0].end) == (10.0, 20.0)
    assert len(gfs[0].frames) == 3
    assert gfs[0].relpaths[0] == "frames/talk1/10.200.jpg"
    assert captured["stem"] == "talk1"


def test_gap_context_disabled_runs_no_docker(gap_ctx, monkeypatch):
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def fail(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("docker must not run when gap_context is disabled")

    monkeypatch.setattr(stages_mod, "run_command", fail)
    gap_ctx.cfg["gap_context"]["enabled"] = False

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)

    out = gap_ctx.stage_dir("gap_context") / "talk1_gaps.json"
    assert json.loads(out.read_text(encoding="utf-8")) == {"gaps": []}


def test_gap_context_skips_a_frame_ffmpeg_failed_to_write(gap_ctx, monkeypatch):
    import nagare_clip.pipeline.stages as stages_mod

    cuts = gap_ctx.stage_dir("audio_silence") / "talk1_cuts.txt"
    cuts.parent.mkdir(parents=True, exist_ok=True)
    cuts.write_text("10.000 - 20.000\n", encoding="utf-8")

    def flaky(cmd, **kwargs):
        raise subprocess.CalledProcessError(1, cmd)

    captured = {}

    def fake_run_gap_context(gap_frames, output, cfg, **kwargs):
        captured["gap_frames"] = gap_frames
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"gaps": []}\n', encoding="utf-8")

    monkeypatch.setattr(stages_mod, "run_command", flaky)
    monkeypatch.setattr(stages_mod, "run_gap_context", fake_run_gap_context)
    gap_ctx.cfg["gap_context"]["enabled"] = True

    stage = next(s for s in stages_mod.STAGES if s.name == "gap_context")
    stage.run(gap_ctx)  # must not raise

    assert captured["gap_frames"] == []  # gap with zero frames is skipped
```

Add the imports the tests need at the top of the file (`import json`, `import subprocess`) and a `gap_ctx` fixture built the same way the file's existing context fixture is (a `PipelineContext` with `cfg=get_effective_config(None, {})`, `output_dir=tmp_path/"output"`, one `SourceMedia` with stem `talk1`, `from_index=0`, `to_index=len(STAGE_NAMES) - 1`). If the file already has such a fixture, reuse it rather than adding a second.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/pipeline/test_stages.py -k gap_context -v`
Expected: FAIL — `ValueError: 'gap_context' is not in list` / `StopIteration` (no such stage).

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/pipeline/stages.py` add imports:

```python
from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.gap_context.describe import GapFrames
from nagare_clip.gap_context.run import run_gap_context
from nagare_clip.gap_context.snapshot import frame_relpath, frame_times, select_gaps
from nagare_clip.pipeline.external import (
    build_blender_cmd,
    build_silencedetect_cmd,
    build_snapshot_cmd,
    build_transcription_cmd,
    run_command,
)
```

Insert `"gap_context"` into `STAGE_NAMES` between `"sentence_split"` and `"summary"`, and add the adapter (after the sentence_split section):

```python
# --- gap_context -------------------------------------------------------------


def _extract_gap_frames(ctx: PipelineContext, src, gaps: list[tuple[float, float]]) -> list:
    """Snapshot each gap via ffmpeg in the whisperx image; skip what fails."""
    width = ctx.cfg["gap_context"]["frame_width"]
    d = ctx.stage_dir("gap_context")
    out: list[GapFrames] = []
    for start, end in gaps:
        frames: list[Path] = []
        relpaths: list[str] = []
        for t in frame_times(start, end):
            rel = frame_relpath(src.stem, t)
            host_path = d / rel
            host_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                run_command(
                    build_snapshot_cmd(
                        ctx.project_root,
                        src.relative,
                        t,
                        f"/output/gap_context/{rel}",
                        width,
                    ),
                    env_extra=_docker_env(ctx),
                )
            except Exception:  # noqa: BLE001 - a missing frame must never abort the run
                logging.warning(
                    "gap_context: frame extraction failed at %.3fs for %s", t, src.stem
                )
                continue
            if not host_path.is_file():
                logging.warning("gap_context: no frame written at %.3fs for %s", t, src.stem)
                continue
            frames.append(host_path)
            relpaths.append(rel)
        if not frames:
            logging.warning(
                "gap_context: no frames for gap %.1f-%.1f in %s; skipping", start, end, src.stem
            )
            continue
        out.append(GapFrames(start=start, end=end, frames=frames, relpaths=relpaths))
    return out


def _gap_context_run(ctx: PipelineContext) -> None:
    g = ctx.cfg["gap_context"]
    rec = _recorder(ctx, "gap_context")
    rec.clear()
    try:
        adir = ctx.stage_dir("audio_silence")
        odir = ctx.stage_dir("gap_context")
        for src in ctx.sources:
            print(f"[gap_context] Silent-gap visual context: {src.stem}")
            gap_frames = []
            if g["enabled"]:
                gaps = select_gaps(read_cuts(adir / f"{src.stem}_cuts.txt"), g["min_gap"])
                gap_frames = _extract_gap_frames(ctx, src, gaps)
            run_gap_context(
                gap_frames,
                odir / f"{src.stem}_gaps.json",
                ctx.cfg,
                stem=src.stem,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _gap_context_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("gap_context")
    return [d / f"{s}_gaps.json" for s in ctx.stems]
```

Add `import logging` at the top of the module if it is not already imported, and register the stage in `STAGES` between `sentence_split` and `summary`:

```python
    Stage("sentence_split", _sentence_split_run, _sentence_split_required),
    Stage("gap_context", _gap_context_run, _gap_context_required),
    Stage("summary", _summary_run, _summary_required),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline/ -v`
Expected: PASS (existing pipeline/CLI tests included — they enumerate stage names, so confirm none hard-code the old list; fix any that do by adding `gap_context` in position).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline/stages.py tests/pipeline/test_stages.py
git commit -m "feat(gap_context): register the stage + Docker frame extraction adapter"
```

---

### Task 7: Consumer-side context helpers (anchor / format / annotate)

**Files:**
- Create: `src/nagare_clip/gap_context/context.py`
- Test: `tests/gap_context/test_context.py`

**Interfaces:**
- Consumes: `Gap` (Task 3), `timing.segment_times` output shape `list[tuple[float | None, float | None]]`.
- Produces:
  - `context.anchor_gaps(gaps: list[Gap], seg_times) -> list[tuple[int, Gap]]` — line number (1-based) of the last line ending at/before the gap start; `0` when the gap precedes every line. Gaps whose anchor cannot be determined still get `0`.
  - `context.format_gap_block(anchored: list[tuple[int, Gap]]) -> str` — the summary block; `""` when empty.
  - `context.annotate_numbered_transcript(transcript: str, anchored: list[tuple[int, Gap]]) -> str` — inserts indented `[silent gap …]` lines after their anchor line; unchanged when empty.

- [ ] **Step 1: Write the failing test**

`tests/gap_context/test_context.py`:

```python
from nagare_clip.gap_context.context import (
    anchor_gaps,
    annotate_numbered_transcript,
    format_gap_block,
)
from nagare_clip.gap_context.gaps import Gap

SEG_TIMES = [(0.0, 10.0), (20.0, 25.0), (25.5, 30.0)]
GAP = Gap(start=10.0, end=20.0, frames=[], description="ビルドが走る")


def test_anchor_gaps_attaches_to_the_last_line_ending_before_the_gap():
    assert anchor_gaps([GAP], SEG_TIMES) == [(1, GAP)]


def test_anchor_gaps_before_the_first_line_anchors_to_zero():
    g = Gap(start=0.0, end=5.0, frames=[], description="d")
    assert anchor_gaps([g], [(6.0, 10.0)]) == [(0, g)]


def test_anchor_gaps_with_no_segment_times():
    assert anchor_gaps([GAP], []) == [(0, GAP)]


def test_anchor_gaps_empty():
    assert anchor_gaps([], SEG_TIMES) == []


def test_format_gap_block():
    assert format_gap_block([(1, GAP)]) == (
        "## Silent gaps (visual context)\n"
        "- after line 1 (10.0s-20.0s, 10.0s): ビルドが走る"
    )


def test_format_gap_block_before_the_first_line():
    g = Gap(start=0.0, end=5.0, frames=[], description="タイトル画面")
    assert format_gap_block([(0, g)]) == (
        "## Silent gaps (visual context)\n"
        "- before line 1 (0.0s-5.0s, 5.0s): タイトル画面"
    )


def test_format_gap_block_empty_is_empty_string():
    assert format_gap_block([]) == ""


def test_annotate_numbered_transcript_inserts_after_the_anchor_line():
    transcript = "1: いち  [10.0s, gap 10.0s]\n2: に  [5.0s, gap 0.5s]\n3: さん  [4.5s]"
    out = annotate_numbered_transcript(transcript, [(1, GAP)])
    assert out == (
        "1: いち  [10.0s, gap 10.0s]\n"
        "    [silent gap 10.0s: ビルドが走る]\n"
        "2: に  [5.0s, gap 0.5s]\n"
        "3: さん  [4.5s]"
    )


def test_annotate_numbered_transcript_anchor_zero_goes_first():
    g = Gap(start=0.0, end=5.0, frames=[], description="タイトル画面")
    out = annotate_numbered_transcript("1: いち", [(0, g)])
    assert out == "    [silent gap 5.0s: タイトル画面]\n1: いち"


def test_annotate_numbered_transcript_is_byte_identical_when_empty():
    transcript = "1: いち\n2: に"
    assert annotate_numbered_transcript(transcript, []) == transcript


def test_annotate_numbered_transcript_ignores_out_of_range_anchors():
    transcript = "1: いち"
    assert annotate_numbered_transcript(transcript, [(9, GAP)]) == transcript
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/gap_context/test_context.py -v`
Expected: FAIL — `ModuleNotFoundError: nagare_clip.gap_context.context`.

- [ ] **Step 3: Write minimal implementation**

`src/nagare_clip/gap_context/context.py`:

```python
"""Consumer-side rendering of described gaps (summary + director).

Pure: no I/O, no LLM.  Lives here (not in the consumers) so summary and
director share one anchoring rule and one annotation format, and so neither
stage has to import the other.
"""

from __future__ import annotations

from nagare_clip.gap_context.gaps import Gap

_EPS = 0.01
_INDENT = "    "


def anchor_gaps(
    gaps: list[Gap], seg_times: list[tuple[float | None, float | None]]
) -> list[tuple[int, Gap]]:
    """Attach each gap to the 1-based line it follows (``0`` = before line 1)."""
    out: list[tuple[int, Gap]] = []
    for gap in gaps:
        anchor = 0
        for i, (_start, end) in enumerate(seg_times):
            if end is not None and end <= gap.start + _EPS:
                anchor = i + 1
        out.append((anchor, gap))
    return out


def format_gap_block(anchored: list[tuple[int, Gap]]) -> str:
    """The ``## Silent gaps`` block appended to the summary stage's user prompt."""
    if not anchored:
        return ""
    lines = ["## Silent gaps (visual context)"]
    for anchor, gap in anchored:
        where = f"after line {anchor}" if anchor else "before line 1"
        lines.append(
            f"- {where} ({gap.start:.1f}s-{gap.end:.1f}s, {gap.duration:.1f}s): "
            f"{gap.description}"
        )
    return "\n".join(lines)


def annotate_numbered_transcript(transcript: str, anchored: list[tuple[int, Gap]]) -> str:
    """Insert indented ``[silent gap …]`` lines into a numbered transcript.

    Annotation lines are deliberately un-numbered so the director's op line
    references stay unambiguous.  An out-of-range anchor is ignored.  An empty
    *anchored* returns *transcript* unchanged (byte-identical).
    """
    if not anchored:
        return transcript
    lines = transcript.split("\n")
    by_anchor: dict[int, list[Gap]] = {}
    for anchor, gap in anchored:
        if 0 <= anchor <= len(lines):
            by_anchor.setdefault(anchor, []).append(gap)
    out: list[str] = []
    for gap in by_anchor.get(0, []):
        out.append(f"{_INDENT}[silent gap {gap.duration:.1f}s: {gap.description}]")
    for i, line in enumerate(lines):
        out.append(line)
        for gap in by_anchor.get(i + 1, []):
            out.append(f"{_INDENT}[silent gap {gap.duration:.1f}s: {gap.description}]")
    return "\n".join(out)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/gap_context/test_context.py -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check**

Temporarily change `anchor_gaps` to use `end <= gap.end` (wrong boundary) and run `uv run pytest tests/gap_context/test_context.py -k anchor -v`; confirm `test_anchor_gaps_attaches_to_the_last_line_ending_before_the_gap` fails. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/gap_context/context.py tests/gap_context/test_context.py
git commit -m "feat(gap_context): pure anchoring + summary block + director annotation helpers"
```

---

### Task 8: `summary` consumes gap descriptions

**Files:**
- Modify: `src/nagare_clip/summary/summarize.py`
- Modify: `src/nagare_clip/summary/run.py`
- Modify: `src/nagare_clip/pipeline/stages.py` (pass `gaps_paths`)
- Test: `tests/summary/test_summarize.py`, `tests/summary/test_run.py`

**Interfaces:**
- Consumes: `load_gaps` (Task 3), `anchor_gaps` / `format_gap_block` (Task 7), `timing.segment_times` (existing).
- Produces:
  - `segment_video(stem, clean_lines, cfg, *, call_llm=..., recorder=..., gap_block: str = "")` — appends `"\n\n" + gap_block` to the user content when non-empty.
  - `build_summary(parts_input, cfg, *, call_llm=..., recorder=..., seg_times_by_stem=None, gap_blocks_by_stem: dict[str, str] | None = None)`
  - `run_summary(txts, output, cfg, *, json_paths=None, gaps_paths: list[Path] | None = None, recorder=...)` — `gaps_paths` is matched to a stem by `path.stem.removesuffix("_gaps")`.
  - `summary.json` schema is unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/summary/test_summarize.py`:

```python
def test_segment_video_appends_the_gap_block_to_the_user_prompt():
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"parts": [], "keywords": [], "video_summary": "v"})

    segment_video(
        "a",
        ["いち", "に"],
        {"prompt": "P", "max_retries": 0},
        call_llm=fake_llm,
        gap_block="## Silent gaps (visual context)\n- after line 1 (1.0s-5.0s, 4.0s): デモ",
    )
    assert seen["user"].startswith("1: いち\n2: に")
    assert seen["user"].endswith(
        "\n\n## Silent gaps (visual context)\n- after line 1 (1.0s-5.0s, 4.0s): デモ"
    )


def test_segment_video_prompt_is_byte_identical_without_a_gap_block():
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"parts": [], "keywords": [], "video_summary": "v"})

    segment_video("a", ["いち", "に"], {"prompt": "P", "max_retries": 0}, call_llm=fake_llm)
    assert seen["user"] == "1: いち\n2: に"


def test_build_summary_routes_each_stem_to_its_own_gap_block():
    seen = {}

    def fake_llm(messages, cfg):
        content = messages[1]["content"]
        if "Silent gaps" in content:
            seen.setdefault("with_gaps", []).append(content)
        return json.dumps({"parts": [], "keywords": [], "video_summary": "v"})

    build_summary(
        [("a", ["いち"]), ("b", ["に"])],
        {"prompt": "P", "overall_prompt": "O", "max_retries": 0},
        call_llm=fake_llm,
        gap_blocks_by_stem={"a": "## Silent gaps (visual context)\n- after line 1 (1.0s-2.0s, 1.0s): X"},
    )
    assert len(seen["with_gaps"]) == 1
    assert seen["with_gaps"][0].startswith("1: いち")
```

Append to `tests/summary/test_run.py`:

```python
def test_run_summary_feeds_gap_descriptions_into_the_prompt(tmp_path, monkeypatch):
    import nagare_clip.summary.summarize as summarize_mod

    txt = tmp_path / "a.txt"
    txt.write_text("いち\nに\n", encoding="utf-8")
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
        seen.setdefault("users", []).append(messages[1]["content"])
        return json.dumps({"parts": [], "keywords": [], "video_summary": "v"})

    monkeypatch.setattr(summarize_mod, "_call_llm", fake_llm)
    run_summary(
        [txt],
        tmp_path / "summary.json",
        {"summary": {"enabled": True, "prompt": "P", "overall_prompt": "O", "max_retries": 0}},
        json_paths=[jsonp],
        gaps_paths=[gapsp],
    )
    assert any("after line 1" in u and "デモが動く" in u for u in seen["users"])
```

(Match the existing `tests/summary/test_run.py` monkeypatching style — if that file patches `call_llm` elsewhere, patch the same symbol.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/summary/ -v`
Expected: FAIL — `TypeError: segment_video() got an unexpected keyword argument 'gap_block'` / `run_summary() got an unexpected keyword argument 'gaps_paths'`.

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/summary/summarize.py`, `segment_video`:

```python
def segment_video(
    stem: str,
    clean_lines: list[str],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    gap_block: str = "",
) -> tuple[list[PartSummary], list[str], str]:
    """Segment one video's transcript into summarised parts, misspelling-prone
    keywords, and a whole-video summary.

    ``gap_block`` (from the gap_context stage) describes what is visible during
    this video's long silences; appended to the user content when non-empty, so
    an absent/empty block leaves the prompt byte-identical.
    """
    user_content = format_numbered_transcript(clean_lines)
    if gap_block:
        user_content = f"{user_content}\n\n{gap_block}"
    messages = [
        {"role": "system", "content": cfg.get("prompt", "")},
        {"role": "user", "content": user_content},
    ]
    # ... rest unchanged
```

`build_summary`:

```python
def build_summary(
    parts_input: list[tuple[str, list[str]]],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    recorder: Recorder = NULL_RECORDER,
    seg_times_by_stem: dict[str, list[tuple[float | None, float | None]]] | None = None,
    gap_blocks_by_stem: dict[str, str] | None = None,
) -> ProjectSummary:
    """Map (``segment_video`` per video) then reduce (``generate_project_summary``)."""
    parts: list[PartSummary] = []
    keywords: dict[str, list[str]] = {}
    video_summaries: dict[str, str] = {}
    for stem, clean_lines in parts_input:
        video_parts, video_keywords, video_summary = segment_video(
            stem,
            clean_lines,
            cfg,
            call_llm=call_llm,
            recorder=recorder,
            gap_block=(gap_blocks_by_stem or {}).get(stem, ""),
        )
        # ... rest unchanged
```

In `src/nagare_clip/summary/run.py`, add the import and the parameter:

```python
from nagare_clip.gap_context.context import anchor_gaps, format_gap_block
from nagare_clip.gap_context.gaps import load_gaps


def run_summary(
    txts: list[Path],
    output: Path,
    cfg: dict,
    *,
    json_paths: list[Path] | None = None,
    gaps_paths: list[Path] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
```

and, inside the `else` branch after `seg_times_by_stem` is built:

```python
        gap_blocks_by_stem: dict[str, str] = {}
        for gpath in gaps_paths or []:
            stem = gpath.stem.removesuffix("_gaps")
            gaps = load_gaps(gpath)
            if not gaps:
                continue
            block = format_gap_block(anchor_gaps(gaps, seg_times_by_stem.get(stem, [])))
            if block:
                gap_blocks_by_stem[stem] = block
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(
            parts_input,
            summary_cfg,
            recorder=recorder,
            seg_times_by_stem=seg_times_by_stem or None,
            gap_blocks_by_stem=gap_blocks_by_stem or None,
        )
```

In `src/nagare_clip/pipeline/stages.py`, `_summary_run`, pass the new paths:

```python
        run_summary(
            [ctx.stage_dir("sentence_split") / f"{s}.txt" for s in ctx.stems],
            ctx.stage_dir("summary") / "summary.json",
            ctx.cfg,
            json_paths=[ctx.stage_dir("sentence_split") / f"{s}.json" for s in ctx.stems],
            gaps_paths=[ctx.stage_dir("gap_context") / f"{s}_gaps.json" for s in ctx.stems],
            recorder=rec,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/summary/ tests/pipeline/ -v`
Expected: PASS.

- [ ] **Step 5: Mutation-check**

Temporarily drop the `if gap_block:` append in `segment_video`; run `uv run pytest tests/summary/ -k gap -v` and confirm the two gap tests fail while `test_segment_video_prompt_is_byte_identical_without_a_gap_block` still passes. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/summary src/nagare_clip/pipeline/stages.py tests/summary
git commit -m "feat(summary): feed gap_context visual descriptions into the segment prompt"
```

---

### Task 9: `director` consumes gap descriptions

**Files:**
- Modify: `src/nagare_clip/director/director_llm.py`
- Modify: `src/nagare_clip/director/run.py`
- Modify: `src/nagare_clip/pipeline/stages.py` (pass `gaps`)
- Test: `tests/director/test_director_llm.py`, `tests/director/test_run.py`

**Interfaces:**
- Consumes: `load_gaps` (Task 3), `anchor_gaps` / `annotate_numbered_transcript` (Task 7), the extended `DIRECTOR_PROMPT` (Task 1).
- Produces:
  - `generate_director_ops(..., gaps: list[Gap] | None = None)` — when `gaps` and `seg_times` are both present, the user content is the annotated transcript; otherwise byte-identical to today.
  - `run_director(edits_txt, output, cfg, *, summary=None, plan=None, stem=None, json_path=None, gaps: Path | None = None, recorder=...)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/director/test_director_llm.py`:

```python
def test_generate_director_ops_annotates_the_transcript_with_gaps():
    from nagare_clip.gap_context.gaps import Gap

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    generate_director_ops(
        ["いち", "に"],
        {"prompt": "P", "max_retries": 0},
        call_llm=fake_llm,
        seg_times=[(0.0, 10.0), (20.0, 25.0)],
        gaps=[Gap(start=10.0, end=20.0, frames=[], description="ビルドが走る")],
    )
    assert seen["user"] == (
        "1: いち  [10.0s, gap 10.0s]\n"
        "    [silent gap 10.0s: ビルドが走る]\n"
        "2: に  [5.0s]"
    )


def test_generate_director_ops_transcript_is_byte_identical_without_gaps():
    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    generate_director_ops(
        ["いち", "に"],
        {"prompt": "P", "max_retries": 0},
        call_llm=fake_llm,
        seg_times=[(0.0, 10.0), (20.0, 25.0)],
    )
    assert seen["user"] == "1: いち  [10.0s, gap 10.0s]\n2: に  [5.0s]"
```

Append to `tests/director/test_run.py`:

```python
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
    run_director(
        edits,
        tmp_path / "a_director.json",
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        stem="a",
        json_path=jsonp,
        gaps=gapsp,
    )
    assert "[silent gap 10.0s: デモが動く]" in seen["user"]


def test_run_director_without_a_gaps_file_is_unchanged(tmp_path, monkeypatch):
    import nagare_clip.director.director_llm as dl

    edits = tmp_path / "a_edits.txt"
    edits.write_text("いち\nに\n", encoding="utf-8")

    seen = {}

    def fake_llm(messages, cfg):
        seen["user"] = messages[1]["content"]
        return json.dumps({"ops": []})

    monkeypatch.setattr(dl, "_call_llm", fake_llm)
    run_director(
        edits,
        tmp_path / "a_director.json",
        {"director": {"enabled": True, "prompt": "P", "max_retries": 0}},
        stem="a",
        gaps=tmp_path / "missing_gaps.json",
    )
    assert seen["user"] == "1: いち\n2: に"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/director/ -v`
Expected: FAIL — `TypeError: generate_director_ops() got an unexpected keyword argument 'gaps'` / `run_director() got an unexpected keyword argument 'gaps'`.

- [ ] **Step 3: Write minimal implementation**

In `src/nagare_clip/director/director_llm.py` add the import and the parameter:

```python
from nagare_clip.gap_context.context import anchor_gaps, annotate_numbered_transcript
from nagare_clip.gap_context.gaps import Gap
```

```python
def generate_director_ops(
    edit_lines: list[str],
    cfg: dict[str, Any],
    *,
    call_llm: CallLLM = _call_llm,
    overview_context: str = "",
    recorder: Recorder = NULL_RECORDER,
    unit: str = "director",
    seg_times: list[tuple[float | None, float | None]] | None = None,
    gaps: list[Gap] | None = None,
) -> list[DirectorOp]:
```

and, where `user_content` is built (replacing the existing two-branch block):

```python
    if seg_times is not None and len(seg_times) == len(clean_lines):
        user_content = format_numbered_transcript_timed(clean_lines, seg_times)
        if gaps:
            # Indented, un-numbered `[silent gap …]` lines describing what is
            # visible during each long silence (see the director prompt).
            user_content = annotate_numbered_transcript(
                user_content, anchor_gaps(gaps, seg_times)
            )
    else:
        user_content = format_numbered_transcript(clean_lines)
```

In `src/nagare_clip/director/run.py`:

```python
from nagare_clip.gap_context.gaps import load_gaps


def run_director(
    edits_txt: Path,
    output: Path,
    cfg: dict,
    *,
    summary: Path | None = None,
    plan: Path | None = None,
    stem: str | None = None,
    json_path: Path | None = None,
    gaps: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
```

and inside the enabled branch, after `seg_times` is computed:

```python
        gap_list = load_gaps(gaps)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines,
            director_cfg,
            overview_context=overview_context,
            recorder=recorder,
            unit=unit,
            seg_times=seg_times,
            gaps=gap_list,
        )
```

In `src/nagare_clip/pipeline/stages.py`, `_director_run`, pass the path:

```python
            run_director(
                ctx.stage_dir("text_filter") / f"{src.stem}_edits.txt",
                ctx.stage_dir("director") / f"{src.stem}_director.json",
                ctx.cfg,
                summary=ctx.stage_dir("summary") / "summary.json",
                plan=ctx.stage_dir("plan") / "plan.json",
                stem=src.stem,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                gaps=ctx.stage_dir("gap_context") / f"{src.stem}_gaps.json",
                recorder=rec,
            )
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (all tests).

- [ ] **Step 5: Mutation-check**

Temporarily drop the `if gaps:` annotation branch in `generate_director_ops`; run `uv run pytest tests/director/ -k gap -v` and confirm the annotation tests fail while `test_generate_director_ops_transcript_is_byte_identical_without_gaps` still passes. Revert.

- [ ] **Step 6: Commit**

```bash
git add src/nagare_clip/director src/nagare_clip/pipeline/stages.py tests/director
git commit -m "feat(director): annotate the transcript with gap_context visual descriptions"
```

---

### Task 10: Documentation + full validation

**Files:**
- Create: `docs/stages/gap_context.md`
- Modify: `AGENTS.md`, `README.md`, `plan.md`, `docs/stages/audio_silence.md` (one cross-reference line)

**Interfaces:**
- Consumes: everything above. Produces: no code.

- [ ] **Step 1: Write `docs/stages/gap_context.md`**

Cover, at the level of detail the sibling stage docs use (`docs/stages/sentence_split.md` is the closest model):

- Purpose: describe silent on-screen action so `summary`/`director` can see what the transcript cannot.
- Gap selection: `select_gaps(read_cuts(audio_silence/{stem}_cuts.txt), min_gap)` — deleting a line from the human-editable cuts file both keeps that audio *and* removes it from snapshotting.
- Frame sampling: `frame_times()` — start+0.2s, midpoint, end−0.2s, deduplicated; the 0.2s inset avoids the boundary word.
- Extraction: `build_snapshot_cmd()` runs ffmpeg **inside the whisperx image**, one container run per frame; frames land in `output/gap_context/frames/{stem}/{t}.jpg`.
- Vision call: one call per gap, frames as base64 `image_url` parts via LiteLLM; plain-text response; empty response = failure; retries via `llm_retry`; the LLM report records frame **paths**, never base64.
- Output contract: `{stem}_gaps.json` (time-based; `gaps_from_dict` is lenient) — hand-editable.
- Consumption: `anchor_gaps()` (last line ending at/before the gap start; `0` = before line 1), the summary `## Silent gaps (visual context)` block, and the director's indented un-numbered `[silent gap …]` annotation plus the prompt rule that a `keep` over `[N, N+1]` rescues the gap.
- Degradation table (frame fails → skip frame; no frames → skip gap; LLM fails → skip gap; disabled → `{"gaps": []}`; consumers byte-identical when absent).

- [ ] **Step 2: Update `AGENTS.md`**

- Add `gap_context` to the numbered objective list (between sentence_split and summary) and to the naming-convention identifier list.
- Add a `### gap_context — Silent-Gap Visual Context` section in Pipeline Overview with **Inputs** (`{stem}_cuts.txt` from audio_silence; source video; sentence_split `{stem}.json` for neighbour lines) and **Outputs** (`output/gap_context/{stem}_gaps.json`, frames under `output/gap_context/frames/{stem}/`).
- Extend the `summary` and `director` sections with their new inputs (`gaps_paths` / `gaps`) and the byte-identical-when-absent guarantee.
- Add `gap_context/` to the Project Structure tree (`snapshot.py`, `gaps.py`, `describe.py`, `context.py`, `run.py`) and `tests/gap_context/`.
- Add the `docs/stages/gap_context.md` bullet to the Current Runtime Quirks list.
- Add `gap_context` to the list of LLM stages in the Configuration System section.

- [ ] **Step 3: Update `README.md` and `plan.md`**

`README.md`: document the `gap_context:` config block (disabled by default, needs a **vision-capable** model), the `{stem}_gaps.json` artifact as a human-reviewable/editable intermediate, and that `--from-stage gap_context` re-runs it. `plan.md`: record the stage as implemented, matching the file's existing status format.

- [ ] **Step 4: Full validation**

Run: `make check`
Expected: ruff lint + format check + validate + the full pytest suite all pass. Fix anything that fails (formatting is auto-fixable with `make format`).

- [ ] **Step 5: Commit**

```bash
git add AGENTS.md README.md plan.md docs/stages/gap_context.md
git commit -m "docs(gap_context): stage deep-dive + AGENTS/README/plan updates"
```

---

## Verification

After Task 10, the stage is complete when:

1. `make check` passes.
2. `uv run python -m nagare_clip.pipeline --from-stage gap_context --to-stage gap_context` with `gap_context.enabled: false` writes `{"gaps": []}` per source and runs no Docker.
3. With a vision model configured and `enabled: true`, `output/gap_context/{stem}_gaps.json` holds one described gap per long silence, `output/gap_context/frames/{stem}/` holds the JPEGs, and `output/llm_report/gap_context/` shows one unit per gap listing frame paths (not base64).
