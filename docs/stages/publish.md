# publish stage — title, description, chapters, thumbnail material

Runs **once project-wide, after `blender`** — the last stage. Everything it
emits already existed somewhere in the pipeline (`summary.json` knows what the
video is about, the `project:` brief knows who it is for, `plan.json` has the
editorial shape, the intervals know what survived), but nothing past the
`.blend` used any of it, so it was retyped by hand for every upload.

Output is **reviewable, not published**: the stage writes files, a human
uploads.

| Path | What it is |
|------|------------|
| `output/publish/publish.md` | The reviewable file: title candidates, a copy-pasteable description, thumbnail copy + renders, the frame shortlist |
| `output/publish/publish.json` | The same material as data — including the model-authored look, the hand-editable contract the re-render CLI reads |
| `output/publish/frames/{stem}/{t:.3f}.jpg` | Candidate stills at the director's payoff moments |
| `output/publish/thumbnails/set{N}.jpg` | One rendered thumbnail per copy set (`N` matches `publish.md`'s `### Set N`) |

Disabled by default (`publish.enabled: false`) → `publish.json` holds the full
shape with nothing in it, `publish.md` says the stage is off, and no LLM or
Docker call is made.

## Two halves

The stage splits cleanly along what an LLM can and cannot know.

**Copy** (one LLM call, `publish_llm.py`) — title candidates, the description
lead, chapter *titles*, thumbnail *copy*.

**Timing** (deterministic, `timeline.py` + `chapters.py`) — every timestamp.
The LLM is never asked for one; it cannot see the finished timeline.

## Why the timestamps can only be computed here

A chapter timestamp needs the mapping from **source** time to **finished**
time, and that mapping exists nowhere else: the `intervals` stage knows which
spans survive, the `blender` stage knows how `speed_ranges` compress them, and
only the two together say what "5:00 into the video" points at.

`timeline.build_placements()` reproduces the blender stage's concatenation —
sources in order, keep intervals back to back, each split at its speed
boundaries — reusing the very same `split_intervals_by_speed()` the placement
loop uses. That helper therefore lives in `blender/frames.py`, not
`blender/timeline.py`: the publish stage runs in the pipeline process where
`bpy` does not exist, and a second copy of the arithmetic would eventually
disagree with the strips it is supposed to describe. `tests/publish/test_timeline.py`
guards the import in a fresh interpreter, because other tests stub
`sys.modules["bpy"]` and would mask a real `bpy` import.

The mapping is computed in **seconds**, while blender places strips in whole
frames. That costs a fraction of a second per interval — invisible at the
`M:SS` resolution a chapter list is written in, and it saves the stage having
to know the source's frame rate.

`first_surviving_time(placements, stem, start, end)` is what a part is looked
up with, not a point lookup:

- a part's opening seconds are routinely cut, so the timestamp is taken at the
  earliest moment of the part that actually survived;
- a part that was cut entirely returns `None` and **drops out** of the list;
- a part inside a timelapse gets its *compressed* position (`/ speed`).

## The four YouTube conditions

YouTube renders a timestamp list as chapters (a segmented progress bar) only
when **all** of these hold:

1. the first timestamp is exactly `0:00`
2. there are at least three of them
3. they are in ascending order
4. each chapter runs at least 10 seconds

The mapping rarely satisfies them on its own, so `chapters.py` *makes* them
hold rather than hoping:

- **`0:00`** — `chapter_timestamps()` forces the first entry, whatever the
  first surviving moment actually was.
- **10 seconds** — `build_chapters()` drops any chapter whose rendered span
  falls short; the previous chapter's title stretches over it. The first
  chapter has no previous neighbour, so it gives way to the *next* one, which
  then inherits `0:00`. Dropping only lengthens the survivors, so the sweep
  repeats until nothing is short — or until one chapter is left, which is as
  far as merging can go.
- **ascending** — an entry that does not advance past the previous one is
  dropped rather than reordered; a title out of order would be wrong wherever
  it landed.

Two details worth not re-deriving:

- The **first** chapter's span is measured from `0.0`, not from its own
  timestamp, because that is how it renders. A part whose surviving footage
  starts at 8s is not a short chapter.
- `format_timestamp()` **truncates**. Rounding 9.9s up to `0:10` would point a
  viewer past the moment the chapter begins.

**The list is always written**, qualifying or not. YouTube auto-links
timestamps in a description regardless of these rules, so a list of two still
lets a viewer jump — it simply does not draw the segmented bar.
`chapter_issues()` reports what is missing (`chapters_qualify` in the JSON, a
note in the markdown); it never suppresses the output.

Chapter titles come from the LLM keyed by the part's **original** 1-based index
— the numbering it was shown — so a dropped part never shifts the others'
titles. A part the LLM skipped falls back to that part's `summary.json` text.

## Copy: candidates, not answers

**Titles** come back as several candidates. Hook quality varies a lot between
attempts and picking from a list is cheap; committing to a single generated
title is not. `publish.temperature` defaults to `0.7` — higher than the editing
stages — because candidates that read as rephrasings of each other are not
candidates.

**Thumbnail copy** is alternative *sets*, each **one to three** lines, each
line tagged `tag` / `hook` / `subtitle`. The count is never normalised: the
previous project's thumbnail happened to use all three, but a punchier video
may want a hook alone, and the layout should follow the copy rather than the
copy being padded to fill a template. A set longer than three lines is trimmed
(logged); an unknown role drops that line only.

The LLM is handed the on-screen captions the director placed
(`collect_overlay_texts()`, from `overlay` and `timelapse` ops) alongside the
summaries and the plan directions — those captions are the edit's own account
of where the payoffs are, which is exactly the register a title or hook wants.

Parsing follows the house style: a hard failure (invalid JSON, no usable
`titles`) retries via `llm_retry`; everything else is dropped item by item and
logged. All attempts failing degrades to empty copy — the stage still writes
its files.

## Thumbnail frame candidates

`thumbs.select_candidates()` reads the director's ops rather than guessing:

| Op | Still(s) |
|----|----------|
| `overlay` | midpoint of the line it is anchored to |
| `keep` | midpoint of the whole event it rescued |
| `timelapse` | **both** boundaries — start of the first line, end of the last |

Midpoints, not line starts: a line's first frame is often still the previous
shot. Coinciding moments collapse to one candidate (they describe one frame,
and the extracted file would collide anyway), keeping the highest-priority
kind — whose label is the more useful one to show a human.

`cap_candidates()` enforces `publish.max_frames` (default 24) by kind priority
(`overlay` > `timelapse` boundaries > `keep`), then restores `(stem, time)`
order so the shortlist reads as a walk through the video.

Extraction reuses gap_context's batching: **one** `docker compose run` for the
whole stage via `build_snapshot_batch_cmd`, because a container costs ~0.8s of
init against ~30ms of actual ffmpeg work per still. A failed batch is logged
and the shortlist comes back empty — it never aborts the run — and a candidate
whose file was not written is dropped individually.

## Thumbnail rendering

The predecessor of this stage was a hand-written ImageMagick script with the
copy hardcoded into it (`-annotate +56+62 "水槽DIY"` and two more lines that
restated what `summary.json` already said). The argument for keeping
compositing out of the pipeline was that fonts, colours and layout are taste —
true, and not a reason: `blender.caption_style`/`overlay_style`/`speed_mark`
already hold exactly that kind of taste and the blender stage renders from
them. What changes per project is the *values*, not the *procedure* — run
ImageMagick once per copy set, stack one to three lines, shrink text that
overruns the frame — so `thumbnail.py` (`render_sets()`) now does that
procedure itself, once per copy set from `publish_llm.py`.

**The look is model-authored, in ImageMagick's own vocabulary.** The same LLM
call that writes a set's copy also writes that copy's style, as magick flags
with magick's own value syntax — the model reasons about the tool it already
knows rather than an abstraction over it:

| key | flag | scope | accepted |
|---|---|---|---|
| `font` | `-font` | line | a key of `publish.thumbnail.fonts` (a config *slot name*, never a path — the model cannot know what is installed) |
| `pointsize` | `-pointsize` | line | int 8–400 |
| `fill` | `-fill` | line | `#RGB`/`#RGBA`/`#RRGGBB`/`#RRGGBBAA`, `rgb(…)`/`rgba(…)`, or a named-colour allowlist |
| `stroke` | `-stroke` | line | as `fill` |
| `strokewidth` | `-strokewidth` | line | int 0–40 |
| `gravity` | `-gravity` | set | one of the nine gravity names |
| `offset` | `-annotate +x+y` | set | `[+-]N[+-]N` (1-4 digit signed pair) — the shape is validated, not whether it lands on-canvas |
| `shadow` | (an inline blurred layer) | set | `{color, blur}` (`blur` as `RxS`), or literal `false` to disable the shadow layer entirely |

`resolve_line_style()`/`resolve_set_style()` validate and clamp every value
against that table; an unknown key is dropped with a log line, and a rejected
value falls back to a preset's value **per key** — one bad colour does not
cost the set the rest of the style the model chose for it. A set with *no*
usable style at all gets a whole preset, chosen **round-robin** by set index
(`preset_for()`) rather than randomly, so a rerun of the same pipeline
produces the same images and four sets still read as four options.

**Line positions are computed, never the model's.** The model gives the block
anchor (`gravity` + `offset`) and each line's point size; it cannot measure a
rendered glyph run, and that is exactly where overlap and overflow come from.
`render_sets()` makes **two** `magick` calls per set:

1. **measure** (`build_measure_cmd()`) — every line as a parenthesised
   `label:`, one call, `-format '%w %h\n' info:` reading back each line's
   natural width/height (`parse_metrics()`; a partial/unparseable result falls
   back to an estimate from point size rather than mislaying a line);
2. **render** (`build_render_cmd()`) — the background `-resize …^ -gravity
   center -extent …`, an inline blurred shadow layer covering every line in
   one composite, then each line drawn twice (`-stroke` pass, `-fill` pass) —
   following `make_thumb.sh`, the predecessor script.

`layout_lines()` stacks the measured lines from the anchor by height + a
config gap (`publish.thumbnail.line_gap`, default `12`px), reversed for
`south*` gravities since a larger `+y` there moves text *up*. An over-wide
line has its **pointsize** scaled down — never its rendered layer resized —
because the shadow/outline/fill passes of one line must share a single point
size to stay in register; `MIN_POINTSIZE` (`8`) floors the shrink so a very
wide line does not vanish.

**Escaping is not optional**, verified against ImageMagick 7.1.2 on both
`label:` and `-annotate`: `%w` expands to the image width (`%%` renders a
literal `%`); a backslash is an escape (`\n` becomes a real newline); and a
**leading** `@` makes ImageMagick read a *file* — `label:@secret.txt` rendered
that file's contents into the image. The copy is LLM-written and may contain
any of the three, so `escape_magick_text()` applies all three rules, in this
order — `\` → `\\`, then `%` → `%%`, then a leading `@` → `\@` — so the
backslash added last is not re-escaped by the first rule.

**Every command is an argument list, never a shell string.** The publish
prompt is fed the summaries, the plan directions and the director's captions —
all derived from the video's transcript — so anything said on camera reaches
the model that would author a `magick` invocation, and `magick` reads and
writes files (`@`, `-write`, MSL). With an allowlisted operator set and argv
construction the worst a bad generation can do is an ugly image.

**Degrading**: a missing `magick` or a non-zero exit from either subprocess
call drops that one set's render with a warning, and the rest still render —
`publish.json` is written either way, exactly as a failed frame batch behaves
today. Unusable measure output does **not** drop a render: `parse_metrics()`
returning `None` falls back to a point-size-based estimate
(`(1, int(pointsize * 1.2))` per line) and the set renders anyway
(`test_unusable_measure_output_still_renders_the_set`). No background is the
one failure that is not per-set: `resolve_background()` runs **once**, before
the per-set loop, and when it returns `None` `render_sets()` returns `[]`
immediately — every set in the run is skipped, not just one
(`test_no_background_renders_nothing`). Background resolution itself:
`publish.thumbnail.background` if set (relative to the stage dir, or
absolute), else the first entry of the frame shortlist, else nothing renders.

`publish.json` gains a `renders` array beside `thumbnails` —
`{set, path, background}` — and `publish.md` embeds
`<img src="thumbnails/set1.jpg" width="480">` under each `### Set N`. The
candidate table's frame column is likewise an `<img>` now, not a backticked
path: reviewing a shortlist of stills means looking at them, not opening files
by hand.

### Re-rendering without re-running the LLM

Picking a background still and nudging a colour is iterative, and re-running
the `publish` stage would call the LLM again and hand you different copy than
the one you were just judging. `publish.json`'s `thumbnail_copy` is therefore
the hand-editable contract for the look — the same pattern as `_director.json`
for the edit — and `python -m nagare_clip.publish.thumbnail` (`main()`) reads
it back with `sets_from_dict()`/`_shots_from_dict()` and calls `render_sets()`
again with **no model call at all**:

```bash
uv run python -m nagare_clip.publish.thumbnail \
  --publish-dir output/publish \
  --background frames/myvideo/2528.021.jpg
```

`--background` overrides `publish.thumbnail.background` for that run only;
omit it to use whatever is already configured or the first shortlist entry.
Hand-edit a set's `fill`/`stroke`/`gravity`/… in `publish.json` first, then
re-run the CLI to see the change without touching the copy.

### ImageMagick runs on the host

`magick` is called on the host, like the `blender` stage already is — a
deliberate exception to AGENTS.md's "route media tooling through the whisperx
Docker image" constraint (see Hard Constraints there). That constraint is
about ffmpeg; this is a different tool, and the decision is explicit: the
whisperx image has neither ImageMagick nor CJK fonts, and font slots resolve
through *host* fontconfig, which is what makes a CJK font slot work at all.

## What stays manual

Uploading. Compositing itself is no longer manual — the stage renders a real
thumbnail per copy set — but the choice of which set to ship, and any taste
adjustment beyond what a config key or a hand-edited `publish.json` covers,
is still a human call.

## Every input is optional

The stage runs last, so an earlier stage may have been disabled or its output
removed. A missing `summary.json`, `plan.json`, `_director.json`,
`{stem}.json` or `{stem}_intervals.json` degrades that part of the output and
nothing else — the titles and thumbnail copy do not depend on the timeline, and
the chapters simply come back empty.

## Config

`publish:` in `config.example.yml`. Beyond the usual LLM keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `min_chapter_duration` | `10.0` | YouTube's threshold; a shorter chapter is merged into a neighbour |
| `max_frames` | `24` | Cap on candidate stills (`0` = no limit) |
| `frame_width` | `1280` | Downscale width of the extracted JPEGs |
| `temperature` | `0.7` | Deliberately higher than the editing stages |

`publish.thumbnail:` (needs `magick` on PATH; `enabled: false` skips
rendering, not the copy):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Render one thumbnail per copy set |
| `background` | `""` | Still to composite onto — relative to `output/publish/`, or absolute; empty = the first frame-shortlist candidate |
| `width` / `height` | `1280` / `720` | Canvas size in px |
| `line_gap` | `12` | Vertical gap between stacked lines, in px |
| `fonts` | `{}` | Slot name → ImageMagick font name/path; the *only* names the model may put in a `font` value, since it cannot know what is installed |

The `project:` brief is appended to the prompt like the other briefed stages
(`apply_brief`), so the copy knows the audience and tone.
