# Python Pipeline Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port `scripts/run_pipeline.sh` (633 lines of bash) to a unit-testable `nagare_clip.pipeline` package; every stage exposes a typed `run()` function, the per-stage CLIs are deleted, and the shell script becomes a 3-line shim.

**Architecture:** A declarative `STAGES` registry (name + run callable + skipped-output validation) driven by a generic runner that owns `--from-stage`/`--to-stage` windowing and per-source looping. Stage logic moves from each `cli.py` into a `run.py` with a typed function. Only Docker (WhisperX/ffmpeg) and Blender remain subprocesses, built by pure arg-builders.

**Tech Stack:** Python 3 (uv-managed), pydantic config (`nagare_clip.config.get_effective_config`), pytest, subprocess for docker compose / blender.

**Spec:** `docs/superpowers/specs/2026-07-06-python-orchestration-design.md`

## Global Constraints

- Run all Python via `uv run` (e.g. `uv run pytest`).
- `rm` is shell-aliased away; use `\rm` (or `git rm`) if needed.
- TDD: write the failing test first when possible. When a test is written against already-working (moved) code, you MUST do a mutation check: temporarily break the implementation, confirm the test fails, revert. Report the mutation evidence.
- Do not change any stage's algorithmic behavior, file formats, or the intervals JSON contract.
- Canonical stage order (names only, never numbers): `transcription, audio_silence, sentence_split, text_filter, summary, plan, director, guided_edit, intervals, blender`.
- Work on a feature branch (e.g. `python-orchestration`), never directly on `main`.
- `make check` must pass at the end (ruff lint + format, validate, pytest).
- Commit after every task.

## File Structure (end state)

```
src/nagare_clip/pipeline/
  __init__.py        # empty
  __main__.py        # python -m nagare_clip.pipeline
  errors.py          # PipelineError
  sources.py         # SourceMedia, discover_sources, resolve_cli_sources, stage_sources
  external.py        # docker/blender arg-builders + run_command
  runner.py          # Stage, PipelineContext, resolve_window, run_stages
  stages.py          # STAGE_NAMES, per-stage adapters, STAGES registry
  cli.py             # argparse, config overrides, env, main()
src/nagare_clip/<stage>/run.py      # typed run_<stage>() per stage (8 stages)
src/nagare_clip/<stage>/cli.py      # DELETED (8 files) in Task 15
src/nagare_clip/__main__.py         # re-aliased to pipeline CLI
scripts/run_pipeline.sh             # 3-line shim
```

---

### Task 1: `pipeline.errors` + `pipeline.sources` — discovery, resolution, staging

**Files:**
- Create: `src/nagare_clip/pipeline/__init__.py` (empty)
- Create: `src/nagare_clip/pipeline/errors.py`
- Create: `src/nagare_clip/pipeline/sources.py`
- Test: `tests/pipeline/__init__.py` (empty), `tests/pipeline/test_sources.py`

**Interfaces:**
- Produces: `PipelineError(Exception)`; `SourceMedia(abs_path: Path, stem: str, relative: str)` frozen dataclass; `discover_sources(input_dir: Path) -> list[Path]`; `resolve_cli_sources(cli_sources: list[str], input_dir: Path) -> list[Path]`; `stage_sources(paths: list[Path], input_dir: Path) -> tuple[list[SourceMedia], list[Path]]` (sources, cleanup-copies).
- Behavior contract (mirrors bash): discovery = files directly in `input_dir` with extensions `.mp4 .mkv .mov .avi .webm` (case-insensitive), sorted; empty → `PipelineError("No video files found in: …")`. A CLI source containing `/` is used as-is, otherwise resolved under `input_dir`; missing file → `PipelineError("Source file not found: …")`. `stage_sources` copies any source outside `input_dir` into it (returned in the cleanup list) but `SourceMedia.abs_path` stays the **original** resolved path (Blender must reference original media); `relative` is the path relative to `input_dir` (the copied basename for out-of-dir sources); `stem` = basename without last suffix.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_sources.py
"""Tests for pipeline source discovery, resolution, and staging."""

from pathlib import Path

import pytest

from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.sources import (
    SourceMedia,
    discover_sources,
    resolve_cli_sources,
    stage_sources,
)


def test_discover_sources_sorted_and_filtered(tmp_path):
    (tmp_path / "b.mp4").touch()
    (tmp_path / "a.MKV").touch()  # case-insensitive extension
    (tmp_path / "notes.txt").touch()  # ignored extension
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.mp4").touch()  # not top-level: ignored
    found = discover_sources(tmp_path)
    assert [p.name for p in found] == ["a.MKV", "b.mp4"]


def test_discover_sources_empty_raises(tmp_path):
    with pytest.raises(PipelineError, match="No video files found"):
        discover_sources(tmp_path)


def test_resolve_cli_sources_bare_name_under_input_dir(tmp_path):
    (tmp_path / "clip.mp4").touch()
    assert resolve_cli_sources(["clip.mp4"], tmp_path) == [tmp_path / "clip.mp4"]


def test_resolve_cli_sources_path_used_as_is(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "clip.mp4").touch()
    assert resolve_cli_sources([str(other / "clip.mp4")], tmp_path) == [other / "clip.mp4"]


def test_resolve_cli_sources_missing_raises(tmp_path):
    with pytest.raises(PipelineError, match="Source file not found"):
        resolve_cli_sources(["nope.mp4"], tmp_path)


def test_stage_sources_inside_dir_no_copy(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"x")
    sources, cleanup = stage_sources([tmp_path / "clip.mp4"], tmp_path)
    assert cleanup == []
    assert sources == [
        SourceMedia(
            abs_path=(tmp_path / "clip.mp4").resolve(), stem="clip", relative="clip.mp4"
        )
    ]


def test_stage_sources_outside_dir_copies_but_keeps_original_abs(tmp_path):
    input_dir = tmp_path / "in"
    input_dir.mkdir()
    outside = tmp_path / "clip.mp4"
    outside.write_bytes(b"x")
    sources, cleanup = stage_sources([outside], input_dir)
    assert cleanup == [input_dir / "clip.mp4"]
    assert (input_dir / "clip.mp4").read_bytes() == b"x"
    # Blender must reference the ORIGINAL media path, not the staging copy.
    assert sources[0].abs_path == outside.resolve()
    assert sources[0].relative == "clip.mp4"
    assert sources[0].stem == "clip"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.pipeline'`

- [ ] **Step 3: Implement**

```python
# src/nagare_clip/pipeline/errors.py
"""Pipeline orchestration errors."""

from __future__ import annotations


class PipelineError(Exception):
    """User-facing orchestration failure; the CLI prints it and exits 1."""
```

```python
# src/nagare_clip/pipeline/sources.py
"""Source-video discovery, resolution, and staging for the orchestrator."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from nagare_clip.pipeline.errors import PipelineError

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".mov", ".avi", ".webm")


@dataclass(frozen=True)
class SourceMedia:
    """One source video: original absolute path, stem, and path relative to
    the input dir (the path Docker sees)."""

    abs_path: Path
    stem: str
    relative: str


def discover_sources(input_dir: Path) -> list[Path]:
    """All video files directly inside *input_dir*, sorted by name."""
    found = [
        p
        for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    if not found:
        raise PipelineError(f"No video files found in: {input_dir}")
    return sorted(found)


def resolve_cli_sources(cli_sources: list[str], input_dir: Path) -> list[Path]:
    """Resolve explicit --source values; bare names live under *input_dir*."""
    paths: list[Path] = []
    for src in cli_sources:
        p = Path(src) if "/" in src else input_dir / src
        if not p.is_file():
            raise PipelineError(f"Source file not found: {p}")
        paths.append(p)
    return paths


def stage_sources(
    paths: list[Path], input_dir: Path
) -> tuple[list[SourceMedia], list[Path]]:
    """Make every source reachable inside *input_dir* for Docker.

    Sources outside the dir are copied in (returned in the cleanup list);
    ``abs_path`` always stays the original file so Blender references the
    original media in place.
    """
    abs_input = input_dir.resolve()
    sources: list[SourceMedia] = []
    cleanup: list[Path] = []
    for p in paths:
        ap = p.resolve()
        if ap.is_relative_to(abs_input):
            relative = str(ap.relative_to(abs_input))
        else:
            dest = input_dir / p.name
            shutil.copyfile(p, dest)
            cleanup.append(dest)
            relative = p.name
        sources.append(SourceMedia(abs_path=ap, stem=p.stem, relative=relative))
    return sources, cleanup
```

Also create empty `src/nagare_clip/pipeline/__init__.py` and `tests/pipeline/__init__.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_sources.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline tests/pipeline
git commit -m "feat(pipeline): add source discovery/resolution/staging"
```

---

### Task 2: `pipeline.external` — docker/blender command builders + runner

**Files:**
- Create: `src/nagare_clip/pipeline/external.py`
- Test: `tests/pipeline/test_external.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure).
- Produces:
  - `effective_align_model(cfg: dict) -> str` — config value, else `"vumichien/wav2vec2-large-xlsr-japanese"` when `transcription.language == "ja"`, else `""`.
  - `build_transcription_cmd(project_root: Path, relatives: list[str], cfg: dict) -> list[str]`
  - `build_silencedetect_cmd(project_root: Path, relative: str, noise: float, min_silence: float) -> list[str]`
  - `build_blender_cmd(project_root: Path, source_paths: list[Path], intervals_paths: list[Path], output_blend: Path, config_path: Path | None, log_file: Path) -> list[str]`
  - `run_command(cmd: list[str], *, env_extra: dict[str, str] | None = None, stderr_to: Path | None = None) -> None` — `subprocess.run(check=True)`; `env_extra` merged over `os.environ`; `stderr_to` redirects stderr to that file and stdout to devnull (the ffmpeg silencedetect pattern).

The exact command lines to replicate (from `scripts/run_pipeline.sh` lines 326–335, 367–372, 615–620):

```
docker compose -f <root>/docker-compose.yml run --rm --user 0:0 whisperx _ <relatives...>
  --output_dir /output/transcription --output_format all --language <L>
  --compute_type <C> --batch_size <B> [--align_model <M>]

docker compose -f <root>/docker-compose.yml run --rm --user 0:0 --entrypoint ffmpeg whisperx
  -hide_banner -nostats -i <relative> -af silencedetect=noise=<noise>dB:d=<min_silence> -f null -

blender --background --factory-startup --python-exit-code 1
  --python <root>/src/nagare_clip/blender/blender_cli.py --
  --source <abs>... --intervals <abs>... --output <blend> [--config <abs>] --log-file <log>
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_external.py
"""Tests for external (docker/blender) command construction."""

from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline.external import (
    build_blender_cmd,
    build_silencedetect_cmd,
    build_transcription_cmd,
    effective_align_model,
    run_command,
)


def _cfg(overrides=None):
    return get_effective_config(None, overrides or {})


def test_effective_align_model_ja_default():
    assert effective_align_model(_cfg()) == "vumichien/wav2vec2-large-xlsr-japanese"


def test_effective_align_model_explicit_wins():
    cfg = _cfg({"transcription": {"align_model": "my/model"}})
    assert effective_align_model(cfg) == "my/model"


def test_effective_align_model_non_ja_empty():
    assert effective_align_model(_cfg({"transcription": {"language": "en"}})) == ""


def test_build_transcription_cmd():
    root = Path("/proj")
    cmd = build_transcription_cmd(root, ["a.mp4", "b.mp4"], _cfg())
    assert cmd == [
        "docker", "compose", "-f", "/proj/docker-compose.yml",
        "run", "--rm", "--user", "0:0", "whisperx", "_",
        "a.mp4", "b.mp4",
        "--output_dir", "/output/transcription",
        "--output_format", "all",
        "--language", "ja",
        "--compute_type", "float16",
        "--batch_size", "16",
        "--align_model", "vumichien/wav2vec2-large-xlsr-japanese",
    ]


def test_build_transcription_cmd_omits_empty_align_model():
    cmd = build_transcription_cmd(
        Path("/proj"), ["a.mp4"], _cfg({"transcription": {"language": "en"}})
    )
    assert "--align_model" not in cmd


def test_build_silencedetect_cmd():
    cmd = build_silencedetect_cmd(Path("/proj"), "a.mp4", -30.0, 0.8)
    assert cmd == [
        "docker", "compose", "-f", "/proj/docker-compose.yml",
        "run", "--rm", "--user", "0:0", "--entrypoint", "ffmpeg", "whisperx",
        "-hide_banner", "-nostats", "-i", "a.mp4",
        "-af", "silencedetect=noise=-30.0dB:d=0.8",
        "-f", "null", "-",
    ]


def test_build_blender_cmd(tmp_path):
    cmd = build_blender_cmd(
        Path("/proj"),
        [Path("/vids/a.mp4")],
        [Path("/out/intervals/a_intervals.json")],
        Path("/out/blender/a_edited.blend"),
        Path("/cfg.yml"),
        Path("/out/pipeline.log"),
    )
    assert cmd == [
        "blender", "--background", "--factory-startup",
        "--python-exit-code", "1",
        "--python", "/proj/src/nagare_clip/blender/blender_cli.py", "--",
        "--source", "/vids/a.mp4",
        "--intervals", "/out/intervals/a_intervals.json",
        "--output", "/out/blender/a_edited.blend",
        "--config", "/cfg.yml",
        "--log-file", "/out/pipeline.log",
    ]


def test_build_blender_cmd_without_config(tmp_path):
    cmd = build_blender_cmd(
        Path("/proj"), [Path("/v/a.mp4")], [Path("/i/a.json")],
        Path("/b/a.blend"), None, Path("/l.log"),
    )
    assert "--config" not in cmd


def test_run_command_stderr_capture(tmp_path):
    log = tmp_path / "err.log"
    run_command(
        ["python3", "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        stderr_to=log,
    )
    assert log.read_text() == "err\n"


def test_run_command_env_extra(tmp_path):
    marker = tmp_path / "env.txt"
    run_command(
        [
            "python3", "-c",
            "import os, sys; open(sys.argv[1], 'w').write(os.environ['PIPE_TEST_VAR'])",
            str(marker),
        ],
        env_extra={"PIPE_TEST_VAR": "hello"},
    )
    assert marker.read_text() == "hello"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_external.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.pipeline.external'`

- [ ] **Step 3: Implement**

```python
# src/nagare_clip/pipeline/external.py
"""External-process command construction and execution.

Only two genuine process boundaries remain in the pipeline: the whisperx
Docker image (WhisperX transcription and ffmpeg silencedetect) and headless
Blender. Command lines are built by pure functions so they are testable
without Docker or Blender installed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

JA_DEFAULT_ALIGN_MODEL = "vumichien/wav2vec2-large-xlsr-japanese"


def effective_align_model(cfg: dict) -> str:
    """Configured align model, else the Japanese default, else empty."""
    t = cfg["transcription"]
    if t["align_model"]:
        return t["align_model"]
    return JA_DEFAULT_ALIGN_MODEL if t["language"] == "ja" else ""


def _compose_prefix(project_root: Path) -> list[str]:
    return [
        "docker", "compose", "-f", str(project_root / "docker-compose.yml"),
        "run", "--rm", "--user", "0:0",
    ]


def build_transcription_cmd(
    project_root: Path, relatives: list[str], cfg: dict
) -> list[str]:
    t = cfg["transcription"]
    cmd = [
        *_compose_prefix(project_root),
        "whisperx",
        "_",
        *relatives,
        "--output_dir", "/output/transcription",
        "--output_format", "all",
        "--language", t["language"],
        "--compute_type", t["compute_type"],
        "--batch_size", str(t["batch_size"]),
    ]
    align = effective_align_model(cfg)
    if align:
        cmd += ["--align_model", align]
    return cmd


def build_silencedetect_cmd(
    project_root: Path, relative: str, noise: float, min_silence: float
) -> list[str]:
    return [
        *_compose_prefix(project_root),
        "--entrypoint", "ffmpeg", "whisperx",
        "-hide_banner", "-nostats",
        "-i", relative,
        "-af", f"silencedetect=noise={noise}dB:d={min_silence}",
        "-f", "null", "-",
    ]


def build_blender_cmd(
    project_root: Path,
    source_paths: list[Path],
    intervals_paths: list[Path],
    output_blend: Path,
    config_path: Path | None,
    log_file: Path,
) -> list[str]:
    cmd = [
        "blender", "--background", "--factory-startup",
        "--python-exit-code", "1",
        "--python", str(project_root / "src/nagare_clip/blender/blender_cli.py"),
        "--",
    ]
    for src in source_paths:
        cmd += ["--source", str(src)]
    for ivp in intervals_paths:
        cmd += ["--intervals", str(ivp)]
    cmd += ["--output", str(output_blend)]
    if config_path is not None:
        cmd += ["--config", str(config_path)]
    cmd += ["--log-file", str(log_file)]
    return cmd


def run_command(
    cmd: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    stderr_to: Path | None = None,
) -> None:
    """Run *cmd* with check=True; optionally merge env vars and capture stderr."""
    env = {**os.environ, **env_extra} if env_extra else None
    if stderr_to is not None:
        with stderr_to.open("w", encoding="utf-8") as f:
            subprocess.run(
                cmd, check=True, env=env, stdout=subprocess.DEVNULL, stderr=f
            )
    else:
        subprocess.run(cmd, check=True, env=env)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_external.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline/external.py tests/pipeline/test_external.py
git commit -m "feat(pipeline): add docker/blender command builders"
```

---

### Task 3: `pipeline.runner` — Stage, PipelineContext, windowing, generic loop

**Files:**
- Create: `src/nagare_clip/pipeline/runner.py`
- Test: `tests/pipeline/test_runner.py`

**Interfaces:**
- Consumes: `PipelineError` (Task 1), `SourceMedia` (Task 1).
- Produces:
  - `Stage(name: str, run: Callable[[PipelineContext], None], required_outputs: Callable[[PipelineContext], list[Path]])` frozen dataclass; `required_outputs` defaults to `lambda ctx: []`.
  - `PipelineContext` dataclass with fields `cfg: dict`, `project_root: Path`, `config_path: Path | None`, `input_videos_dir: Path` (absolute), `output_dir: Path` (absolute), `sources: list[SourceMedia]`, `from_index: int`, `to_index: int`; properties `stems: list[str]`, `log_file: Path` (`output_dir / "pipeline.log"`), `llm_report_dir: Path` (`output_dir / "llm_report"`); method `stage_dir(name: str) -> Path` (`output_dir / name`).
  - `resolve_window(stages: Sequence[Stage], from_stage: str, to_stage: str) -> tuple[int, int]` — raises `PipelineError` for unknown names or `from > to`.
  - `run_stages(stages: Sequence[Stage], ctx: PipelineContext) -> None` — in-window stages run (non-`PipelineError` exceptions wrapped as `PipelineError("[<name>] failed: …")`), past-window prints `[name] Skipped (--to-stage X)`, before-window prints `[name] Skipped (--from-stage X)` and validates `required_outputs`, raising `PipelineError("Missing <name> output: <path> (required when skipping <name>)")` on the first missing file.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_runner.py
"""Tests for the generic stage runner: windowing, skipping, validation."""

from pathlib import Path

import pytest

from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import (
    PipelineContext,
    Stage,
    resolve_window,
    run_stages,
)


def _stages(names, calls, required=None):
    required = required or {}
    return [
        Stage(
            name=n,
            run=lambda ctx, n=n: calls.append(n),
            required_outputs=required.get(n, lambda ctx: []),
        )
        for n in names
    ]


def _ctx(tmp_path, from_index, to_index):
    return PipelineContext(
        cfg={},
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=[],
        from_index=from_index,
        to_index=to_index,
    )


def test_resolve_window_full_range():
    stages = _stages(["a", "b", "c"], [])
    assert resolve_window(stages, "a", "c") == (0, 2)


def test_resolve_window_unknown_from():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="Invalid --from-stage value: x"):
        resolve_window(stages, "x", "b")


def test_resolve_window_unknown_to():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="Invalid --to-stage value: y"):
        resolve_window(stages, "a", "y")


def test_resolve_window_inverted():
    stages = _stages(["a", "b"], [])
    with pytest.raises(PipelineError, match="after --to-stage"):
        resolve_window(stages, "b", "a")


def test_run_stages_runs_only_window(tmp_path, capsys):
    calls = []
    stages = _stages(["a", "b", "c", "d"], calls)
    run_stages(stages, _ctx(tmp_path, 1, 2))
    assert calls == ["b", "c"]
    out = capsys.readouterr().out
    assert "[a] Skipped (--from-stage b)" in out
    assert "[d] Skipped (--to-stage c)" in out


def test_run_stages_validates_skipped_outputs(tmp_path):
    missing = tmp_path / "out" / "a" / "x.json"
    calls = []
    stages = _stages(
        ["a", "b"], calls, required={"a": lambda ctx: [missing]}
    )
    with pytest.raises(PipelineError, match="Missing a output"):
        run_stages(stages, _ctx(tmp_path, 1, 1))
    assert calls == []


def test_run_stages_skipped_outputs_present_ok(tmp_path):
    present = tmp_path / "x.json"
    present.write_text("{}")
    calls = []
    stages = _stages(["a", "b"], calls, required={"a": lambda ctx: [present]})
    run_stages(stages, _ctx(tmp_path, 1, 1))
    assert calls == ["b"]


def test_run_stages_wraps_stage_exception(tmp_path):
    def boom(ctx):
        raise ValueError("kaput")

    stages = [Stage(name="a", run=boom)]
    with pytest.raises(PipelineError, match=r"\[a\] failed: kaput"):
        run_stages(stages, _ctx(tmp_path, 0, 0))


def test_context_paths(tmp_path):
    ctx = _ctx(tmp_path, 0, 0)
    assert ctx.log_file == tmp_path / "out" / "pipeline.log"
    assert ctx.llm_report_dir == tmp_path / "out" / "llm_report"
    assert ctx.stage_dir("intervals") == tmp_path / "out" / "intervals"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_runner.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.pipeline.runner'`

- [ ] **Step 3: Implement**

```python
# src/nagare_clip/pipeline/runner.py
"""Generic stage runner: window resolution, skip validation, execution.

Stages are identified only by name (no numbers); a new stage is inserted by
adding a registry entry, without renumbering anything. Skipped-over stages
(--from-stage) validate that their outputs already exist; stages past
--to-stage are skipped without validation (they are intentionally not built).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.sources import SourceMedia


@dataclass
class PipelineContext:
    """Everything a stage adapter needs to run."""

    cfg: dict
    project_root: Path
    config_path: Path | None
    input_videos_dir: Path
    output_dir: Path
    sources: list[SourceMedia]
    from_index: int
    to_index: int

    @property
    def stems(self) -> list[str]:
        return [s.stem for s in self.sources]

    @property
    def log_file(self) -> Path:
        return self.output_dir / "pipeline.log"

    @property
    def llm_report_dir(self) -> Path:
        return self.output_dir / "llm_report"

    def stage_dir(self, name: str) -> Path:
        return self.output_dir / name


def _no_outputs(ctx: PipelineContext) -> list[Path]:
    return []


@dataclass(frozen=True)
class Stage:
    name: str
    run: Callable[[PipelineContext], None]
    required_outputs: Callable[[PipelineContext], list[Path]] = field(
        default=_no_outputs
    )


def resolve_window(
    stages: Sequence[Stage], from_stage: str, to_stage: str
) -> tuple[int, int]:
    names = [s.name for s in stages]
    hint = f"Use a stage name: {' '.join(names)}."
    if from_stage not in names:
        raise PipelineError(f"Invalid --from-stage value: {from_stage}\n{hint}")
    if to_stage not in names:
        raise PipelineError(f"Invalid --to-stage value: {to_stage}\n{hint}")
    from_index, to_index = names.index(from_stage), names.index(to_stage)
    if from_index > to_index:
        raise PipelineError(
            f"Invalid stage range: --from-stage ({from_stage}) is after "
            f"--to-stage ({to_stage})."
        )
    return from_index, to_index


def run_stages(stages: Sequence[Stage], ctx: PipelineContext) -> None:
    for idx, stage in enumerate(stages):
        if ctx.from_index <= idx <= ctx.to_index:
            try:
                stage.run(ctx)
            except PipelineError:
                raise
            except Exception as exc:
                raise PipelineError(f"[{stage.name}] failed: {exc}") from exc
        elif idx > ctx.to_index:
            print(f"[{stage.name}] Skipped (--to-stage {stages[ctx.to_index].name})")
        else:
            print(
                f"[{stage.name}] Skipped (--from-stage {stages[ctx.from_index].name})"
            )
            for path in stage.required_outputs(ctx):
                if not path.is_file():
                    raise PipelineError(
                        f"Missing {stage.name} output: {path} "
                        f"(required when skipping {stage.name})"
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline/test_runner.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline/runner.py tests/pipeline/test_runner.py
git commit -m "feat(pipeline): add generic stage runner with windowing"
```

---

### Task 4: `audio_silence.run` extraction

**Files:**
- Create: `src/nagare_clip/audio_silence/run.py`
- Modify: `src/nagare_clip/audio_silence/cli.py` (main body delegates to `run_audio_silence`)
- Test: rewrite `tests/audio_silence/test_cli.py` → `tests/audio_silence/test_run.py`

**Interfaces:**
- Produces: `run_audio_silence(output: Path, cfg: dict, *, raw_path: Path | None = None) -> None`. Behavior identical to current `cli.main` after arg parsing: disabled or no raw → header-only cuts file; otherwise parse silencedetect stderr and write ranges.

- [ ] **Step 1: Rewrite the stage tests against `run_audio_silence`**

Port every test in `tests/audio_silence/test_cli.py` to a new `tests/audio_silence/test_run.py`, replacing the `sys.argv` + `main()` harness with a direct call. Config-file scenarios build the cfg dict via `get_effective_config(cfg_path, {})`. Example ports (do all scenarios in the old file the same way):

```python
# tests/audio_silence/test_run.py
"""Tests for the audio_silence stage run() function."""

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.config import get_effective_config

_RAW = """\
    Duration: 00:00:20.00, start: 0.000000
[silencedetect @ 0x55] silence_start: 2.5
[silencedetect @ 0x55] silence_end: 4.0 | silence_duration: 1.5
[silencedetect @ 0x55] silence_start: 18.0
"""


def test_without_raw_writes_header_only(tmp_path):
    out = tmp_path / "clip_cuts.txt"
    run_audio_silence(out, get_effective_config(None, {}))
    assert out.read_text(encoding="utf-8").lstrip().startswith("#")
    assert read_cuts(out) == []


def test_with_raw_writes_cut_ranges(tmp_path):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    out = tmp_path / "clip_cuts.txt"
    run_audio_silence(out, get_effective_config(None, {}), raw_path=raw)
    assert read_cuts(out) == [(2.5, 4.0), (18.0, 20.0)]


def test_disabled_writes_header_only_even_with_raw(tmp_path):
    raw = tmp_path / "sd.log"
    raw.write_text(_RAW, encoding="utf-8")
    out = tmp_path / "clip_cuts.txt"
    cfg = get_effective_config(None, {"audio_silence": {"enabled": False}})
    run_audio_silence(out, cfg, raw_path=raw)
    assert read_cuts(out) == []
```

Delete `tests/audio_silence/test_cli.py` in this task (its scenarios now live in `test_run.py`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/audio_silence/test_run.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.audio_silence.run'`

- [ ] **Step 3: Implement `run.py` and slim `cli.py`**

```python
# src/nagare_clip/audio_silence/run.py
"""audio_silence stage: audio-silence (jump-cut) detection checkpoint.

ffmpeg ``silencedetect`` runs inside the whisperx Docker image (driven by the
pipeline orchestrator); this function consumes the captured stderr and writes
a human-editable ``{stem}_cuts.txt``. Without *raw_path* (or when
``audio_silence.enabled`` is false) it writes a header-only file so the
downstream union in the intervals stage becomes a no-op.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nagare_clip.audio_silence.cuts_file import write_cuts
from nagare_clip.audio_silence.detect import parse_silencedetect_output


def run_audio_silence(
    output: Path, cfg: dict, *, raw_path: Path | None = None
) -> None:
    a = cfg["audio_silence"]
    if not a["enabled"] or raw_path is None:
        reason = "disabled" if not a["enabled"] else "no ffmpeg output provided"
        logging.info("audio_silence: audio-silence %s, writing empty cut list", reason)
        write_cuts(output, [])
        logging.info("audio_silence: wrote %s", output)
        return

    stderr = raw_path.read_text(encoding="utf-8")
    ranges = parse_silencedetect_output(stderr)
    write_cuts(output, ranges)
    logging.info(
        "audio_silence: detected %d silence range(s), wrote %s", len(ranges), output
    )
```

In `cli.py`, replace `main()`'s stage logic with a call (keep `parse_args`, config load, `setup_logging` as-is; the file is deleted entirely in Task 15):

```python
# src/nagare_clip/audio_silence/cli.py — main() body after setup_logging(...)
    run_audio_silence(
        Path(args.output_path),
        cfg,
        raw_path=Path(args.raw_path) if args.raw_path else None,
    )
```

(and replace the now-unused `write_cuts`/`parse_silencedetect_output` imports with `from nagare_clip.audio_silence.run import run_audio_silence`).

- [ ] **Step 4: Run tests + mutation check**

Run: `uv run pytest tests/audio_silence -v` — Expected: all PASS.

Mutation check (tests were written against working code): in `run.py`, temporarily swap the enabled-check to `if a["enabled"] or raw_path is None:`; run `uv run pytest tests/audio_silence/test_run.py -v`; expect `test_with_raw_writes_cut_ranges` (and others) to FAIL. Revert the mutation, re-run, expect PASS. Record which tests failed.

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/audio_silence tests/audio_silence
git commit -m "refactor(audio_silence): extract typed run_audio_silence()"
```

---

### Task 5: `sentence_split.run` extraction

**Files:**
- Create: `src/nagare_clip/sentence_split/run.py`
- Modify: `src/nagare_clip/sentence_split/cli.py` (delegate), any test importing `resegment_json` from `cli`
- Test: rewrite `tests/sentence_split/test_cli.py` → `tests/sentence_split/test_run.py`

**Interfaces:**
- Produces: `run_sentence_split(json_in: Path, txt_in: Path, output_json: Path, output_txt: Path, cfg: dict, *, stem: str = "", recorder: Recorder = NULL_RECORDER) -> None` and `resegment_json(json_data, sp_cfg, nlp, *, recorder=NULL_RECORDER, stem="")` (moved verbatim from `cli.py`).
- `run_sentence_split` does **not** call `recorder.clear()` / `rebuild_index()` — recorder lifecycle belongs to the orchestrator (Task 12).

- [ ] **Step 1: Create `run.py`** — move `resegment_json`, `_copy_through`, and the post-`setup_logging` body of `main()` from `cli.py` verbatim, minus the recorder creation/clear and the `finally: recorder.rebuild_index()` wrapper:

```python
# src/nagare_clip/sentence_split/run.py
"""sentence_split stage: LLM re-segmentation of a WhisperX transcript.

Re-segments a WhisperX ``{stem}.json`` into one-sentence-per-line segments and
writes the re-segmented ``.json`` plus a matching ``.txt``. When
``sentence_split.enabled`` is false it copies the transcription files through
byte-identically, so downstream behaviour is unchanged.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.sentence_split.llm import split_window
from nagare_clip.sentence_split.nlp import bunsetsu_units, load_nlp
from nagare_clip.sentence_split.segment import (
    char_to_word_index,
    concat_word_text,
    iter_windows,
    rebuild_window_segments,
    segment_from_words,
    window_text_and_words,
)


def resegment_json(
    json_data: dict[str, Any],
    sp_cfg: dict[str, Any],
    nlp: Any,
    *,
    recorder: Recorder = NULL_RECORDER,
    stem: str = "",
) -> dict[str, Any]:
    ...  # moved VERBATIM from cli.py (unchanged, including all comments)


def _copy_through(src_json: Path, src_txt: Path, out_json: Path, out_txt: Path) -> None:
    """Copy the transcription ``.json``/``.txt`` through byte-identically."""
    shutil.copyfile(src_json, out_json)
    shutil.copyfile(src_txt, out_txt)


def run_sentence_split(
    json_in: Path,
    txt_in: Path,
    output_json: Path,
    output_txt: Path,
    cfg: dict,
    *,
    stem: str = "",
    recorder: Recorder = NULL_RECORDER,
) -> None:
    sp_cfg = cfg["sentence_split"]
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_txt.parent.mkdir(parents=True, exist_ok=True)

    if not sp_cfg.get("enabled", False):
        logging.info("sentence_split: disabled, copying %s through", stem)
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    json_data = json.loads(json_in.read_text(encoding="utf-8"))
    nlp = load_nlp()
    new_data = resegment_json(json_data, sp_cfg, nlp, recorder=recorder, stem=stem)

    if new_data is json_data:
        # verbatim violation already logged; copy through for safety
        _copy_through(json_in, txt_in, output_json, output_txt)
        return

    output_json.write_text(
        json.dumps(new_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    output_txt.write_text(
        "\n".join(seg.get("text", "") for seg in new_data["segments"]) + "\n",
        encoding="utf-8",
    )
    logging.info(
        "sentence_split: %s %d -> %d segments",
        stem,
        len(json_data.get("segments", [])),
        len(new_data["segments"]),
    )
```

(the `...` above is shorthand in this plan only — copy the real body; the plan's No-Placeholder rule applies to new logic, and this function moves unchanged.)

Slim `cli.py`: `main()` keeps arg parsing, config, logging, recorder creation + clear/rebuild (bash still calls this CLI until Task 14), then calls `run_sentence_split(..., recorder=recorder)` wrapped in `try/finally recorder.rebuild_index()`. Update any import of `resegment_json` from `nagare_clip.sentence_split.cli` (check `tests/sentence_split/`) to import from `nagare_clip.sentence_split.run`.

- [ ] **Step 2: Rewrite stage tests** — port every scenario in `tests/sentence_split/test_cli.py` to `tests/sentence_split/test_run.py` calling `run_sentence_split(...)`/`resegment_json(...)` directly (drop only the `--llm-report-no-clear` flag scenarios; recorder lifecycle is covered in Task 12). Delete `tests/sentence_split/test_cli.py`.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/sentence_split -v` — Expected: all PASS.

Mutation check: in `run_sentence_split`, invert the disabled check to `if sp_cfg.get("enabled", False):` (so enabled copies through); run the tests; expect the disabled-copy-through and enabled-resegmentation tests to FAIL. Revert; re-run; PASS. Record evidence.

- [ ] **Step 4: Full suite**

Run: `uv run pytest` — Expected: PASS (cli delegation keeps old behavior).

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/sentence_split tests/sentence_split
git commit -m "refactor(sentence_split): extract typed run_sentence_split()"
```

---

### Task 6: `text_filter.run` extraction

**Files:**
- Create: `src/nagare_clip/text_filter/run.py`
- Modify: `src/nagare_clip/text_filter/cli.py` (delegate)
- Test: port `tests/text_filter/test_cli_constant_keywords.py` scenarios → `tests/text_filter/test_run.py`

**Interfaces:**
- Produces: `run_text_filter(txt: Path, output_txt: Path, cfg: dict, *, recorder: Recorder = NULL_RECORDER) -> None`.
- Body = current `cli.main` after `setup_logging`, with two changes: the recorder is a parameter (not created inside, no `clear()`), and there is no `recorder.rebuild_index()` call. The summary-LLM prompt enhancement and rule filter move verbatim.

- [ ] **Step 1: Create `run.py`** with this exact function (logic moved verbatim from `cli.main` lines 84–146; only the recorder handling changes):

```python
# src/nagare_clip/text_filter/run.py
"""text_filter stage: text editing checkpoint for WhisperX transcriptions.

Produces ``{stem}_edits.txt`` — either a plain copy of the transcription
``.txt`` (when LLM is disabled) or LLM-filtered text with ``{{old->new}}``
markers preserved for human review.
"""

from __future__ import annotations

import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.text_filter.llm_filter import filter_transcript
from nagare_clip.text_filter.rule_filter import remove_midstream_closing
from nagare_clip.text_filter.summary_llm import (
    SummaryResult,
    build_enhanced_prompt,
    generate_summary,
)


def run_text_filter(
    txt: Path,
    output_txt: Path,
    cfg: dict,
    *,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    s2 = cfg["text_filter"]
    lines = txt.read_text(encoding="utf-8").splitlines()

    # Rule filter — mark hallucinated closing phrases with {{->}} markers
    original_lines = lines
    lines = remove_midstream_closing(lines)
    rule_changes = sum(1 for o, r in zip(original_lines, lines) if o != r)
    if rule_changes:
        logging.info("text_filter: rule filter marked %d line(s)", rule_changes)

    if not s2["use_llm"]:
        logging.info("text_filter: AI filter disabled, writing edits file")
        result_lines = lines
    else:
        logging.info("text_filter: filtering %d lines with AI", len(lines))

        # Summary LLM — generate context for the filter LLM
        filter_cfg = dict(s2)
        summary_cfg = s2.get("summary_llm", {})
        constant_keywords: list = summary_cfg.get("keywords", [])
        if summary_cfg.get("enabled", False):
            summary_result = generate_summary(
                "\n".join(lines), summary_cfg, recorder=recorder
            )
            if summary_result is not None:
                summary_result.keywords = constant_keywords + summary_result.keywords
                filter_cfg["prompt"] = build_enhanced_prompt(
                    s2.get("prompt", ""), summary_result
                )
                logging.info(
                    "text_filter: summary generated, %d keywords",
                    len(summary_result.keywords),
                )
            elif constant_keywords:
                filter_cfg["prompt"] = build_enhanced_prompt(
                    s2.get("prompt", ""),
                    SummaryResult(summary="", keywords=constant_keywords),
                )
        elif constant_keywords:
            filter_cfg["prompt"] = build_enhanced_prompt(
                s2.get("prompt", ""),
                SummaryResult(summary="", keywords=constant_keywords),
            )

        # AI filter — returns lines with {{old->new}} markers preserved
        result_lines = filter_transcript(lines, filter_cfg, recorder=recorder)

        changes = sum(1 for o, c in zip(lines, result_lines) if o != c)
        logging.info("text_filter: %d/%d lines modified by AI", changes, len(lines))

    output_txt.parent.mkdir(parents=True, exist_ok=True)
    output_txt.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("text_filter: wrote %s", output_txt)
```

Slim `cli.py::main` to create the recorder (when `use_llm`), call `run_text_filter(...)`, then `recorder.rebuild_index()` — preserving the CLI's exact current recorder behavior until deletion.

- [ ] **Step 2: Port tests** — rewrite the scenarios of `tests/text_filter/test_cli_constant_keywords.py` into `tests/text_filter/test_run.py` calling `run_text_filter` (monkeypatching `nagare_clip.text_filter.run.filter_transcript` / `generate_summary` where the old tests patched them on `cli`). Add a plain-copy test:

```python
def test_disabled_copies_input(tmp_path):
    src = tmp_path / "clip.txt"
    src.write_text("l1\nl2\n", encoding="utf-8")
    out = tmp_path / "clip_edits.txt"
    run_text_filter(src, out, get_effective_config(None, {}))
    assert out.read_text(encoding="utf-8") == "l1\nl2\n"
```

Delete `tests/text_filter/test_cli_constant_keywords.py`.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/text_filter -v` — PASS.
Mutation: in `run_text_filter`, change `result_lines = lines` (disabled branch) to `result_lines = []`; expect `test_disabled_copies_input` FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/text_filter tests/text_filter
git commit -m "refactor(text_filter): extract typed run_text_filter()"
```

---

### Task 7: `summary.run` extraction

**Files:**
- Create: `src/nagare_clip/summary/run.py`
- Modify: `src/nagare_clip/summary/cli.py` (delegate)
- Test: rewrite `tests/summary/test_cli.py` → `tests/summary/test_run.py`

**Interfaces:**
- Produces: `run_summary(edits_txts: list[Path], output: Path, cfg: dict, *, json_paths: list[Path] | None = None, recorder: Recorder = NULL_RECORDER) -> None`; helper `_stem_from_edits(path: Path) -> str` moves along.
- No recorder clear/rebuild inside.

- [ ] **Step 1: Create `run.py`** — move `_stem_from_edits` and the post-recorder body of `cli.main` (lines 93–134) verbatim, parameterizing `args.edits_txt` → `edits_txts`, `args.json` → `json_paths`, `args.output` → `output`, and dropping `recorder.rebuild_index()`:

```python
# src/nagare_clip/summary/run.py
"""summary stage: project-wide per-part + all-videos summaries.

When ``summary.enabled`` is false (default) it writes an empty artifact so the
downstream stages are no-ops and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import clean_for_display
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.summary.summarize import (
    ProjectSummary,
    build_summary,
    summary_to_dict,
)
from nagare_clip.timing import segment_times


def _stem_from_edits(path: Path) -> str:
    name = path.name
    suffix = "_edits.txt"
    return name[: -len(suffix)] if name.endswith(suffix) else path.stem


def run_summary(
    edits_txts: list[Path],
    output: Path,
    cfg: dict,
    *,
    json_paths: list[Path] | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    summary_cfg = cfg["summary"]

    if not summary_cfg.get("enabled", False):
        logging.info("summary: disabled, writing empty summary")
        project = ProjectSummary(summary="", parts=[])
    else:
        parts_input = []
        for path in edits_txts:
            stem = _stem_from_edits(path)
            clean_lines = clean_for_display(
                path.read_text(encoding="utf-8").splitlines()
            )
            parts_input.append((stem, clean_lines))
        seg_times_by_stem = {}
        for jpath in json_paths or []:
            if jpath.is_file():
                try:
                    seg_times_by_stem[jpath.stem] = segment_times(
                        json.loads(jpath.read_text(encoding="utf-8"))
                    )
                except (ValueError, OSError):
                    logging.warning("summary: could not read --json %s", jpath)
        logging.info("summary: analysing %d video(s) with LLM", len(parts_input))
        project = build_summary(
            parts_input,
            summary_cfg,
            recorder=recorder,
            seg_times_by_stem=seg_times_by_stem or None,
        )
        logging.info(
            "summary: %d part(s) across %d video(s)",
            len(project.parts),
            len(parts_input),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary_to_dict(project), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("summary: wrote %s", output)
```

Slim `cli.py::main` to call it (recorder handling stays in cli until Task 15).

- [ ] **Step 2: Rewrite tests** — port every scenario of `tests/summary/test_cli.py` (they monkeypatch `summary_cli.build_summary`; patch `nagare_clip.summary.run.build_summary` instead) into `tests/summary/test_run.py`, dropping only `--llm-report-no-clear` scenarios. Delete `tests/summary/test_cli.py`.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/summary -v` — PASS.
Mutation: make the disabled branch write `ProjectSummary(summary="X", parts=[])`; expect the disabled-writes-empty test to FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/summary tests/summary
git commit -m "refactor(summary): extract typed run_summary()"
```

---

### Task 8: `plan.run` extraction

**Files:**
- Create: `src/nagare_clip/plan/run.py`
- Modify: `src/nagare_clip/plan/cli.py` (delegate)
- Test: rewrite `tests/plan/test_cli.py` → `tests/plan/test_run.py`

**Interfaces:**
- Produces: `run_plan(summary_json: Path, output: Path, cfg: dict, *, recorder: Recorder = NULL_RECORDER) -> None`. No recorder clear/rebuild inside.

- [ ] **Step 1: Create `run.py`**:

```python
# src/nagare_clip/plan/run.py
"""plan stage: coarse cross-video rough directions per part.

When ``plan.enabled`` is false (default) it writes an empty artifact so the
downstream director is unaffected and the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import generate_plan, plan_to_dict
from nagare_clip.summary.summarize import summary_from_dict


def run_plan(
    summary_json: Path,
    output: Path,
    cfg: dict,
    *,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    plan_cfg = cfg["plan"]

    if not plan_cfg.get("enabled", False):
        logging.info("plan: disabled, writing empty plan")
        directions = []
    else:
        project_summary = summary_from_dict(
            json.loads(summary_json.read_text(encoding="utf-8"))
        )
        logging.info("plan: directing %d part(s) with LLM", len(project_summary.parts))
        directions = generate_plan(project_summary, plan_cfg, recorder=recorder)
        logging.info("plan: %d direction(s)", len(directions))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(plan_to_dict(directions), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("plan: wrote %s", output)
```

Slim `cli.py::main` to call it.

- [ ] **Step 2: Rewrite tests** — port `tests/plan/test_cli.py` scenarios to `tests/plan/test_run.py` (patch `nagare_clip.plan.run.generate_plan`), drop report-no-clear scenarios, delete the old file.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/plan -v` — PASS.
Mutation: in the enabled branch, replace `generate_plan(...)` with `[]`; expect the enabled-writes-directions test to FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/plan tests/plan
git commit -m "refactor(plan): extract typed run_plan()"
```

---

### Task 9: `director.run` extraction

**Files:**
- Create: `src/nagare_clip/director/run.py`
- Modify: `src/nagare_clip/director/cli.py` (delegate)
- Test: rewrite `tests/director/test_cli.py` → `tests/director/test_run.py`

**Interfaces:**
- Produces: `run_director(edits_txt: Path, output: Path, cfg: dict, *, summary: Path | None = None, plan: Path | None = None, stem: str | None = None, json_path: Path | None = None, recorder: Recorder = NULL_RECORDER) -> None` and `_build_overview_context(summary: Path | None, plan: Path | None, stem: str | None) -> str` (moved from `cli.py`, reworked to take the three values instead of `args`).

- [ ] **Step 1: Create `run.py`**:

```python
# src/nagare_clip/director/run.py
"""director stage (Pass A): high-level LLM edit operations.

When ``director.enabled`` is false (default) it writes an empty op list so the
downstream guided_edit stage is a no-op and the pipeline behaves exactly as
before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.context import build_director_context
from nagare_clip.director.director_llm import generate_director_ops, ops_to_dict
from nagare_clip.llm_report import NULL_RECORDER, Recorder
from nagare_clip.plan.plan_llm import plan_from_dict
from nagare_clip.summary.summarize import ProjectSummary, summary_from_dict
from nagare_clip.timing import segment_times


def _build_overview_context(
    summary: Path | None, plan: Path | None, stem: str | None
) -> str:
    """Load summary/plan artifacts (tolerating missing/empty) and render the
    cross-video context for this video's stem.  Returns ``""`` if unavailable."""
    if not stem:
        return ""
    project_summary = ProjectSummary(summary="", parts=[])
    if summary and summary.is_file():
        project_summary = summary_from_dict(
            json.loads(summary.read_text(encoding="utf-8"))
        )
    directions = []
    if plan and plan.is_file():
        directions = plan_from_dict(json.loads(plan.read_text(encoding="utf-8")))
    return build_director_context(project_summary, directions, stem)


def run_director(
    edits_txt: Path,
    output: Path,
    cfg: dict,
    *,
    summary: Path | None = None,
    plan: Path | None = None,
    stem: str | None = None,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    director_cfg = cfg["director"]
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    unit = stem or output.stem.replace("_director", "")

    if not director_cfg.get("enabled", False):
        logging.info("director: disabled, writing empty op list")
        ops = []
    else:
        overview_context = _build_overview_context(summary, plan, stem)
        seg_times = None
        if json_path and json_path.is_file():
            try:
                seg_times = segment_times(
                    json.loads(json_path.read_text(encoding="utf-8"))
                )
            except (ValueError, OSError):
                logging.warning("director: could not read --json %s", json_path)
        logging.info("director: analysing %d line(s) with LLM", len(edit_lines))
        ops = generate_director_ops(
            edit_lines,
            director_cfg,
            overview_context=overview_context,
            recorder=recorder,
            unit=unit,
            seg_times=seg_times,
        )
        logging.info("director: %d operation(s)", len(ops))

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(ops_to_dict(ops), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info("director: wrote %s", output)
```

Slim `cli.py::main` to call it (recorder + rebuild stay in cli until Task 15).

- [ ] **Step 2: Rewrite tests** — port every scenario of `tests/director/test_cli.py` to `tests/director/test_run.py` calling `run_director` directly; monkeypatch `nagare_clip.director.run.generate_director_ops`. Drop the `test_llm_report_no_clear_preserves_existing_report` scenario (covered by Task 12's adapter test). Delete the old file.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/director -v` — PASS.
Mutation: in the enabled branch, pass `overview_context=""` unconditionally; expect the overview-context-injected test to FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/director tests/director
git commit -m "refactor(director): extract typed run_director()"
```

---

### Task 10: `guided_edit.run` extraction

**Files:**
- Create: `src/nagare_clip/guided_edit/run.py`
- Modify: `src/nagare_clip/guided_edit/cli.py` (delegate)
- Test: rewrite `tests/guided_edit/test_cli.py` → `tests/guided_edit/test_run.py`

**Interfaces:**
- Produces: `run_guided_edit(edits_txt: Path, director_json: Path, output: Path, cfg: dict, *, json_path: Path | None = None, recorder: Recorder = NULL_RECORDER) -> None`.

- [ ] **Step 1: Create `run.py`**:

```python
# src/nagare_clip/guided_edit/run.py
"""guided_edit stage (Pass B2): apply director ops into _edits.txt.

When ``guided_edit.enabled`` is false (default) it copies the input edits
through unchanged so the pipeline behaves exactly as before.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from nagare_clip.director.director_llm import ops_from_dict
from nagare_clip.guided_edit.apply import apply_ops
from nagare_clip.intervals.check_edits import check_edits
from nagare_clip.llm_report import NULL_RECORDER, Recorder


def run_guided_edit(
    edits_txt: Path,
    director_json: Path,
    output: Path,
    cfg: dict,
    *,
    json_path: Path | None = None,
    recorder: Recorder = NULL_RECORDER,
) -> None:
    ge_cfg = cfg["guided_edit"]
    edit_lines = edits_txt.read_text(encoding="utf-8").splitlines()
    stem = output.stem.replace("_edits", "")

    if not ge_cfg.get("enabled", False):
        logging.info("guided_edit: disabled, copying edits through")
        result_lines = edit_lines
        unapplied: list = []
    else:
        director_data = json.loads(director_json.read_text(encoding="utf-8"))
        ops = ops_from_dict(director_data, num_lines=len(edit_lines))
        logging.info("guided_edit: applying %d director op(s)", len(ops))
        result_lines, unapplied = apply_ops(
            edit_lines, ops, ge_cfg, recorder=recorder, unit=stem
        )
        logging.info(
            "guided_edit: %d applied, %d unapplied",
            len(ops) - len(unapplied),
            len(unapplied),
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(result_lines) + "\n", encoding="utf-8")
    logging.info("guided_edit: wrote %s", output)

    if json_path:
        json_data = json.loads(json_path.read_text(encoding="utf-8"))
        problems = check_edits(result_lines, json_data)
        for p in problems:
            where = "file" if p.line is None else f"line {p.line}"
            logging.warning("check_edits: %s: %s", where, p.message)
        if problems:
            logging.warning(
                "guided_edit: %d check_edits problem(s) in output", len(problems)
            )
```

Slim `cli.py::main` to call it.

- [ ] **Step 2: Rewrite tests** — port `tests/guided_edit/test_cli.py` scenarios to `tests/guided_edit/test_run.py` calling `run_guided_edit`. Delete the old file.

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/guided_edit -v` — PASS.
Mutation: in the disabled branch, set `result_lines = []`; expect the disabled-copies-through test to FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/guided_edit tests/guided_edit
git commit -m "refactor(guided_edit): extract typed run_guided_edit()"
```

---

### Task 11: `intervals.run` extraction (+ port the four top-level marker tests)

**Files:**
- Create: `src/nagare_clip/intervals/run.py`
- Modify: `src/nagare_clip/intervals/cli.py` (delegate; keep `parse_args` and `_build_cli_overrides` untouched)
- Test: create `tests/intervals/test_run_smoke.py`; port `tests/test_cli_cuts_merge.py`, `tests/test_cli_keep_markers.py`, `tests/test_cli_cut_marker.py`, `tests/test_cli_overlay_markers.py` to call `run_intervals`

**Interfaces:**
- Produces: `run_intervals(edits_txt: Path, json_path: Path, output: Path, cfg: dict, *, cuts_txt: Path | None = None) -> None`.
- Body = current `cli.main` lines 191–377 verbatim (everything after `setup_logging`), with `args.edits_txt/json_path/output_path/cuts_txt` becoming the parameters and `ivl`/`cap`/`bun` still read from `cfg`. The `spacy.load("ja_ginza")` call and all interval math move unchanged.

- [ ] **Step 1: Create `run.py`** — move the body verbatim. Signature and head:

```python
# src/nagare_clip/intervals/run.py
"""intervals stage: apply patches, sync JSON, compute keep intervals."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import spacy

from nagare_clip.audio_silence.cuts_file import read_cuts
from nagare_clip.intervals.bunsetu import build_bunsetu_times
from nagare_clip.intervals.captions import apply_caption_margins, collect_captions
from nagare_clip.intervals.intervals import (
    apply_margins,
    enforce_min_keep_duration,
    ensure_keep_covers_captions,
    invert_intervals,
    merge_intervals,
    subtract_intervals,
)
from nagare_clip.intervals.io import infer_source_file
from nagare_clip.intervals.speech import build_speech_spans, get_duration_sec
from nagare_clip.intervals.sync_json import (
    extract_keep_ranges,
    extract_overlay_ranges,
    extract_speed_ranges,
    sync_text_to_json,
)


def run_intervals(
    edits_txt: Path,
    json_path: Path,
    output: Path,
    cfg: dict,
    *,
    cuts_txt: Path | None = None,
) -> None:
    ivl = cfg["intervals"]
    cap = ivl["caption"]
    bun = ivl["bunsetu"]
    # ... body moved VERBATIM from cli.main (lines 200–377), with
    #     args.edits_txt -> edits_txt, args.json_path -> json_path,
    #     args.cuts_txt -> cuts_txt, output_path -> output.
```

(again: copy the real body — the comment is plan shorthand.) Slim `cli.py::main` to config-load + `setup_logging` + `run_intervals(...)`.

- [ ] **Step 2: Port the four top-level integration tests** — each currently builds files, sets `sys.argv`, and calls `stage_cli.main()`. Rewrite each to call:

```python
from nagare_clip.config import get_effective_config
from nagare_clip.intervals.run import run_intervals

cfg = get_effective_config(cfg_path_or_None, {})
run_intervals(edits_txt, json_path, out_json, cfg, cuts_txt=cuts_path_or_None)
```

keeping every fixture and assertion unchanged. Keep the files' names (`tests/test_cli_cuts_merge.py` → `tests/test_cuts_merge.py`, `tests/test_cli_keep_markers.py` → `tests/test_keep_markers_cli.py`? No — rename all four by dropping the `cli`: `tests/test_cuts_merge.py`, `tests/test_keep_markers_integration.py` conflicts with `tests/intervals/test_keep_markers.py`; use `tests/intervals/test_run_cuts_merge.py`, `tests/intervals/test_run_keep_markers.py`, `tests/intervals/test_run_cut_marker.py`, `tests/intervals/test_run_overlay_markers.py`). Add a smoke test `tests/intervals/test_run_smoke.py` only if the ported four don't already cover the basic flow (they do — skip it in that case).

- [ ] **Step 3: Run tests + mutation check**

Run: `uv run pytest tests/intervals tests/ -k "run_" -v` and the four ported files explicitly — PASS.
Mutation: in `run_intervals`, skip the cuts union (`if cuts_txt:` → `if False and cuts_txt:`); expect the ported cuts-merge test to FAIL; revert; PASS. Record evidence.

- [ ] **Step 4: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/intervals tests
git commit -m "refactor(intervals): extract typed run_intervals()"
```

---

### Task 12: `pipeline.stages` — the real STAGES registry

**Files:**
- Create: `src/nagare_clip/pipeline/stages.py`
- Test: `tests/pipeline/test_stages.py`

**Interfaces:**
- Consumes: `Stage`, `PipelineContext` (Task 3); `run_command` + builders (Task 2); all eight `run_*` functions (Tasks 4–11); `recorder_from_config` from `nagare_clip.llm_report`.
- Produces: `STAGE_NAMES: list[str]` and `STAGES: list[Stage]` (both in canonical order). Adapters print the same progress lines bash echoes.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_stages.py
"""Tests for the real STAGES registry wiring."""

import json
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.pipeline import stages as st
from nagare_clip.pipeline.runner import PipelineContext
from nagare_clip.pipeline.sources import SourceMedia


def _ctx(tmp_path, cfg=None, stems=("a", "b"), from_index=0, to_index=9):
    sources = [
        SourceMedia(abs_path=tmp_path / f"{s}.mp4", stem=s, relative=f"{s}.mp4")
        for s in stems
    ]
    return PipelineContext(
        cfg=cfg or get_effective_config(None, {}),
        project_root=tmp_path,
        config_path=None,
        input_videos_dir=tmp_path / "in",
        output_dir=tmp_path / "out",
        sources=sources,
        from_index=from_index,
        to_index=to_index,
    )


def test_stage_names_canonical_order():
    assert st.STAGE_NAMES == [
        "transcription", "audio_silence", "sentence_split", "text_filter",
        "summary", "plan", "director", "guided_edit", "intervals", "blender",
    ]
    assert [s.name for s in st.STAGES] == st.STAGE_NAMES


def test_transcription_required_only_when_sentence_split_runs(tmp_path):
    ctx = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("sentence_split"))
    req = st.STAGES[0].required_outputs(ctx)
    assert (tmp_path / "out" / "transcription" / "a.json") in req
    assert (tmp_path / "out" / "transcription" / "b.txt") in req
    # starting after sentence_split: transcription outputs not needed
    ctx2 = _ctx(tmp_path, from_index=st.STAGE_NAMES.index("text_filter"))
    assert st.STAGES[0].required_outputs(ctx2) == []


def test_per_source_required_outputs(tmp_path):
    ctx = _ctx(tmp_path, from_index=9)
    by_name = {s.name: s for s in st.STAGES}
    assert by_name["audio_silence"].required_outputs(ctx) == [
        tmp_path / "out" / "audio_silence" / "a_cuts.txt",
        tmp_path / "out" / "audio_silence" / "b_cuts.txt",
    ]
    assert by_name["summary"].required_outputs(ctx) == [
        tmp_path / "out" / "summary" / "summary.json"
    ]
    assert by_name["intervals"].required_outputs(ctx) == [
        tmp_path / "out" / "intervals" / "a_intervals.json",
        tmp_path / "out" / "intervals" / "b_intervals.json",
    ]
    assert by_name["blender"].required_outputs(ctx) == []


def test_sentence_split_adapter_clears_recorder_once(tmp_path, monkeypatch):
    calls = []

    class SpyRecorder:
        def clear(self):
            calls.append("clear")

        def rebuild_index(self):
            calls.append("rebuild")

    monkeypatch.setattr(
        st, "recorder_from_config", lambda *a, **k: SpyRecorder()
    )
    monkeypatch.setattr(
        st, "run_sentence_split", lambda *a, **k: calls.append("run")
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["sentence_split"].run(_ctx(tmp_path))
    assert calls == ["clear", "run", "run", "rebuild"]


def test_intervals_adapter_calls_run_per_source(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        st,
        "run_intervals",
        lambda edits, jsonp, out, cfg, *, cuts_txt=None: seen.append(
            (edits, jsonp, out, cuts_txt)
        ),
    )
    by_name = {s.name: s for s in st.STAGES}
    by_name["intervals"].run(_ctx(tmp_path, stems=("a",)))
    out = tmp_path / "out"
    assert seen == [
        (
            out / "guided_edit" / "a_edits.txt",
            out / "sentence_split" / "a.json",
            out / "intervals" / "a_intervals.json",
            out / "audio_silence" / "a_cuts.txt",
        )
    ]


def test_blender_adapter_builds_command(tmp_path, monkeypatch):
    captured = {}

    def fake_run_command(cmd, **kwargs):
        captured["cmd"] = cmd

    monkeypatch.setattr(st, "run_command", fake_run_command)
    by_name = {s.name: s for s in st.STAGES}
    by_name["blender"].run(_ctx(tmp_path, stems=("a",)))
    cmd = captured["cmd"]
    assert cmd[0] == "blender"
    assert str(tmp_path / "a.mp4") in cmd  # ORIGINAL source path
    assert str((tmp_path / "out" / "blender" / "a_edited.blend")) in cmd


def test_transcription_adapter_env_and_cmd(tmp_path, monkeypatch):
    captured = {}

    def fake_run_command(cmd, *, env_extra=None, stderr_to=None):
        captured["cmd"] = cmd
        captured["env"] = env_extra

    monkeypatch.setattr(st, "run_command", fake_run_command)
    st.STAGES[0].run(_ctx(tmp_path, stems=("a",)))
    assert captured["env"] == {
        "INPUT_VIDEOS_DIR": str(tmp_path / "in"),
        "OUTPUT_DIR": str(tmp_path / "out"),
    }
    assert "a.mp4" in captured["cmd"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_stages.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.pipeline.stages'`

- [ ] **Step 3: Implement**

```python
# src/nagare_clip/pipeline/stages.py
"""The declarative stage registry: one Stage entry per pipeline stage.

Adapters translate the PipelineContext into each stage's typed run()
function (or external command), print the same progress lines the bash
orchestrator echoed, and own the per-stage LLM-report recorder lifecycle
(clear once before the stem loop, rebuild the index after).
"""

from __future__ import annotations

from pathlib import Path

from nagare_clip.audio_silence.run import run_audio_silence
from nagare_clip.director.run import run_director
from nagare_clip.guided_edit.run import run_guided_edit
from nagare_clip.intervals.run import run_intervals
from nagare_clip.llm_report import recorder_from_config
from nagare_clip.pipeline.external import (
    build_blender_cmd,
    build_silencedetect_cmd,
    build_transcription_cmd,
    run_command,
)
from nagare_clip.pipeline.runner import PipelineContext, Stage
from nagare_clip.plan.run import run_plan
from nagare_clip.sentence_split.run import run_sentence_split
from nagare_clip.summary.run import run_summary
from nagare_clip.text_filter.run import run_text_filter

STAGE_NAMES = [
    "transcription", "audio_silence", "sentence_split", "text_filter",
    "summary", "plan", "director", "guided_edit", "intervals", "blender",
]


def _docker_env(ctx: PipelineContext) -> dict[str, str]:
    return {
        "INPUT_VIDEOS_DIR": str(ctx.input_videos_dir),
        "OUTPUT_DIR": str(ctx.output_dir),
    }


def _recorder(ctx: PipelineContext, stage: str):
    return recorder_from_config(stage, ctx.cfg, override_dir=str(ctx.llm_report_dir))


# --- transcription -----------------------------------------------------------

def _transcription_run(ctx: PipelineContext) -> None:
    rels = [s.relative for s in ctx.sources]
    print(f"[transcription] WhisperX: {' '.join(rels)}")
    run_command(
        build_transcription_cmd(ctx.project_root, rels, ctx.cfg),
        env_extra=_docker_env(ctx),
    )


def _transcription_required(ctx: PipelineContext) -> list[Path]:
    # {stem}.json/.txt are consumed only by sentence_split; later start points
    # read output/sentence_split/ instead (validated by that stage's entry).
    if ctx.from_index > STAGE_NAMES.index("sentence_split"):
        return []
    d = ctx.stage_dir("transcription")
    return [d / f"{s}{ext}" for s in ctx.stems for ext in (".json", ".txt")]


# --- audio_silence -----------------------------------------------------------

def _audio_silence_run(ctx: PipelineContext) -> None:
    a = ctx.cfg["audio_silence"]
    d = ctx.stage_dir("audio_silence")
    for src in ctx.sources:
        print(f"[audio_silence] Detection: {src.stem}")
        raw_path = None
        if a["enabled"]:
            raw_path = d / f"{src.stem}_silencedetect.log"
            run_command(
                build_silencedetect_cmd(
                    ctx.project_root, src.relative, a["noise"], a["min_silence"]
                ),
                env_extra=_docker_env(ctx),
                stderr_to=raw_path,
            )
        run_audio_silence(d / f"{src.stem}_cuts.txt", ctx.cfg, raw_path=raw_path)


def _audio_silence_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("audio_silence")
    return [d / f"{s}_cuts.txt" for s in ctx.stems]


# --- sentence_split ----------------------------------------------------------

def _sentence_split_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "sentence_split")
    rec.clear()
    try:
        tdir = ctx.stage_dir("transcription")
        odir = ctx.stage_dir("sentence_split")
        for src in ctx.sources:
            print(f"[sentence_split] Sentence re-segmentation: {src.stem}")
            run_sentence_split(
                tdir / f"{src.stem}.json",
                tdir / f"{src.stem}.txt",
                odir / f"{src.stem}.json",
                odir / f"{src.stem}.txt",
                ctx.cfg,
                stem=src.stem,
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _sentence_split_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("sentence_split")
    return [d / f"{s}{ext}" for s in ctx.stems for ext in (".json", ".txt")]


# --- text_filter -------------------------------------------------------------

def _text_filter_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "text_filter")
    rec.clear()
    try:
        sdir = ctx.stage_dir("sentence_split")
        odir = ctx.stage_dir("text_filter")
        for src in ctx.sources:
            print(f"[text_filter] Text editing checkpoint: {src.stem}")
            run_text_filter(
                sdir / f"{src.stem}.txt",
                odir / f"{src.stem}_edits.txt",
                ctx.cfg,
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _text_filter_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("text_filter")
    return [d / f"{s}_edits.txt" for s in ctx.stems]


# --- summary -----------------------------------------------------------------

def _summary_run(ctx: PipelineContext) -> None:
    print("[summary] Project-wide summaries")
    rec = _recorder(ctx, "summary")
    rec.clear()
    try:
        run_summary(
            [ctx.stage_dir("text_filter") / f"{s}_edits.txt" for s in ctx.stems],
            ctx.stage_dir("summary") / "summary.json",
            ctx.cfg,
            json_paths=[ctx.stage_dir("sentence_split") / f"{s}.json" for s in ctx.stems],
            recorder=rec,
        )
    finally:
        rec.rebuild_index()


def _summary_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("summary") / "summary.json"]


# --- plan --------------------------------------------------------------------

def _plan_run(ctx: PipelineContext) -> None:
    print("[plan] Cross-video rough directions")
    rec = _recorder(ctx, "plan")
    rec.clear()
    try:
        run_plan(
            ctx.stage_dir("summary") / "summary.json",
            ctx.stage_dir("plan") / "plan.json",
            ctx.cfg,
            recorder=rec,
        )
    finally:
        rec.rebuild_index()


def _plan_required(ctx: PipelineContext) -> list[Path]:
    return [ctx.stage_dir("plan") / "plan.json"]


# --- director ----------------------------------------------------------------

def _director_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "director")
    rec.clear()
    try:
        for src in ctx.sources:
            print(f"[director] Edit operations: {src.stem}")
            run_director(
                ctx.stage_dir("text_filter") / f"{src.stem}_edits.txt",
                ctx.stage_dir("director") / f"{src.stem}_director.json",
                ctx.cfg,
                summary=ctx.stage_dir("summary") / "summary.json",
                plan=ctx.stage_dir("plan") / "plan.json",
                stem=src.stem,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _director_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("director")
    return [d / f"{s}_director.json" for s in ctx.stems]


# --- guided_edit -------------------------------------------------------------

def _guided_edit_run(ctx: PipelineContext) -> None:
    rec = _recorder(ctx, "guided_edit")
    rec.clear()
    try:
        for src in ctx.sources:
            print(f"[guided_edit] Applying director ops: {src.stem}")
            run_guided_edit(
                ctx.stage_dir("text_filter") / f"{src.stem}_edits.txt",
                ctx.stage_dir("director") / f"{src.stem}_director.json",
                ctx.stage_dir("guided_edit") / f"{src.stem}_edits.txt",
                ctx.cfg,
                json_path=ctx.stage_dir("sentence_split") / f"{src.stem}.json",
                recorder=rec,
            )
    finally:
        rec.rebuild_index()


def _guided_edit_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("guided_edit")
    return [d / f"{s}_edits.txt" for s in ctx.stems]


# --- intervals ---------------------------------------------------------------

def _intervals_run(ctx: PipelineContext) -> None:
    for src in ctx.sources:
        print(f"[intervals] Patch application + keep intervals: {src.stem}")
        run_intervals(
            ctx.stage_dir("guided_edit") / f"{src.stem}_edits.txt",
            ctx.stage_dir("sentence_split") / f"{src.stem}.json",
            ctx.stage_dir("intervals") / f"{src.stem}_intervals.json",
            ctx.cfg,
            cuts_txt=ctx.stage_dir("audio_silence") / f"{src.stem}_cuts.txt",
        )


def _intervals_required(ctx: PipelineContext) -> list[Path]:
    d = ctx.stage_dir("intervals")
    return [d / f"{s}_intervals.json" for s in ctx.stems]


# --- blender -----------------------------------------------------------------

def _blender_run(ctx: PipelineContext) -> None:
    print("[blender] VSE project generation")
    output_blend = ctx.stage_dir("blender") / f"{ctx.stems[0]}_edited.blend"
    intervals_paths = [
        ctx.stage_dir("intervals") / f"{s}_intervals.json" for s in ctx.stems
    ]
    run_command(
        build_blender_cmd(
            ctx.project_root,
            [s.abs_path for s in ctx.sources],
            intervals_paths,
            output_blend,
            ctx.config_path,
            ctx.log_file,
        )
    )


STAGES = [
    Stage("transcription", _transcription_run, _transcription_required),
    Stage("audio_silence", _audio_silence_run, _audio_silence_required),
    Stage("sentence_split", _sentence_split_run, _sentence_split_required),
    Stage("text_filter", _text_filter_run, _text_filter_required),
    Stage("summary", _summary_run, _summary_required),
    Stage("plan", _plan_run, _plan_required),
    Stage("director", _director_run, _director_required),
    Stage("guided_edit", _guided_edit_run, _guided_edit_required),
    Stage("intervals", _intervals_run, _intervals_required),
    Stage("blender", _blender_run),
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/pipeline/stages.py tests/pipeline/test_stages.py
git commit -m "feat(pipeline): wire the real STAGES registry"
```

---

### Task 13: `pipeline.cli` + `__main__` entry points

**Files:**
- Create: `src/nagare_clip/pipeline/cli.py`, `src/nagare_clip/pipeline/__main__.py`
- Modify: `src/nagare_clip/__main__.py` (re-alias to pipeline)
- Test: `tests/pipeline/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv: list[str] | None = None) -> int`; `parse_args(argv) -> argparse.Namespace`; `build_cli_overrides(args) -> dict`.
- Flags: `--source` (append), `--config`, `--language`, `--input-videos-dir`, `--output-dir`, `--keep-pre-margin` (float), `--keep-post-margin` (float), `--from-stage`, `--to-stage`, `--align-model`.
- Override mapping: `--language → transcription.language`, `--align-model → transcription.align_model`, `--input-videos-dir → pipeline.input_videos_dir`, `--output-dir → pipeline.output_dir`, `--from-stage → pipeline.from_stage`, `--to-stage → pipeline.to_stage`, `--keep-pre-margin → intervals.keep_pre_margin`, `--keep-post-margin → intervals.keep_post_margin`.
- Env: `NAGARE_RUN_ID` set once (`%Y%m%d-%H%M%S`, `setdefault`); `general.langfuse: false` → `os.environ["NAGARE_LANGFUSE"] = "0"`.
- Final line: `Done: <blend>` when blender ran, else `Done (stopped at --to-stage X)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/pipeline/test_cli.py
"""Tests for the pipeline CLI: flags, overrides, env, wiring."""

import os
from pathlib import Path

import pytest
import yaml

from nagare_clip.pipeline import cli


def test_build_cli_overrides_mapping():
    args = cli.parse_args(
        [
            "--language", "en",
            "--align-model", "m/x",
            "--input-videos-dir", "vids",
            "--output-dir", "out2",
            "--keep-pre-margin", "0.5",
            "--keep-post-margin", "0.25",
            "--from-stage", "intervals",
            "--to-stage", "blender",
        ]
    )
    assert cli.build_cli_overrides(args) == {
        "transcription": {"language": "en", "align_model": "m/x"},
        "pipeline": {
            "input_videos_dir": "vids",
            "output_dir": "out2",
            "from_stage": "intervals",
            "to_stage": "blender",
        },
        "intervals": {"keep_pre_margin": 0.5, "keep_post_margin": 0.25},
    }


def test_build_cli_overrides_empty_when_no_flags():
    assert cli.build_cli_overrides(cli.parse_args([])) == {}


def test_missing_config_file_errors(tmp_path, capsys):
    rc = cli.main(["--config", str(tmp_path / "nope.yml")])
    assert rc == 1
    assert "Config file not found" in capsys.readouterr().err


def test_invalid_stage_name_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    rc = cli.main(["--from-stage", "bogus"])
    assert rc == 1
    assert "Invalid --from-stage value: bogus" in capsys.readouterr().err


def test_pipeline_wires_context_and_prints_done(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    seen = {}

    def fake_run_stages(stages, ctx):
        seen["ctx"] = ctx

    monkeypatch.setattr(cli, "run_stages", fake_run_stages)
    rc = cli.main([])
    assert rc == 0
    ctx = seen["ctx"]
    assert ctx.stems == ["a"]
    assert ctx.from_index == 0 and ctx.to_index == 9
    assert ctx.output_dir == (tmp_path / "output").resolve()
    # stage output dirs created upfront
    assert (tmp_path / "output" / "intervals").is_dir()
    assert (tmp_path / "cache").is_dir()
    assert "Done: " in capsys.readouterr().out


def test_to_stage_prints_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    rc = cli.main(["--to-stage", "intervals"])
    assert rc == 0
    assert "Done (stopped at --to-stage intervals)" in capsys.readouterr().out


def test_langfuse_false_sets_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NAGARE_LANGFUSE", raising=False)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    cfg = tmp_path / "c.yml"
    cfg.write_text(yaml.safe_dump({"general": {"langfuse": False}}))
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--config", str(cfg)]) == 0
    assert os.environ["NAGARE_LANGFUSE"] == "0"


def test_run_id_set(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("NAGARE_RUN_ID", raising=False)
    (tmp_path / "src_video").mkdir()
    (tmp_path / "src_video" / "a.mp4").write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main([]) == 0
    assert os.environ.get("NAGARE_RUN_ID")


def test_cleanup_of_copied_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src_video").mkdir()
    outside = tmp_path / "clip.mp4"
    outside.write_bytes(b"x")
    monkeypatch.setattr(cli, "run_stages", lambda stages, ctx: None)
    assert cli.main(["--source", str(outside)]) == 0
    assert not (tmp_path / "src_video" / "clip.mp4").exists()
    assert outside.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/pipeline/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'nagare_clip.pipeline.cli'`

- [ ] **Step 3: Implement**

```python
# src/nagare_clip/pipeline/cli.py
"""Pipeline orchestrator CLI: python -m nagare_clip.pipeline.

Replaces the bash orchestration in scripts/run_pipeline.sh (now a shim).
Flags mirror the historical script; explicit CLI values are merged as
config overrides so precedence is CLI > YAML > model defaults.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from nagare_clip.config import get_effective_config
from nagare_clip.logging_setup import setup_logging
from nagare_clip.pipeline.errors import PipelineError
from nagare_clip.pipeline.runner import PipelineContext, resolve_window, run_stages
from nagare_clip.pipeline.sources import (
    discover_sources,
    resolve_cli_sources,
    stage_sources,
)
from nagare_clip.pipeline.stages import STAGE_NAMES, STAGES

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="nagare-clip pipeline",
        description="Run the rough-cut pipeline end-to-end.",
    )
    parser.add_argument(
        "--source", action="append", default=None,
        help="Source video file (repeatable; default: all videos in the input dir)",
    )
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument("--language", default=None, help="WhisperX language code")
    parser.add_argument("--input-videos-dir", default=None, dest="input_videos_dir")
    parser.add_argument("--output-dir", default=None, dest="output_dir")
    parser.add_argument(
        "--keep-pre-margin", type=float, default=None, dest="keep_pre_margin"
    )
    parser.add_argument(
        "--keep-post-margin", type=float, default=None, dest="keep_post_margin"
    )
    parser.add_argument(
        "--from-stage", default=None, dest="from_stage",
        help=f"Start from this stage; one of: {' '.join(STAGE_NAMES)}",
    )
    parser.add_argument(
        "--to-stage", default=None, dest="to_stage",
        help="Stop after this stage (inclusive)",
    )
    parser.add_argument("--align-model", default=None, dest="align_model")
    return parser.parse_args(argv)


def build_cli_overrides(args: argparse.Namespace) -> dict:
    overrides: dict = {}

    def put(section: str, key: str, value) -> None:
        if value is not None:
            overrides.setdefault(section, {})[key] = value

    put("transcription", "language", args.language)
    put("transcription", "align_model", args.align_model)
    put("pipeline", "input_videos_dir", args.input_videos_dir)
    put("pipeline", "output_dir", args.output_dir)
    put("pipeline", "from_stage", args.from_stage)
    put("pipeline", "to_stage", args.to_stage)
    put("intervals", "keep_pre_margin", args.keep_pre_margin)
    put("intervals", "keep_post_margin", args.keep_post_margin)
    return overrides


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config_path = None
        if args.config:
            config_path = Path(args.config).resolve()
            if not config_path.is_file():
                raise PipelineError(f"Config file not found: {args.config}")
        cfg = get_effective_config(config_path, build_cli_overrides(args))

        # One session id per pipeline run; Langfuse groups all LLM calls under it.
        os.environ.setdefault(
            "NAGARE_RUN_ID", datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        if not cfg["general"]["langfuse"]:
            os.environ["NAGARE_LANGFUSE"] = "0"

        p = cfg["pipeline"]
        input_dir = Path(p["input_videos_dir"])
        output_dir = Path(p["output_dir"])
        input_dir.mkdir(parents=True, exist_ok=True)
        for name in STAGE_NAMES:
            (output_dir / name).mkdir(parents=True, exist_ok=True)
        Path("cache").mkdir(exist_ok=True)

        setup_logging(
            cfg["general"]["log_level"], str(output_dir / "pipeline.log")
        )

        if args.source:
            paths = resolve_cli_sources(args.source, input_dir)
        else:
            paths = discover_sources(input_dir)
        sources, cleanup = stage_sources(paths, input_dir)

        from_index, to_index = resolve_window(STAGES, p["from_stage"], p["to_stage"])
        ctx = PipelineContext(
            cfg=cfg,
            project_root=PROJECT_ROOT,
            config_path=config_path,
            input_videos_dir=input_dir.resolve(),
            output_dir=output_dir.resolve(),
            sources=sources,
            from_index=from_index,
            to_index=to_index,
        )
        try:
            run_stages(STAGES, ctx)
        finally:
            for f in cleanup:
                f.unlink(missing_ok=True)

        if to_index == len(STAGES) - 1:
            blend = ctx.stage_dir("blender") / f"{ctx.stems[0]}_edited.blend"
            print(f"Done: {blend}")
        else:
            print(f"Done (stopped at --to-stage {p['to_stage']})")
        return 0
    except PipelineError as exc:
        print(exc, file=sys.stderr)
        return 1
```

```python
# src/nagare_clip/pipeline/__main__.py
"""Run the pipeline orchestrator via ``python -m nagare_clip.pipeline``."""

import sys

from nagare_clip.pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

```python
# src/nagare_clip/__main__.py  (replaces the intervals alias)
"""Run the pipeline orchestrator via ``python -m nagare_clip``."""

import sys

from nagare_clip.pipeline.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/pipeline -v`
Expected: all PASS

- [ ] **Step 5: Full suite + commit**

```bash
uv run pytest
git add src/nagare_clip/pipeline src/nagare_clip/__main__.py tests/pipeline
git commit -m "feat(pipeline): add orchestrator CLI and __main__ entry points"
```

---

### Task 14: Shim `scripts/run_pipeline.sh`

**Files:**
- Modify: `scripts/run_pipeline.sh` (replace entire content)

**Interfaces:**
- Consumes: `python -m nagare_clip.pipeline` (Task 13).

- [ ] **Step 1: Replace the script**

```bash
#!/usr/bin/env bash
# Thin shim: orchestration lives in nagare_clip.pipeline (Python).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
exec uv run --project "$PROJECT_ROOT" python -m nagare_clip.pipeline "$@"
```

- [ ] **Step 2: Verify**

Run: `bash -n scripts/run_pipeline.sh` — Expected: no output (syntax OK).
Run: `./scripts/run_pipeline.sh --from-stage bogus; echo "exit=$?"` — Expected: `Invalid --from-stage value: bogus` on stderr and `exit=1`.
Run: `./scripts/run_pipeline.sh --help` — Expected: argparse help listing the flags.

- [ ] **Step 3: Commit**

```bash
git add scripts/run_pipeline.sh
git commit -m "feat(pipeline): replace bash orchestrator with 3-line shim"
```

---

### Task 15: Delete the per-stage CLIs

**Files:**
- Delete: `src/nagare_clip/{audio_silence,sentence_split,text_filter,summary,plan,director,guided_edit,intervals}/cli.py`
- Modify: `pyproject.toml` (remove the `nagare-clip-intervals = "nagare_clip.intervals.cli:main"` console script)
- Keep: `src/nagare_clip/blender/blender_cli.py`, `src/nagare_clip/intervals/check_edits.py`

- [ ] **Step 1: Verify nothing references the CLIs anymore**

Run: `grep -rn "\.cli import\|\.cli:main\|\.cli\b" src tests pyproject.toml Makefile .github --include='*' | grep -v blender_cli | grep -v check_edits`
Expected: only the `pyproject.toml` console-script line (and possibly comments). If tests still import a `cli` module, fix them first.

- [ ] **Step 2: Delete**

```bash
git rm src/nagare_clip/audio_silence/cli.py src/nagare_clip/sentence_split/cli.py \
  src/nagare_clip/text_filter/cli.py src/nagare_clip/summary/cli.py \
  src/nagare_clip/plan/cli.py src/nagare_clip/director/cli.py \
  src/nagare_clip/guided_edit/cli.py src/nagare_clip/intervals/cli.py
```

Remove from `pyproject.toml` `[project.scripts]`: the `nagare-clip-intervals` line (delete the whole `[project.scripts]` table if it becomes empty).

- [ ] **Step 3: Verify**

Run: `uv run pytest` — Expected: PASS.
Run: `make validate` — Expected: PASS (py_compile no longer sees the deleted files).
Run: `uv run python -m nagare_clip.pipeline --help` — Expected: help text.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "refactor: delete per-stage CLIs; pipeline CLI is the entry point"
```

---

### Task 16: Documentation + final validation

**Files:**
- Modify: `README.md`, `AGENTS.md`, `plan.md`, `docs/stages/pipeline.md`, `docs/stages/observability.md`

- [ ] **Step 1: Update `AGENTS.md`**
  - Project Structure: add the `pipeline/` package (all 7 modules with one-line descriptions), replace each stage's `cli.py` line with its `run.py`, note `__main__.py` now runs the pipeline.
  - Configuration System: delete the "Known residual: `scripts/run_pipeline.sh` still reads a few `pipeline.*` … values from raw YAML" paragraph and the final paragraph about `run_pipeline.sh` reading config keys via Python/yaml; replace the `--config` pass-through paragraph (only `blender_cli.py` still takes `--config` on its command line; all other stages receive the config dict in-process).
  - Pipeline Overview intro: note `scripts/run_pipeline.sh` is a shim over `python -m nagare_clip.pipeline`.

- [ ] **Step 2: Update `docs/stages/pipeline.md`** — rewrite for the Python orchestrator: registry/runner/windowing semantics (unknown stage names now raise a friendly `PipelineError`; the bash `stage_index` set-e quirk is gone), source discovery/staging behavior, recorder lifecycle (clear once per stage), `NAGARE_RUN_ID`/`NAGARE_LANGFUSE` handling, and the fact that Docker/Blender are the only subprocesses.

- [ ] **Step 3: Update `README.md`** — usage keeps `./scripts/run_pipeline.sh`; replace standalone stage-CLI examples (`python -m nagare_clip.audio_silence.cli`, `python -m nagare_clip.intervals.cli`, the `python -m nagare_clip.director.cli` mention) with `--from-stage X --to-stage X` pipeline invocations or `check_edits` where appropriate; keep the `blender_cli.py` example (unchanged).

- [ ] **Step 4: Update `plan.md`** — add a short status entry: orchestration ported to `nagare_clip.pipeline`; per-stage CLIs removed.

- [ ] **Step 5: Update `docs/stages/observability.md`** — the sentence "`run_pipeline.sh` exports `NAGARE_RUN_ID` … and maps `general.langfuse: false` to `export NAGARE_LANGFUSE=0` for all subprocesses" now attributes this to the pipeline CLI (single process; Blender subprocess inherits), and "each stage CLI clears its own subdir" becomes "the orchestrator clears each stage's subdir once per run".

- [ ] **Step 6: Final validation**

Run: `make check` — Expected: lint, format-check, validate, and the full pytest suite all PASS.

- [ ] **Step 7: Commit**

```bash
git add README.md AGENTS.md plan.md docs/stages
git commit -m "docs: describe Python pipeline orchestration"
```

---

## Self-Review Notes (already applied)

- Spec coverage: layout (T1–3, 12–13), run() extraction + CLI deletion (T4–11, 15), shim (T14), `__main__` re-alias (T13), env/recorder/logging centralization (T12–13), docs incl. residual-note removal (T16), dead bash vars dropped (no task ports them).
- The `nagare-clip-intervals` console script removal (T15) was found during planning; the spec's "no console-script entry point" holds.
- Blender must reference original media: enforced by `SourceMedia.abs_path` semantics (T1) and asserted in T12's blender adapter test.
