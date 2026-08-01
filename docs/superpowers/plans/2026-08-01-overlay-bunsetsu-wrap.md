# Overlay Text Bunsetsu-Spacing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert bunsetsu-separator spaces into `<overlay/>` caption text (currently written verbatim, with no spaces) so Blender's space-only TEXT-strip wrapper has break points, matching how the caption stage already spaces its text.

**Architecture:** Add a timing-free bunsetsu-join function to `intervals/bunsetu.py` (the existing `build_bunsetu_times` is timing-driven and doesn't fit free-form overlay text). Call it once in `intervals/run.py::run_intervals`, mapping over `overlay_marks` right before they're written to `output_data["overlays"]`, reusing the `nlp` object and `caption.bunsetu_separator` config value the stage already has in scope. No Blender-side change: Blender's bundled Python has no GiNZA/spacy on its path, so it keeps rendering whatever `text` string it receives.

**Tech Stack:** Python, GiNZA (`ginza.bunsetu_spans`) via spaCy (`ja_ginza`), pytest, existing `tests/intervals/conftest.py::make_nlp` mock helper.

## Global Constraints

- Reuse `intervals.caption.bunsetu_separator` config key — no new config key (decided during design; see `docs/superpowers/specs/2026-08-01-overlay-bunsetsu-wrap-design.md`).
- No changes to `blender/timeline.py` or any Blender-side code.
- No changes to `<overlay .../>` tag syntax, `escape_overlay_text`/`unescape_overlay_text`, or `duration` semantics.
- Follow TDD: write the failing test before the implementation for every new behavior.

---

## Task 1: `bunsetu_join_text` pure function

**Files:**
- Modify: `src/nagare_clip/intervals/bunsetu.py` (add function; existing `build_bunsetu_times`/`flatten_bunsetu` are unchanged)
- Test: `tests/intervals/test_bunsetu.py` (add tests; uses existing `tests/intervals/conftest.py::make_nlp` helper)

**Interfaces:**
- Produces: `bunsetu_join_text(text: str, nlp: spacy.language.Language, separator: str = " ") -> str` in `nagare_clip.intervals.bunsetu`. Splits `text` on `"\n"`, runs `ginza.bunsetu_spans(nlp(line))` per non-empty line, joins each line's span surfaces with `separator`, rejoins lines with `"\n"`. An empty line (or empty `text`) passes through unchanged (no `nlp`/`ginza` call for that line).

Read `tests/intervals/test_bunsetu.py` and `tests/intervals/conftest.py` first — `make_nlp(bunsetu_lists)` returns a mock `nlp` whose `__call__` produces mock `Doc`s carrying `_bunsetu_spans`; you must also `patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc)` (already defined at the top of `test_bunsetu.py`) inside each test, exactly as the existing `build_bunsetu_times` tests do.

- [ ] **Step 1: Write the failing tests**

Append to `tests/intervals/test_bunsetu.py` (after the existing tests, same file — add the import at the top alongside the existing one):

```python
from nagare_clip.intervals.bunsetu import bunsetu_join_text  # add to existing import line
```

```python
# ---------------------------------------------------------------------------
# bunsetu_join_text (timing-free: overlay text)
# ---------------------------------------------------------------------------


def test_bunsetu_join_text_inserts_separator_between_spans():
    nlp = make_nlp([["前回自作した", "サイフォン式", "排水装置を", "水槽に", "取り付けてテスト"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        result = bunsetu_join_text(
            "前回自作したサイフォン式排水装置を水槽に取り付けてテスト", nlp
        )

    assert result == "前回自作した サイフォン式 排水装置を 水槽に 取り付けてテスト"


def test_bunsetu_join_text_uses_custom_separator():
    nlp = make_nlp([["水浸し", "！"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        result = bunsetu_join_text("水浸し！", nlp, separator="　")

    assert result == "水浸し　！"


def test_bunsetu_join_text_preserves_explicit_line_breaks():
    """Each \\n-separated line is parsed independently (one nlp() call per
    line), so an author's explicit line break is never merged into a single
    GiNZA parse or lost."""
    nlp = make_nlp([["1行目"], ["2行目"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        result = bunsetu_join_text("1行目\n2行目", nlp)

    assert result == "1行目\n2行目"
    assert nlp.call_count == 2


def test_bunsetu_join_text_empty_string_passthrough():
    nlp = make_nlp([])
    result = bunsetu_join_text("", nlp)

    assert result == ""
    assert nlp.call_count == 0


def test_bunsetu_join_text_single_bunsetsu_no_separator_inserted():
    nlp = make_nlp([["水浸し"]])
    with patch("ginza.bunsetu_spans", side_effect=bunsetu_spans_from_doc):
        result = bunsetu_join_text("水浸し", nlp)

    assert result == "水浸し"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/intervals/test_bunsetu.py -k bunsetu_join_text -v`
Expected: FAIL with `ImportError: cannot import name 'bunsetu_join_text'`

- [ ] **Step 3: Implement `bunsetu_join_text`**

Add to `src/nagare_clip/intervals/bunsetu.py`, after `build_bunsetu_times` and before `flatten_bunsetu`:

```python
def bunsetu_join_text(
    text: str,
    nlp: spacy.language.Language,
    separator: str = " ",
) -> str:
    """Insert *separator* between bunsetsu units of *text*.

    Timing-free counterpart to :func:`build_bunsetu_times`: overlay text is
    free-form (director-written, not necessarily verbatim transcript text),
    so there is no per-character timing to map spans back onto -- only the
    surface text matters here. Blender's TEXT strip wraps only at
    whitespace, and overlay text otherwise has none.

    Segmentation runs independently per ``\\n``-separated line so an
    author's explicit line break is preserved rather than merged into one
    GiNZA parse. An empty line (or empty *text*) passes through unchanged.
    """
    import ginza

    lines = text.split("\n")
    joined_lines: list[str] = []
    for line in lines:
        if not line:
            joined_lines.append(line)
            continue
        doc = nlp(line)
        spans = list(ginza.bunsetu_spans(doc))
        joined_lines.append(separator.join(span.text for span in spans) if spans else line)
    return "\n".join(joined_lines)
```

Note: `bunsetu.py` currently only imports `spacy` under `TYPE_CHECKING` (see the top of the file) — that's fine, the type annotation `spacy.language.Language` in this new function's signature resolves the same way `build_bunsetu_times`'s does.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/intervals/test_bunsetu.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones)

- [ ] **Step 5: Commit**

```bash
git add src/nagare_clip/intervals/bunsetu.py tests/intervals/test_bunsetu.py
git commit -m "feat(intervals): add bunsetu_join_text for spacing free-form text"
```

---

## Task 2: Wire into `run_intervals`, fix the affected test, update docs

**Files:**
- Modify: `src/nagare_clip/intervals/run.py` (import + one mapping step over `overlay_marks`)
- Modify: `tests/intervals/test_run_overlay_markers.py` (stub the new function, same pattern as the existing `build_bunsetu_times` stub)
- Modify: `docs/stages/intervals.md` (overlay paragraph)
- Modify: `AGENTS.md` (intervals stage `<overlay>` paragraph)
- Modify: `README.md` (overlay usage paragraph)

**Interfaces:**
- Consumes: `bunsetu_join_text(text: str, nlp, separator: str = " ") -> str` from Task 1.
- Produces: no new public interface — `run_intervals`'s output contract (`output_data["overlays"]`) is unchanged in shape, only the `text` values change (spaces inserted).

### Wiring

Read `src/nagare_clip/intervals/run.py` in full before editing — you need the exact surrounding code to place the edit correctly. Two spots:

1. The import line near the top:
   ```python
   from nagare_clip.intervals.bunsetu import build_bunsetu_times
   ```
   becomes:
   ```python
   from nagare_clip.intervals.bunsetu import build_bunsetu_times, bunsetu_join_text
   ```

2. Immediately after the existing `if overlay_marks:` block that calls `snap_overlay_starts` and reassigns `overlay_marks = moved` (the block ending in `overlay_marks = moved`), and before the `output_data = {...}` construction, insert a second `if overlay_marks:` block:
   ```python
   if overlay_marks:
       # Blender's TEXT strip wraps only at whitespace; free-form overlay
       # text otherwise has none. Reuses the caption separator/nlp already
       # loaded above rather than adding a second config knob.
       overlay_marks = [
           (start, duration, bunsetu_join_text(text, nlp, cap["bunsetu_separator"]))
           for start, duration, text in overlay_marks
       ]
   ```

- [ ] **Step 1: Write the failing test**

`tests/intervals/test_run_overlay_markers.py` currently stubs `stage_run.spacy.load` to return a bare `object()` (see its `_run` and `_run_cut_opening` helpers) — once `run_intervals` calls `bunsetu_join_text(text, nlp, ...)` with that dummy `nlp`, calling `nlp(line)` raises `TypeError: 'object' object is not callable`. First, confirm this without changing the test file — this step just demonstrates the break the wiring step will cause, so run it *after* Step 2 below and *before* Step 3 (Step 2's implementation, without the test fix, is expected to break these tests; that's the signal you're wiring into the right place).

Skip writing a *new* test here — Task 2 wires existing, already-covered behavior (`output["overlays"][i]["text"]`) through a new code path; the coverage is the existing assertions in `test_run_overlay_markers.py` (e.g. `test_overlays_key_present_when_marker_used` asserts `out["overlays"] == [{"start": 0.8, "duration": 3.0, "text": "Chapter 1"}]`). Proceed to Step 2.

- [ ] **Step 2: Implement the wiring**

Make the two edits described above in `src/nagare_clip/intervals/run.py`.

- [ ] **Step 3: Run the overlay tests and confirm they now fail (proves the wiring is live)**

Run: `uv run pytest tests/intervals/test_run_overlay_markers.py -v`
Expected: FAIL — `TypeError: 'object' object is not callable` (raised from inside `bunsetu_join_text`'s `nlp(line)` call), for every test that produces a non-empty `overlays` list (`test_overlays_key_present_when_marker_used`, `test_overlay_does_not_affect_keep_intervals`, `test_overlay_on_a_cut_line_opening_snaps_to_the_first_surviving_moment`, `test_snapped_anchor_lands_inside_a_written_keep_interval`, `test_director_duration_survives_guided_edit_into_intervals`). This is the mutation-catch evidence that the wiring is actually exercised, not a silent no-op.

- [ ] **Step 4: Fix the test file — stub `bunsetu_join_text` the same way `build_bunsetu_times` is already stubbed**

In `tests/intervals/test_run_overlay_markers.py`, both `_run` and `_run_cut_opening` currently have:
```python
    monkeypatch.setattr(stage_run.spacy, "load", lambda *a, **k: object())
    monkeypatch.setattr(stage_run, "build_bunsetu_times", lambda *a, **k: [])
```
Add one line to each, immediately after:
```python
    monkeypatch.setattr(stage_run, "bunsetu_join_text", lambda text, nlp, sep: text)
```
(Two call sites: the `_run` helper and the `_run_cut_opening` helper, both in this file.)

- [ ] **Step 5: Run the overlay tests again to verify they pass**

Run: `uv run pytest tests/intervals/test_run_overlay_markers.py -v`
Expected: PASS (all tests, unchanged assertions — the stub is an identity function, so `text` values are untouched, matching every existing `out["overlays"] == [...]` assertion)

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest`
Expected: PASS. (This confirms no other test file drives overlay text through `run_intervals` with a stubbed `nlp` — per the design doc, `test_overlay_snap.py` and `test_overlay_markers.py` test `snap_overlay_starts`/`extract_overlay_marks` directly, not through `run_intervals`, so they're unaffected. If anything else fails here, find every other call site and stub it the same way before proceeding — do not weaken `bunsetu_join_text` itself to accommodate a test.)

- [ ] **Step 7: Update `docs/stages/intervals.md`**

Read the file's overlay paragraph (the bullet starting `` `extract_overlay_marks()` returns... ``) in full first. Append one sentence to the end of that paragraph (after "...so guided_edit's pre-write check catches it instead of the intervals stage crashing a stage later."):

```
Overlay text is bunsetsu-spaced before it reaches the JSON (`intervals.bunsetu.bunsetu_join_text`, called from `run_intervals` right after the snap pass, reusing the same `nlp` and `caption.bunsetu_separator` the caption chunker uses — no separate config key) — Blender's TEXT strip wraps only at spaces and free-form overlay text otherwise has none; segmentation runs per `\n`-separated line so an author's explicit line break is never merged into a single GiNZA parse.
```

- [ ] **Step 8: Update `AGENTS.md`**

Read `AGENTS.md` around its `<overlay text="..." duration="N.N"/>` paragraph (currently line 83) in full first. Append one sentence to the end of that paragraph (after "...so guided_edit's pre-write check catches it instead of the intervals stage crashing a stage later."):

```
The intervals stage also bunsetsu-spaces the overlay's text before writing it to the output JSON (reusing the caption stage's `nlp`/`caption.bunsetu_separator`, no separate config key), since Blender's TEXT strip only wraps at spaces and free-form overlay text otherwise has none.
```

- [ ] **Step 9: Update `README.md`**

Read `README.md` around its overlay-marker paragraph (currently line 54, starting `` The `<overlay text="..." duration="3.0"/>` marker places... ``) in full first. Append one sentence to the end of that paragraph (after "...Resume with `./scripts/run_pipeline.sh --from-stage intervals` to apply."):

```
The text you write is automatically spaced at natural phrase boundaries before it reaches Blender, so a long caption has somewhere to wrap.
```

- [ ] **Step 10: Run the full validation suite**

Run: `make check`
Expected: PASS (lint + format-check + validate + test)

- [ ] **Step 11: Commit**

```bash
git add src/nagare_clip/intervals/run.py tests/intervals/test_run_overlay_markers.py docs/stages/intervals.md AGENTS.md README.md
git commit -m "feat(intervals): bunsetsu-space overlay text so it can wrap in Blender"
```

---

## Self-Review Notes

- **Spec coverage:** `bunsetu_join_text` (Task 1) ✅, wiring into `run_intervals` reusing `nlp`/`caption.bunsetu_separator` (Task 2) ✅, no Blender-side change (verified — no task touches `blender/`) ✅, test updates for the stubbed-`nlp` breakage (Task 2 Steps 3-6) ✅, docs updates (Task 2 Steps 7-9) ✅.
- **Placeholder scan:** none found — every step has literal code/commands.
- **Type consistency:** `bunsetu_join_text(text: str, nlp, separator: str = " ") -> str` is identical across Task 1's definition, Task 2's call site, and Task 2's test stub signature (`lambda text, nlp, sep: text`).
