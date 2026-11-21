# render — compositing the thumbnails

`render` is the publish tools' compositor, **not a pipeline stage**, and it
**never makes an LLM call under any circumstances**. It is reached only through
`scripts/publish_tools.sh`: `write` runs it after writing `publish.json` (unless
`--no-render`), and the `render` command runs it alone over `publish.json` as it
is on disk. The edit — written by the agent with the user through the
`nagare-publish` skill ([`publish.md`](publish.md)) — decides what a thumbnail
says and how it looks; `render` decides nothing at all. It reads
`<output_dir>/publish/publish.json`, composites one image per copy set with
ImageMagick, and writes them beside a contact sheet.

| Path | What it is |
|------|------------|
| `<output_dir>/render/thumbnails/set{N}.jpg` | One rendered thumbnail per copy set (`N` matches `publish.md`'s `### Set N`) |
| `<output_dir>/render/render.json` | `{"renders": [{set, path, background}], "skipped": [{set, reason}], "warnings": [{set, message}]}` |
| `<output_dir>/render/render.md` | The contact sheet: each set's copy, its background, the finished image — or why there is none |

Calling the tool is the decision to render, so there is no on/off switch
(`render.enabled` is in `config.REMOVED_KEYS` and fails loading). A missing or
unreadable `publish.json` gives an empty contact sheet, never a traceback.

**Stale renders are removed first.** Before compositing, `render_sets()`
deletes every `set<N>.jpg` in `thumbnails/` (only files matching
`set\d+\.jpg`), so a previous run's sets beyond this run's count cannot linger
beside the new ones and be mistaken for current.

## Why it is its own command

The loop is "look at the render, change one thing, look again". With the
publish edit that is `write` again; for a hand edit of `publish.json` itself:

```bash
$EDITOR <output_dir>/publish/publish.json          # retype a hook, swap a colour
<nagare-clip>/scripts/publish_tools.sh render --config <yml>
```

Both cost zero calls. There is deliberately **no** other path into this
behaviour: no standalone thumbnail CLI and no pipeline stage (`--from-stage
render` is an unknown-stage error; a stage after `blender` would only
re-composite the file the tool already rendered).

**The loop is pinned by a test, not just by a promise.**
`test_hand_editing_one_background_changes_exactly_one_thumbnail` renders four
sets with real ImageMagick, edits one set's `background` in `publish.json`,
re-renders and asserts that set's JPEG changed **and the other three came back
byte-identical**. A companion test renders the same project twice and asserts
the bytes match (so the comparison means something) and that four sets with
identical copy still produce four different images (so `preset_for()`'s
round-robin is doing its job). Both skip when `magick` is not installed.

**Zero calls is a property of the code, not of the config.** Every provider in
this repo goes through `llm_client.call_llm` (a hard constraint in AGENTS.md),
and nothing under `src/nagare_clip/render/` or `src/nagare_clip/publish/`
(tools included) names either the module or the function — guarded statically
*and* in a fresh interpreter (`tests/render/test_run.py`), and at run time by
`tests/publish/tools/test_zero_llm.py`, which runs every tool command with the
transport booby-trapped. That is why the copy dataclasses
(`ThumbLine`/`ThumbSet`) live in `render/thumbnail.py`, and why
`publish/thumbs.py` imports `DirectorOp` under `TYPE_CHECKING` only.

## Where the pieces are

- `render/thumbnail.py` — escaping, style validation (`resolve_*` lenient,
  `*_style_problems` strict), presets, layout, argv builders, `render_sets()`,
  `set_background()`/`resolve_background()`, and the
  `sets_from_dict()`/`shots_from_dict()` readers for `publish.json`.
- `render/run.py` — the entry, `run_render()`: read, render, write
  `render.json` + `render.md`. It starts no subprocess itself; the publish
  tools inject `pipeline.external.run_magick`, the same way `render_sets()`
  takes its runner.

## Two directories, deliberately

Backgrounds resolve against `<output_dir>/publish/`, because that is where the
stills are and where `publish.json` names them; the images land under
`<output_dir>/render/`. `ThumbRender.background` is recorded relative to the
publish dir when it sits under it, and as an absolute path when it does not.

## The background is per set

`thumbnail_copy[i].background` — a path relative to `<output_dir>/publish/`, or
an absolute one. A headline and the photograph it sits on are **one** decision.
The publish tool's `write` requires a `background` on every set and refuses one
that does not exist; render itself stays lenient for a hand-edited file.

**It does not have to be a shortlist frame.** The path is resolved as a path,
and `build_render_cmd` composites with `-resize WxH^ -gravity center -extent
WxH`, so any image of any aspect ratio is scaled to cover and centre-cropped. A
photograph the camera never rolled on, or a still from `frames --line`, is one
line of JSON away. Portrait, panorama and square sources are covered by a
real-ImageMagick test.

`set_background()` resolves it per set, with two fallbacks that differ on
purpose:

| the set says | what happens |
|---|---|
| nothing (`""` or absent; only a hand edit can) | the first `thumbnails` still in `publish.json` that is on disk (`resolve_background()`); none → that set is skipped, `no background: …` |
| a path that is not on disk | **that set is dropped**, `background not found: <path>`; the others still render |

The second is not the first rule because rendering a hook over some *other*
frame and presenting it as the chosen one misleads review worse than a missing
image does.

## How a thumbnail is built

What changes per project is the *values*, not the *procedure* — run ImageMagick
once per copy set, stack the lines, shrink text that overruns the frame — so
`render_sets()` does that procedure itself, once per set in `thumbnail_copy`.

**The look is written in ImageMagick's own vocabulary**, as magick flags with
magick's own value syntax — the agent reasons about the tool it already knows
rather than an abstraction over it:

| key | flag | scope | accepted |
|---|---|---|---|
| `font` | `-font` | line | a key of `render.fonts` (a config *slot name*, never a path). A line naming none, or an unknown one, takes the **first slot listed** — see below |
| `pointsize` | `-pointsize` | line | int 8–400 |
| `fill` | `-fill` | line | `#RGB`/`#RGBA`/`#RRGGBB`/`#RRGGBBAA`, `rgb(…)`/`rgba(…)`, or a named-colour allowlist |
| `stroke` | `-stroke` | line | as `fill` |
| `strokewidth` | `-strokewidth` | line | int 0–40 |
| `gravity` | `-gravity` | set | one of the nine gravity names |
| `offset` | `-annotate +x+y` | set | `[+-]N[+-]N` (1-4 digit signed pair) — the shape is validated, not whether it lands on-canvas |
| `line_gap` | (layout: px between stacked lines) | set | int 0–400 (`0` is real; bool rejected); overrides `render.line_gap` for that set in `layout_lines` and the height-overflow check; invalid → `set_style_problems` reports it, render falls back to `render.line_gap` |
| `shadow` | (an inline blurred layer) | set | `{color, blur}` (`blur` as `RxS`), `true`, or literal `false` to disable the shadow layer entirely |

A line's `role` (`tag`/`hook`/`subtitle`, `VALID_ROLES`) only picks the preset
defaults for the keys it leaves out; absent or empty means `hook`, and the
contact sheet then lists the line as `- text` (no `role:` prefix).

**Lenient here, strict in the tool.** `resolve_line_style()`/
`resolve_set_style()` validate and clamp every value against that table, and a
rejected value falls back to a preset's value **per key** — one bad colour in a
hand-edited `publish.json` does not cost the set the rest of its style.
`sets_from_dict()` keeps only `LINE_KEYS`/`SET_KEYS` when reading the file back,
so an unknown key never reaches a `magick` command; `resolve_*` still log one
(`_warn_unknown()`) as a second guard. `line_style_problems(raw, fonts)` /
`set_style_problems(raw)` report exactly what `resolve_*` would silently replace
(unknown key, bad value, a `font` that is not a `render.fonts` slot, a non-empty
`role` outside `VALID_ROLES`). They share one predicate per key with
`resolve_*`, so render stays lenient while the publish tools' `write` refuses.
**There is no cap on the number of lines** — `layout_lines()` stacks any
number.

A set with *no* usable style at all gets a whole preset, chosen **round-robin**
by set index (`preset_for()`) rather than randomly, so a re-render produces the
same images and four sets still read as four options.

**A preset cannot name a font**, and that is why `fallback_font()` exists. Slot
names are project-defined and this module has never seen the config, so every
`LineStyle` in `PRESETS` carries `font = ""` — no `-font` flag, ImageMagick's
own default face. That face has no CJK glyphs and draws such a character as
**nothing at all**, not as a box; a real run whose copy named no slot came back
with the headlines simply missing. So a line that names no slot, or an unknown
one, resolves to the **first face listed in `render.fonts`**. First-listed
rather than sorted: YAML preserves mapping order, so a human writes the face
they want first and the rule is one they can act on. With no fonts configured
at all the flag stays off and `render.md` carries a note saying so, because that
failure is invisible in the image. The publish tool's `view` prints the
effective slots and marks the fallback.

**Line positions are computed, never authored.** The edit gives the block anchor
(`gravity` + `offset`) and each line's point size; nobody can measure a rendered
glyph run by eye, and that is exactly where overlap and overflow come from.
`render_sets()` makes **two** `magick` calls per set:

1. **measure** (`build_measure_cmd()`) — every line as a parenthesised
   `label:`, one call, `-format '%w %h\n' info:` reading back each line's
   natural width/height (`parse_metrics()`; a partial/unparseable result falls
   back to an estimate from point size, `(1, int(pointsize * 1.2))` per line,
   and the set renders anyway);
2. **render** (`build_render_cmd()`) — the background `-resize …^ -gravity
   center -extent …`, an inline blurred shadow layer covering every line in
   one composite, then each line drawn twice (`-stroke` pass, `-fill` pass).

`layout_lines()` stacks the measured lines from the anchor by height + a
config gap (`render.line_gap`, default `12`px), reversed for `south*`
gravities since a larger `+y` there moves text *up*. An over-wide line has its
**pointsize** scaled down — never its rendered layer resized — because the
shadow/outline/fill passes of one line must share a single point size to stay
in register; `MIN_POINTSIZE` (`8`) floors the shrink.

**Height is checked, not shrunk.** After layout, a stacked block taller than
the canvas (placed heights + gaps + `abs(y0)`) adds
`text block is {h}px tall; canvas is {H}px` to `RenderResult.warnings`
(`(set index, message)`); `render.json` carries it under `warnings` and
`render.md` prints `**Warning:** …` under the set. The set still renders — the
agent reads the image and fixes the size or the line count.

**Escaping is not optional**, verified against ImageMagick 7.1.2 on both
`label:` and `-annotate`: `%w` expands to the image width (`%%` renders a
literal `%`); a backslash is an escape (`\n` becomes a real newline); and a
**leading** `@` makes ImageMagick read a *file* — `label:@secret.txt` rendered
that file's contents into the image. The copy may contain any of the three, so
`escape_magick_text()` applies all three rules, in this order — `\` → `\\`, then
`%` → `%%`, then a leading `@` → `\@` — so the backslash added last is not
re-escaped by the first rule.

**Every command is an argument list, never a shell string.** The copy is
written by an agent that read the transcript, so anything said on camera can
reach it, and `magick` reads and writes files (`@`, `-write`, MSL). With an
allowlisted operator set and argv construction the worst a bad value can do is
an ugly image.

**Degrading is per set**: a missing `magick` or a non-zero exit from either call
drops that set's render with a reason (magick's own stderr where there is one),
and the rest still render.

**A set that produced no image keeps its heading and its copy, and says why**
(`**Not rendered:** background not found: frames/z/nope.jpg`). A set that
quietly vanished from the contact sheet would turn a one-character typo into a
mystery whose only trace is a log line. `render_sets()` therefore returns a
`RenderResult(renders, skipped, warnings)`, and every `continue` in its loop
goes through `skip(index, reason)`.

`render.md` embeds each image through `markdown.embed_image(path, alt, width,
markup)`, with `general.image_markup`: `html` (default) a sized `<img>`,
`markdown` a `![alt](path)` for viewers that strip raw HTML (markdown has no
width syntax, so the size hint is dropped rather than faked). `publish.md` and
`output/index.md` share the helper and the key.

### ImageMagick runs on the host

`magick` is called on the host, like `blender` — a deliberate exception to
AGENTS.md's "route media tooling through the whisperx Docker image" constraint
(see Hard Constraints there). That constraint is about ffmpeg (the publish
tool's stills still go through the whisperx image); this is a different tool:
the whisperx image has neither ImageMagick nor CJK fonts, and font slots
resolve through *host* fontconfig, which is what makes a CJK font slot work at
all.

## Config

`render:` in `config.example.yml` (needs `magick` on PATH):

| Key | Default | Meaning |
|-----|---------|---------|
| `width` / `height` | `1280` / `720` | Canvas size in px |
| `line_gap` | `12` | Vertical gap between stacked lines, in px |
| `fonts` | `{}` | Slot name → ImageMagick font name/path; the *only* names a `font` value may use. The first listed is the fallback face |

`render.enabled` is removed. `publish.thumbnail.*` (the pre-split home of these
keys) fails at load because `PublishConfig` is `extra="forbid"`; there is no
`render.background`.
