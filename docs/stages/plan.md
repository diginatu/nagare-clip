# plan — runtime notes

See the [stage overview in AGENTS.md](../../AGENTS.md#plan--cross-video-rough-directions).

## A pure function of the summaries

`plan` reads `summary.json` and nothing else — not its own previous output, not
the conversation. Same summaries in, same directions out, so a re-run is
reproducible and its prompt carries no sentence about revising anything.

Revising a plan against what the human said is the [`plan_revise`](plan_revise.md)
stage's job. The two are separate because they pull a prompt in opposite
directions: an edit vocabulary (delete / add / update) put in front of a first
run has nothing to operate on, and everything added to a prompt competes with
the editorial brief.

What a `plan` run does beyond writing `plan/plan.json`:

- it **deletes `plan_revise/plan.json`** — a revision of a plan that no longer
  exists, which `director` would otherwise keep preferring;
- it **appends a divider** to `plan_dialogue/history.md`, retiring the turns
  above it (they refer to directions this run may not have produced).

Both are described in [`plan_revise.md`](plan_revise.md). The practical
consequence: **do not re-run `plan` to apply a conversation turn** — re-run
`--from-stage plan_revise --to-stage plan_revise` instead.

## The plan's own account (`message`)

Every response carries a `"message"`: a short account of the plan just made —
how the project was read as a whole, what was compressed and what was given
room, and where the model was unsure. `run_plan` appends it to
`plan_dialogue/history.md` as a `## plan` turn, **below** the divider (it
describes the plan that now exists, so it is not retired with the turns about
the plan being replaced), and then leaves a `## human` heading to reply under
(`dialogue.append_reply_slot`).

It is written on **every** run, with or without a conversation. The run that
builds two dozen directions from nothing is the one a human most needs
explained, and it is exactly the run with nothing to reply to: when `message`
was defined only as "your reply to the human", a real 22-part run answered *"No
changes requested since last plan; repeating all directions unchanged."* about a
plan it had just built from scratch. The same model, asked a question, wrote a
perfectly serviceable paragraph — the gap was the instruction, not the ability.

`plan_revise`'s `message` is a different thing (a reply to a person), and the
two prompts define them separately.

A missing or empty `message` is **not** a parse failure: the plan is usable
without its account, and retrying would re-roll every direction to recover one
paragraph. It logs a warning and appends no turn — which a human sees
immediately, since the history then holds nothing to read. The default prompt's
JSON shape shows the key with an instruction as its value (`"message": "how you
read the project, …"`) rather than a sample sentence, because in this pipeline
an example anchors harder than an instruction.

## Splitting a part

`summary` owns the part boundaries and is deliberately **not** conversational:
line numbers and part indices stay fixed, so a message written in round one
still refers to the same footage in round four. Make `summary` steerable and
every stored turn can go stale under it.

That leaves the plan needing its own way to disagree with a part's boundaries. A
direction may carry its own `"lines": [a, b]`, and several directions may share
one `index` — in `plan`'s response, and in a `plan_revise` `add` operation:

```json
{"directions": [
  {"index": 7, "lines": [31, 59], "direction": "remove — digression"},
  {"index": 7, "lines": [60, 83], "direction": "feature — the demonstration itself"}
]}
```

Without this the conversation (`plan_revise`) could acknowledge "[31,83] is
really two things" and do nothing about it, which is worse than not having the
conversation at all. This is the case the feature was built for: a real 53-line
part was directed "climactic demonstration … give it full length" when only 24
of its lines were the demonstration.

Validation (`try_parse_plan_response`): the range must be a 2-integer pair with
`a <= b` **inside its part's own range** — a range reaching outside names lines
the part does not cover, so it is dropped (logged, recorded as `dropped-items`)
rather than clamped. Omitting `lines` means the whole part, exactly as before.
Directions come back ordered by part, then by line; a repeated `(index, lines)`
pair keeps the last one.

## Downstream: one plan file

The consumers of `plan.json` (the director's starting order, `publish`) read
**one** plan file — `plan_revise/plan.json` when it exists and `plan/plan.json`
otherwise (`pipeline.stages._effective_plan_json`) — and must **not** read
`plan_dialogue/`.

## Who reads the directions

Not the director. Since the director writes its own plan in its first turn
(`docs/superpowers/specs/2026-09-27-director-writes-its-plan-design.md`), the
directions are no longer rendered into its prompt: the model with the least
information no longer frames the one with the most, and the plan's line ranges
no longer leak into op boundaries. The plan/director **divergence note** that
reported where the two disagreed went with it (`plan/divergence.py` is gone; the
director stage deletes a stale `llm_report/notes/plan_divergence.md`).

What still reads `plan.json`: the director's **starting order** (the `order` key,
[`order.md`](order.md)) and `publish`, which uses the directions as context for
its chapter titles.

## Failure modes

Every part degrades independently and nothing here can fail a run:

- LLM/parse failure → retried via `llm_retry`, then empty directions
- a response with no `message` → logged warning, no turn appended, directions kept
- an un-writable history file → logged warning, the plan is still written
- a `plan_revise/plan.json` that cannot be removed → logged warning, the plan is
  still written (the revision then wins until it is deleted by hand)

## The playback order

A plan response may carry an `order`: the finished video's segments, in playback
order. It is the director's **starting** order: the director may replace it, and
its `director/order.json` wins once written. Coverage — every line of every source exactly once — is the contract;
sequence is free. An invalid order is dropped **whole** and the pipeline falls
back to shooting order, never a partial repair.

The prompt states the contract as a rule and carries no worked reorder: the
editorial call belongs to the `project:` brief, and an example in this prompt
anchors harder than the instruction around it.

See [`order.md`](order.md) for the value, the contract, the manifest and the
order note.
