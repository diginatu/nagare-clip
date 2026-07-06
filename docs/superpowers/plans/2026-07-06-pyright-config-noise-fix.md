# Fix Pyright noise in `src/nagare_clip/config.py` (follow-up) — DONE

> Deferred follow-up, not part of the pydantic-settings migration branch. Pyright
> is **not** in this repo's CI (`make check` = ruff + pytest, both green); this is
> editor-LSP noise only. **Re-planned and fixed 2026-07-06** — the original
> investigation (written mid-migration) had gone stale; the corrected findings and
> the applied fix are below.

## What changed since the original plan (why it was re-planned)

The original plan described **three** diagnostic clusters. Re-verifying against the
current tree (after `e4ab07c` "make root a plain BaseModel …") showed one was
obsolete and the headline fix was pointless:

| Original cluster | Status now | Action taken |
|---|---|---|
| **1** — `reportMissingImports` for `pydantic_settings`; fix via `pyrightconfig.json` | **Obsolete.** The `pydantic_settings` import is gone — `config.py` imports only `from pydantic import …`. Pyright also **already auto-discovers `.venv`** from the workspace root (verified: adding `pyrightconfig.json` changes the count 21 → 21). | **Dropped.** No `pyrightconfig.json` committed. |
| **2** — 18 `Field(default_factory=<Class>)` false positives (`reportArgumentType`) | **Real but not code-fixable.** | Single file-level suppress (see below). |
| **3** — 3 generator-typing errors | **Real, worth fixing.** | Fixed with type-narrowing. |

## Verified root cause of cluster 2 (the 18 `default_factory` errors)

Not a venv/import problem and **not** pydantic's `dataclass_transform` being
unsupported. Minimal repro (plain pydantic, no repo code, no `from __future__ import
annotations` needed):

```python
from pydantic import BaseModel, ConfigDict, Field

class A(BaseModel):
    model_config = ConfigDict(extra="forbid")   # <-- the trigger
    x: str = Field("v")

class Root(BaseModel):
    a: A = Field(default_factory=A)              # reportArgumentType
```

When a model sets `model_config = ConfigDict(...)`, this pyright build wrongly
treats each `Field(<default>)` field as **required**, so it rejects `type[A]` as a
valid `() -> A` factory. Confirmed reproducing on **pyright 1.1.391** (system CLI)
**and 1.1.411** (`uvx pyright@latest`) — upgrading does not help. Every alternative
spelling is equal or worse (all verified):

- `Field(default_factory=lambda: A())` → `reportCallIssue: Argument missing for "x"`
- `a: A = A()` (instance default) → `reportCallIssue`
- `Field(default=A())` → `reportCallIssue`

So there is **no code spelling** that satisfies pyright while keeping `model_config`
(which every section model needs for `extra="forbid"`/`"allow"` validation). Do NOT
change `Field(default_factory=Class)` — it is the idiomatic form and every
alternative regresses.

## What was applied

1. **Cluster 3 — fixed in code** (`config.py`, pure type-narrowing, no behavior
   change; existing `tests/test_config.py` covers the runtime paths, all green):
   - `_render_model`: `extra = field.json_schema_extra or {}` →
     `extra = field.json_schema_extra if isinstance(field.json_schema_extra, dict) else {}`
     (clears the `reportFunctionMemberAccess` on `.get` and `reportIndexIssue` on
     `extra["sample"]`, since `json_schema_extra` is typed `JsonDict | Callable | None`).
   - `generate_example_yaml`: guard `model_cls = field.annotation` (typed
     `type[Any] | None`) with
     `if not (isinstance(model_cls, type) and issubclass(model_cls, BaseModel)): continue`
     before `getattr(...)`/`_render_model(model_cls, 2)` (clears the
     `reportArgumentType` on the `_render_model` call). All top-level fields are
     `BaseModel` subclasses, so `continue` is never hit — identical output.

2. **Cluster 2 — single file-level suppress** (chosen over 18 inline
   `# pyright: ignore` after weighing the trade-off with the user). A commented
   `# pyright: reportArgumentType=false` at the top of `config.py` (placed after the
   module docstring, before `from __future__ import annotations` — a comment is
   allowed there), with an explanation of the pyright bug and the accepted risk.
   Applied **after** cluster 3, so it masks only the 18 residual false positives, not
   any real `reportArgumentType`. Accepted risk: a future genuine
   `reportArgumentType` in this (declarative) module would also be silenced.

## Verification (all passing)

- `pyright src/nagare_clip/config.py` (system 1.1.391) → **0 errors**.
- `uvx pyright@latest src/nagare_clip/config.py` with `.venv` resolved → **0 errors**.
- `make check` (ruff lint + format + validate + pytest) → **green, 588 passed**.

## Not done (deliberately)

- **No `pyrightconfig.json` committed.** Pyright auto-finds `.venv` from the
  workspace root, so it changes nothing here, and pyright is not a CI gate. (If a
  future editor/CI setup runs pyright from a different cwd and can't find the venv,
  add `{ "venvPath": ".", "venv": ".venv" }` then — it's a one-liner.)
- **`Field(default_factory=Class)` left unchanged** — idiomatic; every alternative
  regresses pyright (see above).
