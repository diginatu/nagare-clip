# publish — the `nagare-publish` skill and its tools

Publishing is **not a pipeline stage**. The pipeline ends at `blender`; the
upload material — title candidates, a description lead, YouTube chapters and
the thumbnails — is written by a coding agent working with the user through the
`nagare-publish` agent skill (`skills/.apm/skills/nagare-publish/`), which calls
deterministic tools. **No LLM call anywhere** under `src/nagare_clip/publish/`
or `src/nagare_clip/render/`: the agent writes the words and chooses the look;
the tools compute every time, extract the stills, validate, write the files and
composite the thumbnails. Uploading stays manual.

The LLM publish (one blind copy call, a vision call per still, a text-only
pairing call, `frames.json`) is deleted. Its complexity existed to make up for
models that could not see the result; a coding agent Reads the stills and the
rendered `set{N}.jpg` with its own image input and iterates. Every key it used
fails config loading (`config.REMOVED_KEYS`, below).

All paths below are under `general.output_dir` (`<output_dir>`, default
`output`), relative to the video directory the tools run in.

| Path | What it is |
|------|------------|
| `<output_dir>/publish/edit.json` | The last edit `write` accepted, saved verbatim — what a later session revises (there is no `dump`) |
| `<output_dir>/publish/publish.json` | The material as data, seconds resolved; the hand-editable contract `render` reads |
| `<output_dir>/publish/publish.md` | The reviewable file: title candidates, a copy-pasteable description, chapters, thumbnail copy, the stills |
| `<output_dir>/publish/stills.json` | The stills `frames` extracted (shortlist and `--line` stills), with their finished times |
| `<output_dir>/publish/frames/{stem}/{t:.3f}.jpg` | The stills themselves (`t` = source seconds) |
| `<output_dir>/render/…` | The thumbnails — see [`render.md`](render.md) |

Nothing rebuilds `publish.json` when an upstream edit moves the timeline: like
every other output it goes stale, and the agent runs `write` again with its
saved edit (times are recomputed; a chapter on a line that is now cut is
reported).

## The tools — `src/nagare_clip/publish/tools/`

`scripts/publish_tools.sh` → `python -m nagare_clip.publish.tools`, run **in the
video directory** (the script keeps the CWD), `--config` repeatable and
deep-merged left to right like the pipeline and the director tools
(`cli._load_cfg` → `get_effective_config`). The project is loaded with the
director's own `director.tools.project.load_project()`, so **line numbers are
the director's display numbering** (`director.display.DisplayView`) and every
tool sees the view the director saw.

| command | does | writes |
| --- | --- | --- |
| `view [--lines A-B \| --compact]` | the page below | nothing |
| `frames [--line N[@pos]]... [--at T]... [--sheet]` | the shortlist, or stills at given lines / finished-video times, in one Docker run; `--sheet` composites the list into contact sheets | `publish/frames/…`, `publish/stills.json`, `publish/sheets/sheet{K}.jpg` |
| `chapters N [N ...]` | try chapter lines: finished time, length, `write`'s problems for them | nothing |
| `write --edit F\|- [--no-render]` | validate; all problems or write `edit.json`, `publish.json`, `publish.md`, then render | `publish/`, `render/` |
| `render` | `render.run.run_render()` over `publish.json` as it is on disk | `render/` |

Exit codes: `0` ok; `1` a problem with the work — a validation problem
(`write`, `chapters`, a bad `frames --line`) or a failed Docker run in `frames`
(`cmd_frames` catches its `PipelineError`); `2` a tool-usage error, including `view --lines` starting past the last display line (`the video has N display lines`), caught by
`cli.main` (`PipelineError`/`OSError`/`ValueError`: no project, unreadable edit
JSON, …), printed as `error: …`. A closed stdout (`view | head`) exits quietly
(`broken_pipe.silence_stdout`).

### `view`

One page, sections separated by blank lines:

1. `output dir: <ctx.output_dir>` — always the first line, so the agent never
   guesses `<output_dir>`.
2. the editorial brief (`brief.format_brief`), the summaries
   (`director.load.project_context` over `summary/summary.json`), the
   director's `plan.md` (`## Plan`);
3. `## Captions` — the director's `overlay`/`timelapse` op texts as
   `- M:SS  text`, each text once (first use), sorted by **finished** time
   (`_captions`): the time of the op's first display line on the finished
   timeline, `✂` when that line does not survive, `—` when there is no
   timeline (untimed rows sort last). Sources are walked in playback order
   (the placements' stem order).
4. `## Fonts (render.fonts — …)` — the **effective** `render.fonts` slots from
   the merged config, the first marked as the fallback for a line with no
   `font` (`render.thumbnail.fallback_font`); with none set it says ImageMagick's
   default face may draw Japanese as nothing and to ask the user to set
   `render.fonts`.
5. `finished runtime M:SS (N.N s)` (`timeline.total_duration`), or
   `finished runtime unknown — no intervals yet`.
6. the lines. By default one row per display line, `N  M:SS  text`, where
   `M:SS` is the line's first surviving moment in the finished video, `✂` a
   line that does not survive and `—` no timeline; `--lines A-B` narrows them.
   **`--compact`** (mutually exclusive with `--lines`) replaces them with the
   director's played transcript (`director.played.played_transcript`, the same
   rows `director_tools.sh transcript` prints) with each row prefixed by its
   finished time through `played_transcript(..., when=...)`: only what plays, in
   playback order, a cut as one `✂` row and a timelapse as one `⏩` row — a long
   video is a few screens instead of thousands of rows. The finished timeline
   has the last word: lines the director's ops let play but whose footage did
   not survive `intervals` become one `✂ dropped lines A-B — not in the
   finished video` row (never a `✂` in the time column), and in a source the
   preview cannot time (headed `(no line timings, so this source's ops are not modelled: …)`, e.g. a speechless source
   kept under a `keep`) a silence line is listed whenever the finished video
   plays it, so an opening with no talking still shows at `0:00`. It reads the director
   files on disk (`current_state`); when they do not map onto the view it warns
   on stderr, lists the unmapped items and falls back to the per-line rows.

### `frames` and `stills.json`

Without `--line`: the **shortlist**, written fresh (`frames.plan_stills`):

1. `thumbs.select_candidates()` per source, over that source's director ops and
   its sentence_split `{stem}.json` segment times — the payoff moments the
   director marked:

   | Op | Still(s) |
   |----|----------|
   | `overlay` | midpoint of the line it is anchored to (a line's first frame is often still the previous shot) |
   | `keep` | midpoint of the whole event it rescued |
   | `timelapse` | **both** boundaries — start of the first line, end of the last (before/after) |

   Coinciding moments collapse to one candidate, keeping the higher-priority
   kind.
2. When the finished timeline is available, every candidate **not on it**
   (`first_surviving_time` is `None`) is left out **before** the cap, so the cap
   fills with stills a viewer can actually see; `N shortlist still(s) not in
   the finished video were left out` is printed.
3. `thumbs.cap_candidates(publish.max_frames)` keeps the best kinds
   (`overlay` > `timelapse-start`/`-end` > `keep`; `0` = no limit).

With `--at T` (repeatable; `M:SS`, `H:MM:SS` or seconds, decimals allowed; a bad
format is an argparse error): one still per time of the **finished** video, via
`timeline.source_at()` (the placement with `tl_start <= t < tl_end`, the last
also accepting `t == tl_end`; source time `src_start + (t − tl_start)·speed`),
kind `at`, label `at M:SS`, `line` the display line containing the source time.
It merges like `--line` and may be combined with it; every still of the call is
one Docker run. No finished timeline, or a time past the runtime, is a problem
(exit 1, nothing extracted, all listed).

`--sheet` (after the extraction and the `stills.json` update) composites every
still now in `stills.json`, in the printed playback order, into
`<output_dir>/publish/sheets/sheet{K}.jpg` — at most 12 per sheet, 4 columns,
tile width `publish.frame_width`, each tile labelled `#<i>  L<line>  <M:SS>`
(ASCII only: `-` for no line or no timeline, `cut` for a still not shown; the printed listing keeps `—`/`✂`; `i` is the row's `#i` in the printed list, which now
leads each row). Stale `sheet<digits>.jpg` go first; a missing still file is
skipped. Host ImageMagick through `run_magick` (`frames.sheet_commands()`, an argv
list; `%` in a label is doubled because `-label` expands it; the
`render.fonts` fallback face is passed as `-font` when set). A magick failure is
`error: …` on stderr, exit 1, with `stills.json` already written. No LLM.

With `--line N[@pos]` (repeatable): one still per request at
`start + pos·(end − start)` of the line's footage (`pos` in `[0, 1]`, default
`0.5`; speech lines and silence lines both have a span, `lines.line_span`), kind
`line`, label `line N @pos`. These are **added** to the existing
`stills.json` (a still at the same path replaces the old row); a requested line
is never dropped for being cut — the agent asked for it. A line that does not
exist or has no timing is a problem (exit 1, nothing extracted). Plain `frames`
again rewrites the list from scratch, dropping added stills.

**Extraction** (`frames.extract_stills`): every still of one call goes through
**one** `docker compose run` of the whisperx image via
`pipeline.external.build_snapshot_batch_cmd` (script on stdin — see
[`gap_context.md`](gap_context.md) for the argv limit), at
`publish.frame_width`. Container startup dwarfs the per-still ffmpeg work. A
Docker-level failure (`OSError`/`CalledProcessError`) raises a `PipelineError`
carrying the exception and the stderr tail (`failure_reason`); `frames` prints
it as `error: …` on stderr and exits 1. A still that a batch which otherwise
ran did not write is dropped from the list and reported as
`warning: frames: no still written at <t>s for <stem>; dropped` on stderr (the
`warnings` list of `extract_stills`; the CLI logs only errors, so a
`logging.warning` alone would never reach the agent).

`stills.json` is a list of
`{stem, source_time, kind, label, line, path, timeline_time}` — `path` relative
to `<output_dir>/publish/` (what a thumbnail's `background` names), `line` the
display line containing the moment, `timeline_time` its finished seconds or
`null`. There are no descriptions: the agent Reads the JPEGs. The command
prints `line  M:SS  kind  label  path` rows in playback order (untimed last),
any left-out note, then `stills in <output_dir>/publish (the paths above are
relative to it)`.

### `chapters`

`chapters N [N ...]` resolves the given display lines with
`edit.resolve_chapters()` — **the same function `write` validates with**, so the
preview cannot disagree with the write — and prints `line  time  length  text`
sorted by finished time, the first chapter at `0:00` and measured from there,
exactly as `write` renders it (`chapter_timestamps`) (the text is the line's own, without the view's
timing bracket, cut to 40 characters), marking a chapter shorter than
`publish.min_chapter_duration`; then the fewer-than-three warning and every
problem (stderr), listed in the edit's chapter order (`chapters[0]` first),
as `write` lists them. Exit 1 on a problem; writes nothing.

### `write`

`write --edit F|-` reads the edit (a file, or stdin), runs
`edit.validate_edit()` and, on **any** problem, prints
`N problem(s); nothing written:` and all of them on stderr and exits 1 having
written nothing. Otherwise it prints the warnings, then writes
`edit.json` (the input verbatim), `publish.json` (`build.build_publish`) and
`publish.md` (`document.render_publish_markdown`, honouring
`general.image_markup`), prints each path, and runs the render
(`_render`, the same as the `render` command) unless `--no-render`.

Two things happen around that. `write` and `render` both end by refreshing
`output/index.md` (`_refresh_index`: `index_page.write_index` with
`PipelineContext.blend_path`, exactly as `pipeline.cli` does; an exception
only prints `warning: index.md not refreshed: <e>` on stderr). And every
successful `write` saves `publish/edit_lines.json`, `{"<line>": "<bare
display text>"}` for each chapter line (`_bare_text`, what `chapters` shows),
because `edit.json` stores line numbers and the display numbering shifts when
transcripts are re-run or `director.silence_line_min` changes. Before
overwriting it, `write` — and `chapters` for the lines it is given — compares
each chapter line's current text with the saved one and prints, on stderr,
`warning: line N read 「old」 when edit.json was last written; it now reads
「new」 — the numbering may have moved; check this chapter`. It is a warning,
not a problem (the file is still written); a missing or unreadable
`edit_lines.json` gives no warnings.

### The `.blend`'s real length (`blend_check.py`)

The tools never read the `.blend`; every time is computed from the intervals
(below). A user who opens `<output_dir>/blender/<stem>_edited.blend`
(`PipelineContext.blend_path`) and trims it by hand changes the real video
behind the tools' back — once every chapter after the trim was late, by up to
~56 s, and nobody saw it until the user watched the video. So `view` (after its
output) and `write` (after validation passes, before writing) open the `.blend`
in headless Blender (`blend_check.read_blend_range`: `blender --background
--factory-startup <blend> --python-expr …`, which prints one
`NAGARE_BLEND_INFO {json}` line; about half a second; Blender is a host binary,
as for the blender stage) and read the scene's frame range — what Blender
renders: `(frame_end − frame_start + 1) × fps_base / fps` (`frame_end` is
inclusive). When that differs from `total_duration(placements)` by
`MISMATCH_SECONDS` (1.0 s) or more, `mismatch_warning()` prints on stderr both
lengths and the difference (each `M:SS` and seconds, shorter/longer), that
chapter and still times can be off from the real video, and the fix: carry the
hand edit back into the director's edit and re-run through `blender`, or check
each chapter in the rendered video before uploading. It is only a warning —
`write` still writes and exits 0. Below the threshold it is silent: the blender
stage rounds each interval to whole frames, so an untouched `.blend` differs by
a fraction of a second (measured: 0.0 s on a 28-minute, 211-strip project). No
`.blend` yet, or no timeline, → no check; Blender missing or failing → one
`warning: the .blend's length was not checked …` line with the reason. `chapters`
does not check (its times are the same; `write` will say so).

## The edit file

```json
{
  "titles": ["まさかの水漏れ…ポンプ交換3日目", "ポンプ交換3日目、原因はここだった"],
  "lead": "Description body; the tool appends the chapter list",
  "chapters": [
    {"line": 1,  "title": "オープニング"},
    {"line": 40, "title": "分解して原因を探す"}
  ],
  "thumbnails": [
    {
      "background": "frames/a/63.200.jpg",
      "gravity": "south", "offset": "+0+40", "shadow": {"blur": "0x6"},
      "lines": [
        {"role": "tag",  "text": "3日目", "fill": "#FFCC00", "font": "sans-bold"},
        {"text": "まさかの水漏れ", "pointsize": 140, "stroke": "black", "strokewidth": 8}
      ]
    }
  ]
}
```

The complete state: `write` replaces everything written before. Top-level keys
are exactly `titles`, `lead`, `chapters`, `thumbnails` (`edit.TOP_KEYS`);
`lead`, `chapters` and `thumbnails` may be absent (`""`/`[]`).

- `titles` — candidates, at least one; the human picks at upload.
- `lead` — the description body. The description is `lead`, a blank line, then
  the chapter lines (`edit.assemble_description`, empty parts dropped).
- `chapters` — `{line, title}` in display numbering. **Never a time.**
- `thumbnails` — one set per alternative thumbnail (`set1`, `set2`, …): a
  required `background` (a path relative to `<output_dir>/publish/`, or
  absolute — any image, not only a still), the set keys `gravity`/`offset`/
  `shadow`/`line_gap`, and `lines` (any number, stacked top to bottom), each with `text`,
  an optional `role` (`tag`/`hook`/`subtitle`; absent → `hook`, which only picks
  the preset defaults for unstated keys) and the line keys `font` (a
  `render.fonts` slot), `pointsize`, `fill`, `stroke`, `strokewidth`. The
  vocabulary and its allowlist are [`render.md`](render.md)'s.

### Problems — `write` writes nothing (`edit.validate_edit`)

Every problem at once:

- **structure** — the edit is not an object; an unknown key at the top or in a
  chapter; a wrong type (`lead` not a string, `chapters`/`thumbnails` not a
  list, a chapter `line` not an integer or `title` empty); empty `titles` or an
  empty title;
- **title** — over 100 characters (`MAX_TITLE_CHARS`), or containing `<` or `>`
  (YouTube rejects both);
- **description** — the assembled description over 5000 characters;
- **chapter** (`resolve_chapters`) — a line that does not exist; a line with no
  timing; a line entirely cut from the finished video; two chapters at the
  same finished time; a chapter shorter than `publish.min_chapter_duration`
  (YouTube's 10 s; formerly merged silently — the agent now re-chooses); **any**
  chapter while the finished timeline is unavailable;
- **thumbnail** — a set that is not an object; a missing/blank `background` or
  one that does not exist; no `lines`; a line that is not an object or has
  empty `text`; and whatever `render.thumbnail.set_style_problems()` /
  `line_style_problems()` report: an unknown style key, a value outside
  render's allowlist (reported, **not** replaced by the preset value as render
  itself would), a `font` that is not a `render.fonts` slot, an unknown `role`.

### Warnings — written anyway

- fewer than three chapters, zero included (`chapter_count_warning`): YouTube
  links the timestamps but draws no segmented bar.
- the `.blend` is 1 s or more shorter/longer than the computed timeline (a hand
  edit in Blender; see *The `.blend`'s real length* above).

## Timing: the tool's, never the edit's

The authority rule, precisely: after `intervals`, `intervals/timeline.json` is
the authority on the order, the cut and every finished-timeline time. The tools
turn a display line into its **source** span from the transcript timings
(`lines.line_span` — a fact about the footage, not an edit decision); only the
manifest, through `build_placements`, places that span on the finished
timeline. No order and no cut is re-derived here. `lines.finished_placements()` lays the finished timeline out with
`pipeline.stages.ordered_sources_for(ctx)` (the manifest's segments, sliced,
in playback order) and `publish.timeline.build_placements()`; `None` when no
source has intervals yet (blender not run — `view`/`frames` show `—`, `write`
refuses every chapter, zero chapters still write).

`build_placements()` reproduces the blender concatenation in **seconds** —
sources in order, keep intervals back to back, each split at its speed
boundaries with the very same `split_intervals_by_speed()` blender uses. That
helper lives in `blender/frames.py`, not `blender/timeline.py`, because `bpy`
does not exist in this process and a second copy of the arithmetic would
eventually disagree with the strips (`tests/publish/test_timeline.py` guards the
import in a fresh interpreter). Seconds instead of frames cost a fraction of a
second per interval, invisible at `M:SS`.

A display line → its source span (`lines.line_span`: a speech line's segment
times, a silence line's silence span) → `lines.line_time()` =
`timeline.first_surviving_time(placements, stem, start, end)`: the earliest
moment of the span that survived (a line's opening is routinely cut), `None`
when it was cut entirely, a compressed position inside a timelapse (`/ speed`).
Under a reorder the placements are already in playback order, so chapters are
resolved on the real finished timeline and then **sorted by time**
(`resolve_chapters`) — see [`order.md`](order.md).

## YouTube's chapter conditions

YouTube draws a segmented progress bar only when the first timestamp is
`0:00`, there are at least three, they ascend, and each runs at least 10 s.
The tool makes or checks each:

- **`0:00`** — `chapters.chapter_timestamps()` renders the first chapter as
  `0:00` whatever its first surviving moment was. Its span is therefore
  measured from `0.0` (`chapters._spans`): a chapter whose footage starts at 8 s
  is not a short chapter.
- **ascending** — chapters are sorted by finished time; two at one time are a
  problem.
- **10 seconds** — a shorter chapter is a problem (`publish.min_chapter_duration`).
  Nothing merges a short chapter away.
- **three** — a warning only; the list is always written, because YouTube
  auto-links timestamps regardless.

`format_timestamp()` **truncates** (rounding 9.9 s up to `0:10` would point past
the chapter's start). `publish.json` still carries `chapters_qualify` and
`chapter_issues` (`chapters.chapter_issues()`), and `publish.md` says why the
bar would not be drawn.

## `publish.json`

`build.build_publish()` — the shape the LLM publish wrote, so `render` and
`index_page` read it unchanged (`document.empty_publish()` is that shape, empty):

```json
{
  "titles": [...], "lead": "...", "description": "lead\n\n0:00 …\n1:23 …",
  "chapters": [{"time": 83.2, "timestamp": "1:23", "title": "…"}],
  "chapters_qualify": true, "chapter_issues": [],
  "thumbnail_copy": [{"background": "…", "gravity": "…", "lines": [{"role": "hook", "text": "…"}]}],
  "thumbnails": [{"stem", "source_time", "timeline_time", "kind", "label", "line", "path"}]
}
```

`thumbnail_copy` is the edit's sets with each line's absent `role` filled with
`hook`; `thumbnails` is `stills.json` re-timed on the current timeline (the
frame shortlist table in `publish.md`, and `render`'s fallback background for a
hand-edited set that names none). `publish.md` embeds images per
`general.image_markup` through `markdown.embed_image`.

## Zero LLM calls

A property of the code: nothing under `publish/` or `render/` names
`llm_client`/`call_llm` (static AST check plus a fresh-interpreter import,
`tests/render/test_run.py`), and `tests/publish/tools/test_zero_llm.py` runs
`view`, `view --compact`, `frames` (Docker faked at the runner), `chapters`,
`write` and `render` (magick faked) with `call_llm` replaced wherever it is
bound and `litellm.completion` raising. `publish/thumbs.py` imports
`DirectorOp` under `TYPE_CHECKING` only for the same reason.

## The skill

`skills/.apm/skills/nagare-publish/SKILL.md` holds the workflow — `view
--compact` → write the copy (titles, lead, chapters via `chapters`, thumbnail
text) **before** looking at any still → `frames` (+ `--line`) → Read every still
→ choose a background and look per set → `write` → Read each render and
`render.md` → revise — plus the tools, the edit format and the enforced rules.
Writing the copy first keeps the copy-before-frames order the blind copy call
used to guarantee by code. References: `shortlist.md` (each kind and how far to
trust a label), `thumbnail-craft.md` (style vocabulary, legibility, placing
text where the picture is empty), `youtube-chapters.md` (the four conditions).
It ships in the same APM package as `nagare-director` (see
[`director_skill.md`](director_skill.md#apm-layout)).

## Config

`publish:` keeps only the tools' settings:

| Key | Default | Meaning |
|-----|---------|---------|
| `min_chapter_duration` | `10.0` | A shorter chapter is a `write` problem |
| `max_frames` | `24` | Cap on shortlist stills (`0` = no limit); overlays outrank keeps |
| `frame_width` | `1280` | Downscale width of the extracted JPEGs |

`general.image_markup` (`html`/`markdown`) picks how `publish.md`, `render.md`
and `output/index.md` embed images. The canvas, fonts and line gap are
`render:`'s ([`render.md`](render.md)). The `project:` brief is no longer
appended to any publish prompt (there is none); `view` prints it.

Removed (`config.REMOVED_KEYS`; loading fails naming every path):
`publish.enabled`, `provider`, `api_base`, `model`, `api_key`, `temperature`,
`reasoning_effort`, `timeout`, `response_format`, `max_retries`,
`retry_temp_step`, `retry_temp_cap`, `prompt`, `pairing`, `describe_frames`; and
`render.enabled`.
