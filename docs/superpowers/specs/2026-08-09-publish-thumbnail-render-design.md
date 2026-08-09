# Render the thumbnail, don't just describe it

Date: 2026-08-09
Stage: `publish` (new `thumbnail` module + LLM style fields + markdown embedding)

## Problem

`publish` writes the *material* for a thumbnail and stops. `publish.json` carries
`thumbnail_copy` — alternative sets of one to three role-tagged lines — and
`thumbnails`, a shortlist of extracted stills. Turning any of it into an image is
a hand-written ImageMagick script per project, as
`/mnt/archive/Projects/YouTube/2026-06-27 water_pump_2/make_thumb.sh` was.

`publish.md` prints the stills as backticked paths, so reviewing 24 candidates
means opening 24 files by hand.

The argument for keeping compositing out was that fonts, colours and shadows are
taste. That is true and not a reason: `blender.caption_style`, `overlay_style`
and `speed_mark` already hold exactly that kind of taste — font paths, RGBA
colours, shadow blur, screen positions — and the blender stage renders from them.
What changes per project is the values. What does not change is the procedure:
run ImageMagick once per copy set, stack one to three lines, shrink text that
overruns the frame.

## Decisions

### The look is model-authored, in ImageMagick's own vocabulary

Config-driven styling was the first design and was rejected: hand-writing colours
in YAML is the work being removed, and four sets that differ only in copy do not
show what the copy will actually look like. The LLM that writes the copy also
writes the style for that copy, so the four renders in `publish.md` are four real
options.

The style keys **are** magick flags, with magick's own value syntax, so the model
reasons about the tool it knows rather than an abstraction over it:

| key | flag | scope | accepted |
|---|---|---|---|
| `font` | `-font` | line | a key of `publish.thumbnail.fonts` |
| `pointsize` | `-pointsize` | line | int 8–400 |
| `fill` | `-fill` | line | `#RGB` / `#RRGGBB` / `#RRGGBBAA` / `rgb(…)` / `rgba(…)` / named-colour allowlist |
| `stroke` | `-stroke` | line | as `fill` |
| `strokewidth` | `-strokewidth` | line | int 0–40 |
| `gravity` | `-gravity` | set | the nine gravity names |
| `offset` | `-annotate +x+y` | set | `[+-]N[+-]N`, within the canvas |
| `blur` | `-blur` | set (shadow) | `RxS` |

`font` is a config **slot name**, not a path or a face name: the model cannot know
what is installed. The available slot names are stated in the system prompt at
runtime — same mechanism as `director_llm.keep_limit_note()` — not baked into the
default prompt.

Line *positions* stay ours. The model gives the block anchor (`gravity` +
`offset`) and each line's point size; the code stacks downward by **measured**
height and shrinks point size to fit the frame. A model cannot measure a rendered
glyph run, and that is exactly where overlap and overflow come from.

### The model emits values, never a command

The rejected alternative was letting the model write the `magick` invocation
itself. The publish prompt is fed the summaries, the plan directions and the
director's captions — all derived from the video's transcript — so anything said
on camera reaches the model that would author the shell. `magick` reads and
writes files (`@`, `-write`, MSL), so a bad generation is not merely an ugly
thumbnail. With an allowlisted operator set the worst outcome is an ugly image.

For the same reason the invocation is built as an **argument list** and never
passed through a shell.

### ImageMagick runs on the host

AGENTS.md says media tooling routes through the whisperx Docker image and no host
binaries are added. That constraint is about ffmpeg; this is a new tool and the
decision is explicit: `magick` is called on the host, like the `blender` stage
already is. Fonts resolve through host fontconfig, which is what makes CJK font
slots work at all — the third-party whisperx image has neither ImageMagick nor
CJK fonts and would need a derived image built for the purpose.

### Text escaping is not optional

Verified against ImageMagick 7.1.2 during design, on both `label:` and
`-annotate`:

- `%w` **expands** to the image width. `%%` renders a literal `%`.
- `\n` inside the text becomes a real newline. Backslashes are interpreted.
- A **leading `@`** makes ImageMagick read a file: `label:@secret.txt` rendered
  that file's contents into the image. `\@` neutralises it.

`escape_magick_text()` therefore escapes `\` → `\\`, then `%` → `%%`, then a
leading `@` → `\@` (in that order, so the backslash added last is not re-escaped).
The copy is LLM-written and may contain any of the three.

### Degrading

Every failure drops the render and keeps the run: a missing `magick`, a non-zero
exit, no background still. `publish.json` is written either way, exactly as a
failed frame batch behaves today.

Validation degrades per key, not per set: an invalid colour falls back to a
preset's value rather than losing the set's whole style. A set with no usable
style at all gets a **randomly chosen** built-in preset, so four sets still read
as four options.

## Design

### `src/nagare_clip/publish/thumbnail.py` (new)

Pure, except for `main()`.

- `escape_magick_text(text) -> str` — the three hazards above.
- `ThumbStyle` / `LineStyle` — the resolved structure the renderer consumes.
  Where it came from (LLM, hand-edited `publish.json`, preset) is not its concern.
- `PRESETS` — built-in styles used as the per-key fallback and as the whole-set
  fallback.
- `resolve_style(raw, fonts) -> ThumbStyle` — validate/clamp per the table above,
  dropping unknown keys with a log line.
- `layout_lines(lines, metrics, canvas) -> list[PlacedLine]` — stack from the
  anchor by measured height plus gap, reversed for `south*` gravities; scale
  `pointsize` down where a line overruns `width − 2·offset_x`.
- `build_measure_cmd(lines, fonts) -> list[str]` — every line as a parenthesised
  `label:` in ONE call, `-format '%w %h\n' info:`.
- `build_render_cmd(background, placed, style, out) -> list[str]` — background
  `-resize WxH^ -gravity center -extent WxH`, an inline shadow layer
  (`\( -size WxH xc:none … -annotate … -blur … \) -composite`), then per line the
  two-pass outline-then-fill `-annotate` from `make_thumb.sh`. The same point size
  across shadow, outline and fill passes, which is why shrink-to-fit adjusts
  `-pointsize` and never resizes a rendered layer.
- `resolve_background(cfg, thumbs, stage_dir) -> Path | None` —
  `publish.thumbnail.background` if set (relative to the stage dir, or absolute),
  else the first entry of `thumbnails`, else `None`.
- `set_relpath(i) -> str` — `thumbnails/set{i}.jpg`.
- `main()` — the standalone re-render CLI, structured like
  `intervals/check_edits.py`: a pure module with a `main()` that imports
  `run_command` locally.

### `publish/publish_llm.py`

`thumbnail_copy` becomes a list of **objects** instead of a list of line-lists:

```json
{
  "lines": [
    {"role": "tag", "text": "水槽DIY", "font": "sans-bold", "pointsize": 70,
     "fill": "white", "stroke": "rgba(30,30,30,1)", "strokewidth": 8},
    {"role": "hook", "text": "穴あけ不要。", "font": "serif-black", "pointsize": 156,
     "fill": "#B08D3E", "stroke": "rgba(250,250,250,1)", "strokewidth": 12}
  ],
  "gravity": "northwest",
  "offset": "+56+62",
  "shadow": {"color": "rgba(0,0,0,0.8)", "blur": "0x8"}
}
```

`ThumbLine` gains the style fields; a new `ThumbSet` carries the lines plus the
set-level keys. Role parsing, the one-to-three-line rule and the drop-on-bad-item
behaviour are unchanged. The shape change makes `publish.json` the hand-editable
contract for the look — the same pattern as `_director.json` and `_cuts.txt` —
which is what the re-render CLI reads.

### `publish/run.py`

`run_publish` gains `render: Callable[[Sequence[ThumbSet]], list[ThumbRender]] | None`.
It owns the copy sets (they come out of its own LLM call), so it renders after
parsing and before writing. `None` → no rendering and the artifact shape is
otherwise unchanged.

`publish.json` gains a `renders` array — `{set, path, background}` — beside the
existing `thumbnails`; paths stay in the JSON for anything reading it
programmatically.

`_render_markdown` changes in two places:

- under each `### Set N`, `<img src="thumbnails/set1.jpg" width="480">`;
- the candidate table's frame column becomes
  `<img src="frames/…/2528.021.jpg" width="240">` in place of the backticked path.

HTML rather than `![](…)` because a 1280px still in a five-column table cell
renders at the previewer's discretion — GitHub shrinks it to the cell, some local
previewers do not.

### `pipeline/stages.py`

`_render_thumbnails(ctx, sets)` builds the argv through `thumbnail.py` and
executes via `external.run_command`, exactly as `_extract_thumb_frames` does for
the candidate stills; it is passed to `run_publish(render=…)`. Subprocess
invocation stays in the pipeline layer.

### Config

```yaml
publish:
  thumbnail:
    enabled: true             # needs `magick` on PATH
    background: ""            # relative to output/publish/, or absolute; empty -> first candidate
    width: 1280
    height: 720
    fonts:                    # the slots the model may pick from; empty -> no -font flag
      sans-bold: "Noto-Sans-CJK-JP-Bold"
      serif-black: "Noto-Serif-CJK-JP-Black"
      sans-medium: "Noto-Sans-CJK-JP-Medium"
```

No `roles:` and no background grading (`-brightness-contrast`, `-sigmoidal-contrast`,
`-unsharp`, `-modulate`, `-color-matrix` from `make_thumb.sh`): dropped as YAGNI.
The style lives with the copy that uses it.

## Testing

TDD. Every test is shown failing against a mutated implementation before the
implementation is accepted — mutation-catch evidence reported alongside the green
run.

- `escape_magick_text`: `%`, backslash, leading `@`, and the ordering that keeps
  the `@` escape from being re-escaped. Expectations pinned to real `magick`
  behaviour observed during design.
- Style validation: each key's accept/reject boundary, unknown key dropped,
  per-key fallback to a preset, whole-set fallback when nothing is usable.
- Layout: stacking by measured height, `south*` reversal, shrink-to-fit point-size
  scaling, and that shrink never resizes a rendered layer.
- Argv builders: text is one argument and never a shell string; escaped text
  reaches the command; no `shell=True` anywhere.
- `resolve_background`: config path, first candidate, nothing.
- `run_publish` with a fake renderer: `renders` in `publish.json`, images embedded
  in `publish.md`, and an empty render list leaving the markdown otherwise as it is.
- The candidate table's `<img>` column.
- CLI round-trip: re-render from an existing `publish.json` with no LLM call.
- One end-to-end render against real `magick`, skipped when it is absent.

## Documentation

Per the documentation policy: `README.md`, `plan.md`, `AGENTS.md` (including the
host-binary constraint note) and `docs/stages/publish.md`, plus a regenerated
`config.example.yml` (`make config-example`).
