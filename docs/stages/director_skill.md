# director skill — directing with a coding agent

The director is an **agent skill**: a general coding agent (Claude Code,
GitHub Copilot, Codex, Cursor, Gemini, …) plays the director interactively
with the user, calling deterministic tools from this repository, and writes
the director files — `{stem}_director.json`, `director/order.json`,
`director/plan.md`.

The LLM director (a hand-rolled conversation over LiteLLM, `director.enabled`,
`conversation.md`, `director_say.sh`) is **gone** (phases 2 and 3). Why: the
conversation.md / done-mark / `director_say.sh` channel made human↔AI
back-and-forth clumsy, and a coding agent already is that channel.

Everything after the director is deterministic, and the pipeline is split at
it:

- the `director` stage is a **checkpoint** with no LLM
  ([pipeline.md](pipeline.md#the-director-checkpoint)): a run that reaches
  it always stops (`PipelineStop`, exit 0) — the stages before it just
  re-ran, so the director files must be written or revised with the skill;
- resuming with `--from-stage guided_edit` checks the files: missing
  `order.json`/`_director.json` or anything that no longer maps onto the
  transcripts is a `PipelineError`; otherwise guided_edit logs
  `Continuing with N op(s) (…); shooting order` and goes on;
- `guided_edit` has no LLM and no settings: it applies every op, the `edit`
  op included, as markers/patches in `_edits.txt`;
- the user resumes with `--from-stage guided_edit` after `write`.

## The tools (`src/nagare_clip/director/tools/`, no LLM call)

```
./scripts/director_tools.sh {view,dump,preview,write} --config <yml> [...]
uv run python -m nagare_clip.director.tools {view,dump,preview,write} ...
```

`--config` is repeatable and deep-merged like the pipeline's. All line numbers
are the **display numbering**: every source in shooting order, numbered once,
speech and silence lines together (`director.display.build_display_view`) —
the view the checkpoint loads too.

| command | does | writes |
| --- | --- | --- |
| `view [--lines A-B] [--no-context]` | brief (`brief.format_brief`), `ops.keep_limit_note` (the cap, and that touching keeps count as one), `project_context_block`, a source index — one row per source with its line range and its default runtime, or `default unknown — <why>` (`project.runtime_status()`: no sentence_split JSON, segment count ≠ transcript line count, or a line without start/end) — and the whole-video default runtime, or `unknown` naming the source(s); never omitted silently, `VIEW_HEADER`, `DisplayView.render(first, last)` | nothing |
| `dump` | `director/` as one edit file in display numbering (`plan`, `order` when not shooting order, `ops`) — revising from it preserves a reorder | nothing |
| `preview [--edit FILE\|-] [--summary [--min-run S]] [--lines A-B] [--source k]` | `preview.edit_state()` over the files on disk or a candidate edit: plan, per-op playback, segment runs, the video as it plays with seams, captions in playback order, whole-video runtime. `--summary` prints `preview.edit_summary()` instead — per-source default → edited runtime, the whole-video total, every `timeline_runs()` run in playback order (`--min-run S` hides runs under S on-screen seconds and counts them; cuts always shown), the captions in playback order — about a screenful, for every iteration. `--lines A-B` / `--source k` (index or stem, repeatable) narrow the per-segment detail (or the summary's run list) via `edit_state(only=, focus=)` / `preview_segment(focus=)`; totals, captions and problems stay whole-video. Nothing is re-modelled: the summary's figures are `segment_preview()`'s, and with no narrowing `edit_state()` prints the whole edit. A candidate's problems go to stderr and exit 1; only the ops that passed their own checks are previewed | nothing |
| `write --edit FILE\|-` | validates; on any problem prints them **all** (stderr, exit 1) and writes nothing; else writes every `{stem}_director.json` (empty list included), and `order.json`/`plan.md` when the edit carries them | director files |

`DisplayView.render()` gained an optional
`first`/`last` range (defaults unchanged): a block heading is kept for every
block with a line in range, its leading gap descriptions only when its first
line is in range.

Loading (`tools/project.py::load_project` → `project_for(ctx, stems)`) goes
through `director.files.director_inputs()` and
`director.load.load_segment_transcript()`; guided_edit's check calls the same
`project_for()` with the pipeline's own context, so the tools and that
check see one view. Stems come from `project_stems(input_videos_dir)`,
else from `text_filter/*_edits.txt`. A missing `_edits.txt` is exit 2 naming
`--to-stage text_filter`. Writing goes through `director.files.write_ops/
write_order/write_plan` (the public home of the director files' I/O, moved
out of `pipeline/stages.py`).

After writing, `write` prints the next step (`run_pipeline.sh --from-stage
guided_edit`). Verified end to end on a fixture project in phase 1:
`write` → `--from-stage guided_edit --to-stage intervals` produced the
expected markers and a reordered `timeline.json`.

`current_state()` maps the files on disk back onto the view
(`view.from_source`, `checks.order_ranges`); an op or order that no longer maps
(a transcript or `director.silence_line_min` changed) is listed as a warning
and left out. Resuming at guided_edit fails on the same list, and `view`
prints it to stderr (after the view) so an agent reading `view` first notices.

## The edit file

```json
{"plan": "<prose>", "order": [[first, last], ...], "ops": [{"type": ..., "lines": [a, b], ...}]}
```

The edit is the **complete state**, so writing it twice gives the same files:

- `ops` is required and **replaces every op** in the director files.
- `order` absent means **shooting order**: `write` always writes
  `director/order.json` (the identity order when absent). The timelapse/break checks use the edit's own order,
  never the one on disk. `dump` emits the order in force, so an edit revised
  from `dump` keeps a reorder.
- `plan` is optional; absent, `plan.md` stays. It is required while `plan.md`
  holds none.
- Unknown top-level keys are a problem.

## Validation (`tools/validate.py::validate_edit`, pure)

| check | function |
| --- | --- |
| type, range bounds, timelapse factor ≥ `TIMELAPSE_MIN_FACTOR`, overlay text/duration, an `edit` op's shape (one line, `old` non-empty, `new` a string, no patch/marker syntax or line break in either) | `director.ops.parse_op` |
| no `speed` (`MENU_TYPES`) | `director.ops.off_menu_problem` |
| keep width ≤ `director.max_keep_lines` (display lines, silence lines count) | `director.ops.keep_cap_problem` |
| no `"n~"` in display numbering | `director.checks.silence_ref_problem` |
| op inside one `[k]` block | `director.checks.join_problem` (every crossing refused) |
| order tiles 1..N; no silence-only range; no break inside a timelapse | `director.checks.order_problems` (uses `read_order`) |
| no timelapse across a break of the edit's order | `director.checks.break_problem` |
| an `edit` op's `old` occurs exactly once in its line as `view` shows it, outside any marker or patch | `guided_edit.patch.edit_problem` |

`parse_op` is also what every reader of a `_director.json` drops a malformed
op by (`ops_from_dict`, used by guided_edit, publish and the tools).

Checks over several ops:

- **keeps that touch or overlap count as one keep** against
  `director.max_keep_lines` — chaining keeps that each fit would restore the
  span the cap exists to keep dropped (it guards against keeps widening over
  talking, with no exemption). A source boundary ends a chain (different
  recordings, not one event); the problem is reported on the chain's first op
  and names the route: cover the event with ONE keep; for one wider than the
  cap, ask the user to raise `max_keep_lines` (or consider a timelapse for
  manual work).
- **a `cut` range may not overlap any other op's range** — guided_edit would
  otherwise clip the cut. Two overlapping cuts are reported once, on the
  earlier.
- **an `edit` op names a speech line** — a silence line has no words.

### The `edit` op

`{"type": "edit", "lines": [n, n], "old": "<exact text>", "new": "<replacement>"}`
(`new` may be `""`, a deletion). The agent reads the real line text through
`view`, so it states the change exactly and no model applies it:
guided_edit wraps the raw characters `old` reads as in one `{{old->new}}`
patch (`guided_edit/patch.py::apply_edit`).

- **Single-line, by decision.** A `{{old->new}}` patch cannot span lines, and
  one `_edits.txt` line is one WhisperX segment; a change across two lines is
  two edit ops.
- **"As the director sees it"** is `transcript.clean_for_display` of the line:
  patches applied, marker tags stripped. `DisplayLine.raw` carries the raw
  `_edits.txt` line; `patch._clean_map()` maps each visible character back to
  its raw index (text a patch contributes maps to nothing), and refuses a line
  whose markup it cannot map rather than guess.
- **Exactly once** (overlapping occurrences count): quote more of the line if
  it repeats.
- **Not through a marker or patch**: the matched characters must be plain,
  contiguous raw text. Touching a marker at the edge is fine.
- `old`/`new` may not contain `{{`, `}}`, `->`, `<`, `>` or a line break, so
  the patch always parses back (and cannot smuggle a marker in).

The prose-`note` form of `edit` is gone; `ops_from_dict` drops one (logged).

Nothing is split or dropped silently: a problem refuses the whole edit.
Problems are `Problem(where, reason, op)`, sorted edit/plan/order first then
by op number, each naming the op (`op #3 (timelapse [60, 72])`) and why. A
test asserts the LLM parser's drop messages equal the tools' reasons for the
Annotation lines (`[silent gap: …]`) have no number, so an op on one is
unrepresentable.

`to_source()` converts with `DisplayView.to_source()` (a silence line at an
edge becomes `"n~"`), `order_segments` + `normalise` convert the order; a test
round-trips every single-segment display range, and `write` → `dump`
round-trips whole edits.

## The skill (`skills/.apm/skills/nagare-director/`, packaged with APM)

- `SKILL.md` — when to use it, the tools, the workflow, the edit format, and
  the enforced rules as bare statements (each rule's reason lives only in
  `editorial-rules.md`; the References section is two one-line links).
  Workflow: run the pipeline to `text_filter` if needed → `view` (every page)
  → read `editorial-rules.md` → plan in prose (throughline, what to
  cut/compress and where, caption moments, order and why, expected runtime
  including the timelapses' combined share) → **stop and confirm the plan with
  the user** → draft ops → iterate on `preview --edit --summary`, watching the
  runtime against the target as the timelapses add up, reading the full
  detail only with `--lines`/`--source` where needed → report the meaningful
  numbers → `write` → tell the user to run
  `./scripts/run_pipeline.sh --from-stage guided_edit`. The user always wins.
- `references/editorial-rules.md` — **the reasoning behind every op and
  rule** (SKILL.md only lists them), migrated from `DIRECTOR_PROMPT` and the
  brief: the two-mode tighten-and-stage goal; what a keep is FOR (an
  on-screen event must not play chopped into fragments when small noises are
  transcribed as speech — one keep over the whole event, never over ordinary
  talking, the user decides on raising the cap); why `speed` is off the menu;
  why a timelapse is 4.0x+, and that about a minute on screen is its
  **upper limit, not a target** — its length follows the target duration,
  how many timelapses there are and the brief (a real run that took the
  minute literally gave 14 × ~55 s ≈ 13 min against a 30 min ceiling); no
  single anchor length or factor is given, for the reason AGENTS.md records
  (one number in a prompt becomes everyone's default); cut vs timelapse;
  overlay timing, with density yielding to the brief (~1 per 3-5 min only
  when the brief is silent); silence as content; joins; the order; pacing from the
  speech figure; "the playback is the viewer's"; the editorial brief.
  SKILL.md points to it in the workflow before planning and drafting.
- `references/reading-the-view.md` — **how to read what the tools print**:
  timing brackets (speech vs dropped silence), numbered silence lines vs
  un-numbered `[silent gap: …]` notes, and each part of `preview`'s output.
  SKILL.md points to it at the step that runs `view`.

The project's own brief is not copied into the skill: `view` prints it.

### Where it runs

The agent works **in the video directory** — the project with the footage and
its configs — not in this repository: config paths such as `output_dir:` are
relative to it. SKILL.md has it pass the tools exactly the pipeline's
`--config` files, in order (projects layer them, e.g.
`--config ../template/nagare_base.yml --config ./nagare_config.yml`), read a
project's own wrapper script to learn them, and run the pipeline through that
wrapper when there is one. The tools are called by path,
`<nagare-clip>/scripts/director_tools.sh` (like `run_pipeline.sh`, it runs
`uv run --project <checkout>` and keeps the CWD). No environment variable is
fixed: when the checkout's path is not known from the conversation, the
project's wrapper scripts or the environment, the skill asks the user.

### APM layout

Verified against APM 0.35.0 and its docs (microsoft.github.io/apm: *Package
types*, *Targets matrix*, *apm compile*, *apm install*):

- **One package for the video-editing skills**, at `skills/`: `skills/apm.yml`
  (`includes: [.apm/]`) and each skill under `skills/.apm/skills/<name>/`
  (the classic `.apm/` package layout, where each primitive is hoisted on
  install). Today it holds `nagare-director`; skills for other stages can be
  added beside it.
- It is installed into a **video directory**, never deployed here — the repo
  root has no `apm.yml`, `.apm/`, `.claude/skills/` or `.agents/skills/`, so
  contributors working on nagare-clip never get it:
  - `apm install <nagare-clip checkout>/skills --target claude,codex` —
    recommended: the skill text and the scripts it calls are the same version;
  - `apm install diginatu/nagare-clip/skills#main --target claude,codex` (or
    `#<tag>`) — the git-ref alternative.

  Both record the dependency in the video directory's `apm.yml`. Verified: a
  local-path install of a two-skill copy of the package from a scratch video
  directory deploys both skills to `.claude/skills/` and `.agents/skills/`;
  the `owner/repo/subdir#ref` form installs a public repo's subdirectory
  (`anthropics/skills/skills/frontend-design#main`). Not verified: our own
  repo by git ref, until `skills/` is on the remote.
- **`apm compile`** only compiles *instructions* primitives into root context
  files and never overwrites a hand-authored `AGENTS.md`/`CLAUDE.md`. The
  package has no instructions (agents discover skills from their skill
  directories), so it is not used.
- **`make apm`** smoke-installs the package into a fresh temp directory
  (removed on exit by a guarded `trap`), diffs `.claude/skills` and `.agents/skills`
  against `skills/.apm/skills`, and runs `apm audit --ci` there. It needs the
  apm CLI, so it is not in CI. `tests/test_apm_skill.py` checks in CI the
  package manifest, each skill's frontmatter (ASCII, `name` = directory,
  description ≤ 1024 chars), that every `director_tools.sh <cmd>` the skill
  names exists, that nothing is deployed at the repo root, and that SKILL.md's
  edit-format example passes `validate_edit` (so the shape agents copy is one
  `write` accepts).

## Phase 2/3 (done)

- The `edit` op is deterministic (above); guided_edit has no LLM and no
  settings (any `guided_edit.*` key fails config loading naming its path; an
  empty section is accepted).
- The director stage is a checkpoint that always stops; guided_edit checks the
  files on resume; the auto-write of empty director files
  for a disabled director is gone.
- Deleted: `director/loop.py`'s conversation protocol,
  `director/conversation.py`, the conversation half of `director/run.py`, the
  generation/prompt half of `director_llm.py`, `DIRECTOR_PROMPT`,
  `scripts/director_say.sh`, the golden conversation test, the director and
  guided_edit units in `llm_report`, and `preview_cli.py` /
  `scripts/director_preview.sh` (superseded by `director_tools.sh preview`;
  the only thing it added was recovering the LLM parser's dropped ops from
  the LLM report, and the tools refuse instead of dropping).
- Shared code moved to public modules: `director/ops.py` (the op contract
  and per-op checks, `max_keep_lines`, `keep_limit_note`),
  `director/transcript.py` (clean/numbered/timed transcript, which `summary`
  reuses), `director/checks.py` (order and join/break checks),
  `director/load.py` (`SegmentInputs`, `load_segment_transcript`,
  `VIEW_HEADER`, `project_context`), `director/files.py` (the director files'
  I/O, `director_inputs`, `missing_files`), `nagare_clip/llm_json.py`
  (`FENCE_RE`, `strip_code_fence`).
- Removed director keys (each fails config loading naming its path):
  `enabled`, `provider`, `api_base`, `model`, `api_key`, `temperature`,
  `reasoning_effort`, `timeout`, `response_format`, `max_retries`,
  `retry_temp_step`, `retry_temp_cap`, `chunk_lines`, `pause_after_plan`,
  `prompt`. `max_keep_lines` (the keep cap) and `silence_line_min` stay.
- `preview`'s header reads as "this edit" (`STATE_HEADER`, `PLAN_HEADER =
  "THE PLAN of this edit:"`); the workaround notes in the skill are gone.
  The old `keep_limit_note()` (which advised chaining keeps) is replaced by
  the skill's note, now `ops.keep_limit_note()`.

## Follow-ups

- Consider a **time-based keep cap** (seconds of restored silence) instead of
  a line count: a line-count cap treats a 2 s pause and a 60 s wait alike.
