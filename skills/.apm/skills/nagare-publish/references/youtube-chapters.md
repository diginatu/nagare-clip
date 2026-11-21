# YouTube chapters

YouTube turns a timestamp list in the description into chapters — a
segmented progress bar with named sections — only when all of these hold:

1. the first timestamp is `0:00`;
2. there are at least **three** timestamps;
3. they are in **ascending** order;
4. every chapter runs at least **10 seconds**.

Otherwise the timestamps are still links a viewer can click, but there is no
segmented bar.

## How `write` makes them hold

You name a chapter by its **display line** in `view`; you never write a time.

- **The time** is the first moment of that line that survives in the
  finished video (the time `view` prints beside it). A line marked `✂` is
  entirely cut and cannot carry a chapter — a problem, re-choose.
- **Order**: the chapters are sorted by that time, so the list is ascending
  even when the director reordered the footage. Two chapters at the same
  time are a problem.
- **`0:00`**: the earliest chapter is always printed as `0:00`, however late
  its line starts, so it is measured from the very start of the video.
- **Length**: every chapter (up to the next one, the last up to the end of
  the video) must run at least `publish.min_chapter_duration` seconds (10 by
  default). A shorter one is a problem: `write` does not merge it for you —
  choose whether to drop it or move a neighbour.
- **Count**: fewer than three chapters (zero included) is only a warning;
  the edit is written.
- Timestamps are truncated (`9.9 s` prints `0:09`), so a link never lands
  past the moment the chapter begins.

## Choosing chapters

- Chapters are free: they need not follow the summary's parts. Mark the
  moments a viewer would want to jump to or skip to — the problem stated,
  each attempt, the fix, the result.
- Put a chapter on the line where the section **starts** for the viewer:
  often the first line of a new topic, not the end of the previous one.
- Name them as a viewer scans them: short, concrete, in the video's
  language. They are also part of the description's 5000-character limit.
- The finished timeline must exist (the pipeline ran through `blender`);
  without it any chapter is a problem, and an edit with `"chapters": []`
  still writes.
