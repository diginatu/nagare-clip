# nagare-clip

Semi-automated video editing pipeline for long-form recordings on Linux.

The pipeline creates a rough-cut Blender project for human review and fine-tuning.

## Pipeline Stages

1. transcription: WhisperX in Docker -> transcript outputs (`json`, `srt`, `vtt`, etc.)
2. audio_silence: Audio-silence (jump-cut) detection -> `_cuts.txt` editable cut list
3. sentence_split (optional): LLM re-segments the transcript into one-sentence-per-line units -> `output/sentence_split/{stem}.json` + `{stem}.txt`; disabled by default (byte-identical copy-through)
4. gap_context (optional): a vision LLM snapshots+describes long silent gaps (from `_cuts.txt`) so summary/director can see what the transcript can't -> reviewable `output/gap_context/{stem}_gaps.json` + JPEG frames; disabled by default (no-op)
5. summary (optional, project-wide): a larger LLM segments every video into line-range parts + summaries, a whole-video summary, and misspelling-prone keywords, and writes one all-videos summary -> reviewable `output/summary/summary.json`; the per-video summary and keywords are used by text_filter/plan/director
6. text_filter: Text editing checkpoint -> `_edits.txt` (copy of `.txt`, or LLM-corrected with `{{old->new}}` markers), optionally primed with summary-stage context
7. plan (optional, project-wide): a larger LLM gives a coarse, cross-video rough direction per part -> reviewable `output/plan/plan.json`
8. director (optional): a larger LLM proposes high-level edits -> reviewable `_director.json` op list (fed the summary/plan overview context, and gap_context's described gaps)
9. guided_edit (optional): a small LLM applies the director's ops into `_edits.txt` (deterministically verified)
10. intervals: Patch application + keep intervals -> `*_intervals.json` keep ranges (audio cuts unioned in)
11. blender: Blender headless -> `.blend` with VSE strips arranged back-to-back
12. publish (optional, project-wide): title candidates, a description with chapter timestamps taken from the finished timeline, thumbnail copy and candidate stills -> reviewable `output/publish/publish.md` + `publish.json`

Stages are referenced by name (`--from-stage <name>`); the gap_context/summary/plan/director/guided_edit/publish stages are no-ops unless enabled in config.

## Human Editing Workflow

1. Run the transcription, audio_silence and text_filter stages: `./scripts/run_pipeline.sh`
2. Edit `output/audio_silence/{stem}_cuts.txt` — delete a line to keep that span, or adjust the `START - END` times
3. Edit `output/text_filter/{stem}_edits.txt` — add/modify `{{old->new}}` patch markers, wrap text in `<keep>...</keep>` to force-keep its audio, wrap text in `<speed factor="N.N">...</speed>` to play that region at the given speed in Blender (does not force-keep audio — nest inside `<keep>` to also keep it), insert `<overlay text="..." duration="N.N"/>` to display an on-screen TEXT strip for that many seconds (does not affect audio), or wrap text in `<cut>...</cut>` to delete it (larger deletions drop out of the timeline via silence detection)
4. Resume: `./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4`

The `{{old->new}}` syntax replaces `old` with `new` in the transcript. Use `{{delete->}}` to remove text, `{{->insert}}` to insert text.

The optional `director` + `guided_edit` stages automate steps like the above with LLMs: enable `director.enabled`/`guided_edit.enabled` in config, then a larger LLM proposes edits (cut/timelapse/overlay/keep/edit) into a reviewable `output/director/{stem}_director.json`, and guided_edit applies them — any op it cannot apply cleanly is logged and recorded in the LLM report (`output/llm_report/`). A `timelapse` op names a range, a factor of 4.0+ and a caption, and guided_edit expands it into a `<keep><speed>` span with an `<overlay/>` that lasts the whole timelapse — so the three markers cannot disagree, which is what produced sped-up jump cuts when they were three separate ops. There is deliberately **no mild `speed` op** on the director's menu: compressing a stretch is a two-mode choice — either it is work worth watching fast (`timelapse`) or material worth cutting — and offering a middle option in between made a sustained mild fast-forward the register several real runs settled into. You can still write a `<speed factor="N.N">` tag into `_edits.txt` yourself (see below), and a `speed` op you hand-write into `_director.json` is still applied. Span ops (cut/speed/overlay/keep) are a pure whole-line-range wrap, applied deterministically with no LLM call; if a span op's range overlaps a same-type tag already in the file (e.g. one you hand-authored) or an earlier op, it is clipped to the free lines (or dropped if fully covered) so the tags stay valid. Only `edit` ops (a within-line `{{old->new}}` the director only described in prose) call the small LLM, retrying per op on an error / failed verification and nudging the temperature up each attempt. The director likewise retries a failed LLM call (connection error / unparseable JSON); tune `max_retries`, `retry_temp_step`, and `retry_temp_cap` per stage (`max_retries: 0` disables retry).

A director `keep` op protects a span from cutting **including its silences**, and its width follows what is on screen: the narrowest range (normally `[N, N+1]`) to rescue one silent gap, or the whole run of lines when a continuous event is playing out — an accident and the cleanup after it, a demo running — since chopping such a sequence into per-gap keeps reads as jump cuts through the payoff. What it is *not* is a way to mark a long span of **talking** as important: speech is never dropped by default, so a keep over talking only restores its pauses (in one real run, keep ops of 20-35 lines took a 22.3-minute cut to 53.9 minutes). `director.max_keep_lines` (default `8`, `0` = no limit) drops any wider `keep` the LLM proposes — a continuous event fits well inside that, precisely because nobody is talking through it — logged, and listed in the LLM report — so those lines fall back to the normal behaviour (speech kept, internal silence cut). The limit is stated to the director in its prompt automatically, so raising or lowering it needs no prompt edit. It applies only to the LLM's output: a `keep` you write by hand into `{stem}_director.json` is always honoured. The cap has no exceptions — a fast span that needs a keep of its own belongs in a `timelapse` op instead, which carries its protection as part of what the op means rather than asking the cap to look the other way.

The optional `summary` + `plan` stages run **once over all source videos** (project-wide) and give downstream stages cross-video context. Enable `summary.enabled`/`plan.enabled` in config: `summary` runs before `text_filter`, segments each sentence_split transcript into line-range parts with a summary each, lists per-video misspelling-prone keywords, generates a whole-video summary per video, and writes one all-videos summary to `output/summary/summary.json` (`{summary, parts, keywords, video_summaries}`) — its per-video summary/part summaries/keywords also prime the `text_filter` LLM's prompt; `plan` still runs before `director`, reading those summaries and writing a coarse, cross-video rough direction per part (e.g. "remove — repeats an earlier part", "shorten — trim the setup") to `output/plan/plan.json`. Both files are human-reviewable/editable. Note the plan's vocabulary avoids the word "keep" on purpose — in the `director` stage `keep` is an *op* that also restores every silence in its range, and a direction reading "keep — …" was being copied across as one, inflating the finished runtime; write "feature", "retain" or "emphasise" instead when hand-editing `plan.json`. When enabled, the `director` for each video receives the overall summary plus that video's whole-video summary, its parts (line ranges, summaries, rough directions), and one-line context for the other videos (preferring each sibling's own whole-video summary), so its precise per-line ops follow the project-wide plan. They share the same `max_retries`/`retry_temp_step`/`retry_temp_cap` retry knobs.

#### Talking to `plan`

`plan` is the one stage you can argue with. It reads a conversation file —
`output/plan_dialogue/history.md` — alongside the summaries and **its own
previous `plan.json`**, and appends its reply to the same file. So instead of
hand-editing JSON you write one sentence and re-run a single stage:

```bash
./scripts/plan_say.sh "PXL_1234 [31,83] は 60-83 だけがデモ本体、33-59 は脱線"
./scripts/run_pipeline.sh --from-stage plan --to-stage plan   # 1 LLM call
```

The first run needs no interaction at all: it writes today's `plan.json` plus a
short message saying what it was unsure about and which directions it would like
confirmed. Every later run reads the whole history, so a correction keeps
applying. Because it is given its previous plan, round two is an *edit* — the
prompt tells it to change only what the conversation asks for and repeat the
rest unchanged.

`plan_say.sh` is a convenience: the file is plain markdown with `## human` /
`## plan` headings and opening it in an editor and typing works just as well
(text before the first heading counts as yours). It is stored under `output/`
because every turn refers to line ranges that this `summary` run defines —
delete `output/` and the conversation goes with it. Do **not** put per-run
corrections in the `project:` brief instead: that reaches `summary` and
`text_filter` too, and has been observed rewriting what the speaker actually
said.

A direction may also cover **part** of a summary part (`"lines": [60, 83]`), and
several directions may share one part — so `plan` can split a part `summary` got
wrong, which is what makes "that part is really two things" actionable rather
than merely heard. The range must sit inside its part's own range. Only `plan`
works this way: `summary` deliberately stays non-conversational so the line
numbers your turns refer to never move under them, and `director` reads
`plan.json` only.

After `director` runs, the pipeline also notes — with no LLM call — where the
ops that landed **argue with** the plan: a part directed `feature` that got
mostly cut, a part directed `remove` that got a `keep`/`overlay`/`timelapse`, a
part directed `timelapse` that got none. The share and the threshold are printed
so the call is arguable, along with the director's own note explaining itself.
It appears at the bottom of `output/llm_report/index.md`. The director is *not*
made to obey the plan — it is the first stage that reads the actual lines and
its override is often right; what was missing was any record that they
disagreed.

The director is also told **where its video sits in the finished video**. The sources are concatenated in input-directory name order, so that order is known before any op is written: the context block states `video 3 of 7` (marking the FIRST and the LAST explicitly) and splits the sibling videos into what plays *earlier* and what plays *later*, numbered. Without it, a whole-project instruction in the editorial brief decomposes into N independent obediences — "add a caption early on explaining the rig" produced the same recap caption on four of seven sources in a real run, each individually correct. Alongside it the director receives the **captions already committed on the earlier videos** (their `_director.json` is written before this one runs), so an explanation placed once is visible as already placed; `director.max_prior_captions` (default `100`, `0` = no limit) keeps only that many of the most recent. Both survive a single-source re-run (`--source b.mp4 --from-stage director`): `--source` narrows what is processed, not what the finished video contains, so the order is re-read from the input directory and the earlier `_director.json` files are read off disk — delete them and the caption list simply renders shorter.

To help the LLMs reason about pacing, the `plan` and `director` stages now also see **calculated durations and in-between gaps**: the `director`'s numbered transcript annotates each line with `[4.2s, gap 0.8s]` (per-sentence duration + gap to the next line) and the `plan`'s per-part context shows each part's duration and gap. Both stages' default prompts explain the bracket notation (what the duration is, that the gap is the silence before the next line/part, and that a negligible gap — or the last line/part — shows no gap at all), so the LLM can act on it. These times come from the WhisperX `{stem}.json`; the orchestrator feeds each stage the sentence_split `{stem}.json` automatically, so there's nothing to wire up yourself. When the audio_silence `{stem}_cuts.txt` is also available, `director` reads it live and splits that duration into speech vs. already-cut silence, e.g. `[12.9s speech, 62.9s silence]` — so a long span that's mostly detected silence doesn't read as "long, keep as-is"; `plan` never reads `_cuts.txt` itself — it renders the same split only when `summary` ran with the cuts file available and persisted a per-part `silence` value into `summary.json`. Both fall back to the plain `[Ns, gap Ns]` form when no silence overlaps or the cuts file is unavailable. Because of this, hand-editing `_cuts.txt` (an explicitly supported human step) silently stales `summary.json`'s persisted `silence` — `director` recomputes it live, so the two can briefly disagree until `summary` is re-run; harmless in the documented workflow, which resumes at `--from-stage intervals` and never re-reads `summary.json`'s stale `silence` for anything but display. To run a single stage on its own (reusing its inputs from a previous pipeline run), use `./scripts/run_pipeline.sh --from-stage X --to-stage X`.

Before resuming, you can validate your edits in one pass (reports **every** problem at once, with line numbers, instead of failing on the first like the intervals stage does):

```bash
uv run python -m nagare_clip.intervals.check_edits \
  --edits-txt output/text_filter/myvideo_edits.txt \
  --json output/transcription/myvideo.json
```

It checks line-count vs. JSON segments, `{{old->new}}` patch syntax (empty `{{old->}}` deletions are allowed), decomposition integrity against the original transcript, `<keep>`/`<speed>`/`<cut>` tag balance, `<overlay/>` marker attributes, and well-formedness; when everything passes, it additionally dry-runs the real intervals-stage sync so a "no problems found" verdict guarantees the intervals stage won't reject the file either. Exit code is non-zero when any problem is found.

The `<keep>...</keep>` tag preserves the audio under the wrapped text — the intervals stage carves that time range out of both the word-gap silence detection and any overlapping `_cuts.txt` ranges, so dramatic pauses and intentional silences survive. The tag may be opened on one line and closed on a later one, so a single `<keep>` block can span multiple lines and preserve the silences between them. The tag is added by the human (not the LLM) after `_edits.txt` is produced.

The `<speed factor="N.N">...</speed>` tag instructs the blender stage to play the wrapped region at the given playback speed (e.g., `factor="2.0"` for 2× fast-forward, `factor="0.5"` for slow-motion) via a Blender VSE Speed Control effect strip. Unlike `<keep>`, **it does not force-keep audio** — silence inside a `<speed>` span is still cut by silence detection, and the speed only applies to the surviving spoken parts. To preserve the audio **and** speed it up, nest the tags: `<keep><speed factor="2.0">…</speed></keep>`. Captions inside the region are timed against the sped-up timeline so they stay in sync. The blender stage also automatically renders a small top-right badge (e.g. `x2.0`) on-screen over every `<speed>` region — disable via `blender.speed_mark.enabled: false`, restyle via `blender.speed_mark.*`, or change the wording via `blender.speed_mark.template` (the `{factor}` placeholder is rendered to one decimal place).

The `<overlay text="..." duration="3.0"/>` marker places an on-screen TEXT strip in Blender. It is self-closing — there is no `</overlay>` — so where you put it is where the text appears, and `duration` says how many seconds it stays up. Those seconds are counted on the **finished** timeline, so cuts and `<speed>` regions falling inside the window never eat into the time a viewer gets to read it, and there is no upper limit. Like `<speed>` (and unlike `<keep>`), **it does not force-keep audio** — the words it sits on can be cut by silence detection. When that happens the caption is not lost: it moves to the first surviving moment of the line it sits on (its `duration` is unchanged, so it may run on over what follows). Only when *nothing* of that line survives is the overlay skipped, with a warning — there is then no timeline content to display it over. Quotes inside the `text="..."` attribute value are not supported, and the attributes must be in the order `text`, `duration`. For a caption on **several on-screen lines**, write the break as the two characters `\n` inside the attribute (`text="1行目\n2行目"`) — the marker itself must stay on one line of `_edits.txt`, since line N of that file maps to segment N of the transcript. A literal backslash is written `\\`. Resume with `./scripts/run_pipeline.sh --from-stage intervals` to apply. The text you write is automatically spaced at natural phrase boundaries before it reaches Blender, so a long caption has somewhere to wrap.

The `<cut>...</cut>` tag deletes the wrapped text. It is a shorthand for `{{wrapped->}}` deletion patches (and can span multiple lines — open on the first, close on the last), so use it to drop whole sentences or sections. There is no separate "cut this time range" mechanism: removing the words opens a gap between the surviving neighbours that the word-gap silence detection cuts from the timeline, so `<cut>` is meant for **larger** deletions (a span shorter than the silence threshold may not actually be cut). Because the text is deleted, no caption is shown for it. Do not overlap `<cut>` with `<keep>`/`<speed>` on the same span, or let it swallow an `<overlay/>` marker.

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

`publish` also **renders** a real thumbnail image per copy set, into
`output/publish/thumbnails/set{N}.jpg`, and embeds each render (plus the
candidate stills above) as an inline image in `publish.md` — so reviewing the
options means looking at them, not opening files by hand. The same LLM call
that writes a set's copy also writes that copy's *look* — font, colours,
outline, block position — in ImageMagick's own vocabulary, so the four sets
you see are four real options rather than four wordings of one image; the
pipeline validates every value and builds every `magick` command as an
argument list (never a shell string). This needs `magick` (ImageMagick) on
`PATH`; set `publish.thumbnail.enabled: false` to keep the copy but skip
rendering. `output/publish/publish.json`'s `thumbnail_copy` — including that
model-authored look — is a hand-editable contract, like `_director.json` is
for the edit; see [`docs/stages/publish.md`](docs/stages/publish.md) for the
full style-key table and escaping rules, and the "Re-rendering" note below for
iterating on a background/colour without calling the LLM again.

Those images are embedded as sized `<img>` tags by default. If you read
`publish.md` in a viewer that strips raw HTML, set `publish.image_markup:
markdown` and they become plain `![alt](path)` images instead (markdown has no
width syntax, so they render full width).

`output/publish/publish.json` holds the same material as data, including the
`renders` array (`{set, path, background}`), for anything reading it
programmatically.

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

Every LLM stage (`sentence_split`, `gap_context`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `publish`)
writes a per-call record under `output/llm_report/`: an `index.md` table
(stage, unit, attempts, outcome, reason) linking to per-call detail files under
`<stage>/<unit>.md` that hold the full prompt and raw response for every attempt,
including retries. Outcomes: `ok`, `ok-empty`, `llm-error`, `unparseable`,
`verify-fail`, `dropped-items` (a call that parsed but discarded some items).
Re-running a stage refreshes only that stage's section. Toggle with
`general.llm_report` (default `true`) and relocate with `general.llm_report_dir`.
Deterministic findings that cost no LLM call live in `notes/*.md` and are inlined
at the bottom of `index.md` — currently the plan/director divergence report
(see above).

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
through the pipeline CLI. (`python -m nagare_clip.publish.thumbnail`, below,
is a standalone CLI, but it never calls an LLM — it only re-runs ImageMagick —
so there is nothing for it to bypass.)

Traces are grouped by pipeline run (`session_id` = one timestamp per
`run_pipeline.sh` invocation, exported as `NAGARE_RUN_ID`), by stage
(`tags: ["stage:<name>"]`), and by source file (`tags: ["stem:<stem>"]`), so
you can filter by any dimension in the Langfuse UI.

The existing markdown LLM report (`output/llm_report/`) continues to run
alongside Langfuse — the two are independent sinks.

## Requirements

- Linux
- NVIDIA GPU + NVIDIA Container Toolkit
- Docker + Docker Compose
- Blender available as `blender`
- ImageMagick available as `magick` (only needed for `publish.thumbnail.enabled`, i.e. when the optional `publish` stage renders thumbnails)
- Python 3.11+

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
uv sync          # install runtime deps
uv sync --dev    # install runtime + dev deps (includes pytest)
```

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

Override the language (default is `ja`):

```bash
./scripts/run_pipeline.sh --language en
```

Re-run from a specific stage (skip expensive earlier stages when iterating on config):

```bash
# Skip transcription, reuse WhisperX output, re-run silence detection + edits + intervals + Blender
./scripts/run_pipeline.sh --from-stage audio_silence --source myvideo.mp4

# Skip through text_filter, apply silence cuts + text edits and regenerate intervals + Blender project
./scripts/run_pipeline.sh --from-stage intervals --source myvideo.mp4

# Skip through intervals, only regenerate the Blender project
./scripts/run_pipeline.sh --from-stage blender --source myvideo.mp4
```

Override the alignment model (e.g. to revert to the WhisperX built-in default for Japanese):

```bash
./scripts/run_pipeline.sh --align-model jonatasgrosman/wav2vec2-large-xlsr-53-japanese
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
2. Config file values
3. Built-in defaults

### project: the editorial brief

The `project:` section tells the editorial LLM stages what the transcript never says — who the video is for, how long it should be, how it should feel, and what happened last episode. Everything set here is appended to the system prompts of `summary`, `plan`, `director` and `text_filter`; every field is optional free text and defaults to empty (with nothing set, prompts are exactly as they were before this section existed).

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

`previous_summary` is how a series carries over: point it at the earlier project's `output/summary/summary.json` and the director learns what "これ" refers to when the cut opens mid-story. A missing or unreadable file just drops that one line (logged), leaving the rest of the brief intact. `target_duration` and `tone` are what stop `plan` from defaulting every part to a conservative "shorten" and let `director` deviate from its default ~1-overlay-per-3-5-minutes density. The mechanical stages (`gap_context`, `sentence_split`, `guided_edit`) are deliberately not briefed.

The config file covers all sections, each named after its stage: `general`, `project`, `transcription`, `audio_silence`, `sentence_split`, `gap_context`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `intervals`, `blender`, `publish`, `pipeline`. See `config.example.yml` for the full list of keys and their defaults.

### Choosing an LLM provider

Every LLM stage (`sentence_split`, `gap_context`, `text_filter`, `summary`, `plan`, `director`, `guided_edit`, `publish`) routes through a unified transport backed by the [LiteLLM](https://github.com/BerriAI/litellm) library, so you can point any stage at a local or cloud provider. On a stage's config block, set `provider:` to one of `ollama_chat` (default, local Ollama), `openai`, `gemini`, or `anthropic`, and set `model:` to that provider's model name — LiteLLM receives the combined `"<provider>/<model>"`. Supply credentials with `api_key:` (or the provider's standard environment variable, e.g. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`). Leave `api_base:` empty for cloud providers; for `ollama_chat` an empty `api_base` falls back to local Ollama (`http://localhost:11434`). Each stage chooses its provider independently, so you can mix (e.g. a cloud model for `director` and local Ollama for `guided_edit`).

```yaml
director:
  enabled: true
  provider: "openai"
  model: "gpt-4o"
  api_key: ""        # or set OPENAI_API_KEY in the environment
  api_base: ""       # leave empty for cloud providers
```

### audio_silence: Audio-Silence Detection

The audio_silence stage runs ffmpeg `silencedetect` (inside the whisperx Docker image) on the waveform and writes an editable `{stem}_cuts.txt` cut list. Each non-comment line is a `START - END` silent span (seconds) that will be cut from the video; delete a line to keep that span, or adjust the times. The intervals stage unions these reviewed ranges into its keep-interval excludes. This is acoustic silence — distinct from `intervals.silence_threshold`, which is a WhisperX word-gap heuristic.

```yaml
audio_silence:
  enabled: true       # false = write an empty cut list (no audio cuts applied)
  noise: -30.0        # ffmpeg silencedetect noise threshold, in dB
  min_silence: 0.8    # minimum silence duration to report, seconds
```

### sentence_split: LLM Sentence Re-Segmentation (optional)

The `sentence_split` stage rewrites the WhisperX transcript into one-sentence-per-line units before the text-editing checkpoint. It improves caption readability (each line maps to a complete sentence) and gives downstream LLM stages (director, guided_edit) cleaner line boundaries to reason about. Disabled by default — when `sentence_split.enabled` is `false`, the stage copies `output/transcription/{stem}.{json,txt}` byte-identically into `output/sentence_split/`, so all downstream behaviour is unchanged.

When enabled, the LLM receives a numbered list of GiNZA bunsetsu units (Japanese morpho-syntactic chunks) and returns contiguous bunsetsu-index ranges (`{"sentences":[[0,3],[4,7],…]}`) that map to one sentence each. The stage reconstructs new segments by slicing the original word list at those boundaries — words are only reassigned, never edited, so word timings are preserved and output text is verbatim. Processing is windowed (`sentence_split.window_segments`, default 20 segments per call — the batch size); each window falls back to the original segmentation independently on LLM failure. Windows carry their trailing sentence over the seam, so a sentence split across a window boundary is re-joined with its continuation in the next window (a single-sentence window keeps the boundary, bounding context growth).

**Force split at long silences.** When `force_split` is `true` (default), a long silence detected by the `audio_silence` stage becomes a hard sentence boundary: after the LLM segments a window, every output segment is deterministically split before the first word that starts past the silence's midpoint — but only when the word timings corroborate the silence with a real inter-word gap there. WhisperX often stretches one word across a pause; splitting next to such a word would chop off the sentence's first characters, so those ambiguous silences are skipped instead. Only cut spans at least `force_split_min_silence` seconds long (default 3.0) qualify — `audio_silence.min_silence` is tuned for short jump-cut pauses, which would over-split. The forced split reads the human-editable `output/audio_silence/{stem}_cuts.txt`, so deleting a cut line (to keep that span's audio) also stops it forcing a split, and it survives even a total LLM failure. It adds no extra LLM calls.

```yaml
sentence_split:
  enabled: true
  provider: "ollama_chat"   # see "Choosing an LLM provider" above
  model: "gpt-oss:120b"
  window_segments: 20       # segments per LLM window
  force_split: true         # split sentences at long detected silences
  force_split_min_silence: 3.0   # seconds; only cut spans at least this long
```

Outputs land in `output/sentence_split/{stem}.json` and `output/sentence_split/{stem}.txt`; all downstream stages (summary, text_filter, director, etc.) read from there.

### gap_context: Silent-Gap Visual Context (optional)

The `gap_context` stage runs once per video, between `sentence_split` and `summary`. It looks at the audio_silence `{stem}_cuts.txt` for spans at least `gap_context.min_gap` seconds long, snapshots up to 3 JPEG frames per span (start+0.2s, midpoint, end-0.2s) via ffmpeg inside the whisperx Docker image, and asks a **vision-capable** LLM to describe what's on screen during each gap in one or two plain-text sentences. Disabled by default (`gap_context.enabled: false`) — no Docker calls, and the `summary`/`director` stages render byte-identical to before this feature.

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

`context_lines` controls how much of the surrounding talk the vision LLM is shown: the last N spoken lines before the gap and the first N after it, which help it judge what the silence is *for*. The default of `1` matches the previous fixed behaviour; raise it when a single line is too little to make sense of the scene (it costs prompt tokens on every gap of every video), or set `0` to send frames alone.

Before calling the vision LLM at all, an ffmpeg SSIM comparison runs over every *consecutive* pair of a gap's extracted frames (a 3-frame gap yields 2 pairs: first-vs-mid, mid-vs-last) and takes the minimum score — comparing only the first and last frame would miss a camera pan-away-and-return, since the middle frame (already extracted, already paid for) is the one that would reveal the on-screen action. When that minimum is at least `gap_context.static_ssim` (default `0.96`), the gap is recorded straight to `{stem}_gaps.json` as `static: true` with a placeholder description and **no vision-LLM call is made** — cutting real-run cost on footage with long pixel-static spans (e.g. a locked-off tripod shot). Set `static_ssim: 0` to disable the prefilter and always call the vision LLM.

Output is a hand-editable intermediate, `output/gap_context/{stem}_gaps.json` (`{"gaps": [{start, end, frames, description, static}]}`), plus the extracted frames under `output/gap_context/frames/{stem}/`. The vision LLM classifies each gap as action or static (its reply starts with `ACTION:`/`STATIC:`); gaps marked `"static": true` — scenes with no meaningful on-screen change — are kept in the file for review but **excluded** from the summary/director prompts, so tripod footage of nothing happening doesn't drown out the gaps that matter. Deleting a line from `output/audio_silence/{stem}_cuts.txt` both keeps that span's audio (as with sentence_split's force-split) *and* removes it from gap-context snapshotting; editing a gap's `description` directly changes what `summary`/`director` are told about it, and flipping `"static": false` forces a gap back into those prompts. `--from-stage gap_context --to-stage gap_context` re-runs just this stage against an already-edited `_cuts.txt` (reusing the transcription/audio_silence/sentence_split outputs already on disk) without redoing transcription.

When enabled, `summary` appends a `## Silent gaps (visual context)` block (each gap anchored to the transcript line it follows) to its per-video prompt, and `director`'s numbered transcript gets an indented, un-numbered `[silent gap N.Ns: description]` line after the relevant line — the director's prompt explains that a `keep` op spanning that line and the next one (`[N, N+1]`) preserves the silence if the visual content is worth keeping.

### text_filter: Text Editing (LLM optional)

The text-editing checkpoint always produces `{stem}_edits.txt`. When `text_filter.use_llm` is `false` (default), it copies the transcription `.txt` as-is. When enabled, it runs LLM-based transcription correction and writes output with `{{old->new}}` markers preserved for human review.

```yaml
text_filter:
  use_llm: true
  provider: "ollama_chat"   # see "Choosing an LLM provider" above
  api_base: ""              # empty -> local Ollama; leave empty for cloud providers
  model: "qwen3.5:4b"
  thinking: "low"   # thinking mode: true/false, or "low"/"medium"/"high" for supported models
```

The LLM uses `{{old->new}}` inline patch syntax to mark corrections. Human editors can then review and modify the markers in `_edits.txt` before the intervals stage applies them.

Human editors can also wrap a span in `<keep>...</keep>` to force-preserve the audio under that text. The intervals stage derives the time range from the first wrapped word's start to the last wrapped word's end and carves it out of both the word-gap silence and any overlapping `_cuts.txt` ranges. The marker may be opened on one line and closed on a later line, so a single `<keep>` block can span multiple WhisperX segments and preserve the silences between them. The marker is added after the LLM filter has produced `_edits.txt`, so the LLM never sees it.

`<speed factor="N.N">...</speed>` is a companion marker carrying a playback-speed annotation. Unlike `<keep>`, it does **not** force-keep audio — its span does not carve silence out of the excludes, so silence inside it is still cut and the speed applies only to the surviving spoken parts (nest inside `<keep>` to preserve the audio too). The intervals stage records each marked span as an entry in a top-level `speed_ranges` array (`{start, end, factor}`) in `_intervals.json`, independent of `keep_intervals`. The blender stage splits keep intervals at those boundaries, so a `<speed>` span may cover an arbitrary sub-range of a keep interval (or span several), and adds a Blender VSE Speed Control effect strip over each sped-up sub-range so it plays at the requested speed in the final `.blend`. A `speed_ranges` entry that falls entirely on cut content simply matches no surviving interval and is ignored.

`thinking` enables chain-of-thought reasoning for supported models (e.g. qwen3, deepseek-r1); it maps to LiteLLM's `reasoning_effort` (best-effort per provider). Set `true`/`false`, or a string level like `"low"`, `"medium"`, `"high"` for models that support granular control (e.g. Qwen 3.5). The pipeline uses only the final answer, not the reasoning trace.

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
- `--config FILE` — path to a YAML config file; config values fill in between CLI overrides and built-in defaults.
- `--language LANG` — ISO 639-1 language code passed to WhisperX (default: `ja`). Also settable via `transcription.language` in config.
- `--from-stage NAME` — start from stage `NAME`, reusing earlier stage outputs. `NAME` is a stage name: `transcription`, `audio_silence`, `sentence_split`, `gap_context`, `summary`, `text_filter`, `plan`, `director`, `guided_edit`, `intervals`, `blender`, `publish`. Also settable via `pipeline.from_stage` in config.
- `--to-stage NAME` — stop **after** stage `NAME` (inclusive); later stages are skipped. Same stage names as `--from-stage`, and must not precede it. Defaults to `publish` (run to the end). Also settable via `pipeline.to_stage` in config. Combine with `--from-stage` to run a window of stages, e.g. `--from-stage summary --to-stage director`.
- Defaults: input videos under `src_video/`, outputs under `output/`.
- If `--source` contains `/`, it is treated as the exact path; otherwise it is resolved inside `--input-videos-dir`.
- `silence_threshold` and `min_keep` default to `1.5` and `1.0` (overridable via config).
- `pre-margin`/`post-margin` extend keep intervals before/after by default `1.0s` and merge overlaps.
- `--align-model` overrides the HuggingFace model used for WhisperX forced alignment. For Japanese (`ja`), defaults to `vumichien/wav2vec2-large-xlsr-japanese` which showed better alignment scores than the WhisperX built-in default (`jonatasgrosman/wav2vec2-large-xlsr-53-japanese`); for other languages the WhisperX built-in model is used.

## Stage Commands

### transcription only (WhisperX)

```bash
docker compose run --rm --user "0:0" whisperx \
  _ \
  "myvideo.mp4" \
  --output_dir /output \
  --output_format all \
  --language ja \
  --compute_type float16 \
  --batch_size 16
```

Notes:

- Input files are mounted to `/app` via `${INPUT_VIDEOS_DIR:-src_video}:/app` (set env vars or rely on defaults).
- Output files are mounted to `/output` via `${OUTPUT_DIR:-output}:/output`.
- This image tag does not accept `--word_timestamps`.
- No diarization flags are used.

### audio_silence only (audio-silence detection)

There is no standalone CLI for *this* stage — it runs through the pipeline
orchestrator like the rest (the `python -m nagare_clip.publish.thumbnail`
re-render CLI, documented under "publish only" below, is the one exception).
Run just this stage with `--from-stage`/`--to-stage` (it drives the ffmpeg
`silencedetect` Docker call and parses its stderr into `{stem}_cuts.txt`
internally):

```bash
./scripts/run_pipeline.sh --from-stage audio_silence --to-stage audio_silence \
  --source myvideo.mp4 --config my_project.yml
```

Set `audio_silence.enabled: false` in config to write an empty cut list instead.

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

Keep-interval silence detection uses WhisperX word timings (`word.start`/`word.end`) with a per-word max-span cap (0.6s) so inflated token ends do not mask real pauses. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries are attached to the preceding content word, producing natural subtitle line-break units. It detects large intra-bunsetsu character gaps (> 0.6s) caused by WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster so silence is not hidden inside a single bunsetsu. Caption chunks use bunsetsu-level timing (`end = min(start+0.02s, next_bunsetu_start)`) and are split on detected silence gaps and keep-boundary crossings. Captions are preserved as transcript chunks and the interval stage expands keep intervals to include caption spans so subtitle text is not dropped at the Blender stage, then re-applies minimum keep duration (`min_keep`) to avoid tiny strips.

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

Reuses `output/summary/summary.json`, `output/plan/plan.json`, each source's
`output/director/{stem}_director.json` and `output/intervals/{stem}_intervals.json`,
so it can be re-run on its own to get a different set of title/thumbnail
candidates without touching the cut. Any of those inputs missing degrades just
the part that needed it (no intervals → no chapter timestamps).

#### Re-rendering thumbnails without re-running the LLM

Picking a background still and nudging a colour is iterative, and re-running
the `publish` stage above would call the LLM again and hand you *different*
copy than the one you were judging. `output/publish/publish.json`'s
`thumbnail_copy` (including the model-authored font/colour/position values) is
a hand-editable contract, the same way `_director.json` is for the edit — this
CLI reads it back and re-runs ImageMagick with **no LLM call at all**:

```bash
uv run python -m nagare_clip.publish.thumbnail \
  --publish-dir output/publish \
  --config my_project.yml \
  --background frames/myvideo/2528.021.jpg
```

Pass `--config` explicitly, the same as the pipeline CLI — this CLI does not
pick up a project config file on its own, so without it the render falls back
to built-in defaults (1280x720, no `-font` flag) instead of your configured
canvas size and CJK font slots.

`--background` (a path relative to `--publish-dir`, or absolute) overrides
`publish.thumbnail.background` for that run only; omit it to reuse whatever is
already configured, or the first frame-shortlist candidate. Hand-edit a set's
`fill`/`stroke`/`gravity`/… in `publish.json` first, then re-run this CLI to
see the change. See [`docs/stages/publish.md`](docs/stages/publish.md) for the
full style-key table.

## Operational Notes

- `scripts/run_pipeline.sh` currently runs WhisperX as root (`--user "0:0"`) for compatibility with this image/runtime.
- As a result, transcription output files can be root-owned on host.
- If needed, fix ownership after run:

```bash
sudo chown -R "$(id -u):$(id -g)" output cache
```
