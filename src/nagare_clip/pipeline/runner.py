"""Generic stage runner: window resolution, skip validation, execution.

Stages are identified only by name (no numbers); a new stage is inserted by
adding a registry entry, without renumbering anything. Skipped-over stages
(--from-stage) validate that their outputs already exist; stages past
--to-stage are skipped without validation (they are intentionally not built).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from nagare_clip.pipeline.errors import PipelineError, PipelineStop
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
    required_outputs: Callable[[PipelineContext], list[Path]] = field(default=_no_outputs)


def resolve_window(stages: Sequence[Stage], from_stage: str, to_stage: str) -> tuple[int, int]:
    names = [s.name for s in stages]
    hint = f"Use a stage name: {' '.join(names)}."
    if from_stage not in names:
        raise PipelineError(f"Invalid --from-stage value: {from_stage}\n{hint}")
    if to_stage not in names:
        raise PipelineError(f"Invalid --to-stage value: {to_stage}\n{hint}")
    from_index, to_index = names.index(from_stage), names.index(to_stage)
    if from_index > to_index:
        raise PipelineError(
            f"Invalid stage range: --from-stage ({from_stage}) is after --to-stage ({to_stage})."
        )
    return from_index, to_index


def run_stages(stages: Sequence[Stage], ctx: PipelineContext) -> None:
    for idx, stage in enumerate(stages):
        if ctx.from_index <= idx <= ctx.to_index:
            try:
                stage.run(ctx)
            except (PipelineError, PipelineStop):
                raise
            except Exception as exc:
                logging.exception("[%s] stage failed", stage.name)
                raise PipelineError(f"[{stage.name}] failed: {exc}") from exc
        elif idx > ctx.to_index:
            print(f"[{stage.name}] Skipped (--to-stage {stages[ctx.to_index].name})")
        else:
            print(f"[{stage.name}] Skipped (--from-stage {stages[ctx.from_index].name})")
            for path in stage.required_outputs(ctx):
                if not path.is_file():
                    raise PipelineError(
                        f"Missing {stage.name} output: {path} (required when skipping {stage.name})"
                    )
