# nagare-clip

Semi-automated video editing pipeline for long-form recordings on Linux.

The pipeline creates a rough-cut Blender project for human review and fine-tuning.

## Pipeline Stages

1. audio_silence: Silero VAD v6 speech regions (always on) -> `{stem}_vad.json` and the `_cuts.txt` editable cut list (the silences between the regions)
2. transcription: Haas-ja-v1.0 on chunks packed from those speech regions -> `{stem}.json` (WhisperX-shaped word timings) + `{stem}.txt`
2b. whisper (optional): faster-whisper large-v3 re-transcribes the same chunks in the `whisperx` Docker image -> `output/whisper/{stem}_whisper.json`; used only as per-line reference hints for text_filter's LLM (Haas stays the transcript); disabled by default
3. sentence_split (optional): LLM splits each transcript line longer than `max_line_seconds` (default 8 s) at sentence boundaries -> `output/sentence_split/{stem}.json` + `{stem}.txt`; short lines are kept, lines are never merged; disabled by default (byte-identical copy-through)
4. gap_context (optional): a vision LLM snapshots+describes long silent gaps (from `_cuts.txt`) so summary/director can see what the transcript can't -> reviewable `output/gap_context/{stem}_gaps.json` + JPEG frames; disabled by default (no-op)
5. summary (optional, project-wide): a larger LLM segments every video into line-range parts + summaries, a whole-video summary, and misspelling-prone keywords, and writes one all-videos summary -> reviewable `output/summary/summary.json`; the per-video summary and keywords are used by text_filter/director
6. text_filter: Text editing checkpoint -> `_edits.txt` (copy of `.txt`, or LLM-corrected with `{{old->new}}` markers), optionally primed with summary-stage context
7. director: a **checkpoint** — you direct the cut with a coding agent through the `nagare-director` skill, which writes `output/director/` (`plan.md`, `order.json`, `{stem}_director.json`); a run always stops here, and you resume with `--from-stage guided_edit`
8. guided_edit: applies the director's ops into `_edits.txt` deterministically (no LLM)
9. intervals: Patch application + keep intervals -> `*_intervals.json` keep ranges (audio cuts unioned in), plus `output/intervals/timeline.json` — the order in seconds
10. blender: Blender headless -> `.blend` with VSE strips arranged back-to-back, in the order `timeline.json` gives
11. publish (optional, project-wide): title candidates, a description with chapter timestamps taken from the finished timeline, thumbnail copy and candidate stills -> reviewable `output/publish/publish.md` + `publish.json`

Stages are referenced by name (`--from-stage <name>`); the sentence_split/gap_context/summary/publish stages are no-ops unless enabled in config. A run always stops at the director (exit 0); resume with `--from-stage guided_edit` once the director files are written — see *Directing with a coding agent* below.

## Human Editing Workflow

1. Run the audio_silence, transcription and text_filter stages: `./scripts/run_pipeline.sh`
2. Edit `output/audio_silence/{stem}_cuts.txt` — delete a line to keep that span, or adjust the `START - END` times
3. Edit `output/text_filter/{stem}_edits.txt` — add/modify `{{old->new}}` patch markers, wrap text in `<keep>...</keep>` to force-keep its audio, wrap text in `<speed factor="N.N">...</speed>` to play that region at the given speed in Blender (does not force-keep audio — nest inside `<keep>` to also keep it), insert `<overlay text="..." duration="N.N"/>` to display an on-screen TEXT strip for that many seconds (does not affect audio), or wrap text in `<cut>...</cut>` to delete it (larger deletions drop out of the timeline via silence detection)
4. Resume: `./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4`

The `{{old->new}}` syntax replaces `old` with `new` in the transcript. Use `{{delete->}}` to remove text, `{{->insert}}` to insert text.

The `director` checkpoint and `guided_edit` automate steps like the above: you direct the cut with a coding agent (below), it writes edits (cut/timelapse/overlay/keep/edit) into a reviewable `output/director/{stem}_director.json`, and guided_edit applies them into `_edits.txt` with no LLM call — any op it cannot apply cleanly is logged. A `timelapse` op names a range, a factor of 4.0+ and a caption, and guided_edit expands it into a `<keep><speed>` span with an `<overlay/>` that lasts the whole timelapse — so the three markers cannot disagree, which is what produced sped-up jump cuts when they were three separate ops. There is deliberately **no mild `speed` op** for the director: compressing a stretch is a two-mode choice — either it is work worth watching fast (`timelapse`) or material worth cutting. You can still write a `<speed factor="N.N">` tag into `_edits.txt` yourself (see below), and a `speed` op you hand-write into `_director.json` is still applied. Span ops (cut/speed/overlay/keep) are a whole-line-range wrap; if a span op's range overlaps a same-type tag already in the file (e.g. one you hand-authored) or an earlier op, it is clipped to the free lines (or dropped if fully covered) so the tags stay valid. An `edit` op states its change exactly — `{"type": "edit", "lines": [n, n], "old": "<text as view shows it>", "new": "<replacement>"}` (`""` deletes) — and is applied as one `{{old->new}}` patch on that line.

A director `keep` op protects a span from cutting **including its silences**, and its width follows what is on screen: the narrowest range to rescue one silent gap, or the whole run of lines when a continuous event is playing out — an accident and the cleanup after it, a demo running — since chopping such a sequence into per-gap keeps reads as jump cuts through the payoff. What it is *not* is a way to mark a long span of **talking** as important: speech is never dropped by default, so a keep over talking only restores its pauses. `director.max_keep_lines` (default `8`, `0` = no limit) is the cap the skill's `write` enforces, counting keeps that touch as one keep; a `keep` you write by hand into `{stem}_director.json` is always honoured. A fast span that needs a keep of its own belongs in a `timelapse` op instead.

The optional `summary` stage runs **once over all source videos** (project-wide) and gives downstream stages cross-video context. Enable `summary.enabled` in config: it runs before `text_filter`, segments each sentence_split transcript into line-range parts with a summary each, lists per-video misspelling-prone keywords, generates a whole-video summary per video, and writes one all-videos summary to `output/summary/summary.json` (`{summary, parts, keywords, video_summaries}`) — its per-video summary/part summaries/keywords also prime the `text_filter` LLM's prompt, the director's `view` shows the overall and per-video summaries as facts, and `publish` builds its chapters from the parts.

#### The order of the finished video

By default the finished video is the source clips concatenated in name order.
The **director** decides the order: its edit may carry an `order` — the ranges
of the transcript in the order they should play, down to a single line. An edit
without one is shooting order. The order is written to
`output/director/order.json` as a list of *segments* (one stretch of one source)
in playback order:

```json
{"order": [
  {"stem": "device_1"},
  {"stem": "mixed_clip", "lines": [31, "83~"]},
  {"stem": "mixed_clip", "lines": [1, 30]}
]}
```

`lines` is optional (omit it for a whole source), and a source may appear more
than once. `"83~"` means the segment ends on the silence *after* line 83, so that
silence plays at the end of this segment instead of at the start of whatever
plays line 84. The one rule is that the segments **cover every line of every
source exactly once** — dropping footage is `cut`'s job, so an order that leaves
lines out is rejected whole and the pipeline falls back to shooting order. The
director's `write` also refuses an order that would split one of its timelapses in two.

You can hand-edit `director/order.json` and resume with `--from-stage intervals`;
it is validated on every run.

A reorder is never silent: whenever the finished video is not in shooting order
(or an order was rejected), `output/llm_report/notes/order.md` says so — which
segment plays where, and where it was before.

#### Directing with a coding agent (the `nagare-director` skill)

The director is an **agent skill**: you direct the cut together with a coding agent (Claude Code, GitHub Copilot, Codex, Cursor, Gemini, …) in its own chat, and it writes the `output/director/` files. A run that reaches the `director` stage **always stops** there (exit 0): either no director files exist yet, or the stages before it just re-ran and the files were made for the old transcripts. Direct (or revise: `dump`, then `write`) with the skill, then resume with `--from-stage guided_edit`. Resuming checks the files: `director/order.json` plus a `{stem}_director.json` for every source must exist, and anything that no longer maps onto the transcripts fails the run with a list and the same advice. When it passes, guided_edit prints how many ops each source has and whether the order moved, so an empty edit is never silent.

0. Install the skill into your **video directory** once, with [APM](https://github.com/microsoft/apm). The skills for editing a video are one APM package in this repo's `skills/` directory; it is never deployed into the nagare-clip repo itself:

   ```bash
   cd <your video directory>
   apm install <nagare-clip checkout>/skills --target claude,codex          # recommended: local checkout
   apm install diginatu/nagare-clip/skills#main --target claude,codex       # alternative: from GitHub (or #<tag>)
   ```

   Prefer the local checkout: the skill text and the scripts it calls are then the same version. Either form records the dependency in the video directory's `apm.yml`, so a later bare `apm install` reproduces it. It deploys to `.claude/skills/` (Claude Code) and `.agents/skills/` (Copilot, Codex, Cursor, Gemini).
1. Run the pipeline up to the text checkpoint, from the video directory: your project's wrapper script, or `<nagare-clip>/scripts/run_pipeline.sh --config <yml> --to-stage text_filter`.
2. In the video directory, ask your agent to direct the video (for example "nagare-director で編集して"). It calls `<nagare-clip>/scripts/director_tools.sh` with the same `--config` files the pipeline uses, and asks you for the checkout's path if it cannot find it. The agent reads the whole video, writes a plan, **asks you to confirm it**, drafts the edits, shows you the runtime/timelapse/caption numbers from a preview, and changes them as you ask. You can redirect it at any time.
3. When you are happy, it writes the files and you resume from guided_edit (your wrapper, or `<nagare-clip>/scripts/run_pipeline.sh --config <yml> --from-stage guided_edit`). guided_edit applies every op with no LLM call.

The agent uses deterministic tools you can also run yourself (no LLM call):

```bash
./scripts/director_tools.sh view    --config <yml> [--lines 1-200] [--no-context]  # the numbered video + brief + summaries
./scripts/director_tools.sh dump    --config <yml> > edit.json   # the current director files, as an edit
./scripts/director_tools.sh preview --config <yml> [--edit edit.json]  # what it will play; writes nothing
./scripts/director_tools.sh preview --config <yml> --summary [--min-run 20] [--edit edit.json]  # the same in a screenful
./scripts/director_tools.sh preview --config <yml> --lines 400-520 [--source 3] [--edit edit.json]  # detail for part of the video only
./scripts/director_tools.sh transcript --config <yml> [--edit edit.json] [--source 3]  # the transcript as the finished video plays it
./scripts/director_tools.sh write   --config <yml> --edit edit.json    # validate, then write — all or nothing
```

An edit is `{"plan": "...", "order": [[a, b], ...], "ops": [...]}` in the line numbers `view` prints. It is the complete state: `ops` replaces every op and an edit without `order` is shooting order (`order.json` is always written), so revise from `dump` to keep a reorder; without `plan`, `plan.md` stays. `write` checks no `speed` op, timelapse factor ≥ 4.0, overlay text + duration, the keep-width cap (touching keeps count as one), one source per op, that a `cut` does not overlap another op, that an `edit` op names one speech line whose text holds its `old` exactly once outside any marker or patch, and that the order covers every line once and never splits a timelapse — and lists **every** problem at once without writing anything. Hand edits of the written files are fine; `preview` (no `--edit`) shows what is on disk. Maintainers: the skill's source is `skills/.apm/skills/nagare-director/` (package manifest `skills/apm.yml`); after editing it, run `make apm`, which smoke-installs the package into a temp video directory and runs `apm audit --ci` there. Details: [`docs/stages/director_skill.md`](docs/stages/director_skill.md).

To help reason about pacing, the director's `view` also shows **calculated durations and in-between gaps**: each line carries `[4.2s, gap 0.8s]` (per-sentence duration + gap to the next line; the skill's `references/reading-the-view.md` explains the notation). These times come from the sentence_split `{stem}.json`. The view splits that duration into what plays vs. what the `intervals` stage will drop, e.g. `[12.9s speech, 62.9s silence]` — computed live with the same keep-interval code the `intervals` stage runs (word gaps over `intervals.silence_threshold`, the audio_silence `{stem}_cuts.txt`, keep margins, captions, `min_cut`), so the two figures add up to the line's footage and the speech figure is exactly what plays with no op. It falls back to the plain `[Ns, gap Ns]` form when nothing inside the span is dropped. To run a single stage on its own (reusing its inputs from a previous pipeline run), use `./scripts/run_pipeline.sh --from-stage X --to-stage X`.

Before resuming, you can validate your edits in one pass (reports **every** problem at once, with line numbers, instead of failing on the first like the intervals stage does):

```bash
uv run python -m nagare_clip.intervals.check_edits \
  --edits-txt output/text_filter/myvideo_edits.txt \
  --json output/transcription/myvideo.json
```

It checks line-count vs. JSON segments (silence lines not counted — see below), the silence lines themselves, `{{old->new}}` patch syntax (empty `{{old->}}` deletions are allowed), decomposition integrity against the original transcript, `<keep>`/`<speed>`/`<cut>` tag balance, `<overlay/>` marker attributes, and well-formedness; when everything passes, it additionally dry-runs the real intervals-stage sync so a "no problems found" verdict guarantees the intervals stage won't reject the file either. Exit code is non-zero when any problem is found.

`guided_edit/{stem}_edits.txt` also carries **silence lines**: between two lines of speech, wherever the wait is at least `director.silence_line_min` (default 5.0s), a line like `[silent 29.9s: a hand enters from the right]` — the same line the director read. The wait before the first line gets one too (the **leading** silence line, written above line 1), and a video with no speech at all is a single silence line covering the whole video — so it can be kept (`<keep>[silent 42.5s: …]</keep>`) or time-lapsed like any other wait; by default it is dropped. Its length comes from the media duration audio_silence records in `_cuts.txt` (a `# media duration:` comment); without it (a cut list from before this was recorded) such a video has no line and plays nothing. It is where an edit of that wait lives: `<keep>[silent 29.9s: …]</keep>` keeps the wait, `<keep><speed factor="8.0">[silent 29.9s: …]</speed></keep>` turns it into a timelapse, `<cut>` over it drops it, and a tag that opens (or closes) on a silence line starts (or ends) exactly where that wait does. Silence lines are generated — you may add or remove tags on them and edit their description, but deleting, moving or duplicating one is an error (`check_edits` names the line; pass `--silence-line-min` if you changed that setting). text_filter's `_edits.txt` has none. For a video with no speech, pass `--cuts-txt output/audio_silence/{stem}_cuts.txt` to `check_edits` so it knows where the video ends.

The `<keep>...</keep>` tag preserves the audio under the wrapped text — the intervals stage carves that time range out of both the word-gap silence detection and any overlapping `_cuts.txt` ranges, so dramatic pauses and intentional silences survive. The tag may be opened on one line and closed on a later one, so a single `<keep>` block can span multiple lines and preserve the silences between them. The tag is added by the human (not the LLM) after `_edits.txt` is produced.

The `<speed factor="N.N">...</speed>` tag instructs the blender stage to play the wrapped region at the given playback speed (e.g., `factor="2.0"` for 2× fast-forward, `factor="0.5"` for slow-motion) via a Blender VSE Speed Control effect strip. Unlike `<keep>`, **it does not force-keep audio** — silence inside a `<speed>` span is still cut by silence detection, and the speed only applies to the surviving spoken parts. To preserve the audio **and** speed it up, nest the tags: `<keep><speed factor="2.0">…</speed></keep>`. Captions inside the region are timed against the sped-up timeline so they stay in sync. The blender stage also automatically renders a small top-right badge (e.g. `x2.0`) on-screen over every `<speed>` region — disable via `blender.speed_mark.enabled: false`, restyle via `blender.speed_mark.*`, or change the wording via `blender.speed_mark.template` (the `{factor}` placeholder is rendered to one decimal place).

The `<overlay text="..." duration="3.0"/>` marker places an on-screen TEXT strip in Blender. It is self-closing — there is no `</overlay>` — so where you put it is where the text appears, and `duration` says how many seconds it stays up. Those seconds are counted on the **finished** timeline, so cuts and `<speed>` regions falling inside the window never eat into the time a viewer gets to read it, and there is no upper limit. Like `<speed>` (and unlike `<keep>`), **it does not force-keep audio** — the words it sits on can be cut by silence detection. When that happens the caption is not lost: it moves to the first surviving moment of the line it sits on (its `duration` is unchanged, so it may run on over what follows). Only when *nothing* of that line survives is the overlay skipped, with a warning — there is then no timeline content to display it over. Quotes inside the `text="..."` attribute value are not supported, and the attributes must be in the order `text`, `duration`. For a caption on **several on-screen lines**, write the break as the two characters `\n` inside the attribute (`text="1行目\n2行目"`) — the marker itself must stay on one line of `_edits.txt`, since line N of that file maps to segment N of the transcript. A literal backslash is written `\\`. Resume with `./scripts/run_pipeline.sh --from-stage intervals` to apply. The text you write is automatically spaced at natural phrase boundaries before it reaches Blender, so a long caption has somewhere to wrap.

The `<cut>...</cut>` tag deletes the wrapped text. It is a shorthand for `{{wrapped->}}` deletion patches (and can span multiple lines — open on the first, close on the last), so use it to drop whole sentences or sections. There is no separate "cut this time range" mechanism: removing the words opens a gap between the surviving neighbours that the word-gap silence detection cuts from the timeline, so `<cut>` is meant for **larger** deletions (a span shorter than the silence threshold may not actually be cut). Because the text is deleted, no caption is shown for it. Do not overlap `<cut>` with `<keep>`/`<speed>` on the same span, or let it swallow an `<overlay/>` marker.

### The output index (`output/index.md`)

Every invocation of the pipeline — whatever stage range you asked for, and even
one that failed partway — rewrites one page at the top of the output directory.
It names the `.blend` (the deliverable is otherwise mentioned in no file at
all), links the files written to be read by a human, states
the finished cut in one line, and embeds the rendered thumbnails. The rows
follow the pipeline's flow: `director/plan.md`, the `.blend`,
`publish/publish.md`, `render/render.md`, and last `llm_report/index.md`,
which covers the whole run's LLM calls rather than one stage. The cut report
has no row of its own: `llm_report/index.md` already embeds it, and the
headline line already states its source → finished minutes. It costs no
LLM call at any setting and honours `general.image_markup` like the other two
reviewable files.

Each embedded thumbnail's **alt text** carries that set's hook, taken from
`publish.json` (`Set 1 — まさかの水漏れ、また発覚`). It is in the alt rather than
in a caption line under the picture because the copy is burned into the image:
you can already read it, so a visible line would only repeat itself at you. The
alt costs you nothing and is the only description that reaches a screen reader
or a model reading the file. A set whose copy has no hook keeps the bare label.

Each row carries the file's mtime, or `—` when the file is not there. A row
never explains an absence: a timestamp is a fact, while a guess at *why*
something is missing, written by something that cannot see your run, is worse
than a dash.

### Publishing material (`output/publish/`)

The optional `publish` stage runs **after** Blender and writes what you would
otherwise retype by hand for every upload. Enable `publish.enabled` in config;
it writes files, you upload.

`output/publish/publish.md` is the reviewable file:

- **Title candidates** — several, not one. Hook quality varies a lot between
  attempts and picking from a list is cheap.
- **Description** — a short lead written from the whole-video summary and your
  `project:` brief, followed by the chapter list, ready to paste.
- **Thumbnail copy** — alternative sets of one to three lines, each tagged
  `tag` / `hook` / `subtitle`. The count is never padded to fill a template:
  a punchier video may want just a hook.
- **Thumbnail frame candidates** — a table of stills extracted at the moments
  the director marked as payoffs (`overlay` captions, `keep` events, and both
  boundaries of a `timelapse`), so you pick from a shortlist instead of
  scrubbing the timeline. The JPEGs are under `output/publish/frames/`.

Images are embedded as sized `<img>` tags by default. If you read these files
in a viewer that strips raw HTML, set `general.image_markup: markdown` and they
become plain `![alt](path)` images instead (markdown has no width syntax, so
they render full width). One key, read by both `publish.md` and `render.md` —
it describes your viewer, not a stage.

`output/publish/publish.json` holds the same material as data, and is the
**hand-editable contract** the `render` stage below reads — like
`_director.json` is for the edit. `publish` itself composites nothing.

**How a headline meets its picture.** `publish` makes two text calls where it
used to make one. The **copy** call writes the hooks from the story and is shown
no frames at all — two dozen frame descriptions in front of it and it starts
captioning the photographs instead. The **pairing** call then takes those hooks
and the frame *descriptions* (never the images) and decides, per set, which
frame it goes on and where and in what colour the text sits. It names its frame
by index, and `publish` writes the resolved **path** into `publish.json`, which
is what you then edit. Turn it off with `publish.pairing.enabled: false` and
every set falls back to the built-in presets.

**Looking at the frames.** With `publish.describe_frames.enabled` (and a
vision-capable model configured), each candidate still is described once — what
is actually legible in it, where the subject sits, which regions are empty and
what colour they are — into `output/publish/frames.json`. A label from the
director says what it *thought* was happening at that moment; only looking says
what a viewer can make out. Descriptions are cached by a content hash of the
JPEG itself, so re-running `publish` for better copy over an unchanged shortlist
costs **no** vision calls, a re-extracted frame whose picture changed is
described again, and a description you rewrite by hand in `frames.json` is the
description from then on.

### Rendered thumbnails (`output/render/`)

`render` is the last stage, and the only one that **never makes an LLM call
under any circumstances**. It reads `output/publish/publish.json` and
composites one real thumbnail per copy set into
`output/render/thumbnails/set{N}.jpg`, with `output/render/render.md` as a
contact sheet embedding the finished images beside the copy they carry.

Each set names **its own background** and carries its own *look* — font,
colours, outline, block position — so the sets you see are real options rather
than several wordings of one image. Both are decided by `publish`'s **pairing**
call, in ImageMagick's own vocabulary;
the pipeline validates every value and builds every `magick` command as an
argument list (never a shell string). This needs `magick` (ImageMagick) on
`PATH`; set `render.enabled: false` to keep the copy but skip compositing.

**Set `render.fonts` if your copy is not plain ASCII.** ImageMagick's default
face draws nothing at all — not even a box — for a character it has no glyph
for, so a Japanese headline comes back invisible. The first slot you list is
also the face any line falls back to when the model named none, so put a font
that covers your language first. `render.md` says so at the top when it applies.

The split exists for one loop — look at a render, change one thing, look
again:

```bash
$EDITOR output/publish/publish.json          # retype a hook, swap a colour
./scripts/run_pipeline.sh --from-stage render --to-stage render
```

That costs **zero** LLM calls, every time. See
[`docs/stages/render.md`](docs/stages/render.md) for the full style-key table
and escaping rules.

**Chapter timestamps come from the finished timeline**, not from the source: a
part that was cut entirely drops out of the list, and a part inside a timelapse
gets its compressed position. YouTube only turns a timestamp list into chapters
when the first entry is exactly `0:00`, there are at least three, they ascend,
and each runs at least 10 seconds — so the stage forces the first entry to
`0:00` (your opening is almost always partly cut) and merges any chapter that
lands under 10 seconds into a neighbour. The list is written either way: YouTube
auto-links timestamps regardless, so a list of two still lets a viewer jump — it
just does not draw the segmented progress bar. When the conditions are not met,
`publish.md` says so and why.

### LLM report (`output/llm_report/`)

Every LLM stage (`sentence_split`, `gap_context`, `text_filter`, `summary`, `publish`)
writes a per-call record under `output/llm_report/`: an `index.md` table
(stage, unit, attempts, outcome, reason) linking to per-call detail files under
`<stage>/<unit>.md` that hold the full prompt and raw response for every attempt,
including retries. Outcomes: `ok`, `ok-empty`, `llm-error`, `unparseable`,
`verify-fail`, `dropped-items` (a call that parsed but discarded some items).
Re-running a stage refreshes only that stage's section. Toggle with
`general.llm_report` (default `true`) and relocate with `general.llm_report_dir`.
Deterministic findings that cost no LLM call live in `notes/*.md` and are inlined
at the bottom of `index.md` — currently the order note (see above) and the
finished-cut report (below).

### Finished-cut report (`cut_report`)

After `intervals` (and again after `blender`, which adds Blender's own
warnings), the pipeline measures the cut it just produced and writes a
`## finished cut` section into `output/llm_report/index.md`. **No LLM call.**

Two kinds of content. The **measurements always print**, breach or no breach —
finished duration, the 1x / timelapse split, keep-interval, strip, caption and
overlay counts, overlay density per finished minute, and one summary line each
for keep-interval gaps and fragments. That is the per-run regression table you
would otherwise keep by hand, and it is how you notice a prompt change that
regressed.

The **findings print only on a breach**, each naming the threshold it breached:

| finding | what it means | threshold |
|---|---|---|
| `caption-compressed` | a caption already too dense to read, *and* inside a `speed_range` that divides its screen time again | `cut_report.caption_chars_per_sec` (18.0) |
| `caption-fast` | the same density outside any speed range | same |
| `timelapse-long` | a timelapse left on screen far past the skill's "about a minute" ceiling — a factor picked too low leaves a sustained fast-forward | `cut_report.timelapse_max_screen` (180.0s) |
| `keep-gap` | a cut too short to be worth the jump | `intervals.min_cut` |
| `keep-fragment` | a keep interval too short to register | `cut_report.min_keep_fragment` (1.0s) |
| `blender-warning` | one of Blender's clamp/overlap notices, lifted out of its startup noise |  |

There is deliberately **no floor** on a timelapse's on-screen time: a short
fast-forward is ordinary, so how long each one plays for is printed in the
on-screen table as a measurement rather than flagged.

It deliberately does **not** compare against `project.target_duration` or the
brief's caption-density sentence: those are free text, and a wrong parse would
be worse than none. The numbers are stated; you compare.

Turn it off with `cut_report.enabled: false`. See
[`docs/stages/cut_report.md`](docs/stages/cut_report.md).

### Langfuse tracing (optional)

Every LLM call can be traced to [Langfuse](https://langfuse.com) via LiteLLM's
`langfuse_otel` OTEL callback. Tracing is **off by default** and activates only
when both `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are set in the
environment.

```bash
export LANGFUSE_PUBLIC_KEY="pk-lf-..."
export LANGFUSE_SECRET_KEY="sk-lf-..."
./scripts/run_pipeline.sh
```

Set `LANGFUSE_OTEL_HOST` to select a region or self-hosted endpoint (default: US
cloud; EU cloud: `https://cloud.langfuse.com`).

To disable tracing even when keys are present, set `general.langfuse: false` in
your config file, or export `NAGARE_LANGFUSE=0` before running the pipeline.
`run_pipeline.sh` (via `python -m nagare_clip.pipeline`) maps the config flag to
`NAGARE_LANGFUSE` automatically, once, for the whole run. Note: `call_llm` reads
only the env var, so `general.langfuse: false` takes effect only when run
through the pipeline CLI.

Traces are grouped by pipeline run (`session_id` = one timestamp per
`run_pipeline.sh` invocation, exported as `NAGARE_RUN_ID`), by stage
(`tags: ["stage:<name>"]`), and by source file (`tags: ["stem:<stem>"]`), so
you can filter by any dimension in the Langfuse UI.

The existing markdown LLM report (`output/llm_report/`) continues to run
alongside Langfuse — the two are independent sinks.

## Requirements

- Linux
- NVIDIA GPU (CUDA) for the `transcription` stage (Haas-ja ASR); the VAD in `audio_silence` runs on CPU
- `ffmpeg` on the host `PATH` (the `transcribe/` worker decodes audio with it, and the `blender` stage builds the VSE proxies with it — decoding on the NVIDIA GPU when it can, otherwise on the CPU)
- Docker + Docker Compose with the NVIDIA Container Toolkit (used by `gap_context` and `publish` for frame snapshots, and by `whisper` for faster-whisper on the GPU)
- Blender available as `blender`
- ImageMagick available as `magick` (only needed for `render.enabled`, i.e. when the `render` stage composites thumbnails)
- Python 3.11+

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv sync          # install runtime deps
uv sync --dev    # install runtime + dev deps (includes pytest)
uv sync --project transcribe   # the Haas ASR + Silero VAD worker environment (torch, NeMo; large)
```

The `transcribe/` subproject is a separate uv environment that holds the heavy
dependencies; the main package never imports it and the pipeline runs it as a
subprocess (`uv run --project transcribe python -m nagare_transcribe {vad,asr}`).
The first `transcription` run downloads the Haas-ja-v1.0 model from Hugging Face.
See [`transcribe/README.md`](transcribe/README.md) and
[`docs/stages/transcription.md`](docs/stages/transcription.md).

Run tests:

```bash
uv run pytest
```

## Quick Start

1. Put source media in `src_video/` (default input directory).
2. Run the full pipeline — processes all videos in `src_video/` alphabetically:

```bash
./scripts/run_pipeline.sh
```

The first run stops at the director (exit 0) and says so: direct the cut with
the `nagare-director` skill (see *Directing with a coding agent*), then resume
with `./scripts/run_pipeline.sh --from-stage guided_edit`. Any run that starts
before the director stops there again, because the stages it re-ran may have
changed what the director files refer to; revise them with the skill and
resume from guided_edit.

Or target a single file with `--source`:

```bash
./scripts/run_pipeline.sh --source myvideo.mp4
```

Pass custom locations with options:

```bash
./scripts/run_pipeline.sh --input-videos-dir my_videos --output-dir my_out
```

Use a YAML config file to tune pipeline parameters (see `config.example.yml`):

```bash
./scripts/run_pipeline.sh --config my_project.yml
```

Re-run from a specific stage (skip expensive earlier stages when iterating on config):

```bash
# Run everything (audio_silence is the first stage, so this is also the default)
./scripts/run_pipeline.sh --from-stage audio_silence --source myvideo.mp4

# Skip through text_filter, apply silence cuts + text edits and regenerate intervals + Blender project
./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4

# Skip through intervals, only regenerate the Blender project
./scripts/run_pipeline.sh --from-stage blender --source myvideo.mp4
```

This produces outputs under `output/` (or your `--output-dir`), including:

- `transcription/myvideo.json`, `transcription/myvideo.txt`
- `audio_silence/myvideo_cuts.txt`
- `text_filter/myvideo_edits.txt`
- `intervals/myvideo_intervals.json`
- `blender/myvideo_edited.blend` (named after the first source file)
- `publish/publish.md` + `publish/publish.json` (when `publish.enabled`)

## Configuration

All pipeline parameters can be controlled via a YAML config file. Copy `config.example.yml` as a starting point:

```bash
cp config.example.yml my_project.yml
# edit my_project.yml as needed
./scripts/run_pipeline.sh --config my_project.yml
```

Parameters resolve in this priority order (highest wins):

1. CLI flags (e.g. `--pre-margin 2.0`)
2. Config file values (with several `--config` files, the later file wins)
3. Built-in defaults

### Layering several config files

`--config` may be given more than once. The files are deep-merged left to
right, so a series of videos can share one base file and keep only what
differs — usually the `project:` brief — in a small per-video file:

```bash
cd /mnt/work/YouTube/my_video
/path/to/nagare-clip/scripts/run_pipeline.sh \
  --config ../nagare_base.yml --config ./nagare_config.yml
```

```yaml
# ../nagare_base.yml — shared by every video
director:
  max_keep_lines: 8
  silence_line_min: 5.0
text_filter:
  keywords: [サイフォン, オーバーフロー]

# ./nagare_config.yml — this video only
project:
  audience: "DIY hobbyists on YouTube"
  target_duration: "about 12 minutes"
director:
  max_keep_lines: 4        # overrides this one key; silence_line_min stays 5.0
text_filter:
  keywords: [ポンプ]        # replaces the base list; lists are not concatenated
```

Merge rules: mappings merge key by key at every depth; a list or a scalar in a
later file replaces the earlier value whole (to empty a list, write `[]`). CLI
flags still beat every file. The removed-key check and validation run once, on
the merged result, so a typo or a removed key in any file fails the run and
names its dotted path. A missing file fails with its path. Relative paths
*inside* the files (`input_videos_dir`, `output_dir`, `previous_summary`, …)
resolve against the current working directory, not against the file that
holds them — run from the video's directory.

Each run writes the merged result to `output/effective_config.yml` (with
`api_key` values blanked, and a header listing the files it came from). It is
what the Blender subprocess reads, and a record of what the run actually used;
edit your own files, not this one.

### Migrating a config from the LLM director

The LLM director and guided_edit's LLM are gone, and their keys now fail config
loading with a message naming each one. Delete:

- every key under `guided_edit:` (`enabled`, `provider`, `api_base`, `model`,
  `api_key`, `temperature`, `reasoning_effort`, `timeout`, `context_lines`,
  `max_retries`, `retry_temp_step`, `retry_temp_cap`, `prompt`) — the section
  has no settings left; an empty `guided_edit:` is accepted;
- under `director:`, `enabled`, `provider`, `api_base`, `model`, `api_key`,
  `temperature`, `reasoning_effort`, `timeout`, `response_format`,
  `max_retries`, `retry_temp_step`, `retry_temp_cap`, `chunk_lines`,
  `pause_after_plan` and `prompt`. Keep `max_keep_lines` and
  `silence_line_min`.

An older project's `output/director/` may hold empty `_director.json` files
the old pipeline wrote for a disabled director; resuming from guided_edit then
passes with `Continuing with 0 op(s)` — run the skill to direct it.
`director/conversation.md` is no longer read and can be deleted.

### project: the editorial brief

The `project:` section tells the editorial LLM stages what the transcript never says — who the video is for, how long it should be, how it should feel, and what happened last episode. Everything set here is appended to the system prompts of `summary`, `text_filter` and `publish`, and printed at the top of the director's `view`; every field is optional free text and defaults to empty (with nothing set, prompts are exactly as they were before this section existed).

```yaml
project:
  audience: "DIY hobbyists on YouTube, already familiar with the build"
  purpose: "show whether the siphon overflow drain actually works"
  target_duration: "about 12 minutes"
  tone: "fast, punchy vlog; frequent on-screen captions"
  story_so_far: "the previous episode built the rig; this one tests it"
  # Path to a previous project's summary.json — its overall summary joins the brief:
  previous_summary: "../water_pump_2/video-editor-ai/summary/summary.json"
```

`previous_summary` is how a series carries over: point it at the earlier project's `output/summary/summary.json` and the director learns what "これ" refers to when the cut opens mid-story. A missing or unreadable file just drops that one line (logged), leaving the rest of the brief intact. `target_duration` and `tone` are what let `director` plan for the runtime you want and deviate from its default ~1-overlay-per-3-5-minutes density. The mechanical stages (`gap_context`, `sentence_split`, `guided_edit`) are deliberately not briefed.

The config file covers all sections, each named after its stage: `general`, `project`, `transcription`, `audio_silence`, `whisper`, `sentence_split`, `gap_context`, `text_filter`, `summary`, `director`, `intervals`, `blender`, `publish`, `render`, `cut_report`, `pipeline`. The old `plan:`/`plan_revise:` sections, every `guided_edit:` key and the LLM director's keys are rejected with a message naming them (see *Migrating a config from the LLM director*). See `config.example.yml` for the full list of keys and their defaults.

### Choosing an LLM provider

Every LLM stage (`sentence_split`, `gap_context`, `text_filter`, `summary`, `publish`) routes through a unified transport backed by the [LiteLLM](https://github.com/BerriAI/litellm) library, so you can point any stage at a local or cloud provider. On a stage's config block, set `provider:` to one of `ollama_chat` (default, local Ollama), `openai`, `gemini`, or `anthropic`, and set `model:` to that provider's model name — LiteLLM receives the combined `"<provider>/<model>"`. Supply credentials with `api_key:` (or the provider's standard environment variable, e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`). Leave `api_base:` empty for cloud providers; for `ollama_chat` an empty `api_base` falls back to local Ollama (`http://localhost:11434`). Each stage chooses its provider independently, so you can mix (e.g. a cloud model for `director` and local Ollama for `guided_edit`).

```yaml
director:
  enabled: true
  provider: "openai"
  model: "gpt-4o"
  api_key: ""        # or set OPENAI_API_KEY in the environment
  api_base: ""       # leave empty for cloud providers
```

### blender: Proxies

Blender only builds a proxy when you select strips and press Rebuild Proxy, one
batch at a time, on the CPU. With `blender.use_proxy` (default) the `blender`
stage builds them itself before Blender starts, with the host ffmpeg: one
`proxy_<proxy_size>.avi` per source, written where Blender looks for it
(`BL_proxy/<file name>/` beside the source video), so the `.blend` opens with
proxies ready. By default the video is decoded and scaled on the NVIDIA GPU
(`blender.proxy_decode: cuda`) and encoded with x264 using Blender's own
settings (`blender.proxy_encoder: libx264`) — the same file Blender would
make, about 1.4x faster. `proxy_encoder: h264_nvenc` builds a little faster
again, but its proxies are heavier to scrub. When a GPU build fails it is
retried on the CPU, and if that fails too the run carries on without a
proxy. A proxy newer than its source is reused; replace the source and it is
rebuilt. Set `blender.proxy_build: false` to leave proxy building to Blender.

### blender.render: Render/Output Settings

The `.blend` this pipeline hands you opens with the resolution and frame rate of whatever source plays first, and Blender's own defaults for everything else — so the encoder, the container, the audio codec and the output path are set by hand every time you render. `blender.render:` writes them onto the scene instead. Empty by default; nothing set here keeps today's behaviour exactly.

Every key is the Blender attribute name, forwarded verbatim to `scene.render` (see [`bpy.types.RenderSettings`](https://docs.blender.org/api/current/bpy.types.RenderSettings.html)), and a nested mapping recurses into the sub-struct of that name — which is why `image_settings:` and `ffmpeg:` need no special support. Any render setting Blender exposes is reachable, including ones not shown here.

```yaml
blender:
  render:
    resolution_x: 1920
    resolution_y: 1080
    fps: 30                        # overrides the first source's measured fps
    filepath: "//../renders/final" # see below
    image_settings:
      media_type: VIDEO              # see below
      file_format: FFMPEG
    ffmpeg:
      format: MPEG4
      codec: H264
      constant_rate_factor: HIGH   # LOSSLESS | PERC_LOSSLESS | HIGH | MEDIUM | LOW | ...
      ffmpeg_preset: GOOD          # BEST | GOOD | REALTIME
      audio_codec: AAC             # NONE | AAC | AC3 | FLAC | MP2 | MP3 | OPUS | PCM | VORBIS
      audio_bitrate: 192
      audio_mixrate: 48000
      audio_channels: STEREO
```

- **`filepath`** takes Blender's `//` prefix, which is relative to the `.blend` file. The `.blend` sits in `output/blender/`, so `//final.mp4` writes beside it, `//../renders/final.mp4` writes to `output/renders/`, and an absolute path works too. Blender creates missing directories. Paths are handed over untouched — `~` is not expanded, because Blender does not read it that way.
- **`fps`** written without `fps_base` resets `fps_base` to `1.0`. The scene otherwise carries the first source's pulldown (`1.001` for 29.97), and `fps: 30` would quietly stay 29.97. Overriding it also changes every frame computation the stage does, so the cut lands where the new rate puts it.
- Keys are applied **in the order you write them**, which matters where one setting widens another's choices. On Blender 5.x, `image_settings.file_format` offers only still-image formats until `media_type: VIDEO` is set — so `media_type` has to come first, or you get `enum "FFMPEG" not found`.
- An **unknown key** is logged and skipped. An **invalid value** — a misspelt enum member such as `codec: H246` — is a hard error, because rendering with a codec other than the one you asked for is a defect you would only find by playing the file.

### whisper: Whisper Hints

Optional (`whisper.enabled: true`, default `false`). Haas-ja stays the transcript, but a second recognizer sometimes hears what Haas misses (on a 120 s clip Haas wrote 「散乱感」/「水が」 where Whisper had 「産卵管」/「水位が」). The `whisper` stage runs faster-whisper (`whisper.model`, default `large-v3`, already baked into the `whisperx` image; `batch_size` 8, `beam_size` 5, language fixed to Japanese) over the same chunks in one Docker run, and `text_filter` shows its reading of each line to the LLM as a **reference only**; the LLM still emits `{{old->new}}` patches on the Haas line. Needs `text_filter.use_llm`. Caveats measured: Whisper drops fillers, normalises colloquial forms (流してる→流している), inserts stray spaces and hallucinated once (えっと→越冬), so hints are never truth. Disabled = no Docker call and no hints. Details: [`docs/stages/whisper.md`](docs/stages/whisper.md).

```yaml
whisper:
  enabled: true
```

### audio_silence: Audio-Silence Detection

The audio_silence stage runs Silero VAD v6 (in the `transcribe/` worker) on the audio and writes an editable `{stem}_cuts.txt` cut list. Each non-comment line is a `START - END` silent span (seconds) that will be cut from the video; delete a line to keep that span, or adjust the times. A `# media duration: …s` comment records the video's length (from the VAD run); it is what a video with no speech ends at. The intervals stage unions these reviewed ranges into its keep-interval excludes. This is acoustic silence — distinct from `intervals.silence_threshold`, which is a word-gap heuristic over the transcript's word timings.

```yaml
audio_silence:
  threshold: 0.5      # Silero VAD speech probability threshold
  min_speech: 0.25    # shortest speech region kept, seconds
  min_silence: 0.5    # shortest silence between speech regions that is cut, seconds
  speech_pad: 0.2     # padding around each speech region, seconds
```

### sentence_split: Split Long Lines (optional)

A transcript line is one speech span between two `audio_silence` cuts, so a speaker who never pauses for `audio_silence.min_silence` produces a long line. The `sentence_split` stage splits **only** lines longer than `sentence_split.max_line_seconds` (default 8.0 s) at natural sentence or clause boundaries; it never merges lines and never touches a short one. Disabled by default — when `sentence_split.enabled` is `false`, the stage copies `output/transcription/{stem}.{json,txt}` byte-identically into `output/sentence_split/`, so all downstream behaviour is unchanged.

When enabled, each long line is sent on its own (one LLM call per long line) as a numbered list of GiNZA bunsetsu units (Japanese morpho-syntactic chunks); the LLM returns contiguous bunsetsu-index ranges (`{"sentences":[[0,3],[4,7],…]}`), one per piece. The stage rebuilds the line's pieces by slicing its original word list at those boundaries — words are only reassigned, never edited, so word timings are preserved and output text is verbatim. An LLM failure, invalid ranges, or a single range (the line is one sentence) keeps the line as it is. A run with no long line makes no LLM call.

`window_segments`, `force_split` and `force_split_min_silence` were removed (every cut is already a line boundary); a config that still sets them fails to load, naming each key. The stage no longer reads `_cuts.txt`.

```yaml
sentence_split:
  enabled: true
  provider: "ollama_chat"   # see "Choosing an LLM provider" above
  model: "gpt-oss:120b"
  max_line_seconds: 8.0     # only lines longer than this are split
```

Outputs land in `output/sentence_split/{stem}.json` and `output/sentence_split/{stem}.txt`; all downstream stages (summary, text_filter, director, etc.) read from there.

### gap_context: Silent-Gap Visual Context (optional)

The `gap_context` stage runs once per video, between `sentence_split` and `summary`. It looks at the audio_silence `{stem}_cuts.txt` for spans at least `gap_context.min_gap` seconds long, snapshots up to 3 JPEG frames per span (start+0.2s, midpoint, end-0.2s) via ffmpeg inside the Docker image (`docker-compose.yml`), and asks a **vision-capable** LLM to describe what's on screen during each gap in one or two plain-text sentences. Disabled by default (`gap_context.enabled: false`) — no Docker calls, and the `summary`/`director` stages render byte-identical to before this feature.

```yaml
gap_context:
  enabled: true
  provider: "ollama_chat"    # see "Choosing an LLM provider" above
  model: "qwen2.5vl:7b"      # must be a vision-capable model
  min_gap: 3.0                # seconds; only cut spans at least this long get snapshotted
  frame_width: 960            # downscale width (px) of the extracted JPEGs
  static_ssim: 0.96           # skip the vision call when min SSIM across consecutive frame pairs >= this; 0 disables
  context_lines: 1            # transcript lines quoted to the LLM on each side of the gap (0 = none)
```

`context_lines` controls how much of the surrounding talk the vision LLM is shown: the last N spoken lines leading into the gap and the first N after it (a line whose last word runs on into the silence, or that contains it, counts as leading into it), which help it judge what the silence is *for*. The default of `1` matches the previous fixed behaviour; raise it when a single line is too little to make sense of the scene (it costs prompt tokens on every gap of every video), or set `0` to send frames alone.

Before calling the vision LLM at all, an ffmpeg SSIM comparison runs over every *consecutive* pair of a gap's extracted frames (a 3-frame gap yields 2 pairs: first-vs-mid, mid-vs-last) and takes the minimum score — comparing only the first and last frame would miss a camera pan-away-and-return, since the middle frame (already extracted, already paid for) is the one that would reveal the on-screen action. When that minimum is at least `gap_context.static_ssim` (default `0.96`), the gap is recorded straight to `{stem}_gaps.json` as `static: true` with a placeholder description and **no vision-LLM call is made** — cutting real-run cost on footage with long pixel-static spans (e.g. a locked-off tripod shot). Set `static_ssim: 0` to disable the prefilter and always call the vision LLM.

Output is a hand-editable intermediate, `output/gap_context/{stem}_gaps.json` (`{"gaps": [{start, end, frames, description, static}]}`), plus the extracted frames under `output/gap_context/frames/{stem}/`. The vision LLM classifies each gap as action or static (its reply starts with `ACTION:`/`STATIC:`); gaps marked `"static": true` — scenes with no meaningful on-screen change — are kept in the file for review but **excluded** from the summary/director prompts, so tripod footage of nothing happening doesn't drown out the gaps that matter. Deleting a line from `output/audio_silence/{stem}_cuts.txt` both keeps that span's audio *and* removes it from gap-context snapshotting; editing a gap's `description` directly changes what `summary`/`director` are told about it, and flipping `"static": false` forces a gap back into those prompts. `--from-stage gap_context --to-stage gap_context` re-runs just this stage against an already-edited `_cuts.txt` (reusing the transcription/audio_silence/sentence_split outputs already on disk) without redoing transcription.

When enabled, `summary` appends a `## Silent gaps (visual context)` block (each gap anchored to the transcript line it follows) to its per-video prompt, and `director`'s numbered transcript gets an indented, un-numbered `[silent gap N.Ns: description]` line after the relevant line — the director's prompt explains that a `keep` op spanning that line and the next one (`[N, N+1]`) preserves the silence if the visual content is worth keeping.

### text_filter: Text Editing (LLM optional)

The text-editing checkpoint always produces `{stem}_edits.txt`. When `text_filter.use_llm` is `false` (default), it copies the transcription `.txt` as-is. When enabled, it runs LLM-based transcription correction and writes output with `{{old->new}}` markers preserved for human review.

```yaml
text_filter:
  use_llm: true
  provider: "ollama_chat"   # see "Choosing an LLM provider" above
  api_base: ""              # empty -> local Ollama; leave empty for cloud providers
  model: "qwen3.5:4b"
  reasoning_effort: "low"   # passed to LiteLLM as-is; omit to send nothing
```

The LLM uses `{{old->new}}` inline patch syntax to mark corrections. Human editors can then review and modify the markers in `_edits.txt` before the intervals stage applies them.

Human editors can also wrap a span in `<keep>...</keep>` to force-preserve the audio under that text. The intervals stage derives the time range from the first wrapped word's start to the last wrapped word's end and carves it out of both the word-gap silence and any overlapping `_cuts.txt` ranges. The marker may be opened on one line and closed on a later line, so a single `<keep>` block can span multiple transcript segments and preserve the silences between them. The marker is added after the LLM filter has produced `_edits.txt`, so the LLM never sees it.

`<speed factor="N.N">...</speed>` is a companion marker carrying a playback-speed annotation. Unlike `<keep>`, it does **not** force-keep audio — its span does not carve silence out of the excludes, so silence inside it is still cut and the speed applies only to the surviving spoken parts (nest inside `<keep>` to preserve the audio too). The intervals stage records each marked span as an entry in a top-level `speed_ranges` array (`{start, end, factor}`) in `_intervals.json`, independent of `keep_intervals`. The blender stage splits keep intervals at those boundaries, so a `<speed>` span may cover an arbitrary sub-range of a keep interval (or span several), and adds a Blender VSE Speed Control effect strip over each sped-up sub-range so it plays at the requested speed in the final `.blend`. A `speed_ranges` entry that falls entirely on cut content simply matches no surviving interval and is ignored.

`reasoning_effort` is passed to LiteLLM's `reasoning_effort` **unchanged** — no translation, no per-provider special case — so what a value (`"none"`, `"low"`, `"medium"`, `"high"`, …) does for a given model is exactly what LiteLLM documents for that provider. Leave it unset (the default, `null`) to send nothing, in which case the model runs at its own default (current reasoning models think by default). The pipeline uses only the final answer, not the reasoning trace. The old `thinking` key is gone: a config that still contains it fails to load with a message naming the key.

#### Filter context from the summary stage (optional)

When the project-wide `summary` stage is enabled (`summary.enabled: true`), it runs
before text_filter and its `summary.json` carries, per video, a whole-video summary, the
part summaries, and a list of rare/domain-specific keywords that speech recognition might
misspell. text_filter appends this video's whole-video summary, part summaries, and
keywords to the filter LLM's system prompt so it can better correct mis-dictated words.
You can also pin constant keywords that are always injected, without enabling the summary
stage:

```yaml
text_filter:
  use_llm: true
  keywords: ["Kubernetes", "PostgreSQL"]   # always appended to the filter prompt
summary:
  enabled: true                            # per-video summaries + keywords for the filter
```

Falls back gracefully — a missing or empty `summary.json` just means filtering proceeds
without the extra context.

> **Migration:** the former `text_filter.summary_llm` section was removed and now fails
> validation. Move `summary_llm.keywords` to `text_filter.keywords` and use
> `summary.enabled` for LLM-generated context.

## CLI

```bash
./scripts/run_pipeline.sh [OPTIONS]
```

Options:
- `--source FILE` — source video file (may be repeated for multiple sources); when omitted, all videos in `--input-videos-dir` are processed alphabetically.
- `--config FILE` — path to a YAML config file; config values fill in between CLI overrides and built-in defaults. Repeatable: files are deep-merged left to right, later files win (see [Layering several config files](#layering-several-config-files)).
- `--from-stage NAME` — start from stage `NAME`, reusing earlier stage outputs. `NAME` is a stage name: `audio_silence`, `transcription`, `whisper`, `sentence_split`, `gap_context`, `summary`, `text_filter`, `director`, `guided_edit`, `intervals`, `blender`, `publish`, `render`. Also settable via `pipeline.from_stage` in config.
- `--to-stage NAME` — stop **after** stage `NAME` (inclusive); later stages are skipped. Same stage names as `--from-stage`, and must not precede it. Defaults to `publish` (run to the end). Also settable via `pipeline.to_stage` in config. Combine with `--from-stage` to run a window of stages, e.g. `--from-stage summary --to-stage director`.
- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `--source` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
- `silence_threshold` and `min_keep` default to `1.5` and `1.0` (overridable via config).
- `pre-margin`/`post-margin` extend keep intervals before/after by default `1.0s` and merge overlaps.
- The transcription language is fixed (Haas-ja-v1.0 is Japanese-only); `--language`, `--align-model` and the `transcription.language`/`align_model`/`compute_type` keys are removed.

## Stage Commands

### transcription only (Haas-ja)

There is no standalone CLI; it runs through the pipeline orchestrator and needs
`{stem}_vad.json` from a previous `audio_silence` run on disk:

```bash
./scripts/run_pipeline.sh --from-stage transcription --to-stage transcription \
  --source myvideo.mp4 --config my_project.yml
```

Notes:

- One `transcribe/` worker process handles all sources (Haas warm-up takes several seconds); it needs a CUDA GPU.
- Output is `{stem}.json` in the WhisperX JSON shape (one segment per speech span between cuts — VAD regions closer than `audio_silence.min_silence` are joined, because splitting inside a word is worse than joining two phrases — one `words` entry per non-whitespace character) plus `{stem}.txt`.
- The chunking and timestamp details are in [`docs/stages/transcription.md`](docs/stages/transcription.md).

### audio_silence only (audio-silence detection)

There is no standalone CLI for *this* stage — it runs through the pipeline
orchestrator, like every stage in this repo. Run just this stage with `--from-stage`/`--to-stage` (it drives the Silero VAD
worker call and turns its `{stem}_vad.json` into `{stem}_cuts.txt`
internally):

```bash
./scripts/run_pipeline.sh --from-stage audio_silence --to-stage audio_silence \
  --source myvideo.mp4 --config my_project.yml
```

To keep a silence, delete its line from the generated `_cuts.txt`.

### intervals only (patch application + interval generation)

```bash
./scripts/run_pipeline.sh --from-stage intervals --to-stage intervals \
  --source myvideo.mp4 --config my_project.yml
```

`--keep-pre-margin`/`--keep-post-margin` are exposed as top-level pipeline
flags and override the config file:

```bash
./scripts/run_pipeline.sh --from-stage intervals --to-stage intervals \
  --source myvideo.mp4 \
  --keep-pre-margin 1.0 \
  --keep-post-margin 1.0
```

`intervals.min_cut` (default 0.4s) merges two adjacent keep intervals when the
cut between them is shorter than that — a cut that brief jumps the picture
without saving meaningful runtime, and the keep/caption margins expanding from
both sides routinely leave such slivers behind. Raising `audio_silence.min_silence`
is *not* the equivalent knob: that changes which silences are detected at all, so
it also keeps the long pauses. Set `min_cut: 0` to make every detected cut, as
before.

The remaining intervals knobs (`silence_threshold`, `min_keep`,
`caption.max_bunsetu`, `caption.min_bunsetu`, `caption.max_duration`,
`caption.min_duration`, `caption.silence_flush`, …) no longer have dedicated
CLI flags — set them under `intervals:`/`intervals.caption:` in your config
file (see `config.example.yml`).

Keep-interval silence detection uses the transcript's word timings (`word.start`/`word.end`) with a per-word max-span cap (0.6s) so inflated token ends do not mask real pauses. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries are attached to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by timing misalignment and snaps the bunsetsu start forward to the later character cluster so silence is not hidden inside a single bunsetsu. Caption chunks use bunsetsu-level timing (`end = min(start+0.02s, next_bunsetu_start)`) and are split on detected silence gaps and keep-boundary crossings. Captions are preserved as transcript chunks and the interval stage expands keep intervals to include caption spans so subtitle text is not dropped at the Blender stage, then re-applies minimum keep duration (`min_keep`) to avoid tiny strips.

### blender only (Blender VSE project)

```bash
blender --background --factory-startup --python-exit-code 1 --python src/nagare_clip/blender/blender_cli.py -- \
  --source src_video/myvideo.mp4 \
  --intervals output/intervals/myvideo_intervals.json \
  --output output/blender/myvideo_edited.blend \
  --config my_project.yml
```

### publish only (title, description, chapters, thumbnail material)

```bash
./scripts/run_pipeline.sh --from-stage publish --to-stage publish --config my_project.yml
```

Reuses `output/summary/summary.json`, the director's plan (`output/director/plan.md`), each source's
`output/director/{stem}_director.json` and `output/intervals/{stem}_intervals.json`,
so it can be re-run on its own to get a different set of title/thumbnail
candidates without touching the cut. Any of those inputs missing degrades just
the part that needed it (no intervals → no chapter timestamps).

### render only (composite the thumbnails)

```bash
./scripts/run_pipeline.sh --from-stage render --to-stage render --config my_project.yml
```

Picking a background still and nudging a colour is iterative, and re-running
`publish` would call the LLM again and hand you *different* copy than the one
you were judging. So `render` is its own stage: it reads
`output/publish/publish.json` (including the model-authored font/colour/
position values), re-runs ImageMagick and makes **no LLM call at all** — hand-
edit a set's `background`/`fill`/`stroke`/`gravity`/… first, then re-run the
stage to see the change.

A set's `background` is a path relative to `output/publish/` (where the
shortlist stills are), or an absolute path. It does **not** have to be a
shortlist frame: any image of any aspect ratio works, and is scaled to cover
and centre-cropped to the canvas — so a photograph the camera never rolled on,
or a frame you pulled by hand at a timestamp the shortlist missed, is one line
of JSON away. A set that names nothing falls back to the first shortlist
candidate; a set that names a file which is not there is skipped with a warning
rather than quietly rendered onto some other frame — `render.md` keeps that
set's heading and copy and says why it produced no image, so a typo is visible
in the file you are already reading. See
[`docs/stages/render.md`](docs/stages/render.md) for the full style-key table.

## Operational Notes

- `gap_context` and `publish` run ffmpeg as root in the Docker image (`--user "0:0"`), so frame files under `output/` can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output
```
