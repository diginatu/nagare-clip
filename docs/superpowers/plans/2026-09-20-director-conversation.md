# Increment 3: the director as one conversation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the nine independent per-segment director calls with one conversation over the whole video, where every turn's ops come back with a computed playback preview and any earlier range may be rewritten.

**Architecture:** One global display numbering over the finished video (speech lines and silence lines, in playback order) is rendered once into the cached system prefix. The loop asks for an approximate range per turn, parses a JSON reply, converts its ops from display numbers to per-source coordinates, previews them, and replies with the preview plus the next range. The model ends with `done`. Reaching the turn cap raises, after writing what was accepted.

**Tech Stack:** Python 3.12, pytest, uv, ruff. No new dependencies, no tool-calling: plain multi-turn chat, so nothing in `llm_client` changes.

**Spec:** `docs/superpowers/specs/2026-09-19-director-agent-loop-design.md`

## Global Constraints

- TDD; mutation-verify every behaviour a test guards, and confirm the *intended* test fails (four tests on this branch passed for the wrong reason).
- Never `git checkout -- <file>`, `git reset`, `git stash`; undo a mutation by copying the file aside to `/tmp/claude-1000/`.
- `rm` is aliased off — `\rm`. Check exit codes directly; a `| tail` hides them.
- `_director.json` stays per source, in source coordinates, hand-editable. `guided_edit` onward is untouched.
- Commit messages end with `Claude-Session: https://claude.ai/code/session_01KuPe2gfZ9ns9Mm5FRvhpPA`.

## File Structure

| File | Responsibility |
|---|---|
| `src/nagare_clip/director/display.py` (create) | The display view: build the numbered lines for the whole video in playback order, and convert a display range to per-source ops and back. No LLM, no I/O. |
| `src/nagare_clip/director/loop.py` (create) | The turn protocol: which range to ask for next, parse a reply, apply it to the accumulated state, decide when to stop. No I/O, no LLM — the caller injects `call_llm`. |
| `src/nagare_clip/director/run.py` (modify) | `run_director_conversation(...)`: load every segment once, render, run the loop, return per-source ops. |
| `src/nagare_clip/pipeline/stages.py` (modify) | `_director_run` becomes one call; write every source's file; raise on the cap after writing. |
| `src/nagare_clip/config.py` (modify) | `director.chunk_lines` (default 40). Remove `whole_project_context` (now unconditional) and `max_prior_captions`. |
| Deleted | the per-segment loop, `prior_edits`/`PriorEdits`, the seam block (`Seam`, `seam_lines`, `_seam_block`, `director.seam_lines`) — the reference now contains both sides of every join. |

---

### Task 1: The global display view

**Files:** create `src/nagare_clip/director/display.py`, `tests/director/test_display.py`.

**Interfaces — Produces:**
```python
@dataclass(frozen=True)
class DisplayLine:
    number: int                 # 1-based, global, playback order
    segment: int                # 1-based index in the playback order
    stem: str
    source_line: int            # the speech line, or the line a silence follows
    is_silence: bool
    text: str                   # rendered body, without the leading "N: "

@dataclass(frozen=True)
class DisplayView:
    lines: list[DisplayLine]
    def render(self) -> str: ...
    def to_source(self, first: int, last: int) -> tuple[str, tuple[int, int], bool, bool] | None
    # -> (stem, (source first, source last), gap_start, gap_end); None when the
    #    range crosses a segment join.
    def segment_join_after(self, number: int) -> bool
```
`to_source` maps a display range back exactly as increment 1's `"n~"` form
expects: a silence display line at the START of a range gives `gap_start`, at
the END gives `gap_end`, and its `source_line` is the line it follows.

- [ ] **Step 1: failing test** — a two-segment fixture (one source split, so the same stem appears twice) asserts: numbering is continuous across segments; `to_source` on a speech range returns plain source lines; a range beginning on a silence line returns `gap_start=True` with the preceding line's number; a range crossing a join returns `None`; `segment_join_after` is true exactly at the last line of each segment.
- [ ] **Step 2: run it, watch it fail** (`ModuleNotFoundError`).
- [ ] **Step 3: implement** over increment 2's silence-line builder, reusing it rather than re-deriving which gaps qualify.
- [ ] **Step 4: run the tests.**
- [ ] **Step 5: mutations** — numbering restarts per segment; `gap_start`/`gap_end` swapped; a join-crossing range silently clipped instead of refused; a silence line mapped to the FOLLOWING source line.
- [ ] **Step 6: commit.**

---

### Task 2: The turn protocol

**Files:** create `src/nagare_clip/director/loop.py`, `tests/director/test_loop.py`.

**Interfaces — Consumes** `DisplayView` (Task 1). **Produces:**
```python
@dataclass
class LoopState:
    ops: dict[int, list[DirectorOp]]   # keyed by segment index
    reviewed_through: int              # highest display line the model has reviewed
    turns: int

def next_request(view: DisplayView, state: LoopState, chunk_lines: int) -> str
def apply_reply(view: DisplayView, state: LoopState, reply: str) -> ReplyResult
```
A reply is one JSON object, either `{"done": true}` or
`{"range": [a, b], "reviewed_through": n, "ops": [...]}` whose ops carry
display-numbered `lines`. `apply_reply` REPLACES every op whose start falls in
`[a, b]`, so a rewrite of an earlier range is the same operation as a first
pass. `ReplyResult` carries the accepted ops, the parser's drops, whether the
model declared `done`, and a refusal message when `done` arrives with lines
still unreviewed, or when a `timelapse`/`overlay` crosses a segment join
(`cut` and `keep` are split at the join instead).

`next_request` asks for an **approximate** range — "around lines X to Y" — so
the model picks the actual end near a natural break, never a hard boundary
(hard boundaries are how the plan's ranges became op boundaries).

- [ ] **Step 1: failing test** — scripted replies: a first pass; a rewrite of an earlier range replacing only its ops; `done` refused while lines remain; a join-crossing `cut` split and a join-crossing `timelapse` refused with the join's line number; a malformed reply returning a parse error without raising.
- [ ] **Step 2: run it, watch it fail.**
- [ ] **Step 3: implement.**
- [ ] **Step 4: run the tests.**
- [ ] **Step 5: mutations** — a rewrite appends instead of replacing; `reviewed_through` taken from the request rather than the reply; `done` accepted with lines unreviewed; the request phrased as an exact range.
- [ ] **Step 6: commit.**

---

### Task 3: Preview every turn

**Files:** modify `src/nagare_clip/director/preview.py`, `tests/director/test_preview.py`.

The preview already computes each op's on-screen seconds, unintelligible
speech, caption seconds, drops and runtimes. It gains: display numbering in
its output, and the whole-video runtime so far (segments with accepted ops at
their edited runtime, the rest at default).

- [ ] Steps as above; mutation: report the segment runtime where the whole-video one belongs.

---

### Task 4: `run_director_conversation` and the stage

**Files:** modify `src/nagare_clip/director/run.py`, `src/nagare_clip/pipeline/stages.py`, `src/nagare_clip/config.py`; tests in `tests/pipeline/test_director_conversation.py`.

- Cap = `ceil(len(view.lines) / chunk_lines) * 2`. On reaching it: write the
  accepted ops for every source, then raise `PipelineError` naming the last
  reviewed display line and the file paths — the user resumes from
  `guided_edit` by hand.
- Retries: the existing ladder applies per turn; a turn that fails every retry
  is treated like the cap (write, then raise).
- `--source` no longer narrows the director: log it once, plainly.
- The system prefix is byte-identical on every turn (assert it in a test) so
  the Anthropic cache keeps reading.

- [ ] Steps as above; mutations: the cap raising *before* writing; the cap off
  by one; `--source` still narrowing; the prefix rebuilt per turn.

---

### Task 5: Delete the per-segment path

**Files:** `src/nagare_clip/director/run.py`, `context.py`, `pipeline/stages.py`, `config.py`, and their tests.

Remove `run_director`, the per-segment loop, `PriorEdits`/`format_prior_edits`,
the seam block and `director.seam_lines`, `whole_project_context` (now
unconditional) and `max_prior_captions`. Regenerate `config.example.yml`.
Keep: `render_transcript`, `speech_seconds`, `qualify_line_numbers` if the
display view still uses it, the parser, `_write_director_ops`, the preview,
caching and usage.

- [ ] Steps as above; the suite must be green with nothing referring to the
  deleted names (`grep` for each in `src` and `tests` as a step).

---

### Task 6: Measure on the real footage (controller, not the implementer)

**This makes LLM calls — declare the cost to the user and wait for approval.**
One director run, `--from-stage director --to-stage director`. Compare with
the three earlier runs (`/tmp/claude-1000/before-ctx`,
`/tmp/claude-1000/checkpoint-high`, and the current project's `director/`):

- ops whose notes still claim a gap outside their range (3 across two runs before);
- seconds of speech made unintelligible inside timelapses (314.2 s in the last run);
- gaps dropped between neighbouring ops (9 in the last run);
- timelapse count and share of the runtime (watching for the improvement-16 regression);
- turns used, rewrites used, and how many ops changed after a preview — the number that says whether the preview did anything.
