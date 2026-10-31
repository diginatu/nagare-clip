# director skill — directing with a coding agent (phase 1)

The `director` stage is being replaced by an **agent skill**: a general coding
agent (Claude Code, GitHub Copilot, Codex, Cursor, Gemini, …) plays the
director interactively with the user, calling deterministic tools from this
repository, and writes the same files the LLM director wrote —
`{stem}_director.json`, `director/order.json`, `director/plan.md`.

**Phase 1 is purely additive.** The LLM director (`director/loop.py`,
`run.py`, `conversation.py`, `director_llm.py`, `director_say.sh`) still exists,
still runs when `director.enabled` is on, and behaves byte-identically (the
golden conversation test is unchanged). It is scheduled for removal in a later
phase, together with making `guided_edit` fully deterministic and splitting
the pipeline at the director (stop after `text_filter`, resume with
`--from-stage guided_edit`).

Why: the conversation.md / done-mark / `director_say.sh` channel made
human↔AI back-and-forth clumsy. A coding agent already is that channel.

## The tools (`src/nagare_clip/director/tools/`, no LLM call)

```
./scripts/director_tools.sh {view,dump,preview,write} --config <yml> [...]
uv run python -m nagare_clip.director.tools {view,dump,preview,write} ...
```

`--config` is repeatable and deep-merged like the pipeline's. All line numbers
are the **display numbering**: every source in shooting order, numbered once,
speech and silence lines together (`director.display.build_display_view`) —
the same view the LLM director reads.

| command | does | writes |
| --- | --- | --- |
| `view [--lines A-B] [--no-context]` | brief (`brief.format_brief`), `validate.skill_keep_limit_note` (the cap, and that touching keeps count as one — the LLM director's own `keep_limit_note` advises chaining keeps and is left unchanged), `project_context_block`, a source index with line ranges and the default runtime, `VIEW_HEADER`, `DisplayView.render(first, last)` | nothing |
| `dump` | `director/` as one edit file in display numbering (`plan`, `order` when not shooting order, `ops`) — revising from it preserves a reorder | nothing |
| `preview [--edit FILE\|-]` | `preview.edit_state()` over the files on disk or a candidate edit: plan, per-op playback, segment runs, the video as it plays with seams, captions in playback order, whole-video runtime. A candidate's problems go to stderr and exit 1; only the ops that passed their own checks are previewed | nothing |
| `write --edit FILE\|-` | validates; on any problem prints them **all** (stderr, exit 1) and writes nothing; else writes every `{stem}_director.json` (empty list included), and `order.json`/`plan.md` when the edit carries them | director files |

`view` without `--lines` is what `director.run.system_message()` renders minus
the instructional `director.prompt`. `DisplayView.render()` gained an optional
`first`/`last` range (defaults unchanged): a block heading is kept for every
block with a line in range, its leading gap descriptions only when its first
line is in range.

Loading (`tools/project.py::load_project`) goes through
`pipeline.stages.director_inputs()` — factored out of `_director_run`, which
now calls it too — and `director.run.load_segment_transcript()`, so the view,
brackets, silence lines and gap annotations are the stage's. Stems come from
`project_stems(input_videos_dir)`, else from `text_filter/*_edits.txt`. A
missing `_edits.txt` is exit 2 naming `--to-stage text_filter`. Writing reuses
`stages._write_director_ops/_order/_plan`.

After writing, `write` warns (stderr) when `guided_edit.enabled` is false —
a disabled guided_edit copies `_edits.txt` through and the ops never land
(span ops need no LLM; only an `edit` op uses guided_edit's model) — and when
`director.enabled` is true (the LLM director would resume from these files if
the director stage ran). Verified end to end on a fixture project:
`write` → `run_pipeline.sh --from-stage guided_edit --to-stage intervals`
with `guided_edit.enabled: true` produced the expected markers and a reordered
`timeline.json`.

`current_state()` maps the files on disk back onto the view
(`view.from_source`, `loop.order_ranges`); an op or order that no longer maps
(a transcript or `director.silence_line_min` changed) is listed as a warning
and left out.

## The edit file

```json
{"plan": "<prose>", "order": [[first, last], ...], "ops": [{"type": ..., "lines": [a, b], ...}]}
```

The edit is the **complete state**, so writing it twice gives the same files:

- `ops` is required and **replaces every op** in the director files.
- `order` absent means **shooting order**: `write` always writes
  `director/order.json` (the identity order when absent), as the LLM director
  writes it on every run. The timelapse/break checks use the edit's own order,
  never the one on disk. `dump` emits the order in force, so an edit revised
  from `dump` keeps a reorder.
- `plan` is optional; absent, `plan.md` stays. It is required while `plan.md`
  holds none.
- Unknown top-level keys are a problem.

## Validation (`tools/validate.py::validate_edit`, pure)

Every check is a shared pure function the LLM director also calls, extracted
in this phase so the two cannot drift:

| check | function | LLM director |
| --- | --- | --- |
| type, range bounds, timelapse factor ≥ `TIMELAPSE_MIN_FACTOR`, overlay text/duration | `director_llm.parse_op` (was `_parse_op`) | drops the op |
| no `speed` (`MENU_TYPES`) | `director_llm.off_menu_problem` | drops (`_drop_off_menu_ops`) |
| keep width ≤ `director.max_keep_lines` (display lines, silence lines count) | `director_llm.keep_cap_problem` | drops (`_apply_keep_cap`) |
| no `"n~"` in display numbering | `loop.silence_ref_problem` | drops |
| op inside one `[k]` block | `loop.join_problem(splittable=frozenset())` | splits cut/keep, refuses the rest |
| order tiles 1..N; no silence-only range; no break inside a timelapse | `loop.order_problems` (uses `loop.read_order`, was `_read_order`) | refuses the order (`_apply_order` reports the first problem) |
| no timelapse across a break of the edit's order | `loop.break_problem` | refuses the op |

Three checks only the tools enforce (the LLM path's behaviour is unchanged, by
decision):

- **keeps that touch or overlap count as one keep** against
  `director.max_keep_lines` — chaining keeps that each fit would restore the
  span the cap exists to keep dropped (it guards against keeps widening over
  talking, with no exemption). A source boundary ends a chain (different
  recordings, not one event); the problem is reported on the chain's first op
  and suggests a timelapse or asking the user.
- **a `cut` range may not overlap any other op's range** — the prompt asks for
  it; guided_edit otherwise clips the cut. Two overlapping cuts are reported
  once, on the earlier.
- **an `edit` op names speech lines only** — guided_edit reports an edit on a
  silence line unapplied.

Nothing is split or dropped silently: a problem refuses the whole edit.
Problems are `Problem(where, reason, op)`, sorted edit/plan/order first then
by op number, each naming the op (`op #3 (timelapse [60, 72])`) and why. A
test asserts the LLM parser's drop messages equal the tools' reasons for the
same ops. Annotation lines (`[silent gap: …]`) have no number, so an op on
one is unrepresentable.

`to_source()` converts with `DisplayView.to_source()` (a silence line at an
edge becomes `"n~"`), `order_segments` + `normalise` convert the order; a test
round-trips every single-segment display range, and `write` → `dump`
round-trips whole edits.

## The skill (`.apm/skills/nagare-director/`, packaged with APM)

- `SKILL.md` — when to use it, the tools, the workflow, the edit format, the
  enforced rules. Workflow: run the pipeline to `text_filter` if needed →
  `view` (every page) → plan in prose (throughline, what to cut/compress and
  where, caption moments, order and why, expected runtime) → **stop and
  confirm the plan with the user** → draft ops → `preview --edit` → report the
  meaningful numbers → iterate → `write` → tell the user to run
  `./scripts/run_pipeline.sh --from-stage guided_edit`. The user always wins.
- `references/editorial-rules.md` — the rules from `DIRECTOR_PROMPT` (the
  two-mode tighten-and-stage goal, each op's when-to-use, why no `speed`,
  keep width, silence as content, joins, order, pacing from the speech
  figure, "the playback is the viewer's") and the editorial brief.
- `references/reading-the-view.md` — brackets, silence lines, annotations,
  and how to read `preview`.

The project's own brief is not copied into the skill: `view` prints it.

### APM layout

Verified against APM 0.35.0 and its docs (microsoft.github.io/apm: *Package
types*, *Targets matrix*, *apm compile*):

- `apm.yml` at the root (`targets: [claude, copilot, codex]`,
  `includes: [.apm/]`); the skill under `.apm/skills/<name>/SKILL.md` (the
  classic APM package layout; each primitive is hoisted on install).
- **`apm install`** deploys skills: `.claude/skills/nagare-director/` for
  Claude, `.agents/skills/nagare-director/` for Copilot, Codex, Cursor and
  Gemini. The deployed copies and `apm.lock.yaml` are **committed**, so a
  fresh clone works without apm; `apm_modules/` is git-ignored (apm added
  the entry).
- **`apm compile`** only compiles *instructions* primitives into root context
  files and never overwrites a hand-authored `AGENTS.md`/`CLAUDE.md` (no
  `<!-- Generated by APM CLI -->` marker). This package has no instructions —
  agents discover skills from their skill directories, and the skill itself
  says how to use the tools — so `apm compile` writes nothing and
  `apm compile --validate` exits 1 ("No instruction files found"). The
  meaningful check is **`apm audit --ci`** (lockfile, hidden Unicode, drift of
  deployed copies vs. source).
- `make apm` = `apm install && apm audit --ci` (needs the apm CLI; not in CI).
  `tests/test_apm_skill.py` guards the same drift in CI (deployed copies
  byte-identical to `.apm/`), the frontmatter (ASCII, `name` = directory,
  description ≤ 1024 chars), and that every `director_tools.sh <cmd>` the
  skill names exists.

After editing the skill, edit `.apm/skills/nagare-director/` (never the
deployed copies) and run `make apm`.
