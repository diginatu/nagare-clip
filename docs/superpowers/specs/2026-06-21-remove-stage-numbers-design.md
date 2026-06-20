# Remove all stage numbers — migrate to functional names

**Date:** 2026-06-21
**Status:** Approved

## Goal

Eliminate every stage *number* from the living codebase, completing the
functional-name migration that AGENTS.md already declared as the direction
("numbers are being phased out so new stages can be inserted without
renumbering"). After this change, stages are identified only by their
functional / config-section name.

## Canonical mapping

| Old | New (functional name) |
|---|---|
| `src/nagare_clip/stage2/` | `src/nagare_clip/text_filter/` |
| `src/nagare_clip/stage3/` | `src/nagare_clip/intervals/` |
| `src/nagare_clip/stage4/` | `src/nagare_clip/blender/` |
| `src/nagare_clip/cli.py` (intervals-stage CLI) | `src/nagare_clip/intervals/cli.py` |
| `tests/stage2/` | `tests/text_filter/` |
| `tests/stage3/` | `tests/intervals/` |
| `tests/stage4/` | `tests/blender/` |
| `output/stage1` | `output/transcription` |
| `output/stage2` | `output/audio_silence` |
| `output/stage3` | `output/text_filter` |
| `output/stage4` | `output/intervals` |
| `output/stage5` | `output/blender` |
| prose `Stage 1..5` / `### Stage N —` headings | `transcription` / `audio_silence` / `text_filter` / `intervals` / `blender` |

The stages already named (`audio_silence`, `summary`, `plan`, `director`,
`guided_edit`) are unchanged.

## Changes

1. **Rename 3 package dirs + 3 test dirs** via `git mv`; rewrite all
   `nagare_clip.stage{2,3,4}` import references (~81) and any internal
   relative imports.
2. **Move `src/nagare_clip/cli.py` → `src/nagare_clip/intervals/cli.py`** so
   the intervals stage matches every other stage (`<name>/cli.py`). Update
   `__main__.py` to re-export `from nagare_clip.intervals.cli import main`
   (keeps `python -m nagare_clip` working) and drop its "Stage 3" docstring.
3. **`pyproject.toml`**: `nagare-clip-stage2` → `nagare-clip-intervals`,
   target `nagare_clip.intervals.cli:main`.
4. **`scripts/run_pipeline.sh`**:
   - rename `STAGE{1..5}_DIR` vars and `output/stageN` literals to the named
     output dirs (incl. the Docker `--output_dir /output/stage1` →
     `/output/transcription`);
   - update module invocations `nagare_clip.stage2.cli` →
     `nagare_clip.text_filter.cli` and `nagare_clip.cli` →
     `nagare_clip.intervals.cli`;
   - **drop** the legacy `1-5` numeric `--from-stage`/`--to-stage` mapping —
     stage names only; update usage/help text accordingly.
5. **`.opencode/plugin/validate.ts`**: repoint the `py_compile` list at the
   renamed dirs with the correct current file list (it is currently stale —
   references `stage2/morpheme.py` etc. that no longer exist).
6. **Living docs** (`README.md`, `AGENTS.md`, `config.example.yml`,
   `docs/llm-editing-context.md`): convert `### Stage N —` headings to
   `### <name> —` (matching the existing `### summary —` style), rewrite
   inline `Stage N` cross-references to functional names, and rewrite the
   AGENTS.md naming-convention note (which currently documents the legacy
   numbers as intentional — now false).
7. **Code comments / docstrings / echo strings** referencing `Stage N`.

## Out of scope

- Historical `docs/superpowers/specs/` and `docs/superpowers/plans/` records —
  dated archive, left untouched.
- The ordered pipeline-overview *list* (`1. WhisperX … 9. Blender`) stays an
  ordered list — that is enumeration, not a stage identifier.
- Existing `output/stageN/` runtime artifacts on disk — regenerable, left
  as-is.

## Verification

- The test suite is the primary guard: renames must keep `uv run pytest`
  green.
- Behavioral change (dropping numeric `--from-stage`): add/adjust a test
  asserting a numeric value is now rejected, confirmed failing against the
  pre-change name-accepting code first.
- Completion gate: `grep -rIE 'stage[1-5]|Stage [1-5]'` over living files
  (excluding historical docs) returns empty.
