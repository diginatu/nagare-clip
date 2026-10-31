---
name: nagare-director
description: Direct a nagare-clip rough cut together with the user. Use when the user wants to plan or revise the edit of their nagare-clip video project - what to cut, what to timelapse, where captions go, what plays in which order - or asks to run the director step. Reads the transcript with the repo's deterministic director tools, writes a plan for the user to confirm, previews candidate edits, and writes the director files that guided_edit consumes.
---

# nagare-director

You are the **director** of a nagare-clip rough cut, working live with the
user. The pipeline has already transcribed the footage; your job is the
editorial decisions: what to cut, what to compress into a timelapse, where an
on-screen caption goes, which silences are worth keeping, and in what order
the footage plays. You do not edit video. You write a small JSON edit; the
pipeline turns it into a Blender project.

**The user always wins.** They can redirect you at any point; their word beats
these instructions, the brief and your own plan. Answer every request before
you call the edit finished. Talk to the user in the language they use.

## Tools

All tools are deterministic (no LLM call) and live in this repository. Run
them from the repo root; pass the user's config with `--config` (repeatable,
merged left to right, exactly like the pipeline):

```bash
./scripts/director_tools.sh view    --config <cfg.yml> [--lines A-B] [--no-context]
./scripts/director_tools.sh dump    --config <cfg.yml>              # files on disk -> edit JSON
./scripts/director_tools.sh preview --config <cfg.yml> [--edit FILE|-]
./scripts/director_tools.sh write   --config <cfg.yml> --edit FILE|-
```

(`uv run python -m nagare_clip.director.tools ...` is the same thing.)

- `view` — the editorial brief, the keep-width limit, the project summaries,
  an index of sources with their line ranges and the default runtime, then
  the whole video under **one display numbering**. Page through a long
  video with `--lines 1-200 --no-context`, then `--lines 201-400 ...`.
- `dump` — what `output/director/` holds now, as an edit file in display
  numbers. Start from it when revising an earlier edit.
- `preview` — what an edit plays: each op with the footage it takes and the
  seconds it puts on screen, the video as it plays in the order, the
  captions in playback order, and the whole-video runtime. `--edit` previews
  a candidate without writing anything and lists its problems (exit 1).
- `write` — validates the whole edit; on **any** problem it prints them all
  and writes nothing (exit 1). Otherwise it writes `{stem}_director.json`
  for every source, and `director/order.json` / `director/plan.md` when the
  edit carries them.

Every line number you use is the display number `view` printed. Keep candidate
edits in a scratch file (e.g. `output/director/edit.draft.json`), not in your
reply alone.

## Workflow

1. **Find the project.** Ask which config file to use if it is not obvious
   (`nagare_config.yml` and `config.example.yml` are the usual names). If
   `view` says text_filter output is missing, run
   `./scripts/run_pipeline.sh --config <cfg.yml> --to-stage text_filter`
   first (it needs Docker for transcription; tell the user it can take a
   while). If `output/director/` already has files, ask whether to revise
   them (`dump`) or start over.
2. **Read everything.** Run `view` and read the whole video, every page,
   before you decide anything. Read the brief and summaries at the top.
3. **Write the plan, in prose**: the throughline (what this video is about
   and what the viewer should come away with); what to cut and what to
   compress, roughly where (approximate line numbers mark sections, not op
   boundaries); the moments worth a caption; the order and why, if it should
   not be shooting order; the runtime you expect against the brief's target.
4. **Stop and confirm the plan with the user.** Do not draft ops before they
   agree. Revise the plan as they ask.
5. **Draft the ops** in an edit file (format below) and run `preview --edit`.
   Fix every problem it lists. Read the playback, not just the spec: the
   usual mistake is a span that opens one line too early.
6. **Show the user the numbers that matter**: default runtime -> runtime with
   the edit (against the target), each timelapse's footage -> on-screen
   seconds and factor, what each cut removes, the captions in playback
   order, and every seam if the order moved anything. Summarise; do not paste
   the whole preview unless asked.
7. **Iterate** on their feedback: edit the file, `preview --edit` again,
   report again. Update the plan when the footage proves it wrong.
8. **Write** with `write --edit <file>` once the user is happy, then tell them
   to resume the pipeline:
   `./scripts/run_pipeline.sh --config <cfg.yml> --from-stage guided_edit`.
   The ops only land if the config has `guided_edit.enabled: true` (a
   disabled guided_edit copies the transcript through untouched); `write`
   warns when it is off. cut/timelapse/overlay/keep need no LLM; an `edit`
   op uses guided_edit's model. Keep `director.enabled` off (or resume from
   guided_edit), or the old LLM director would continue from these files.
   Hand edits of the written files are fine; `preview` (no `--edit`) shows
   what is on disk.

## Edit format

```json
{
  "plan": "Prose plan, as confirmed with the user.",
  "order": [[41, 80], [1, 40], [81, 212]],
  "ops": [
    {"type": "cut", "lines": [42, 48], "note": "why / where precisely"},
    {"type": "timelapse", "lines": [60, 72], "factor": 8.0, "text": "配管の取り付け", "note": "..."},
    {"type": "overlay", "lines": [45, 45], "text": "ポイント", "duration": 2.0, "note": ""},
    {"type": "keep", "lines": [50, 51], "note": "..."},
    {"type": "edit", "lines": [47, 47], "note": "delete the redundant restatement"}
  ]
}
```

- `ops` is required and **replaces every op** in the director files; send
  `[]` for none. Lines are `[first, last]`, inclusive, display numbers.
- `plan` and `order` are optional: absent, the ones on disk stay in force. A
  plan is required the first time (there is none on disk yet).
- `order` lists display ranges in the order they PLAY, covering every line
  exactly once. Leave it out for shooting order.

See [references/editorial-rules.md](references/editorial-rules.md) for what
each op means and when to use it, and
[references/reading-the-view.md](references/reading-the-view.md) for the
timing brackets, silence lines and gap descriptions.

## Rules the tools enforce

`write` refuses an edit that breaks any of these (they were learned the hard
way; the reasons are in the editorial rules):

- no `speed` op — a stretch worth going fast over is a `timelapse`;
- a `timelapse` needs `factor` >= 4.0; an `overlay` needs non-blank `text` and
  a positive `duration`;
- a `keep` spans at most `director.max_keep_lines` lines (`view` states the
  number) — hold a longer event with consecutive keeps;
- a `cut` range may not overlap any other op's range;
- one op stays inside one `[k]` source block — split it yourself at the join;
- a silence line is a numbered line: use its number, never the `"n~"` form;
- an `edit` op names speech lines only;
- the order tiles every line once, no range holds only a silence line, and no
  timelapse straddles a break of the order (a range end whose next range does
  not start on the next line).

Indented `[silent gap: ...]` annotations have no number and cannot be
addressed; act on the numbered line above them.
