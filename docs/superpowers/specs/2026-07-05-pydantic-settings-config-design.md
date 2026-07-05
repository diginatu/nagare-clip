# Config migration to pydantic-settings — design

**Date:** 2026-07-05
**Status:** design (awaiting user review)

## Problem

`src/nagare_clip/config.py` hand-maintains a 300+ line `DEFAULTS` dict, hand-merges
YAML/CLI via `deep_merge`, and relies on a dedicated lint test
(`TestExampleConfigInSync`, plus its heuristic helper `_missing_example_paths`) to
stop `config.example.yml` from drifting out of sync with `DEFAULTS`.

Consequences today:

- **No validation.** A typo'd key (`intervals.silence_threshld`, or a mis-nested
  block) silently vanishes into the merged dict and is ignored — the stage runs
  with the default and the user never learns their setting did nothing.
- **Two sources of truth for the example file.** `DEFAULTS` (authoritative) and
  the hand-written `config.example.yml` must be kept aligned by a *heuristic*
  string-matching test that accepts either a real key or a `# commented:` line.
- **`pipeline.to_stage` drift.** `run_pipeline.sh` reads `pipeline.to_stage`, but
  it is absent from `DEFAULTS` — an existing inconsistency.

`pydantic-settings>=2.14.2` is already a declared dependency (`pyproject.toml`) but
is currently unused. This migration makes the typed models the single source of
truth.

## Goals

1. **Typed pydantic models are the single source of truth** for all config
   defaults, structure, and value types.
2. **Load-time validation.** Unknown keys and wrong-typed values raise a clear
   error at `get_effective_config` time instead of being silently dropped.
3. **`config.example.yml` is generated** from the models' fields + descriptions,
   so it cannot drift. The sync test becomes an exact "regenerate and compare"
   check (like `black --check`), not a heuristic.

## Non-goals (explicit)

- **Consumers keep the dict interface.** `get_effective_config(config_path,
  cli_overrides)` keeps its signature and still returns a **plain `dict`**
  (`model_dump()`). All 10 stage CLIs, `blender_cli.py`, `llm_client.call_llm`,
  and `llm_retry` are **unchanged** — they keep reading `cfg["general"]["…"]` /
  `cfg.get(...)`. Typed attribute access downstream is a possible later
  follow-up, not part of this migration. *Rationale: smallest, safest diff;
  validation happens once at the load boundary, which is where it matters.*
- **`scripts/run_pipeline.sh` is not migrated.** It keeps reading raw YAML with
  its own inline defaults. *Rationale: shell behavior risk; it duplicates only a
  handful of defaults. See "Known residual" below for the follow-up.*
- **Environment-variable overrides are not wired in.** Section models are plain
  `BaseModel`; the root is `BaseSettings` (honoring the pydantic-settings choice
  and leaving env support trivial to add later), but no env source is enabled, so
  precedence stays exactly **CLI > YAML > defaults**. *Rationale: enabling env
  would add a new precedence layer and surprising behavior the user didn't ask
  for (YAGNI).*

## Design

### Model structure (`src/nagare_clip/config.py`, rewritten)

One `BaseModel` per config section, nested to mirror today's `DEFAULTS` shape,
composed by a root `NagareClipConfig(BaseSettings)`:

```
NagareClipConfig(BaseSettings)          # extra="forbid"
├─ general:        GeneralConfig
├─ transcription:  TranscriptionConfig
├─ audio_silence:  AudioSilenceConfig
├─ sentence_split: SentenceSplitConfig
├─ text_filter:    TextFilterConfig
│                   └─ summary_llm: SummaryLLMConfig
├─ summary:        SummaryConfig
├─ plan:           PlanConfig
├─ director:       DirectorConfig
├─ guided_edit:    GuidedEditConfig
├─ intervals:      IntervalsConfig
│                   ├─ caption: CaptionConfig
│                   └─ bunsetu: BunsetuConfig
├─ blender:        BlenderConfig
│                   ├─ caption_style: CaptionStyleConfig
│                   ├─ overlay_style: OverlayStyleConfig
│                   └─ speed_mark:    SpeedMarkConfig
└─ pipeline:       PipelineConfig
```

- **`extra` is a per-model policy, not global:**
  - **`extra="forbid"`** on every model *except* the three Blender style blocks.
    This is the validation win: unknown top-level sections *and* unknown/typo'd
    leaf keys raise `ValidationError`.
  - **`extra="allow"`** on `CaptionStyleConfig`, `OverlayStyleConfig`, and
    `SpeedMarkConfig`. These are **intentionally open-ended style pass-throughs**:
    `blender/timeline.py::apply_text_style` forwards *arbitrary* keys 1:1 to
    Blender TextStrip RNA attributes (`font` — an absolute font path loaded into
    a `VectorFont` — plus `shadow_blur`, `shadow_color`, `box_margin`, …), none
    of which live in `DEFAULTS`. Forbidding extras here would break custom fonts
    and arbitrary styling (covered by `tests/blender/test_text_style.py`). The
    known 5 layout keys (`font_size`, `alignment_x`, `anchor_y`, `location_x`,
    `location_y`) are still typed fields; only *additional* keys are allowed
    through. A typo'd style key (`font_sze`) is therefore *not* caught at config
    load — but that already matches today's behavior (Blender RNA rejects it and
    `apply_text_style` logs "Unknown caption_style key ignored"), so nothing
    regresses.
- **Every field carries its default and a `description`** ported from the
  corresponding comment in today's `config.example.yml` (e.g.
  `log_level: str = Field("INFO", description="DEBUG | INFO | WARNING | ERROR | CRITICAL")`).
  Field types come from the current default values (`float`, `int`, `bool`,
  `str`, `list[str]`).
- **`pipeline.to_stage`** is added (e.g. `to_stage: str | None = None`) to close
  the existing `DEFAULTS` gap and avoid `extra="forbid"` rejecting a valid,
  currently-used YAML key. `from_stage` keeps its present default to preserve
  `run_pipeline.sh` behavior (its staleness vs. the name-based-stage convention
  is noted but not changed here).
- **Multiline prompt strings** (`sentence_split.prompt`, `text_filter.prompt`,
  `summary.prompt`/`overall_prompt`, `plan.prompt`, `director.prompt`,
  `guided_edit.prompt`, `text_filter.summary_llm.prompt`) are field defaults,
  exactly as today.

### Public API (backward compatible)

```python
DEFAULTS: dict           # = NagareClipConfig().model_dump()  — DERIVED, not authored
def load_config(path: Path | None) -> dict          # unchanged
def deep_merge(base: dict, override: dict) -> dict   # kept (used internally + tested)
def get_effective_config(config_path, cli_overrides=None) -> dict
```

`get_effective_config` new internals:

1. `file_cfg = load_config(config_path)`
2. `merged = deep_merge(file_cfg, cli_overrides or {})`  (CLI wins over file)
3. `model = NagareClipConfig.model_validate(merged)`  ← **validation happens here**
4. `return model.model_dump()`  ← dict boundary; consumers unchanged

`DEFAULTS` stays exported (tests and readers reference it) but is now *derived*
from the model, so it can never disagree with the schema.

Validation errors are surfaced with a clear, user-actionable message (wrap
`ValidationError` into a concise "invalid config: <field>: <reason>" log/raise so
a typo names the offending key).

### Example-file generation

- **`generate_example_yaml() -> str`** walks `NagareClipConfig`'s fields,
  emitting YAML with each field's `description` as an inline `# comment`, section
  by section.
- **Fields emitted commented-out** (prompts, `transcription.align_model`,
  `summary.overall_prompt`, the advanced `intervals.bunsetu` block — matching
  what is commented in today's example) are marked with
  `json_schema_extra={"commented": True}` and rendered as `# key: value`.
- **Curated prose** that isn't field-specific (the top-of-file provider block and
  Langfuse block, per-section header paragraphs) is preserved as: a module-level
  `PREAMBLE` constant + a per-section-model `section_comment` class attribute.
- **CLI / make target:** `python -m nagare_clip.config --write-example` writes
  `config.example.yml`; a `make config-example` target wraps it.

### Test changes (`tests/test_config.py`)

Preserved (behavior unchanged for known keys): `TestLoadConfig`, `TestDeepMerge`,
`TestGetEffectiveConfig` (defaults, precedence, nested overrides).

Changed / added:

- **`test_unknown_keys_preserved` → `test_unknown_keys_rejected`.** Now asserts a
  custom/unknown top-level section raises `ValidationError` (intended behavior
  flip — this is the whole point of validation).
- **New `test_typo_leaf_key_rejected`.** `intervals: {silence_threshld: 2.0}`
  raises, naming the bad key.
- **New `test_wrong_type_rejected`** (or coercion, per pydantic default): a
  non-numeric `intervals.silence_threshold` raises.
- **`TestExampleConfigInSync` → exact check.** Replaces the `_missing_example_paths`
  heuristic (and its helper unit tests) with
  `assert generate_example_yaml() == Path("config.example.yml").read_text()`.
  Drift becomes impossible; regenerating is the fix.
- **New `test_pipeline_to_stage_valid`.** `pipeline.to_stage` set in YAML is
  accepted (guards the migration hazard above).
- **New `test_blender_style_allows_extra_keys`.** `blender.caption_style.font`
  (and an arbitrary key like `shadow_blur`) survive `get_effective_config` and
  appear in the returned dict — guards the open-ended-style-block hazard so a
  future "tighten everything to forbid" change can't silently break custom fonts.
- **New default-snapshot guard** (optional): assert a handful of representative
  `DEFAULTS` leaves keep their current values, so refactoring the models can't
  silently change a default.

### Docs

- **`AGENTS.md`** "Configuration System" section: `DEFAULTS` is now derived from
  pydantic models; unknown/typo'd keys are rejected (not silently dropped);
  `config.example.yml` is generated (`make config-example`) and the sync test is
  an exact regenerate-and-compare.
- **`config.example.yml`** regenerated from the models (content equivalent; may be
  reformatted). Its curated prose is preserved via `PREAMBLE` + `section_comment`.
- **`README.md`** if it documents config editing/validation.

## Known residual (follow-up, not this change)

`run_pipeline.sh` still hardcodes a few defaults inline (`# Precedence: CLI >
config > defaults`) and reads raw YAML, so a small second source of truth remains
for `transcription.*`, `audio_silence.*`, `intervals.*` margins, and
`pipeline.*`. Suggested follow-up: add `python -m nagare_clip.config
--shell-export` that prints validated `CFG_*` values for bash to `eval`,
eliminating the duplication. Deferred here to keep shell risk out of this
migration.

## Risks & mitigations

- **Generated example diverges from the curated file.** Mitigation: the generator
  is the new source of truth; we regenerate and commit once, porting curated
  comments into field `description`s + `section_comment`s so the output stays
  human-friendly. The sync test then guarantees the committed file *is* the
  generated output.
- **`extra="forbid"` breaks a real, currently-used key.** Two instances found:
  (1) `pipeline.to_stage`, explicitly added to the model; (2) the Blender style
  blocks' open-ended `font`/RNA pass-through keys, handled by scoping those three
  models to `extra="allow"` (see Model structure). Inventory confirmed all other
  keys read by `run_pipeline.sh` and by `blender/timeline.py` exist in / are
  permitted by the models.
- **Behavior flip is intended but visible.** `test_unknown_keys_preserved`
  flipping is a deliberate, documented change, not a regression.

## TDD sequencing (for the plan)

Per the repo's TDD guideline: write the validation/rejection tests first (red),
implement the models, confirm green; then write the generator equality test (red),
implement `generate_example_yaml`, regenerate the committed example, confirm
green; then update docs. Each new "rejects X" test is verified by confirming it
fails against a permissive (`extra="ignore"`) model before locking in
`extra="forbid"`.
