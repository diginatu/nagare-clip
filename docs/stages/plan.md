# plan — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#plan--cross-video-rough-directions).

## The conversation (`plan_dialogue/history.md`)

`plan` is the one stage a human can talk to. Everything it needs is cheap
(1 LLM call for the whole project), so correcting it by sentence is strictly
cheaper than hand-editing `plan.json` — the previous loop cost an operator most
of a session to edit 61 director ops by hand, and the LLM calls it saved were
worth cents.

Per run, `plan` receives the current `summary.json` (as always), **plus** its own
previous `plan.json`, and the whole conversation so far. It writes `plan.json`
and appends its own turn to the history.

```
output/
  plan_dialogue/history.md   ← append-only; plan reads it and appends its turn
  plan/plan.json             ← rewritten every run
```

- The history lives in its own directory, not in `plan/`, so the invariant "a
  stage rewrites its own directory wholesale" stays true with no exception.
- It lives under the **output tree**, not beside the config: every turn refers to
  part indices and line ranges that this `summary` run defines, so deleting
  `output/` invalidates the references anyway and the conversation should go
  with them.
- It is deliberately **not** in `nagare_config.yml`'s `project:` block. That
  brief reaches more stages than intended — in a real project a word used there
  travelled `summary` → `summary.json` → `text_filter` and rewrote what the
  speaker actually said. A per-run instruction to `plan` must not take that path.

### Format

Forgiving markdown, parsed by `plan/dialogue.py`:

- `## human` / `## plan` headings (any level, case-insensitive, trailing words
  allowed: `## human (2026-08-22)` works).
- Text before the first heading is read as a **human** turn — opening the file
  and typing is a supported way to write one.
- `<!-- … -->` blocks are stripped, which is how the file's own header explains
  itself without becoming an instruction the LLM reads.
- An unrecognised heading stays inside the current turn's text; blank turns are
  dropped. Nothing here raises — a malformed turn degrades to text the LLM can
  still read.

Write a turn with `./scripts/plan_say.sh "…"` (or `-` / a pipe for stdin), which
is a shim over `python -m nagare_clip.plan.dialogue`. It resolves the history
path from `pipeline.output_dir` (`--config`/`--output-dir` override) and refuses
an empty turn (exit 1, nothing written).

### Why the previous plan is fed back

Feeding `plan.json` back is what makes round two an **edit** rather than a
re-roll. Without it the 21 directions the human was happy with are silently
rewritten — and prompt-driven output in this pipeline regresses about as often
as it improves. The previous directions are rendered inline under the part each
belongs to (`    your last direction [31-59]: …`), matched by **largest line
overlap** rather than by an exact range, so a hand-edited range that straddles a
boundary still lands somewhere readable. A direction overlapping no part of its
own video is dropped (logged).

The default `plan.prompt` states the contract: change only the directions the
conversation calls for, repeat every other direction unchanged, word for word.

### `message`

The response's optional `"message"` is what `plan` says back — what it was
unsure about, which directions it would like confirmed, what it did with the
last instruction. It is appended to the history as a `## plan` turn. An empty or
missing message appends nothing (a missing one is not a parse failure). The
history file itself is still created when the stage is enabled, so a human can
find where to reply.

The previous plan is fed back on **every** enabled run, not only when a human
has written a turn — that is what keeps a correction applying on a full
`--from-stage summary` re-run. The cost is that after `summary` re-segments,
the previous directions are stale: they are still matched onto the new parts by
overlap, and the model sees both them and the new parts document. If a
re-summarised project should start from a blank plan, delete `plan/plan.json`
(and, if the line numbers moved, `plan_dialogue/history.md` with it — its turns
refer to the old ranges).

## Splitting a part

`summary` owns the part boundaries and is deliberately **not** conversational:
line numbers and part indices stay fixed, so a message written in round one
still refers to the same footage in round four. Make `summary` steerable and
every stored turn can go stale under it.

That leaves `plan` needing its own way to disagree with a part's boundaries. A
direction may carry its own `"lines": [a, b]`, and several directions may share
one `index`:

```json
{"directions": [
  {"index": 7, "lines": [31, 59], "direction": "remove — digression"},
  {"index": 7, "lines": [60, 83], "direction": "feature — the demonstration itself"}
], "message": "split part 7 as you asked"}
```

Without this the conversation could acknowledge "[31,83] is really two things"
and do nothing about it, which is worse than not having the conversation. This
is the case the feature was built for: a real 53-line part was directed
"climactic demonstration … give it full length" when only 24 of its lines were
the demonstration.

Validation (`try_parse_plan_response`): the range must be a 2-integer pair with
`a <= b` **inside its part's own range** — a range reaching outside names lines
the part does not cover, so it is dropped (logged, recorded as `dropped-items`)
rather than clamped. Omitting `lines` means the whole part, exactly as before.
Directions come back ordered by part, then by line; a repeated `(index, lines)`
pair keeps the last one.

## Downstream: the director reads split directions

`director/context.py` matches directions to parts by **overlap**, not by an exact
`(stem, lines)` key. A part with one whole-part direction renders exactly as it
always did:

```
- lines 1-4: intro → direction: feature — sets up the build
```

A split part renders its directions with their own ranges:

```
- lines 31-83: the pump demonstration → directions:
    - lines 31-59: remove — digression
    - lines 60-83: feature — the demonstration itself
```

`director` reads `plan.json` only — it must **not** read `plan_dialogue/`. Two
channels of editorial intent into one stage would contradict each other; `plan`
owns translating what the human said into directions. (A `director_dialogue/` is
a reasonable later change, deliberately out of scope.)

## The divergence note

After `director` finishes (no LLM call, `plan/divergence.py`), each direction is
compared against the ops that landed in its line range and the conflicts are
written to `llm_report/notes/plan_divergence.md`, which `rebuild_index()` inlines
into `llm_report/index.md`. It lives in `notes/` so it survives later stages'
index rebuilds; an empty result deletes a stale note.

Three conflicts, each reporting its number so the threshold is arguable:

| kind | condition |
|---|---|
| `cut-over-feature` | a `feature`/`retain`/`emphasise` direction whose lines are covered by `cut` ops at or above `DEFAULT_CUT_SHARE` (0.5) |
| `protected-over-remove` | a `remove`/`cut`/`drop` direction that received a `keep`/`overlay`/`timelapse` op |
| `no-timelapse` | a `timelapse`/`speed` direction where no `timelapse` op landed |

Each entry carries the director's own `note` — the argument for the override.

The director is **not** made to obey the plan: in the case that motivated this,
the override was substantially correct (it cut 27 lines of digression the plan
had called the payoff). The defect was silence, not disobedience. Direction text
is free-form, so the verb is read from the direction's **leading clause** (before
the first dash/colon) — the `feature — why` shape the default prompt asks for; a
clause with no known verb is ignored rather than guessed at, and the vocabulary
is English (what the default prompt produces).

## Failure modes

Every part degrades independently and nothing here can fail a run:

- unreadable/missing history → no conversation, byte-identical prompt to a first run
- unreadable previous `plan.json` → treated as a first run (logged)
- LLM/parse failure → retried via `llm_retry`, then empty directions and no turn appended
- an un-writable history file → logged warning, the plan is still written
