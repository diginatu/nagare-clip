# plan_revise — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#plan_revise--revising-the-plan-with-the-human-editor).

`plan_revise` runs once project-wide between `plan` and `director`. It owns the
conversation with the human editor and turns it into **operations** against the
directions `plan` wrote.

```
output/
  plan_dialogue/history.md   ← the conversation; plan_revise reads and appends
  plan/plan.json             ← plan's output, a pure function of the summaries
  plan_revise/plan.json      ← the revised plan; director prefers it when present
```

`diff plan/plan.json plan_revise/plan.json` is exactly the human's influence on
the edit — which was invisible while `plan.json` was overwritten in place.

## Why it is its own stage

`plan` used to do two jobs through one prompt and one call: build a plan from
the summaries, and edit that plan against the conversation. They pull in
opposite directions.

- **The stage could not be skipped when there was nothing to say.** Two
  consecutive runs on a real project each cost one `claude-sonnet-5` call to
  answer *"No changes requested since last plan; repeating all directions
  unchanged."*
- **The model had to rewrite everything to change one thing.** One human turn
  asking for a part to be split produced 24 directions where 22 existed; the
  other 21 came back byte-identical only because the prompt asked for it. Since
  nothing merged the response with the previous plan, a model that returned only
  the three new directions would have silently deleted the other 21. The cost of
  restating grows with the part count, and so does the chance the model
  economises.
- **Deletion was not expressible.** The only way to remove a direction was to
  omit it from a full restatement — indistinguishable from the model forgetting
  it.
- **An edit vocabulary in the plan prompt reaches the first run too**, which has
  nothing to delete. Everything added to a prompt competes with the editorial
  brief.

So `plan` went back to being a pure function of the summaries (same
`summary.json` in, same directions out), and the conversation moved here.

## It only fires on an unanswered human turn

`run_plan_revise` reads the turns after the last divider and calls the LLM only
when the **last** turn is the human's (`dialogue.has_unanswered_human`). No
conversation, no call — the common case costs zero. The firing condition is
structural rather than a special case inside a prompt.

Because the stage always answers a turn it acts on (an empty `message` still
appends `(1 added, 1 deleted, 0 updated)`), "the human spoke last" is exactly
"there is something not yet answered". A **failed** call answers nothing and
writes nothing, so the next run retries instead of silently swallowing the turn.

When it does not fire, an existing `plan_revise/plan.json` is left alone — that
is what carries a revision forward across later runs.

## Revision as operations

```json
{
  "delete": ["k7f2"],
  "add":    [{"index": 21, "lines": [60, 83], "direction": "feature — …"}],
  "update": [{"id": "m3q8", "direction": "shorten heavily — …"}],
  "message": "パート21を3つに分割しました…"
}
```

A direction no operation names is carried through **by the code**
(`apply_revision`), not by the model's diligence. Output size is proportional to
the change rather than to the project.

- **`add` needs no insertion position.** A direction is located by its part
  `index` and its `lines`; sorting by those two gives the order, exactly as
  `plan` already produces it. An "insert after" operation would add a second,
  contradictory way to say where a direction sits.
- `add` validates `index`/`lines` with the same rules as a plan direction
  (`plan_llm.coerce_lines`): the range must sit inside its part's own range.
  Omitting `lines` means the whole part.
- An `add` over a range that already has a direction **replaces** it (the merge
  is keyed by `(stem, lines)` — the same rule `plan` parses by).
- `update` changes only the text; to move a boundary, `delete` and `add`.
- **Splitting** a part is one `delete` plus one `add` per thread.
- `message` here is a **reply to a person**: how the last instruction was read,
  what changed because of it, what wants confirming. It is not a summary of the
  plan — `plan` writes one of those every run ([plan.md](plan.md)), and it is
  the first active turn `plan_revise` reads below the divider, so the two are
  visible side by side in the history.
- Unknown ids are dropped and logged through the same `_drop()` path malformed
  entries use, so a hallucinated id degrades one operation rather than the run.
- Hard parse failure (invalid JSON, or none of the four keys) retries via
  `llm_retry`; everything else drops item by item. A `message` alone is valid —
  answering a question without editing is a real reply.

The operations themselves are **not persisted**: they are re-derived from the
conversation against the current plan on every run, so they are a derivation,
not a record.

## Ids: a hash, abbreviated to the shortest unique prefix

`plan_revise/ids.py` derives each direction's id from its identity,
`(stem, lines)`:

- a **sequence number** invites the model to read it as a part index, and
  misleads when a boundary moves;
- a **random value** changes on every `plan` run, so nothing can be said to have
  survived;
- a **hash of `(stem, lines)`** is stable while the summary's parts are stable
  and changes exactly when the footage a direction refers to changes —
  self-invalidating in the right cases and only those.

The full id is a base32 sha1 prefix; what is rendered is the **shortest prefix
unique across this plan** (`abbrev_len`, floor `ID_FLOOR = 4`), the way git
abbreviates object names. A full hash is noise to read and gives the model more
characters to copy wrong; with a couple of dozen directions this lands on 4.

The abbreviation may differ between runs as directions are added or removed.
That is safe: **a human never types an id.** They write prose in `history.md`;
ids exist only between the rendered input and the returned operations of a
single call. `resolve()` accepts any prefix of the full id (so a model echoing a
longer one still lands), drops a token that matches more than one *distinct* id,
and resolves duplicate footage to every entry over it.

## What a `plan` re-run invalidates

`plan` runs again for all sorts of reasons (a prompt change, an unrelated
`--from-stage plan`). Two things go stale, and `plan/run.py` handles both:

**The revised artifact.** `plan_revise/plan.json` revises a plan that no longer
exists, and `director` would keep preferring it, so `run_plan(..., revised=…)`
deletes it — on the disabled path too, since an empty `plan.json` with a stale
revision beside it is worse still.

**The conversation.** Turns above a `plan` run refer to directions that may no
longer be there. Deleting them is too costly — `summary` re-runs rarely, so an
instruction like 「[31,83] は 60-83 だけがデモ本体」 is usually still true, and it
took a human twenty minutes of measuring footage to write. So `plan` **divides**
instead:

```markdown
## plan
パート21を指示通り3つに分割しました…

--- plan re-ran 2026-08-22T19:04 — turns above this line no longer apply ---

## plan
実演が payoff。前半の組み立ては圧縮し、テスト走行は full length で残した…

## human
```

`plan` then appends its own account of the new plan below the divider and a
`## human` heading to reply under, so the log always ends somewhere to type.
`plan_revise` reads only the turns after the last divider. What a human sees is
the same as clearing — nothing old is re-applied — but the record survives, a
still-valid instruction can be copied down rather than reconstructed, and the
file explains why an earlier turn stopped having an effect. Nothing is appended
when nothing has been said since the last divider, so repeated `plan` runs do
not pile dividers up; the trailing `## human` heading is where to type.

**Do not re-run `plan` to apply a turn** — it retires the turn you just wrote.
Re-run `--from-stage plan_revise --to-stage plan_revise`.

## Failure modes

Nothing here can fail a run:

- stage disabled → a stale `plan_revise/plan.json` is removed, no LLM call
- no history / no unanswered turn → no call, existing revision untouched
- unreadable `summary.json` → logged, no call (an `add` could not be validated)
- unreadable/missing `plan/plan.json` → treated as an empty plan (`add` still works)
- LLM/parse failure → retried via `llm_retry`, then nothing written and no reply
  appended, so the next run tries again
- an un-writable history file → logged warning, the revision is still written
