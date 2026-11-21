# Thumbnail craft

A thumbnail is seen small — a phone's feed, a sidebar — for a moment. It has
one job: make the viewer want what the title promises. The words come from
the story (written before you looked at the stills); the picture is the still
that best carries those words.

## The vocabulary

The canvas is `render.width` x `render.height` (1280 x 720 by default). The
background is cropped to fill it, centred, whatever its aspect ratio.

**Per set** (the text block as a whole):

| key | values | default |
| --- | --- | --- |
| `gravity` | `northwest` `north` `northeast` `west` `center` `east` `southwest` `south` `southeast` | `northwest` |
| `offset` | `+X+Y` px from that anchor (`+56+62`) | `+56+62` |
| `line_gap` | int 0-400, px between the stacked lines of this set | `render.line_gap` (12) |
| `shadow` | `true`, `false`, or `{"color": "rgba(0,0,0,0.8)", "blur": "0x8"}` | on, `rgba(0,0,0,0.8)`, `0x8` |

The lines stack top to bottom from the anchor. With a `south*` gravity the
block is laid out from its last line, and a larger `+Y` moves it **up**.

**Per line:**

| key | values |
| --- | --- |
| `text` | required; the words |
| `role` | `tag`, `hook`, `subtitle` (absent = `hook`); only picks defaults for the keys you leave out |
| `font` | a slot name from the project's `render.fonts` (`write` lists the slots when you name a wrong one) |
| `pointsize` | 8-400 |
| `fill`, `stroke` | `#RRGGBB` / `#RGB` / `#RRGGBBAA`, `rgb(r,g,b)` / `rgba(r,g,b,a)`, or `white` `black` `gray` `red` `green` `blue` `yellow` `cyan` `magenta` `orange` `none` `transparent` |
| `strokewidth` | 0-40 |

Role defaults: `tag` 70 pt, `hook` 156 pt, `subtitle` 98 pt, with an outline
and colours that differ per set. A line that names no font gets the first
slot in `render.fonts`; with no slots configured, ImageMagick's default face
draws **nothing** for a CJK character, so if every render comes back without
text, tell the user to set `render.fonts`.

A line wider than the canvas (less the `X` offset on both sides) has its
point size shrunk to fit. Height is not shrunk: when the block is taller
than the canvas, `render.md` warns — shorten the copy, drop a line or lower
the sizes.

## Legibility

- **Few words, big.** One hook line that reads at a glance beats three that
  explain. A tag (`3日目`, `解決編`) or subtitle supports the hook; it does
  not repeat it.
- **Contrast against what is behind it**, not against the frame on average.
  Light text with a dark outline (`stroke` + `strokewidth` 6-12 at hook
  sizes) and a shadow holds up on a busy background; on a flat dark area,
  plain light text may be enough. Check the render, not your intention.
- **One accent colour.** A yellow or red tag against a white hook draws the
  eye; five colours read as noise.
- Judge at **small size**: if the headline needs the full-size image to be
  read, it is too small or too thin.

## Where the text goes

- **Put the text where the picture is empty** — sky, wall, floor, an
  out-of-focus area — and choose the `gravity` from that, not from habit.
- **Never cover the subject**: the face, the part, the result the video is
  about. If there is no empty area, pick another still, or put a smaller
  block in a corner.
- Keep clear of the bottom-right corner: YouTube draws the duration there.
- Different sets should be different **decisions** (another still, another
  angle on the copy), not one thumbnail in four colours.

## Iterate on the render

After every `write`, Read `<output_dir>/render/thumbnails/set{N}.jpg`. Typical
fixes: text over the subject → change `gravity`/`offset` or the still;
unreadable → thicker `stroke`, a shadow, another colour or another still;
cramped → fewer words or a smaller `pointsize`; a set missing → its reason is
in `<output_dir>/render/render.md` (`**Not rendered:** ...`).
