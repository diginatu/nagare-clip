# The shortlist

`frames` with no `--line` reads the shortlist off the director's ops: the
director already marked where the payoffs are, so the candidate moments come
from its edit rather than being guessed from the timeline. Each row says why
the still was chosen (its **kind** and the director's **label**), its display
line, its time in the finished video and its path.

## The kinds

- **overlay** — the director put a caption here, so it thought the moment
  worth naming. The still is taken **in the middle of the line** the caption
  is anchored to, not at its start: a line's first frame is often still the
  previous shot. The label is the caption's text.
- **keep** — the director kept a silent stretch because something happens on
  screen. The still is taken **in the middle of the event** it rescued, where
  the payoff usually is. The label is the op's note.
- **timelapse-start** / **timelapse-end** — a stretch of work played fast.
  It gets **both boundaries**: the state before the work and the state after
  it, a natural before/after pair. The label is the timelapse's caption, or
  its note.

Moments that coincide are collapsed into one still, keeping the kind listed
first above. The list is capped at `publish.max_frames` (24 by default; `0`
= no limit); when it is cut, overlays and timelapse boundaries outrank keeps.

## It is where to look, not the answer

- **The label is a claim.** It is what the director meant to be on screen,
  written from the transcript, not from the picture. Read the image and trust
  what is actually legible in it: a hand in the way, motion blur, a cut-off
  subject or a dark frame make a still unusable whatever its label says.
- **Add stills freely.** A strong frame often sits in a line the director
  never marked. Find it in `view` and run `frames --line N`; move within the
  line's footage with `N@pos` (`0` = its first moment, `1` = its last, `0.5`
  by default). A line marked `✂` in `view` is not in the finished video:
  avoid it as a background unless the user wants a frame from outside the cut.
- **`--sheet` is the cheap first look.** It composites every still in the list
  into `<output_dir>/publish/sheets/sheet<K>.jpg` (12 per sheet, tiles labelled
  `#i  L<line>  M:SS`, `#i` being the row in the printed list). Read the sheet
  to survey the list, then Read single stills to judge the ones you may use.
  **`--at M:SS`** takes a still at a time of the *finished* video, for a scene
  the user points at by its time.
- **`--line` adds, plain `frames` replaces.** Plain `frames` rewrites
  `<output_dir>/publish/stills.json` from the director's ops; the stills you added
  with `--line` drop off the list (their JPEGs stay on disk, and a
  `background` may still name them).
- Several `--line` flags in one call share one Docker run, which is far
  cheaper than one call per still: ask for every line you want at once.
