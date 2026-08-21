# Operator Prompt — running nagare-clip on a real video project

Paste the block below into a fresh Claude Code session started **in the video
project directory**, with the nagare-clip repository added as a second working
directory. It drives one project from raw footage to a finished `.blend` and
publish material, checking in with you between stages, and ends by turning what
went wrong into improvement requests for nagare-clip itself.

The "learned the hard way" notes throughout come from earlier projects run with
this prompt; they are there so the same round trips are not repeated.

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
- **Blender, possibly over MCP.** If Blender tools are available, use them — see
  "Inspecting the .blend". They turn "the output looks wrong" from a guess into a
  measurement.

Always run the pipeline through **this project's wrapper script**, never the
repo's script directly. The stages, in order:

```
transcription  audio_silence  sentence_split  gap_context  summary
text_filter  plan  director  guided_edit  intervals  blender  publish
```

`--from-stage X --to-stage Y` sets the window. If the wrapper hardcodes its
stages, fix it to pass arguments through and to tee all output to a per-run log
file — `pipeline.log` only captures Python logging, not the `[stage]` progress
lines or Blender's own output, and you will want both.

### Cost gate

Before any run that makes cloud LLM calls, tell me which stages call out, with
which model, and roughly how many calls this footage means — then wait for my
go-ahead. Local-only re-runs don't need this. Always prefer the narrowest window
that can show the effect of a change.

Rough counts for ~90 minutes across 3 sources, so you can quote a window without
recomputing:

| stage | calls | notes |
|---|---|---|
| transcription, audio_silence, intervals, blender | 0 | Docker/ffmpeg/Python only |
| sentence_split | ~25 | scales with `window_segments` |
| gap_context | ~50 vision calls | fewer when `static_ssim` prefilters |
| summary | 1 per source + 1 | |
| text_filter | ~55 | scales with `batch_size` |
| plan | 1 | project-wide |
| director | 1 per source | |
| guided_edit | 0 for span ops | only `edit` ops call out; span ops are deterministic |
| publish | 1 | thumbnail rendering is ImageMagick, not an LLM |

So a full run is ~145 calls, `--from-stage director --to-stage publish` is about
4, and `--from-stage intervals --to-stage blender` is free.

### Hand-editable checkpoints

`_cuts.txt`, `_edits.txt`, `summary.json`, `plan.json`, `_director.json` are all
meant to be edited by hand.

**A hand edit to `guided_edit/{stem}_edits.txt` is destroyed by re-running
`guided_edit` or anything upstream of it.** After editing that file, the only
safe window is `--from-stage intervals`. Say so before proposing a re-run that
would cross it.

## Phase 0 — Recon

1. Inventory the sources: count, duration, resolution/fps.
2. Report how `nagare_config.yml` differs from the repo defaults — enabled
   stages, models, tuned thresholds — and list config keys the repo has gained
   that this project does not set yet.
3. Check the `publish` prerequisites: `magick` on PATH, and
   `publish.thumbnail.fonts` listing fonts actually installed here. The stage
   writes the thumbnail copy but cannot know what fonts exist.
4. Ask me about the video: audience, purpose, target length, tone, what happened
   in the previous episode if this is a series, anything I already want cut or
   kept.
5. Write my answers into the **`project:` section of `nagare_config.yml`** —
   that is where `summary`, `plan`, `director` and `text_filter` read it from.
   Also record them in a notes file here, with the inventory. Re-read the notes
   before every editorial judgement; they are also Phase 5's evidence.

**Wording the brief matters more than you would expect.** It is an instruction to
several LLMs, and they over-correct. Write what to *do*, not what to avoid:

- Say "roughly 70% of the footage should be dropped; compress the build-up with a
  timelapse rather than deleting it", not "don't cut the good parts".
- Never state the same preference twice in different fields. A brief that said
  "don't cut" in both `target_duration` and `tone` produced a 54-minute cut
  against a 20-30 minute target.
- Give a target length even when I say length doesn't matter much. Without one,
  `plan` returns the same vague direction for every part.

## Phase 1 — One full pass

Quote the cost, then on my go-ahead run the whole pipeline end to end in the
background, logging to file. Then report, and nothing else — no fixes yet:

- Which stages produced output for every source, and anything that degraded to an
  empty result.
- Source duration vs. kept duration, per source and total, with the ratio.
- Failures: errors in the log, and the `llm_report/index.md` call total.

Blender prints a wall of `bl_pkg` / `cattrs` / `register_class` tracebacks at
startup on some installs. That is Blender's own extension manager failing to
register, unrelated to the pipeline. Don't report it as an error; check that
`Done: N strip(s)` follows.

## Phase 2 — Review the decisive outputs

Editorial decisions concentrate in a few small files; transcripts are thousands
of lines and reading them buys almost nothing. So:

**Read in full:**

- `summary.json` — do the part boundaries match how the video flows? Would I
  recognise the video from the whole-video summary?
- `plan.json` — do the directions name a real redundancy or pacing problem, or
  are they filler? Repeated boilerplate across parts means the brief is not
  reaching it.
- `{stem}_director.json` — the op list is the edit. Ops are `cut`, `timelapse`,
  `overlay`, `keep`, `edit`. Are cuts on the dull parts? Anything cut that I said
  to keep? Pull up only the transcript lines an op refers to.
- `llm_report/index.md` — every non-`ok` row.
- `publish/publish.md` — titles, description, chapters, thumbnail copy and stills.

**Measure, don't read:**

- **Playback-rate breakdown.** What share of the finished timeline runs at 1x and
  what share under a `timelapse`. The single most informative number about
  pacing. A sustained mild fast-forward (1.3-2.0x) over a large share of the
  video is a failure mode this pipeline hits repeatedly: it reads as neither
  "listen to this" nor "watch this go by".
- **Timelapse factors vs on-screen length.** Each should land around 1-2 minutes
  on screen. All sharing one factor means the director anchored on the example
  instead of judging.
- **`keep` op widths.** A narrow keep rescues one silent gap (`[N, N+1]`); a wide
  one holds a continuous event together. Every keep being exactly two lines means
  the narrow form became the only form.
- **Overlay count and durations.** Roughly one per 3-5 minutes unless the brief
  says otherwise. Durations should track reading length rather than cluster on
  one value, and none should survive more than a few seconds.
- **intervals**: keep-interval count, kept duration, shortest fragments,
  histogram of gaps between keeps (tiny gaps are visible jump cuts that save
  nothing — `intervals.min_cut` absorbs them), caption count, and captions faster
  than ~18 characters per second.
- **gap_context**: gap count, `static` vs non-static split, total duration. Read
  one or two non-static descriptions; go deeper only if the split looks
  implausible.
- **audio_silence**: total silent time and span-length distribution.
- **sentence_split**: line-length distribution; sample only the extremes.
- **text_filter**: `grep` the `{{old->new}}` markers and read just those lines —
  that is the stage's entire change.
- **blender**: did it build, and does the strip count look right against the
  interval count. A timelapse collapses many intervals into one strip, so the two
  no longer match one-to-one.

**Skip:** the transcription outputs. A misrecognition that matters surfaces in
the text_filter patches or the summary.

Then rank the findings by how much each hurts the video. For each: the file and
line or time range, and whether it is a **config/edit issue in this project** or a
**limitation in nagare-clip**. Record that split in the notes file.

**I will also watch the video.** My verdict on which parts feel weak is evidence
you cannot derive from the JSON — ask for it, and when it disagrees with your
reading of the numbers, go and find the mechanism that explains it.

## Phase 3 — Iterate

For each finding I pick:

1. Propose the concrete change — a config key (old → new value) or a hand edit to
   a checkpoint — and **what you expect it to change downstream, as a number**.
2. On my go-ahead, snapshot the outputs that will be overwritten.
3. Re-run the narrowest window that shows the effect.
4. Show the diff against the snapshot, not the file, and whether it matched your
   prediction. **Say plainly when it did not.** A missed prediction is the most
   useful signal available — more than once the miss has located the real
   mechanism, nowhere near where the change was aimed.
5. I decide: keep, adjust, or roll back.

Keep a small table in the notes of the key metrics per run — finished length, op
mix, playback-rate breakdown. Prompt changes regress as easily as they improve,
and the table is how you notice.

After hand-editing an `_edits.txt`, validate it from the nagare-clip repo — a
clean verdict guarantees the intervals stage will accept the file:

```
uv run python -m nagare_clip.intervals.check_edits --edits-txt <file> --json <file>
```

## Phase 4 — Finish

Run through `intervals`, `blender` and `publish`, then report: source duration,
finished duration, interval count, caption count, path to the `.blend`, the
publish material, and anything you expect me to still fix by hand in Blender.

## Inspecting the .blend

Useful from Phase 2 onward, whenever the output looks wrong. With Blender MCP you
can read the sequencer directly, which settles most questions in one call:

- Compare every strip's actual `frame_final_start` / `frame_final_duration`
  against what the intervals JSON predicts. Rebuild the prediction by replicating
  `split_intervals_by_speed` and the cursor arithmetic.
- Check strips sit on their intended channels — video 1, sound 2, speed badge 3,
  caption 4, overlay 5. A strip pushed onto a neighbouring channel means an
  overlap Blender resolved silently, and nothing logs it.
- Caption strips are named `cap_{source_seconds}`, so you can confirm the caption
  showing at a timeline frame matches the source moment the picture shows there.
- Render a single frame and compare it against an `ffmpeg`-extracted still from
  the source at the expected timestamp. This is the decisive test for "is the
  picture where we think it is".

## Phase 5 — Improve nagare-clip

From the notes file, separate:

- **Project-level** — fixed by config or hand editing in Phase 3. Summarise as a
  short "settings that worked here" note for the next project.
- **Tool-level** — what no config value could fix: a missing knob, a prompt that
  consistently misreads this kind of material, an output contract that loses
  information the next stage needed, a silent degradation.

Present the tool-level items ranked. For each: what goes wrong, the evidence from
this project (paths, counts), the owning stage or module, the change in a
sentence or two, and whether you think it is worth doing. Write no code.

For each item I approve, write a short prompt to its own file here that I can
paste into a fresh session in the nagare-clip repository. Structure each as
**current behaviour → evidence → wanted behaviour**. State the change, not how to
build it. Quote the actual prompt text or code you are asking to change, and give
real paths and counts — a request without evidence gets implemented as written,
and if you were wrong about the mechanism, it lands wrong.

### Verify before you assert

State a mechanism only after checking it in the code or the data. Earlier
projects produced three confident explanations that were all wrong: that `keep`
could not protect silence inside a line, that a config threshold could rescue a
gap the vision LLM had misjudged, and that rounding drift explained an
audio/caption desync. Each cost a round trip, and one nearly produced an
improvement request aimed at the wrong stage.

The desync is the cautionary tale: the captions turned out to be placed
correctly and merely compressed to 0.22 seconds each inside an 8x timelapse.
Unreadable is not the same as misplaced, and only measurement told them apart.
Read the function, or measure the output, before you name a cause.

### How these requests tend to go wrong

**Concrete numbers in a prompt anchor harder than the instructions around them.**
Seen four times: `normally ([N, N+1])` made every keep exactly two lines; a
`"duration": 2.0` example clustered every overlay on 2-4 seconds; `factor 4.0 or
more` with a `4.0` example produced 4.0 everywhere; and a constraint written with
a number was obeyed while the one beside it written without a number was ignored
entirely. To make a value vary, tie it to a property the model must look at, and
show two or three worked examples — never one.

**Making the right option harder to choose pushes the model to the easier one.**
Adding conditions to the `timelapse` op made the director retreat to plain
`speed`; the fix was removing `speed` from its menu, not tightening its wording
further. When a mode keeps being avoided, check what it costs to choose before
adding more rules to the alternative.
