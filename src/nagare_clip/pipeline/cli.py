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
        "--source",
        action="append",
        default=None,
        help="Source video file (repeatable; default: all videos in the input dir)",
    )
    parser.add_argument("--config", default=None, help="Path to YAML config file")
    parser.add_argument("--language", default=None, help="WhisperX language code")
    parser.add_argument("--input-videos-dir", default=None, dest="input_videos_dir")
    parser.add_argument("--output-dir", default=None, dest="output_dir")
    parser.add_argument("--keep-pre-margin", type=float, default=None, dest="keep_pre_margin")
    parser.add_argument("--keep-post-margin", type=float, default=None, dest="keep_post_margin")
    parser.add_argument(
        "--from-stage",
        default=None,
        dest="from_stage",
        help=f"Start from this stage; one of: {' '.join(STAGE_NAMES)}",
    )
    parser.add_argument(
        "--to-stage",
        default=None,
        dest="to_stage",
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
        os.environ.setdefault("NAGARE_RUN_ID", datetime.now().strftime("%Y%m%d-%H%M%S"))
        if not cfg["general"]["langfuse"]:
            os.environ["NAGARE_LANGFUSE"] = "0"

        p = cfg["pipeline"]
        input_dir = Path(p["input_videos_dir"])
        output_dir = Path(p["output_dir"])
        input_dir.mkdir(parents=True, exist_ok=True)
        for name in STAGE_NAMES:
            (output_dir / name).mkdir(parents=True, exist_ok=True)
        (PROJECT_ROOT / "cache").mkdir(exist_ok=True)

        setup_logging(cfg["general"]["log_level"], str(output_dir / "pipeline.log"))

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

        # The .blend is the deliverable; the publish material (when the run
        # reached that stage) sits beside it and is the next thing a human
        # opens, so both paths are printed.
        last_stage = STAGES[to_index].name
        if last_stage in ("blender", "publish"):
            print(f"Done: {ctx.stage_dir('blender') / f'{ctx.stems[0]}_edited.blend'}")
            if last_stage == "publish":
                print(f"Publish material: {ctx.stage_dir('publish') / 'publish.md'}")
        else:
            print(f"Done (stopped at --to-stage {p['to_stage']})")
        return 0
    except PipelineError as exc:
        print(exc, file=sys.stderr)
        return 1
