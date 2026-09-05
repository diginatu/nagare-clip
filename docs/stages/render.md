# render stage — compositing the thumbnails

Runs **once project-wide, after `publish`** — the last stage, and the only one
that **never makes an LLM call under any circumstances**. `publish` decides
what a thumbnail says; `render` decides nothing at all. It reads
`output/publish/publish.json`, composites one image per copy set with
ImageMagick, and writes them beside a contact sheet.

| Path | What it is |
|------|------------|
| `output/render/thumbnails/set{N}.jpg` | One rendered thumbnail per copy set (`N` matches `publish.md`'s `### Set N`) |
| `output/render/render.json` | `{"renders": [{set, path, background}]}` — what was actually composited, onto what |
| `output/render/render.md` | The contact sheet: each set's copy, its background, and the finished image |

Disabled (`render.enabled: false`) → `render.json` holds an empty `renders`
array, `render.md` says the stage is off, and no `magick` call is made.

## Why it is a stage and not a flag

The human loop here is "look at the render, change one thing, look again", and
the thing being changed sits *between* the two halves — a background that
should have been the next frame over, a headline the model got nearly right. A
stage boundary is exactly the mechanism this pipeline already has for "stop
here, edit the artifact, resume", and every other hand-editable contract in the
repo (`_director.json`, `plan.json`, `history.md`) sits on one:

```bash
$EDITOR output/publish/publish.json          # retype a hook, swap a colour
./scripts/run_pipeline.sh --from-stage render --to-stage render
```

Re-running `publish` instead would call the LLM again and hand back *different*
copy than the one being judged. So there is deliberately **no** second path
into this behaviour: the standalone `python -m nagare_clip.publish.thumbnail`
CLI that used to exist was a side door around a stage that could not be
re-entered, and the stage can now be re-entered.

**The loop is pinned by a test, not just by a promise.**
`test_hand_editing_one_background_changes_exactly_one_thumbnail` renders four
sets with real ImageMagick, edits one set's `background` in `publish.json`,
re-runs the stage and asserts that set's JPEG changed **and the other three
came back byte-identical**. A companion test renders the same project twice and
asserts the bytes match (so the comparison means something) and that four sets
with identical copy still produce four different images (so `preset_for()`'s
round-robin is doing its job). Both skip when `magick` is not installed.

**Zero calls is a property of the code, not of the config.** Every provider in
this repo goes through `llm_client.call_llm` (a hard constraint in AGENTS.md),
and nothing under `src/nagare_clip/render/` names either the module or the
function — guarded statically *and* in a fresh interpreter, which is why the
copy dataclasses (`ThumbLine`/`ThumbSet`) live in `render/thumbnail.py` rather
than beside the LLM call that writes them, and why `publish/thumbs.py` imports
`DirectorOp` under `TYPE_CHECKING` only.

## Where the pieces are

- `render/thumbnail.py` — escaping, style validation, presets, layout, argv
  builders, `render_sets()`, `resolve_background()`, and the
  `sets_from_dict()`/`shots_from_dict()` readers for `publish.json`.
- `render/run.py` — the stage entry: read, render, write `render.json` +
  `render.md`. It starts no subprocess itself; the pipeline adapter injects
  `external.run_magick`, the same way `render_sets()` takes its runner.

## Two directories, deliberately

Backgrounds resolve against the **publish** stage dir, because that is where
the stills are and where `publish.json` names them; the images land under the
**render** stage dir. `ThumbRender.background` is recorded relative to the
publish dir when it sits under it, and as an absolute path when it does not.

## The background is per set

`thumbnail_copy[i].background` — a path relative to `output/publish/`, or an
absolute one. A headline and the photograph it sits on are **one** decision: a
single project-wide background made four copy treatments into four wordings of
one thumbnail, and no config key, no model and no human could say "this set
gets this photo". `set_background()` resolves it per set; the old
`publish.thumbnail.background` and `--background` are deleted rather than
moved.

**It does not have to be a shortlist frame.** This falls out of the design
rather than being bolted on: the path is resolved as a path, and
`build_render_cmd` composites with `-resize WxH^ -gravity center -extent WxH`,
so any image of any aspect ratio is scaled to cover and centre-cropped. A
photograph the camera never rolled on, or a frame pulled by hand at a timestamp
the shortlist missed, is one line of JSON away — and a good deal of what the
stage split is *for*. Portrait, panorama and square sources are covered by a
real-ImageMagick test.

Two fallbacks, and they differ on purpose:

| the set says | what happens |
|---|---|
| nothing (`""` or absent) | the first frame-shortlist candidate on disk — a project that says nothing renders exactly as it did before per-set backgrounds existed |
| a path that is not on disk | **that set is dropped**, with a warning; the others still render |

The second is not the same rule as the first because rendering a hook over some
*other* frame and presenting it as the chosen one misleads review worse than a
missing image does. Before the split this was a whole-run failure (a named
background that was missing rendered nothing at all); it is now per set.

`publish.json` carries `"background"` on every set, **even when empty**, so a
human opening the file can see where a path goes without reading these docs
first.

## How a thumbnail is built

The predecessor of this stage was a hand-written ImageMagick script with the
copy hardcoded into it (`-annotate +56+62 "水槽DIY"` and two more lines that
restated what `summary.json` already said). The argument for keeping
compositing out of the pipeline was that fonts, colours and layout are taste —
true, and not a reason: `blender.caption_style`/`overlay_style`/`speed_mark`
already hold exactly that kind of taste and the blender stage renders from
them. What changes per project is the *values*, not the *procedure* — run
ImageMagick once per copy set, stack one to three lines, shrink text that
overruns the frame — so `render/thumbnail.py` (`render_sets()`) now does that
procedure itself, once per copy set written by `publish_llm.py`.

**The look is model-authored, in ImageMagick's own vocabulary.** `publish`'s
**pairing** call writes each set's style as magick flags with magick's own value
syntax — the model reasons about the tool it already knows rather than an
abstraction over it. (It is a separate call from the one that writes the copy,
and it has the frame descriptions: see
[`publish.md`](publish.md#pairing-a-headline-to-a-picture).)

| key | flag | scope | accepted |
|---|---|---|---|
| `font` | `-font` | line | a key of `render.fonts` (a config *slot name*, never a path — the model cannot know what is installed). A line naming none, or an unknown one, takes the **first slot listed** — see below |
| `pointsize` | `-pointsize` | line | int 8–400 |
| `fill` | `-fill` | line | `#RGB`/`#RGBA`/`#RRGGBB`/`#RRGGBBAA`, `rgb(…)`/`rgba(…)`, or a named-colour allowlist |
| `stroke` | `-stroke` | line | as `fill` |
| `strokewidth` | `-strokewidth` | line | int 0–40 |
| `gravity` | `-gravity` | set | one of the nine gravity names |
| `offset` | `-annotate +x+y` | set | `[+-]N[+-]N` (1-4 digit signed pair) — the shape is validated, not whether it lands on-canvas |
| `shadow` | (an inline blurred layer) | set | `{color, blur}` (`blur` as `RxS`), or literal `false` to disable the shadow layer entirely |

`resolve_line_style()`/`resolve_set_style()` validate and clamp every value
against that table, and a rejected value falls back to a preset's value **per
key** — one bad colour does not cost the set the rest of the style the model
chose for it. An unknown key never reaches either function in practice: both
producers of a style dict already filter to `LINE_KEYS`/`SET_KEYS` at the
parse boundary (`publish_llm._pick()` on the LLM path, the same-named
comprehensions in `thumbnail.sets_from_dict()` when reading `publish.json`
back), so a key
outside that table is dropped before it enters `publish.json` or reaches a
`magick` command. `resolve_line_style()`/`resolve_set_style()` still validate
and log an unknown key themselves (`_warn_unknown()`) as a second guard, but
that path is exercised by direct unit tests, not by the stage in production.

A set with *no* usable style at all gets a whole preset, chosen **round-robin**
by set index (`preset_for()`) rather than randomly, so a rerun of the same pipeline
produces the same images and four sets still read as four options.

**A preset cannot name a font**, and that is why `fallback_font()` exists. Slot
names are project-defined and this module has never seen the config, so every
`LineStyle` in `PRESETS` carries `font = ""` — no `-font` flag, ImageMagick's
own default face. That face has no CJK glyphs and draws such a character as
**nothing at all**, not as a box. On the first real run of the pairing feature
the pairing call failed on every attempt, every set fell to the presets, and
the thumbnails came back with the headlines simply missing — so "a project with
pairing disabled renders exactly as it does today" was false, because before
the copy call was blinded it had named a slot on every line.

So a line that names no slot, or an unknown one, resolves to the **first face
listed in `render.fonts`**. First-listed rather than sorted: YAML preserves
mapping order, so a human writes the face they want first and the rule is one
they can act on. With no fonts configured at all there is nothing to reach and
the flag stays off — `render.md` then carries a note saying so, because that
failure is invisible in the image.

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
config gap (`render.line_gap`, default `12`px), reversed for
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
(`test_no_background_renders_nothing`) — but that guard now only fires when
**no** set names a background *and* the shortlist yields none, so a set with
its own path still renders on an empty shortlist. Per-set resolution is above.

`render.json` records `{set, path, background}` per rendered set and
`{set, reason}` per skipped one; `render.md` embeds
`<img src="thumbnails/set1.jpg" width="480">` under each `## Set N` beside the
copy it carries and the background it went onto.

**A set that produced no image keeps its heading and its copy, and says why**
(`**Not rendered:** background not found: frames/z/nope.jpg`). The loop is
"edit publish.json, run render, read render.md", so a set that quietly vanishes
from that file turns a one-character typo into a mystery whose only trace is a
log line the human is not reading. `render_sets()` therefore returns a
`RenderResult(renders, skipped)` rather than a bare list, and every `continue`
in its loop goes through `skip(index, reason)` — a missing background (with the
path that was tried), a failed measure or a failed `magick`, each carrying
magick's own stderr where there is one.

The embed goes through `markdown.embed_image(path, alt, width, markup)`, which
`render.image_markup` selects: `html` (default) keeps the sized `<img>` and
`markdown` emits `![alt](path)` for viewers that strip raw HTML. Markdown has
no width syntax, so the size hint is dropped rather than faked. `publish.md`
shares that helper for its own frame-shortlist table (under its own
`publish.image_markup`) — two markups rendered by two functions is two places
for a raw-HTML setting to be half-honoured.

### ImageMagick runs on the host

`magick` is called on the host, like the `blender` stage already is — a
deliberate exception to AGENTS.md's "route media tooling through the whisperx
Docker image" constraint (see Hard Constraints there). That constraint is
about ffmpeg; this is a different tool, and the decision is explicit: the
whisperx image has neither ImageMagick nor CJK fonts, and font slots resolve
through *host* fontconfig, which is what makes a CJK font slot work at all.

## Config

`render:` in `config.example.yml` (needs `magick` on PATH; `enabled: false`
skips compositing, not the copy):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Composite one thumbnail per copy set |
| `width` / `height` | `1280` / `720` | Canvas size in px |
| `line_gap` | `12` | Vertical gap between stacked lines, in px |
| `image_markup` | `html` | How `render.md` embeds images: `html` = sized `<img>`, `markdown` = `![alt](path)` |
| `fonts` | `{}` | Slot name → ImageMagick font name/path; the *only* names the model may put in a `font` value, since it cannot know what is installed |

These were `publish.thumbnail.*` before the split. There is **no config shim
and no deprecation window**: `PublishConfig` is `extra="forbid"`, so a config
still carrying `publish.thumbnail` fails at load with pydantic naming the key.
That is the right failure — one project has to move five lines, and a silent
fallback would leave two places to look for the canvas size.
`publish.thumbnail.background` is **deleted** rather than moved; there is no
`render.background`.
