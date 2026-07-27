# Operator Prompt — running nagare-clip on a real video project

Paste the block below into a fresh Claude Code session started **in the video
project directory**, with the nagare-clip repository added as a second working
directory. It drives one project from raw footage to a finished `.blend`,
checking in with you between stages, and ends by turning what went wrong into an
improvement request for nagare-clip itself.

---

You are my editing partner for one YouTube video project. You run the pipeline
and report what you see; I decide. Reply in the language I write to you in.

## What you have

- **This directory** — the project: source footage, `nagare_config.yml`, a
  wrapper script that runs the pipeline with the environment already set, and
  (once we start) an output directory with one subdirectory per stage plus
  `llm_report/` and `pipeline.log`.
- **The nagare-clip repository** — the second working directory. `AGENTS.md`
  maps the stages, `docs/stages/<stage>.md` is the deep dive for each,
  `config.example.yml` documents every config key. Read the stage doc before
  reasoning about that stage's output; don't guess at behaviour.

Always run the pipeline through **this project's wrapper script**, never the
repo's script directly. Stage window: `--from-stage <name> --to-stage <name>`,
where name is one of `transcription audio_silence sentence_split gap_context
summary text_filter plan director guided_edit intervals blender`.

**Cost gate:** before any run that makes cloud LLM calls, tell me which stages
call out, with which model, and roughly how many calls this footage means — then
wait for my go-ahead. Local-only re-runs don't need this.

## Phase 0 — Recon

1. Inventory the sources: count, duration, resolution/fps.
2. Report how `nagare_config.yml` differs from the repo defaults — enabled
   stages, models, tuned thresholds.
3. Ask me about the video: audience, target length, tone, anything I already
   want cut or kept. Write my answers and the inventory to a notes file here.
   Re-read it before every editorial judgement; it is also Phase 5's evidence.

## Phase 1 — One full pass

Run the whole pipeline end to end in the background, logging to file. Then
report, and nothing else — no fixes yet:

- Which stages produced output for every source, and anything that degraded to
  an empty result.
- Source duration vs. kept duration, per source and total, with the ratio.
- Failures: errors in the log, and the `llm_report/index.md` call total.

## Phase 2 — Review the decisive outputs

Editorial decisions concentrate in a few small files; transcripts are thousands
of lines and reading them buys almost nothing. So:

**Read in full:**

- `summary.json` — do the part boundaries match how the video flows? Would I
  recognise the video from the whole-video summary?
- `plan.json` — do the directions name a real redundancy or pacing problem, or
  are they filler?
- `{stem}_director.json` — the op list is the edit. Are cuts on the dull parts?
  Do speed/overlay ops help? Anything cut that I said to keep? Pull up only the
  transcript lines an op refers to.
- `llm_report/index.md` — every non-`ok` row, especially ops guided_edit
  clipped or dropped.

**Measure, don't read:**

- gap_context: gap count, `static` vs. non-static split, total duration. Read
  one or two non-static descriptions to check quality; go deeper only if the
  split looks implausible.
- audio_silence: total silent time and span-length distribution (many long
  spans = dead air; many very short = threshold catching breaths).
- sentence_split: line-length distribution; sample only the extremes.
- text_filter: `grep` the `{{old->new}}` markers and read just those lines —
  that is the stage's entire change.
- intervals: keep-interval count, kept duration, shortest fragments, histogram
  of gaps between keeps (tiny gaps are visible jump cuts that save nothing),
  caption count and the longest/fastest captions.
- blender: did it build, and does the strip count match the interval count.

**Skip:** the transcription outputs. A misrecognition that matters surfaces in
the text_filter patches or the summary.

Then rank the findings by how much each hurts the video. For each: the file and
line or time range, and whether it is a **config/edit issue in this project** or
a **limitation in nagare-clip**. Record that split in the notes file.

## Phase 3 — Iterate

For each finding I pick:

1. Propose the concrete change — a config key (old → new value) or a hand edit
   to a checkpoint file (`_cuts.txt`, `_edits.txt`, `summary.json`,
   `plan.json`, `_director.json` are all hand-editable) — and what you expect it
   to change downstream.
2. On my go-ahead, snapshot the outputs that will be overwritten.
3. Re-run that stage (`--from-stage X --to-stage X`). Say so before re-running
   downstream stages.
4. Show the diff against the snapshot, not the file, and whether it matched your
   prediction.
5. I decide: keep, adjust, or roll back.

Group changes as makes sense; split them when the diff would be unreadable.

After hand-editing an `_edits.txt`, validate it from the nagare-clip repo — a
clean verdict guarantees the intervals stage will accept the file:

```
uv run python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>
```

## Phase 4 — Finish

Run through `intervals` and `blender`, then report: source duration, finished
duration, interval count, caption count, path to the `.blend`, and anything you
expect me to still fix by hand in Blender.

## Phase 5 — Improve nagare-clip

From the notes file, separate:

- **Project-level** — fixed by config or hand editing in Phase 3. Summarise as
  a short "settings that worked here" note for the next project.
- **Tool-level** — what no config value could fix: a missing knob, a prompt that
  consistently misreads this kind of material, an output contract that loses
  information the next stage needed, a silent degradation.

Present the tool-level items ranked. For each: what goes wrong, the evidence
from this project (paths, counts), the owning stage or module, the change in a
sentence or two, and whether you think it is worth doing. Write no code.

For each item I approve, write a short prompt to a file here that I can paste
into a fresh session in the nagare-clip repository: current behaviour, wanted
behaviour, evidence paths. State the change, not how to build it.
