# Silence lines in `_edits.txt` — design

Status: spec (phase 1). Nothing here is implemented yet.
Branch: `feat/silence-lines-in-edits` (from `main` c0479c4).

## 1. Problem

Until 2026-09-20 every edit reached `intervals` as a text marker in
`guided_edit/{stem}_edits.txt`. That file was the one place to read and
hand-edit the whole edit, whether the edit came from a person or from the LLM.

Director ops with a `"n~"` edge (the silence AFTER source line `n`,
`DirectorOp.gap_start` / `gap_end`) broke that:

- `5fbc4a8` — `guided_edit.apply.is_time_resolved()` skips `keep`/`speed`/
  `timelapse`/`overlay` ops with a `~` edge. It writes no marker, and the op only
  "occupies" its lines so a cut clips around it.
- `0e8dafa` — `intervals/op_times.py::resolve_op_times()` reads those ops back out
  of `director/{stem}_director.json`. `pipeline/stages.py::_silence_op_times()`
  passes them to `run_intervals(..., extra=OpTimes)`.

On water_pump_4, all 12 timelapses of `PXL_20260328_082352713` have a `~` edge.
Its `_edits.txt` shows 3 cuts and 1 overlay. The 12 timelapses exist only in
`intervals/*_intervals.json`, so the edit can't be seen or hand-edited, and
`intervals` reaches past `guided_edit` to the director's output.

A side finding: a `cut` op with a `~` edge stays on the marker path, and
`apply_span_op` ignores the flag. `cut ["53~", 55]` wraps lines 53..55 and so
**deletes line 53's words**, when it was meant to start after them. This design
fixes that as a side effect (see §4.4).

**Root cause.** No written rule said every edit must live in `_edits.txt`. So the
bypass could land without breaking a stated constraint. §9 adds that rule to
`AGENTS.md` and a test that guards it.

## 2. The format

### 2.1 Example

`text_filter/{stem}_edits.txt` does not change: one line per WhisperX segment.

`guided_edit/{stem}_edits.txt` puts a **silence line** between two speech lines
wherever the director was shown a silence line (§3):

```
その状態で
[silent 29.9s: a hand enters from the right holding a clear tube / the tube is pushed onto the pump outlet]
この状態で今予備水持ってきたんで
```

With edits applied (a timelapse over `["53~", "53~"]`, then a cut over 55..56):

```
その状態で
<keep><speed factor="8.0"><overlay text="予備水を準備" duration="3.74"/>[silent 29.9s: a hand enters from the right holding a clear tube / the tube is pushed onto the pump outlet]</speed></keep>
この状態で今予備水持ってきたんで
<cut>えーと</cut>
```

A timelapse over `["53~", 56]` puts the opening tags on the silence line and the
closing tags on the last speech line, as usual:

```
<keep><speed factor="8.0"><overlay text="…" duration="…"/>[silent 29.9s: …]
この状態で今予備水持ってきたんで
…(line 55)
…(line 56)</speed></keep>
```

### 2.2 Line shape

A silence line, after its marker tags are removed, is exactly the text
`silence_body(seconds, descriptions)` renders: `[silent 29.9s]` or
`[silent 29.9s: d1 / d2]`. It has no indent, no line number and no `after line n`.
This is the same string the director's whole-video view prints after
`N: ` (`display.py`, `DisplayLine.text` for `is_silence=True`).

The parser only needs the shape: markers stripped, then
`^\[silent [0-9]+(?:\.[0-9]+)?s(?:: .*)?\]$`. **The parser ignores the seconds
and the description text.** Editing either is not an error (user decision).

## 3. One renderer, one parser, one module

The director's view and `_edits.txt` must not be able to drift. That requires
one renderer, with the parser next to it, in a module that `intervals` can
import without importing `director`. `intervals` importing `director` is the
dependency direction §9 forbids.

**New module `src/nagare_clip/edit_lines.py`** (top level, next to `timing.py`
and `order.py`; pure, no I/O):

| Name | What it does | Moved from |
|---|---|---|
| `silence_body(seconds, descriptions=(), after_line=None)` | The one formatter for a silence bracket | `director/silence_lines.py` (re-exported there, so existing imports keep working) |
| `gap_spans(whisperx_data)` | `{n: (start, end)}` for every between-line gap, from `line_speech_spans` | `director/silence_lines.py` (re-exported) |
| `silence_line_min(cfg_section)` | Defensive read of `director.silence_line_min` | `director/run.py` (re-exported) |
| `SILENCE_LINE_RE` | The shape in §2.2 | new |
| `EditFile`, `Slot` | Parse result (§4.1) | new |
| `parse_edit_lines(lines) -> EditFile` | **The** parser. Every reader of an `_edits.txt` goes through it (§6) | new |
| `insert_silence_lines(speech_lines, bodies: Mapping[int, str]) -> list[str]` | **The** writer. Puts `bodies[n]` after speech line `n` | new |
| `expected_silences(whisperx_data, min_seconds) -> set[int]` | The `n`s that must have a silence line (§3.1) | new |
| `silence_problems(file, expected) -> list[tuple[int, str]]` | Silence-line validation (§8), physical line numbers | new |

The director-side renderer is one method used by both views:

- `director/silence_lines.py::SilenceLine.body() -> str` returns
  `silence_body(self.duration, self.descriptions)`.
- `display.py::build_display_view` replaces its inline `silence_body(...)` call
  with `silence.body()`.
- `guided_edit` builds `bodies = {s.after_line: s.body() for s in silence_lines}`
  and hands them to `insert_silence_lines`.

A test asserts that, for the same inputs, every silence line `guided_edit`
writes equals the `DisplayLine.text` of the matching silence line in
`build_display_view`.

### 3.1 Which silences

The set is `build_silence_lines(data, anchored_gaps, min_seconds=silence_line_min(cfg["director"]))`
over the **whole source** (`lines=None`). Descriptions come from the gap_context
`{stem}_gaps.json`, anchored with `anchor_gaps` exactly as
`director/run.py::load_segment_transcript` does.

`expected_silences()` is the same set without descriptions: the `n` in
`gap_spans` with `end - start >= min_seconds`. It depends only on the WhisperX
JSON and one number, so `check_edits` and `intervals` can recompute it.

This is a superset of what the director saw when the plan reorders a source.
Per segment, `build_silence_lines(lines=(a, b))` leaves out the silence after
`b`. Every `n~` the director can write still maps to exactly one line; the
extra lines at segment ends carry no op. See open question Q1.

A `n~` from a hand-written `_director.json` that names a silence with **no**
silence line (shorter than `silence_line_min`, or no gap) is reported as
unapplied: `addresses the silence after line n, which has no silence line
(< silence_line_min …)`. It is not guessed at.

## 4. Parse contract and time semantics

### 4.1 `parse_edit_lines(lines) -> EditFile`

```python
@dataclass(frozen=True)
class Slot:
    file_line: int          # 1-based physical line in _edits.txt
    kind: Literal["speech", "silence"]
    speech_line: int        # speech: its own 1-based number (= WhisperX segment)
                            # silence: the n it follows (the n of "n~")
    text: str               # the raw physical line, markers included

@dataclass(frozen=True)
class EditFile:
    slots: list[Slot]
    def speech_lines(self) -> list[str]            # raw speech lines, in order
    def silences(self) -> list[Slot]
    def file_index(self, line: int, gap: bool) -> int | None   # (n, "n~"?) -> physical
    def speech_projection(self) -> list[str]       # §4.3
```

Rules:

- A physical line is a silence line iff it matches `SILENCE_LINE_RE` after
  `KEEP/SPEED/OVERLAY/CUT` tags are stripped. Otherwise it is a speech line.
- A silence line's `speech_line` is the number of speech lines seen before it.
- The parser never raises. Structural problems (a silence line before the first
  speech line, two in a row, `{{…}}` or other text on one) are reported by
  `silence_problems`, not by parse.
- A file with no silence lines parses to exactly today's structure. This is the
  legacy format (§7).

### 4.2 Time of a tag on a silence line

Let `(s, e) = gap_spans(original_whisperx)[n]` for the silence after line `n`.
`original_whisperx` means **before** `sync_text_to_json`, which is the data
`op_times` and `build_silence_lines` use today. A `<cut>` that deletes line `n`
must not move the silence.

| Tag on the silence line after `n` | Resolves to |
|---|---|
| any opener (`<keep>`, `<speed …>`, `<cut>`) | `s` (the silence start: the end of line `n`'s last speech span) |
| any closer (`</keep>`, `</speed>`, `</cut>`) | `e` (the silence end: the start of line `n+1`'s first speech span) |
| `<overlay …/>` | start `s` |

Where the tag sits inside the silence line does not matter. `<keep>[silent…]</keep>`
and `[silent…]<keep></keep>` both mean `(s, e)`. An open-then-close on one
silence line covers exactly that silence.

Tags on speech lines keep today's word-position semantics
(`_first_word_at_or_after` / `_last_word_before`). A tag at the end of a speech
line still falls forward to the next **speech** line's first word, past any
silence line in between, as it does today.

This reproduces `op_times._span_bounds` on every silence edge:
`gap_start → spans[n-1][-1][1]`, `gap_end → spans[n][0][0]`.
On a **speech** edge, `_span_bounds` uses `line_speech_spans` (end clamped to
`start + SILENCE_MAX_WORD_SPAN`), while the marker path uses the raw word
`end`. They differ only where a line's last word is stretched. See Q2.

### 4.3 Speech projection (for text sync and `<cut>` word deletion)

`sync_text_to_json`, the patch/decomposition checks and `clean_for_display`
work on one line per segment. `speech_projection()` produces that list:

- An opener on the silence after `n` moves to the **start** of speech line `n+1`.
- A closer moves to the **end** of speech line `n`.
- A pair that opens and closes on the same silence line is dropped. It covers no
  words.
- Silence lines disappear.

For `<cut>`, this gives exactly the word deletion: `<cut>` from `S53` to `S55`
deletes lines 54..55 and **not** line 53. The projection is used only for word
text and deletion. Keep/speed/overlay times come from §4.2, never from
projected positions.

### 4.4 What each marker does over a silence

- `<keep>` covering a silence → the silence is in `force_keep_ranges` and plays.
- `<speed>` inside `<keep>` over a silence → the timelapse: kept and sped up.
- `<speed>` alone over a silence → annotation only. The silence is still dropped,
  exactly as a bare `<speed>` over speech is today.
- `<cut>` covering a silence → dropped. Deleting words already makes the merged
  gap exceed `silence_threshold`. On top of that, every silence `(s, e)` inside a
  `<cut>` span is **added to the excludes** (with `cut_ranges`). The drop then
  doesn't depend on `silence_threshold` being below `silence_line_min`. A
  `<keep>` still wins over it, as `subtract_intervals` already makes a keep win
  over every exclude.
- `<overlay/>` on a silence line → start `s`. It then goes through
  `snap_overlay_starts` like any other overlay. `op_times` overlays went through
  the same pass today, so the result is the same.

## 5. guided_edit

`run_guided_edit(edits_txt, director_json, output, cfg, *, json_path, gaps_path, recorder)`:

1. Parse the input (`text_filter/{stem}_edits.txt`) with `parse_edit_lines`.
   If it already has silence lines, they are validated and kept.
2. Build the silence lines (§3.1) and call `insert_silence_lines`. This happens
   **whether or not** `guided_edit.enabled` is true (Q3). It needs `json_path`;
   without it no silence lines are inserted, and every `~` op is unapplied with
   a reason.
3. `ops_from_dict(num_lines=<speech count>)`, then
   `expand_timelapse_ops(ops, seg_times, silences=gap_spans(data))`.
   `caption_duration` takes a silence edge's time from `(s, e)` and a speech
   edge's from `seg_times`, as today. So a timelapse with no `~` edge is
   byte-identical to today (Q2).
4. Map every op to physical lines:
   `(file_index(a, gap_start), file_index(b, gap_end))`. Unmappable → unapplied
   with a reason. An `edit` op with a `~` edge → unapplied
   (`edit op cannot address a silence`).
5. `apply_ops` runs on physical lines. `blocked_lines`, `clip_range`,
   `apply_span_op` and `apply_point_op` don't change; they already count lines.
   Every span op becomes a marker, and nothing is time-resolved.
6. **Clip-edge rule.** A clip may not leave an op edge on a silence line the op
   did not address. After `clip_range`, if an edge lands on a silence line that
   was not the op's own `~` edge, trim that edge inward to the nearest speech
   line. An overlay without `gap_start` picks the first free **speech** line. So
   ops without `~` edges place exactly the words they place today (a test pins
   this on the existing `test_apply.py` table). Silence lines **inside** a span
   are simply part of it.
7. `reconcile.verify_op` also checks that the silence structure is unchanged:
   `[(s.file_line, s.speech_line) for s in parse(after).silences()]` equals the
   one from `parse(before)`. `clean_old` equality per line already rejects a
   rewritten silence body inside `guided_edit`.
8. The `edit` op LLM prompt (`build_user_prompt`) numbers lines by physical
   index. Returned lines are parsed against the same numbers.
9. The final `check_edits` pass runs as today, with the silence set.

## 6. Every consumer and how it changes

All readers go through `parse_edit_lines`. After this change,
`grep -rn 'splitlines()' src | grep -i edit` should show every hit wrapped in it.

| Module | Today | Change |
|---|---|---|
| `edit_lines.py` (new) | — | §3, §4 |
| `director/silence_lines.py` | owns `silence_body`, `gap_spans` | re-exports both; adds `SilenceLine.body()` |
| `director/display.py` | inline `silence_body(...)` | `silence.body()`; speech lines taken from `EditFile.speech_lines()` |
| `director/run.py` | `splitlines()` on `text_filter` edits; `silence_line_min` | `parse_edit_lines(...).speech_lines()` (still text_filter's file, so no silence lines expected); re-export `silence_line_min`; `source_drops` gets the raw lines, and `dropped_ranges` parses them |
| `director/director_llm.py` | `clean_for_display(edit_lines)` | unchanged signature; callers pass speech lines only |
| `director/preview.py` | `resolve_placements` → `resolve_span_ops`, with `is_time_resolved` holding lines | builds the same silence-lined file `guided_edit` would write, places ops through the same `resolve_span_ops`, and maps placements back to `(line, gap)` with `EditFile`. A parity test against `guided_edit` output covers a table of ops with and without `~` |
| `director/preview_cli.py` | reads `text_filter` edits | parse; needs `gaps` and `silence_line_min`, which it already loads through `load_segment_transcript` |
| `guided_edit/run.py` | copy / `apply_ops` | §5; new `gaps_path` argument |
| `guided_edit/apply.py` | `is_time_resolved`, `occupied`, `RESOLVED_TYPES` import | delete all three; physical-line mapping; clip-edge rule |
| `guided_edit/timelapse.py` | `caption_duration(op, seg_times)` | adds `silences`; silence edges use `(s, e)` |
| `guided_edit/reconcile.py` | `clean_old` per line | adds the silence-structure check |
| `intervals/sync_json.py` | `enumerate(edit_lines)` == segment index | `sync_text_to_json` parses and uses `speech_projection()`. `extract_keep_ranges` / `extract_speed_ranges` / `extract_overlay_marks` walk `EditFile.slots` with a new required-when-present `silences: Mapping[int, (s, e)]` argument, resolving §4.2 anchors. New `extract_cut_silences(file, silences)` for §4.4 |
| `intervals/run.py` | `extra: OpTimes` | delete `extra`; compute `gap_spans` from the pre-sync JSON; validate silences (fail fast with `ValueError`, as the sync does); union cut-covered silences into the excludes |
| `intervals/keep.py` | `dropped_ranges(edit_lines=…)` | parses; passes `silences`. #15 bracket accounting: the director calls it with `text_filter` lines (legacy shape), so the result is unchanged. A test pins it |
| `intervals/check_edits.py` | line count == segment count; tags per line | §8; `--silence-line-min` flag (default `DEFAULT_SILENCE_LINE_MIN`); every `Problem.line` is the physical line |
| `intervals/op_times.py` | resolver | **deleted** (§7) |
| `pipeline/stages.py` | `_silence_op_times`, `extra=`; `_line_counts` via `splitlines` | delete `_silence_op_times` and `extra=`; the guided_edit adapter passes `gaps_path` and the director section; `_line_counts` counts `speech_lines()` |
| `text_filter/run.py` | writes one line per segment | **unchanged** output; docstring notes that only guided_edit adds silence lines |
| `config.py` | `guided_edit` / `director.silence_line_min` descriptions | text only: `silence_line_min` also decides the silence lines in `guided_edit`'s `_edits.txt`; regenerate `config.example.yml` |

Docs to update in the implementing commits: `AGENTS.md` (the guided_edit,
intervals and director overviews, and §9's constraint), `README.md`,
`plan.md`, `docs/stages/intervals.md`, and `docs/stages/pipeline.md` if it
mentions `extra`.

## 7. Deletions and backward compatibility

### Deleted

- `guided_edit.apply.is_time_resolved`, the `occupied` parameter of
  `place_span_op`, the `is_time_resolved` branches in `resolve_span_ops` and
  `apply_ops`, and the `RESOLVED_TYPES` import.
- `intervals/op_times.py` as a whole. `_span_bounds`' silence-edge rule lives on
  as §4.2's resolver in `sync_json`. `OpTimes`, `resolve_op_times` and
  `RESOLVED_TYPES` have no remaining user.
- `run_intervals(extra=…)` and its merge branch; `stages._silence_op_times`.
- Tests: `tests/intervals/test_op_times.py` and
  `tests/intervals/test_run_extra_ranges.py` are replaced by §10's equivalence
  tests; `tests/guided_edit/test_gap_ops_skip_markers.py` is rewritten to assert
  the markers are **written**.

### Backward compatibility

- **Legacy files** (no silence lines) parse to today's structure. The
  extractors, sync and `check_edits` give byte-identical results. A test runs
  the existing marker-test corpus through both the old entry points (frozen
  outputs) and the new ones.
- **A silence-lined file with no markers on its silence lines** gives an
  intervals JSON byte-identical to the same file with the silence lines
  removed. This is a property test over fixtures, and it is what makes adding
  the lines safe.
- **Existing projects whose ops have `~` edges** (water_pump_4 included) lose
  those ops when `intervals` re-runs: nothing reads `_director.json` there any
  more. Re-running `--from-stage guided_edit` restores them. For span ops that
  costs no LLM call; only `edit` ops call the small model. But it overwrites
  hand edits in `guided_edit/_edits.txt`. See Q4.
- `sentence_split` re-run, or a changed `silence_line_min` after `guided_edit`:
  the silence set no longer matches and `intervals` fails with a message that
  names the line and says to re-run `guided_edit`. Today a changed segment
  count fails the same way.

## 8. Hand-edit rules and their errors

Option (a): silence lines are generated, never hand-added. Only markers may be
added or removed on or around them. Checks are in `silence_problems`, reported
by `check_edits` (all at once) and raised by `intervals` (first one, like the
sync). They apply only when the file has **at least one** silence line; a file
with none is legacy (Q5).

| Hand edit | Error (physical line named) |
|---|---|
| Delete a silence line | `line 57: missing silence line after speech line 53 (silence lines are generated by guided_edit; restore it — only markers may change around it)`. Named at the line where it belongs |
| Add / duplicate one | `line 61: unexpected silence line after speech line 57 (no silence ≥ 5.0s there)` / `line 61: duplicate silence line after speech line 57` |
| Move one | reported as missing at the old position plus unexpected at the new one; both lines named |
| Put text or a `{{old->new}}` on a silence line | `line 57: a silence line may carry only markers` |
| Edit the seconds or description | not an error; the parser ignores them |
| Delete or alter speech text | unchanged from today |
| Tag balance | counted across all physical lines, silence lines included, as today |

`check_edits`' parity guard (`sync_text_to_json` plus the extractors) also
receives `silences`, so it cannot pass a file `intervals` would reject.

## 9. The written rule and its guard

Add to `AGENTS.md` → Hard Constraints, right after "Preserve the interval JSON
(`intervals/` package) as the human-editable contract for the Blender stage.":

> - `_edits.txt` is the single human-editable record of every edit. `intervals`
>   applies only what is in it (plus the audio_silence `_cuts.txt`, see below).
>   No stage may feed `intervals` an edit from another source; an edit the file
>   cannot express is a format change to `_edits.txt`, not a side channel.

The parenthetical depends on Q6.

Guard tests (`tests/intervals/test_single_edit_record.py`):

1. **Static:** no module under `src/nagare_clip/intervals/` imports
   `nagare_clip.director` or `nagare_clip.guided_edit` (AST walk).
   `edit_lines.py` must not import them either.
2. **Signature:** `inspect.signature(run_intervals).parameters` is exactly
   `edits_txt, json_path, output, cfg, cuts_txt`. A new input has to edit this
   test, and the test's docstring quotes the AGENTS.md rule.
3. **Behavioural:** `stages._intervals_run` on a fixture project gives
   byte-identical `_intervals.json` with and without a
   `director/{stem}_director.json` full of ops (`~` ones included).

## 10. Test plan

Standing rules:

- **TDD, red first.** Every behaviour below gets a failing test before its code.
  Where a test comes after the code (the deletion commits, and the equivalence
  tests written against code that already exists), mutate the implementation,
  watch the test fail, and restore it by copying the file aside and back
  (`cp f /tmp/..; …; cp /tmp/.. f`). **Never** `git checkout`, `reset` or
  `stash`. Report the mutation evidence next to the green result.
- Run `uv run pytest -q`, `uv run ruff check` and
  `uv run ruff format --check src tests` before each commit.
- No LLM calls and no pipeline runs. The user runs the pipeline.
- Commit to the branch; do not merge or push.

Order (each step is red → green → refactor, with its own commit):

1. **`edit_lines.py`.** Parse/insert round trip. Legacy file → zero silences.
   `file_index` for `n` and `n~`. Silence line with markers recognised. A speech
   line that merely contains `[silent` is not a silence line. `speech_projection`
   cases (open → next line, close → previous line, same-line pair dropped).
   Each `silence_problems` message and its line number.
   *Mutations:* off-by-one in `speech_line`; treat a marker-wrapped silence line
   as speech; drop the same-line-pair rule.
2. **Renderer parity.** `SilenceLine.body()` == `DisplayLine.text`; a
   guided_edit-written line == the display line.
   *Mutation:* change the format in one place → red.
3. **Equivalence oracle.** Before anything is deleted, add
   `tests/intervals/test_silence_equivalence.py`. It has a **test-local copy**
   of `op_times._span_bounds` and `resolve_op_times` (frozen, documented as the
   pre-change reference) and a table of ops on a fixture JSON: keep / speed /
   timelapse / overlay × {`n~` start, `n~` end, both, single silence
   `["n~","n~"]`}, across 1-line and multi-line ranges, next to non-`~` ops.
   For each case it asserts that new `guided_edit` (markers) → new
   `run_intervals` produces the same `force_keep_ranges`, `speed_ranges` and
   `overlays` as the oracle. It also asserts the **whole intervals JSON** is
   equal to what the old path wrote (old guided_edit + `extra=` path, captured
   as frozen expected JSON in the same commit that still has the old code).
   Fixtures have no stretched final words, so speech edges agree (Q2). One
   separate test pins the stretched-word divergence explicitly.
   *Mutations:* swap `s`/`e`; resolve a closer on a silence to `s`; take silence
   bounds from the synced JSON instead of the original.
4. **intervals.** Extractors per §4.2; cut semantics per §4.3/§4.4, including
   the regression that `cut ["53~", 55]` no longer deletes line 53; the
   no-marker property test (§7); `dropped_ranges` unchanged for text_filter
   input (#15).
   *Mutations:* drop the cut-silence excludes; keep the projection's
   same-line pair.
5. **check_edits.** Every row of §8, with all problems reported at once; the
   parity guard with silences.
   *Mutation:* disable the silence check → the deletion test goes red.
6. **guided_edit.** Insertion (enabled and disabled per Q3); op mapping;
   unmappable `n~` unapplied; `edit` op with `~` unapplied; timelapse caption
   with silence edges; clip-edge rule, with the existing `test_apply.py` cases
   still green and one new case where a clip would otherwise stop on a silence
   line; reconcile silence-structure check.
   *Mutations:* remove the clip-edge trim; remove the reconcile check.
7. **preview.** `resolve_placements` parity with guided_edit output for the
   table in step 3.
8. **Delete** the time-resolved machinery (§7). Re-run the full suite. The
   equivalence tests must still pass against the frozen oracle.
9. **Guard tests** (§9). *Mutations:* add a dummy parameter to `run_intervals`;
   add `import nagare_clip.director` to an intervals module; make
   `_intervals_run` pass anything read from `director/`. Each must go red.
10. **Docs**, plus the `AGENTS.md` constraint and `config.example.yml`
    regeneration (`make config-example`).

Optional, only if the user approves reading project data: a throwaway script
(not committed, no pipeline run, no LLM) that, for water_pump_4's
`PXL_20260328_082352713`, compares `op_times` on the real `_director.json` with
the new path on the same inputs, and lists any range that differs.

## 11. Risks

- **A speech line that looks like a silence line.** WhisperX text that is exactly
  `[silent 3.0s]` would parse as silence. It is very unlikely, and
  `silence_problems` then reports it as unexpected, naming the line.
- **Marker syntax inside a description.** A vision description containing
  `<keep>`, `</speed>` or `{{` would be read as a marker (Q7).
- **Clip behaviour drift** for ops without `~` edges. Physical ranges now
  contain silence lines, so run lengths in `clip_range` change and ties may break
  differently. The clip-edge rule restores the placed words. The existing
  `test_apply` table plus one tie case guard this.
- **Stale guided_edit output** after `sentence_split` re-runs or
  `silence_line_min` changes. It fails loudly (by design), with a clear message.
- **Existing projects** lose `~` ops until `guided_edit` re-runs (Q4).
- **Speech-edge seconds** of a `~` op move by up to the stretched-word excess on
  the (measured) 5 of 49 gaps with a stretched final word (Q2).
- **Parser in every reader.** Missing one reader would shift numbering silently.
  The grep in §6 plus a test that feeds a silence-lined file to every reader
  covered by an existing test mitigate this.

## 12. Open questions

- **Q1 — which silence set.** The spec uses the whole-source set
  (`build_silence_lines(lines=None)`). It covers every `n~` the director can
  write and needs no plan order to recompute, so `check_edits` stays a
  JSON+number check. It adds a silence line after a segment's last line under a
  reorder, which the director never saw. The alternative is the exact per-segment
  union, which needs the order in guided_edit, check_edits and intervals.
  Recommendation: whole-source.
- **Q2 — speech edge of a `~` op.** Marker semantics (raw word start/end, and
  `seg_times` for the caption) keep ops without `~` byte-identical, but differ
  from `_span_bounds` where the last word is stretched. The alternative is to
  resolve every edge of every tag through `line_speech_spans`, which changes
  today's output for every op. Recommendation: marker semantics; equivalence is
  asserted on the silence edge, and the divergence is pinned by a test.
- **Q3 — `guided_edit.enabled: false`.** Recommendation: still insert silence
  lines, so a human can add `<keep>` around a silence without the director. The
  cost is that the disabled path is no longer a byte-identical copy.
- **Q4 — existing projects with `~` ops.** (a) Accept: re-run `guided_edit`
  (overwrites hand edits in `guided_edit/_edits.txt`). (b) Add a deterministic,
  no-LLM migration command that inserts silence lines into an existing
  `guided_edit/_edits.txt` and applies only its `~` span ops, keeping hand edits.
  Recommendation: (b), if hand-edited guided_edit files exist in live projects;
  otherwise (a).
- **Q5 — telling legacy from all-deleted.** A file where every silence line was
  hand-deleted looks legacy and passes. Alternatives: a header line (it would
  break "line N is a segment" for plain readers), or a sidecar. Recommendation:
  accept the gap.
- **Q6 — `_cuts.txt` and the new rule.** `intervals` also applies audio_silence's
  human-editable `_cuts.txt`, which is not in `_edits.txt`. As worded, the rule
  would forbid that. Recommendation: name `_cuts.txt` as the one other input (a
  detection result the human prunes, not an edit decision), as in the draft
  bullet in §9. Or fold the cut list into `_edits.txt` in a later change.
- **Q7 — sanitising descriptions.** Recommendation: `silence_body` replaces
  `< > { }` in descriptions with their full-width forms. Because it is the one
  renderer, the director's view changes the same way; that affects only
  descriptions containing those characters.
