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
| `output/publish/publish.md` | The reviewable file: title candidates, a copy-pasteable description, thumbnail copy, the frame shortlist |
| `output/publish/publish.json` | The same material as data — including the model-authored look; the hand-editable contract the `render` stage reads |
| `output/publish/frames/{stem}/{t:.3f}.jpg` | Candidate stills at the director's payoff moments |
| `output/publish/frames.json` | One entry per still: its shortlist fields, a content hash, and what looking at it showed |

Compositing is **not** done here — that is the [`render`](render.md) stage,
which makes no LLM call, so a hook or a colour can be hand-edited in
`publish.json` and re-rendered without paying for the copy again.

Disabled by default (`publish.enabled: false`) → `publish.json` holds the full
shape with nothing in it, `publish.md` says the stage is off, and no LLM or
Docker call is made.

## Three phases

The stage splits along what an LLM can and cannot know, and — for the two that
can — along what each is allowed to see.

**Look** (one vision call per candidate still, `describe_frames.py`) — what is
actually legible in each frame, cached in `frames.json` by the frame's own
bytes. Depends on nothing but the frame.

**Copy** (one LLM call, `publish_llm.py`) — title candidates, the description
lead, chapter *titles*, thumbnail *copy* as plain `role`+`text`. It is shown
**no frames at all** and its prompt carries no look vocabulary.

**Pair** (one text-only LLM call, `pairing.py`) — per set: which frame, where
the text sits, what colour it is. From the descriptions, never from images.

**Timing** (deterministic, `timeline.py` + `chapters.py`) — every timestamp.
The LLM is never asked for one; it cannot see the finished timeline.

## Looking at the frames (`describe_frames.py`)

The shortlist already carries the director's `label` for each still — but a
label says what the director thought was happening at that moment, not what a
viewer can make out. A frame labelled 「まさかの水漏れ発覚」 may show a
person's back. Only looking can tell.

So each still is described once, in prose (not JSON — it is read by a model and
by a human, and neither needs a schema): what is visible and **legible**, where
the subject sits, which regions are **empty**, and the colour and lightness of
those empty regions. The default prompt asks for exactly that and forbids
writing a headline, because a vision model handed a frame will happily caption
it.

**The label is deliberately not shown to the model.** The description exists to
catch a label whose frame does not match it; handing the claim to the model
meant to check it would only get the claim confirmed. A test asserts the label
text never appears in the messages.

**Described once, ever.** `frames.json` keys each description to
`hashlib.sha256` of the JPEG's **bytes** — not its path and not its timestamp,
both of which are stable across a re-extraction that changed the picture and
change across one that did not. So:

- re-running `publish` for better copy over an unchanged shortlist makes
  **zero** vision calls;
- re-running `director` changes the shortlist, and a frame whose bytes changed
  is described again even at the same filename;
- **a description you rewrite by hand survives.** If you disagree with what the
  model saw, correct the prose in `frames.json` and that is the description
  from then on.

An entry with an *empty* description is not reusable — that is a call that
failed, and the next run should retry it rather than cache the failure. Where
two shortlist entries share one hash (an identical frame filed under two
timestamps), the described one wins whichever order they sit in.

The vision path is `gap_context/describe.py`'s, not a second one: frames go as
base64 data-URI `image_url` parts, and this module owns its own multimodal
`CallLLM` alias so `publish_llm.py`'s stays text-only. The LLM report records
frame **paths**, never base64 payloads — a 24-frame shortlist of inlined JPEGs
would make the report unopenable, and the payload says nothing a path does not.

Disabled (`publish.describe_frames.enabled: false`, the default — a
vision-capable model has to be configured first) `frames.json` is still written
with the shortlist fields and hashes, and no call is made: the descriptions can
then be written by hand, and turning the setting on later reuses them.

## Pairing a headline to a picture

One call used to write the copy *and* its `fill`/`gravity`/`pointsize` — which
is precisely how the look came to be chosen blind: the model deciding where the
text goes had never seen the photograph, so it picked a corner and the sets that
read cleanly did so by luck. Splitting the two buys three things.

**The copy call comes out blind.** It emits `role` and `text` and nothing else.
`PUBLISH_PROMPT` lost the colour/placement vocabulary entirely (a test asserts
`fill`, `stroke`, `pointsize`, `gravity`, `offset` and `imagemagick` do not
appear in it), `_parse_thumb_set` drops a style key that arrives anyway, and
`font_slot_note()` moved to `pairing.py` — a font is part of the look. If any
frame material could reach this call the anchoring problem would move rather
than go away, so the guard is a test, not a convention.

**The pairing call sees descriptions, not images.** It receives the copy sets
(lines numbered) and the shortlist (frames numbered, each with kind, time, the
director's label and the description), and answers per set with a frame plus
`gravity`/`offset`/`shadow` and per-line `fill`/`stroke`/`strokewidth`/
`pointsize`/`font`. Its prompt tells it to believe the description over the
label where they disagree — the label is a claim about the moment, the
description is what a viewer would really see — to put the block where the
description says the picture is empty, and to choose colours against what the
description says is behind the text.

**It names its frame by INDEX, never a path.** A path is a string a model can
invent; an index is bounded and checkable. `apply_pairing()` resolves the index
to a path before it reaches `publish.json`, because the human editing that file
wants a filename, not a number.

Degrading follows the house style, with one deliberate asymmetry: only invalid
JSON or a response with no usable `sets` array is a hard failure that retries. A
bad **set** index drops that entry; a bad **frame** index costs only the
background, and the rest of that set's decision still applies — the set renders
on the shortlist's first still rather than not at all. Style *values* are not
validated here, because `render/thumbnail.py` already checks every one against
an allowlist and falls back **per key** to a preset. So a set the pairing never
mentioned, a decision it got wrong, an index that did not resolve, a failed call
and `pairing.enabled: false` all converge on the same place: today's `PRESETS`.

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

## What stays manual

Uploading, and the choice of which set to ship. Compositing itself is no longer
manual — the [`render`](render.md) stage does it — but any taste adjustment
beyond what a config key or a hand-edited `publish.json` covers is still a
human call.

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
| `image_markup` | `html` | How `publish.md` embeds the frame shortlist: `html` = sized `<img>`, `markdown` = `![alt](path)` |
| `temperature` | `0.7` | Deliberately higher than the editing stages |

`publish.pairing:` — the second text call. It uses `publish:`'s own
provider/model (it is the same kind of call, and configuring a model twice
would only be a way to configure it wrong), overriding only:

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Pair each copy set to a frame and a look; `false` = every set falls back to the presets |
| `temperature` | `0.2` | Lower than `publish.temperature`: this is a matching task, not writing |
| `max_retries` | `2` | Extra attempts on an LLM error or an unparseable response |
| `prompt` | (default) | Carries the style-key vocabulary the copy prompt gave up |

`publish.describe_frames:` — its own LLM block, because it needs a **vision**
model where the rest of the stage needs a text one:

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Look at each candidate still (needs a vision-capable model) |
| `provider` / `model` | `ollama_chat` / `qwen2.5vl:7b` | Same shape as `gap_context:` |
| `max_retries` | `2` | Extra attempts on an LLM error or an empty answer |
| `prompt` | (default) | What to say about a frame; forbids writing a headline |

The canvas size, the font slots and the rest of the compositing settings are
the render stage's: `render:`, documented in [`render.md`](render.md). They
were `publish.thumbnail.*` before the split.

The `project:` brief is appended to the prompt like the other briefed stages
(`apply_brief`), so the copy knows the audience and tone.

## Under a reordered timeline

`build_placements` lays out the manifest's segments, sliced, in playback order —
computing chapter times in shooting order and sorting afterwards would be
sorting wrong numbers. `_chapter_entries` then sorts by finished-timeline time,
because `build_chapters` drops an entry that does not advance and would
otherwise delete chapters silently. Caption lists follow finished-video order.

See [`order.md`](order.md).
