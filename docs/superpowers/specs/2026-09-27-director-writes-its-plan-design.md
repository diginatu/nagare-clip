# The director writes its own plan

Status: spec; decisions taken on 2026-09-27 (§7).
Step 2 of folding `plan`/`plan_revise` into the director conversation
(step 1: `2026-09-26-director-decided-order-design.md`).

## 1. Problem

The `plan` stage decides per-part directions from the summaries alone and the
director is handed them as instructions. The model with the least information
sets the frame; the model with the most can only follow it or silently
diverge (hence `plan/divergence.py`, a report of the disagreement). And the plan's
ranges leaked into op boundaries often enough to need `SECTION_BOUNDARY_NOTE`.

## 2. The planning turn

The conversation's first turn asks for a plan, not ops:

```json
{"plan": "The throughline is … Lines ~40-70 are the fitting work: a timelapse …", "order": [...]}
```

- Natural language. It may cite approximate line numbers; the prompt says a
  plan's ranges are sections, not op boundaries.
- `order` may come with it (step 1's key, unchanged).
- The ask names what a plan covers: the throughline, what to cut or compress
  and roughly where, the moments worth a caption, the order and why if it
  changes, and the expected runtime against the brief. It is sent once; later
  turns carry it only as the one-line history summary, like every ask.
- A first reply without `"plan"` is unusable (the retry ladder hands the error
  back). Ops sent with the plan are refused — the plan turn owns no range.

## 3. The plan is state

Like the order: `LoopState.plan`, replaced whole by any later reply carrying
`"plan"` (alone as `{"plan": …}` or beside a range's ops). Every turn's edit
state opens with it:

```
YOUR PLAN (in force — send "plan" in any reply to replace it):
<text>
```

In the history it is also the model's own first reply, verbatim; the state
block keeps the CURRENT version in view however long the conversation gets,
so a plan the ops have drifted from is visible to the one who can fix either.

## 4. What the director stops reading

- The `plan` stage's **directions** are no longer rendered into the prefix.
  The project context becomes the overall summary plus each source's
  whole-video summary (`summary.json`'s `video_summaries`) — facts, not
  instructions. `SECTION_BOUNDARY_NOTE` and the direction rendering go.
- `plan/divergence.py` and `llm_report/notes/plan_divergence.md` go: there is
  no second opinion to diverge from. A stale note is deleted.
- The plan's `order` still seeds the conversation (step 1).
- `publish` still reads `plan.json`'s directions (step 4 replaces them).

## 5. Outputs

- `ConversationResult.plan: str`.
- `director/plan.md` — the plan in force when the conversation ended, for the
  human (step 3's checkpoint will be a reply to it). Written whenever there is
  one, before a failing conversation raises; deleted at the start of a run.

## 6. Cost and limits

One more turn: the turn cap becomes `ceil(lines / chunk_lines) * 2 + 1`. The
system message is unchanged across turns, so the planning turn writes the
cache every later turn reads.

`DIRECTOR_PROMPT` gains a short paragraph (the planning turn, the plan in
force, ranges are sections). The detailed "what a plan covers" lives in the
first ask, which is sent once. The ceiling rises to 6900, recorded in the
test's docstring.

## 7. Decisions

1. Plan directions are no longer fed to the director; summaries still are.
2. A plan may cite approximate line numbers; the prompt says they are
   sections, not op boundaries.
3. `publish` is left alone until step 4.

## 8. Tests

- `loop.py`: the first request asks for the plan; a plan reply sets the state
  and moves on to ranges; a first reply without a plan is an error; ops in a
  range-less reply are an error; a later `"plan"` replaces it; a non-string
  plan is an error.
- `preview.edit_state`: the plan block leads when there is a plan.
- `run.py`/stage: `plan.md` written (and on a failed run), deleted at start;
  divergence note gone; directions absent from the prefix, video summaries
  present; turn cap `+1`.
- The golden conversation gains the planning turn and a plan revision.
