# The director as a step over its own directory

Status: spec; decisions taken on 2026-09-27 (§8).
Step 3 of folding `plan`/`plan_revise` into the director conversation
(step 1: `2026-09-26-director-decided-order-design.md`, step 2:
`2026-09-27-director-writes-its-plan-design.md`).

## 1. Problem

The director conversation lives in memory for one run. A human cannot join it,
a run that hits the turn cap can only be continued by hand from `guided_edit`,
and every re-run starts over. The human checkpoint the plan stages offered
(`plan_say` → `plan_revise`) has no counterpart.

Human input is exceptional, but it should be an ordinary part of the system's
normal operation, not a special path.

## 2. The rule

The director's directory **is** its state. One run of the stage is:

```
if conversation.md holds a done mark → do nothing (no LLM call, no file touched)
else → read the state from the files, take turns until done or the turn cap,
       writing the files back after every turn
```

That is the whole control flow. A human is not special: to have the director
continue, they delete the done mark and write an `editor` entry. Every
distinction lives in the guidance the pipeline writes from the state (no plan
→ "write the plan"; lines unreviewed → "review around …"; all reviewed →
"finish, or rewrite a range") and in the system prompt, which says what an
`Editor:` entry is.

## 3. The files

| file | holds | missing means |
|---|---|---|
| `plan.md` | the plan in force (`# The director's plan` + text) | the next turn asks for a plan |
| `order.json` | the playback order (step 1's shape) | seeded from the plan stage's order |
| `{stem}_director.json` | that source's ops | no ops yet |
| `conversation.md` | the conversation, and the done mark | a fresh conversation |

Each is read at the start of the run and written after every accepted turn, so
a crash or the cap loses at most the turn in flight. Any file may be edited by
hand between runs and is honoured: that is how a human changes the plan, the
order or an op directly. To start over, delete the directory.

Progress (`reviewed_through`) is the largest `reviewed_through` of the
director's replies in `conversation.md`.

## 4. `conversation.md`

```markdown
## guide
Write the plan.

## director
{"plan": "…", "order": [[12, 15], [1, 4], [5, 11]]}

## guide
Review around lines 1 to 8.

## director
{"range": [1, 8], "reviewed_through": 8, "ops": [...]}

## done

## editor
冒頭のあいさつは残して。配管の速回しは長すぎる
```

- Entries are `## <speaker>` headings: `guide` (the pipeline; its one-line
  summary only — the full guidance is recomputed every turn), `director` (the
  model's reply, verbatim), `editor` (a person), `done` (the mark, no body).
  An unknown heading, or text before the first heading, reads as `editor`: a
  person wrote it.
- **The done mark** is appended by the pipeline when the model's `done` is
  accepted. `is_done` = a `done` entry exists anywhere. Deleting it is how
  anyone asks for more.
- **To the API**, a `director` entry is an assistant message. Every run of
  consecutive other entries becomes ONE user message, each entry prefixed by
  its label (`Guide: …`, `Editor: …`); `done` entries are dropped. The live turn
  appends the full guidance as a `Guide:` part of that same user message, so
  roles always alternate and a pending editor entry sits right above it.
- `scripts/director_say.sh "…"` removes the done mark and appends an `editor`
  entry — a convenience for the same edit by hand.

The system prompt gains one paragraph: `Guide:` is the pipeline's routine
guidance, `Editor:` is the human editor; the editor wins where they disagree,
and must be answered before `done`.

## 5. Pausing after the plan

`director.pause_after_plan` (default `false`). When true, the turn that writes
the first plan (there was none before it) is followed by a done mark, and the
stage stops the pipeline with a message: read `director/plan.md`, delete the
done mark (or use `director_say.sh`), run again. The stop is a
`PipelineStop` — exit 0, not an error. A later plan revision never pauses.

## 6. What changes

- The stage no longer deletes `_director.json`/`order.json`/`plan.md` at the
  start; state is resumed.
- The turn cap counts this run's turns. A run that hits it fails as today,
  but the message says to re-run to continue: every turn is already on disk.
- `run_director_conversation` takes the resumed state and a `checkpoint`
  callback (called after every accepted turn); the stage owns the files.
- A disabled director writes missing outputs only (empty ops, the seed order)
  and touches nothing that exists.
- `loop.request_summary` becomes the text of the `guide` entry written before
  each call; the history is read from `conversation.md`.

## 7. Deferred

Staleness — the transcripts changing under a saved conversation — is part of a
pipeline-wide dependency problem (every stage's inputs), to be solved there,
make-style. Until then, deleting `director/` is the reset.

## 8. Decisions

1. State is the files; the log is conversation, not a replay source.
2. The done mark is an entry; a human deletes it. No "is the human last" check.
3. Only user-side entries carry a label (`Guide:`, `Editor:`); the director is
   the assistant role.
4. Re-running resumes; deleting the directory resets.
5. `pause_after_plan` exists, default off.
6. A run that takes no turn touches no file.

## 9. Tests

- `conversation.py`: parse/render round-trip; unknown heading and leading text
  read as editor; `is_done`; messages merge consecutive user-side entries with
  labels and drop `done`; `say` removes the mark and appends.
- stage: a done directory makes no call and touches no file; a resumed run
  continues from `reviewed_through` with the files' plan/order/ops; hand edits
  to `plan.md`/`_director.json` are what the next turn sees; per-turn
  checkpoint (a failing turn leaves earlier turns on disk); the cap message;
  `pause_after_plan` stops with the mark written and exit 0.
- golden: run 1 to done; delete the mark and add an editor entry; run 2 shows
  `Editor:` above the guidance and ends done again; run 3 makes no call.
