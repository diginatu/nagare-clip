# Fix Pyright noise in `src/nagare_clip/config.py` (follow-up)

> Deferred follow-up, not part of the pydantic-settings migration branch. Pyright
> is **not** in this repo's CI (`make check` = ruff + pytest, both green); this is
> editor-LSP noise only. Written 2026-07-06 while migrating config to
> pydantic-settings, so the investigation below is already done.

## Context / findings (already investigated — don't re-derive)

Environment at time of writing:
- `pyright 1.1.391` (system CLI, `/usr/bin/pyright`)
- `pydantic 2.12.5`, `pydantic-settings` installed in `.venv` (`.venv/lib/python3.12/...`)
- No `pyrightconfig.json` and no `[tool.pyright]` in `pyproject.toml`.
- Pyright is not referenced by `pyproject.toml`, `Makefile`, or `.github/` — it is
  not a gate.

`config.py` (after the pydantic-settings migration) produces **three distinct
clusters** of Pyright diagnostics — verified by running `pyright src/nagare_clip/config.py`:

1. **`reportMissingImports` for `pydantic_settings`** (1 error).
   Cause: Pyright is not pointed at `.venv`.
   Fix: a `pyrightconfig.json` at repo root:
   ```json
   { "venvPath": ".", "venv": ".venv" }
   ```
   **Confirmed:** this clears the import error (and lets genuine type errors surface;
   e.g. it un-masked cluster 3 below, which was previously hidden behind the
   unresolved import).

2. **`reportArgumentType` on `Field(default_factory=<ModelClass>)`** (18 errors,
   one per nested-model field: `summary_llm`, `caption`, `bunsetu`, the three
   blender style blocks, and the 12 top-level sections in `NagareClipConfig`).
   Cause: this Pyright build does **not** apply pydantic's `dataclass_transform`,
   so it treats every model field as a *required* constructor parameter and
   therefore rejects `type[X]` as a `() -> X` factory.
   **Do NOT "fix" this in code.** Tested empirically: rewriting
   `Field(default_factory=Class)` → `Field(default_factory=lambda: Class())` (or a
   plain instance default `= Class()`) makes Pyright **worse** — it then emits
   `reportCallIssue: Arguments missing for parameters "enabled", "provider", …` on
   every model construction, because it still doesn't understand the synthesized
   `__init__`. `default_factory=Class` is the idiomatic pydantic form; keep it.
   Options to actually silence, in order of preference:
   - **Preferred:** verify whether the user's editor uses Pylance / a
     pydantic-aware Pyright. With the venv resolved (cluster 1 fixed), a
     pydantic-aware checker models the synthesized `__init__` and these 18
     vanish with no code change. If so, cluster 2 needs nothing.
   - If a pydantic-unaware Pyright must stay: add a per-line
     `# pyright: ignore[reportArgumentType]` on each of the 18 `Field(default_factory=…)`
     lines. Explicit and local. (Do **not** globally disable `reportArgumentType`
     — it hides real bugs elsewhere.)

3. **Generator typing imprecision** (3 errors, real and worth fixing):
   - `field.json_schema_extra or {}` then `.get("emit")` — `json_schema_extra` is
     typed `JsonDict | Callable | None`, so `.get`/subscript fail
     (`config.py` `_render_model`, the `extra = field.json_schema_extra or {}`
     line and `extra["sample"]`). Fix: coerce/guard, e.g.
     `extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}`.
   - `_render_model(model_cls, indent)` is called with `field.annotation`, typed
     `type[Any] | None`, against a `model_cls: type[BaseModel]` param. Fix: narrow
     before the call (`ann = field.annotation; if isinstance(ann, type) and issubclass(ann, BaseModel): _render_model(ann, …)` already guards at runtime — add a
     `cast`/local-typed variable so Pyright sees `type[BaseModel]`), or annotate the
     helper param as `type[BaseModel]` and pass the already-narrowed `ann`.

## Recommended scope for the fix task

1. Add `pyrightconfig.json` (`{ "venvPath": ".", "venv": ".venv" }`). One commit.
2. Fix the 3 generator-typing issues in `_render_model` with `isinstance`
   narrowing / `cast` (no behavior change; `make check` stays green).
3. Cluster 2 (the 18 `default_factory`): first check whether the venv fix alone
   clears them under the user's editor checker. Only if not, add per-line
   `# pyright: ignore[reportArgumentType]`. Decide with the user — 18 inline
   ignores is a judgment call.

Do NOT change `Field(default_factory=Class)` to instance/lambda defaults — tested,
it regresses Pyright and de-idiomatizes the code.

## Verification for the fix task

- `pyright src/nagare_clip/config.py` → target: 0 errors (cluster 1 + 3 fixed;
  cluster 2 either gone via editor checker or ignored).
- `make check` must stay green (ruff + pytest) — these changes are type-annotation
  / config only, no runtime behavior change.
- Add `pyrightconfig.json` to `.gitignore`? No — commit it, so the setting is
  shared. (Confirm it doesn't conflict with any per-user editor settings.)
