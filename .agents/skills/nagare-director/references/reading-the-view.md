# Reading the view

`view` prints the whole video, every source in shooting order, under one
numbering:

```
[1] PXL_20260502_085157585
1: 今日はポンプを直します  [3.2s]
2: まずタンクを外して  [2.1s, gap 0.8s]
3: [silent 29.9s: a hand enters from the right holding a clear tube]
4: この状態で  [13.0s speech, 62.9s silence]
    [silent gap: the pump is reassembled on the bench]

[2] PXL_20260426_090431216
5: ...
```

- `[k] <stem>` heads one source (one recorded video). Ops never cross from
  one block into the next.
- **Brackets** are the line's timing. `[4.2s]` is its duration;
  `[4.2s, gap 0.8s]` adds the short silent gap before the next line (a
  negligible gap is omitted). A line with long internal silence splits it:
  `[13.0s speech, 62.9s silence]` — 13.0 s plays and 62.9 s is dropped by
  default; together they are the line's footage. It may carry a gap too.
- A **silence line** (`3: [silent 29.9s: ...]`) is a long wait between the
  lines either side of it, with what is visible during it when the
  gap_context stage described it. It has a number of its own: put that
  number in an op (a keep restores the wait, a timelapse plays it fast, an
  order range may start or end on it). Without a description it reads
  `[silent 29.9s]`.
- An indented `[silent gap: ...]` with no number is silence INSIDE the line
  above. It cannot be addressed; act on the numbered line.
- Lines without a bracket mean the timing is unknown (no WhisperX JSON for
  that source); runtimes are then not computable.

The header above the transcript gives the source index (`[k] stem: lines
a-b`) and the **default runtime** — what the video plays with no op at all.

`preview` reports, per op, the footage it takes and the seconds it puts on
screen beside the default seconds for the same lines, the boundary silences it
leaves out, every unintelligible line under a fast span, the caption timing,
then the segment as it plays (runs of `1x`, `1x+silence`, `timelapse`, `cut`),
the video in playback order with every seam quoted, the captions in playback
order, and `whole video so far: default X → with the ops Y`. Its header text
was written for the old LLM director ("this conversation", "send plan in any
reply"); read it as "this edit".
