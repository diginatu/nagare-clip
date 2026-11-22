---
name: nagare-publish
description: Write the upload material of a nagare-clip video together with the user - title candidates, the description lead, YouTube chapters and the thumbnails. Use after the director's edit has been rendered to a .blend (the pipeline ran through blender), when the user wants to publish, title, describe, chapter or make thumbnails for their nagare-clip video, or to revise them later. Reads the finished video with nagare-clip's deterministic publish tools, looks at candidate stills and the rendered thumbnails with your own image input, and writes publish.json, publish.md and the thumbnails.
---

# nagare-publish

You write what a video needs to be uploaded, working live with the user:
**title candidates**, the **description lead**, the **chapters** and the
**thumbnails**. The director has already decided the edit and the pipeline has
built it; you do not change the cut. You write one small JSON edit; the tool
computes every time from the finished timeline, writes `publish.json` and
`publish.md`, and composites the thumbnails. Uploading stays manual.

You can see. Read every still you might use and every thumbnail you render
with your own image input; a thumbnail nobody looked at is a guess.

**The user always wins.** Their word beats these instructions, the brief and
your own taste. Talk to the user in the language they use; write the copy in
the video's language unless they say otherwise.

## Where you are, and the tools

**The working directory is the video directory** — the project holding the
footage and its config, not the nagare-clip repository. Config paths such as
`output_dir: video-editor-ai/` are relative to it, so stay in it.

- **The configs.** A project may layer several, e.g.
  `--config ../template/nagare_base.yml --config ./nagare_config.yml`. Pass
  the tools exactly the `--config` files the pipeline uses, in the same
  order (later files win). A project often has its own wrapper script for
  running the pipeline (e.g. `./run_nagare_clip.sh`); read it to learn the
  configs.
- **`<output_dir>`** below is the config's `general.output_dir`, relative to
  the video directory (it may be anything, e.g. `video-editor-ai`; the default
  is `output`). `view` prints `output dir: …` as its first line, `frames`
  prints `stills in …` and `write`/`render` print the real paths — use those,
  and do not guess.
- **The nagare-clip checkout.** The tools live in it, at
  `<nagare-clip>/scripts/publish_tools.sh` (the script keeps your working
  directory). If the checkout's path is not already known — from the
  conversation, the project's wrapper scripts or the environment — ask the
  user once.

The tools are deterministic (no LLM call):

```bash
<nagare-clip>/scripts/publish_tools.sh view   --config A.yml [--config B.yml] [--lines A-B | --compact]
<nagare-clip>/scripts/publish_tools.sh frames --config ... [--line N[@pos]]... [--at M:SS]... [--sheet]
<nagare-clip>/scripts/publish_tools.sh chapters N [N ...] --config ...
<nagare-clip>/scripts/publish_tools.sh write  --config ... --edit FILE|- [--no-render]
<nagare-clip>/scripts/publish_tools.sh render --config ...
```

- `view` — first line `output dir: …`, then the editorial brief, the
  summaries (overall and per video), the director's `plan.md`, the director's
  captions as `- M:SS  text` in playback order (`✂` = on a line that does not
  survive, `—` = no finished timeline), the **font slots** a thumbnail line
  may name (`render.fonts`, from the merged config; the first is the fallback
  for a line with no `font`), the finished runtime, then the lines. By default
  one row per display line: `N  M:SS  text`. `M:SS` is where the line starts
  **in the finished video**; `✂` means the line does not survive the cut; `—`
  means there is no finished timeline yet. Line numbers are the director's
  display numbering. **`--compact`** replaces the per-line rows with the
  finished video as the director's played transcript: only what plays, in
  playback order, each row prefixed with its finished time, a cut as one `✂`
  row and a timelapse as one `⏩` row — a long video is a few screens instead
  of thousands of rows. `✂ dropped lines A-B` marks lines whose footage did not
  survive the cut; a kept stretch with no talking (an opening shot) shows as
  its `[silent …]` line with its time. `--lines A-B` zooms into those display lines (it does
  not combine with `--compact`).
- `frames` — with no `--line`: the thumbnail **shortlist** read off the
  director's ops (capped by `publish.max_frames`), written fresh to
  `<output_dir>/publish/stills.json`. With `--line N[@pos]` (repeatable; `pos` in
  `[0,1]` within the line's footage, default `0.5`): stills at those lines,
  **added** to the existing list. Each row is `line  M:SS  kind  label
  path`, in playback order; the path is relative to `<output_dir>/publish/` and is
  what a thumbnail's `background` names. All stills of one call come from
  one Docker run; if Docker fails it exits 1 with the reason. Rows carry the
  position `#i` (`#i  line  M:SS  kind  label  path`).
  `--at T` (repeatable; `M:SS`, `H:MM:SS` or seconds) takes a still at a
  **finished-video** time — use it when the user names a time ("the last water
  shot, 28:16"); it merges like `--line` and may be combined with it.
  `--sheet` also composites every still in the list into contact sheets
  `<output_dir>/publish/sheets/sheet<K>.jpg` (12 per sheet, 4 columns, each tile
  labelled `#i  L<line>  M:SS` to match the rows; ASCII: `-` for none, `cut` for a cut still) and prints `sheet: <path>`.
- `chapters N [N ...]` — try chapter lines before writing: prints each one's
  finished time and length (`line time length text`, sorted by time, the
  first shown at `0:00` as `write` renders it; a chapter under `publish.min_chapter_duration` is marked), then every problem
  `write` would report for them and the fewer-than-three warning. Exit 1 on a
  problem. It writes nothing.
- `write` — validates the whole edit; on **any** problem it prints them all
  and writes nothing (exit 1). Otherwise it saves the edit verbatim as
  `<output_dir>/publish/edit.json`, writes `publish.json` and `publish.md`, then
  composites the thumbnails into `<output_dir>/render/` (skip with `--no-render`).
  Warnings print but do not block.
- `render` — composites the thumbnails again from `publish.json` as it is
  on disk (for a hand edit of that file). Zero LLM calls, always.

Keep the draft edit in a file in the project's output directory (e.g.
`<output_dir>/publish/edit.draft.json`), not in your reply alone.

## Workflow

1. **Find the project.** Work out the config files and their order and the
   nagare-clip checkout. If `view` says the finished runtime is unknown, the
   pipeline has not run through `blender` yet: chapters cannot be resolved.
   Tell the user to resume the pipeline (from `guided_edit`, after the
   director) first. If `<output_dir>/publish/edit.json` exists, ask whether to
   revise it (see "Revising later") or start over.
2. **Read everything.** Run `view --compact` first: the brief (audience,
   tone, target), the summaries, the plan, the captions with their times, the
   fonts, and the finished video top to bottom with every row timed. Zoom
   into a stretch with `view --lines A-B` (every shot line, cut ones
   included) when you need the exact lines around a moment.
3. **Write the copy first — before you look at any frame.** Titles
   (several candidates that genuinely differ), the description lead, the
   chapters (by display line, see
   [references/youtube-chapters.md](references/youtube-chapters.md); try the
   lines with `chapters N N ...` until none is too short or cut) and the
   thumbnail **text** for each set (one to a few lines each). Show them to
   the user and revise until they agree.
   *Why first:* copy written while looking at the stills ends up captioning
   them — "a man holding a pump" instead of what makes the video worth a
   click. The headline comes from the story; the picture is chosen to carry
   it.
4. **Get the stills.** Run `frames --sheet` (the shortlist), Read the
   **sheet** to see the whole list in one Read, then **zoom by Reading the
   single stills** that matter (a sheet tile is too small to judge legibility). [references/shortlist.md](references/shortlist.md)
   says what each kind is. The label is the director's claim about the
   moment; trust what is actually legible in the image, not the label.
5. **Look for more.** The shortlist is where to start looking, not the
   answer. For a moment the copy needs and the shortlist lacks, find its line
   in `view` and run `frames --line N` (or `N@0.2`, `N@0.8` to move within
   the line), or `frames --at M:SS` when the user names a time in the finished
   video; Read those too (add `--sheet` to refresh the sheet). Run plain `frames` only once: it rewrites the
   list from scratch, dropping the stills you added.
6. **Choose a background and a look per set.** Pick the still that carries
   that set's headline; place the text where the picture is empty and make it
   legible against what is behind it —
   [references/thumbnail-craft.md](references/thumbnail-craft.md).
7. **Write** with `write --edit <file>`. Fix every problem it lists and write
   again.
8. **Read each render**, `<output_dir>/render/thumbnails/set{N}.jpg` (`write` prints the directory), with your
   image input, and `<output_dir>/render/render.md` for a set that was not
   rendered (its reason is printed) or whose text block overflows the canvas.
   Is the headline readable at a glance, at phone size? Does it cover the
   thing the picture is of? Revise the look (or the still, or the wording)
   and write again. Show the user the thumbnails and the copy; iterate on
   their feedback.
9. **Hand over**: `<output_dir>/publish/publish.md` holds the titles, the
   description with its chapter list and the thumbnails, ready to paste and
   upload.

## Edit format

```json
{
  "titles": ["まさかの水漏れ…ポンプ交換3日目", "ポンプ交換3日目、原因はここだった"],
  "lead": "ポンプ交換の3日目。水漏れの原因を探して分解します。",
  "chapters": [
    {"line": 1, "title": "オープニング"},
    {"line": 40, "title": "分解して原因を探す"},
    {"line": 95, "title": "組み直して試運転"}
  ],
  "thumbnails": [
    {
      "background": "frames/a/63.200.jpg",
      "gravity": "south", "offset": "+0+40", "shadow": {"blur": "0x6"},
      "lines": [
        {"role": "tag", "text": "3日目", "fill": "#FFCC00", "font": "sans-bold"},
        {"text": "まさかの水漏れ", "pointsize": 140, "stroke": "black", "strokewidth": 8}
      ]
    }
  ]
}
```

- `titles`: candidates, at least one; the user picks one at upload.
- `lead`: the description's body. The tool appends the chapter list.
- `chapters`: `line` is a display number from `view`, `title` the chapter's
  name. **Never write a time**: the tool takes the first moment of that line
  that survives in the finished video, sorts the chapters by it and forces
  the first to `0:00`. Optional (`[]` is allowed, with a warning).
- `thumbnails`: one entry per alternative thumbnail (`set1`, `set2`, ...).
  - `background` (required): a still's path as `frames` printed it,
    relative to `<output_dir>/publish/`, or an absolute path.
  - Set keys: `gravity` (`northwest`, `north`, `northeast`, `west`,
    `center`, `east`, `southwest`, `south`, `southeast`) — where the text
    block is anchored; `offset` `+X+Y` in px from that anchor; `shadow`
    `true`, `false` or `{"color": ..., "blur": "0x6"}`; `line_gap` an int
    0-400, the px between stacked lines for this set (absent = `render.line_gap`).
  - `lines` (at least one; any number, stacked top to bottom): `text`
    (required), `role` (`tag`, `hook`, `subtitle`; absent = `hook`) — it only
    picks the default size and colours for keys you leave out — and the
    style keys `font` (a slot name from the `## Fonts` list `view` prints),
    `pointsize` (8-400), `fill`, `stroke` (colours: `#RRGGBB`, `#RGB`,
    `rgb(...)`/`rgba(...)` or a name such as `white`), `strokewidth` (0-40).
- The edit is the **complete state**: `write` replaces everything written
  before.

## Rules the tools enforce

`write` refuses an edit (and writes nothing) for any of these, listing all of
them at once:

- an unknown key or a wrong type anywhere; an empty `titles`;
- a title over 100 characters, or containing `<` or `>` (YouTube rejects
  both);
- the assembled description (lead + chapter list) over 5000 characters;
- a chapter on a line that does not exist, has no timing, or is entirely
  cut (`✂` in `view`); two chapters at one time; a chapter shorter than
  `publish.min_chapter_duration` (10 s by default) — re-choose the lines, the
  tool does not merge them for you; any chapter at all while the finished
  timeline is unavailable;
- a thumbnail with no lines, a line with empty text, an unknown `role`, an
  unknown style key, a style value outside the allowed set (it is reported,
  not replaced), a `font` that is not a `render.fonts` slot, or a
  `background` that does not exist.

It warns but writes when there are fewer than three chapters: YouTube links
the timestamps but draws no segmented progress bar.

`view` and `write` also warn when the `.blend` is 1 s or more shorter or longer
than the timeline the tools compute: the user edited it by hand in Blender, and
the tools cannot see that edit, so every chapter and still time after it can be
off from the real video. Tell the user. Either they carry the edit back into
the director's edit and re-run the pipeline through `blender`, or they check
each chapter's position in the rendered video before uploading.

## Revising later

`write` saved the edit verbatim as `<output_dir>/publish/edit.json`; there is no
`dump`. To revise, copy it to your draft file, change it, and `write` it
again. After the director's edit changes and the pipeline is re-run through
`blender`, the finished timeline has moved, and the display numbering may
have too: first run `chapters` on the old edit's chapter lines — it warns
`line N read 「old」 … now reads 「new」` for every line whose text changed
since `write` last saved `edit_lines.json` — and fix each line it names. Then
run `write` again with the saved
edit — the times are recomputed, and a chapter on a line that is now cut is
reported, so you re-choose it. `publish.json` is never rebuilt by the
pipeline; until you write again, it describes the old cut.

## Limits

The tool can express one look per line: a font, a size, a fill, a stroke,
and a set's anchor, offset and shadow. When you or the user want something it
cannot express — a colour change in the middle of a line, a gradient, an
arrow or circle over the picture, a cropped or zoomed background, a raw
ImageMagick command — **propose it to the user as what it is** instead of
settling for the nearest approximation without saying so. They may do it by
hand in an image editor, or decide it is not needed.

## References

- [references/shortlist.md](references/shortlist.md) — what each shortlist kind is, and how far to trust its label.
- [references/thumbnail-craft.md](references/thumbnail-craft.md) — the style vocabulary, legibility, placing text where the picture is empty.
- [references/youtube-chapters.md](references/youtube-chapters.md) — YouTube's chapter rules and how `write` enforces them.
