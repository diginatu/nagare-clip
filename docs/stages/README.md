# Stage runtime notes

Deep per-stage implementation detail — the "why it's built this way" and the
non-obvious edge cases. Loaded **on demand**: read the relevant file before
touching a stage. High-level per-stage overviews (inputs/outputs, purpose) and
the hard constraints live in the top-level [`AGENTS.md`](../../AGENTS.md).

| Topic | File |
|-------|------|
| Audio-silence detection | [audio_silence.md](audio_silence.md) |
| Sentence re-segmentation, windowing/carry-over, force-split | [sentence_split.md](sentence_split.md) |
| Silent-gap visual context (vision LLM, summary/director consumption) | [gap_context.md](gap_context.md) |
| Text filter + summary-stage filter context | [text_filter.md](text_filter.md) |
| Intervals: `<keep>`/`<speed>`/`<overlay>`/`<cut>` markers, margins, captions | [intervals.md](intervals.md) |
| Blender VSE layout, text styling, retiming | [blender.md](blender.md) |
| Pipeline orchestration (`run_pipeline.sh`) | [pipeline.md](pipeline.md) |
| Observability: LLM report + Langfuse tracing | [observability.md](observability.md) |
