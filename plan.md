# Video Editor AI Plan

## Goal

Build a semi-automated rough-cut pipeline for long-form recordings:

1. Stage 1: WhisperX in Docker -> transcript files (`json/srt/vtt/...`)
2. Stage 2: Python interval processor -> `*_intervals.json`
3. Stage 3: Blender headless script -> pre-arranged `.blend` VSE project

Output is a reviewable `.blend`, not a final rendered export.

## Current Status

- `docker-compose.yml` implemented for `ghcr.io/jim60105/whisperx:large-v3-ja` with GPU reservation and persistent cache.
- `stage2_intervals.py` implemented with filler-word/silence exclusion, merge, invert, and min keep filtering.
- `stage3_blender.py` implemented with Blender arg split (`--`), source metadata detection, VSE strip packing, and `.blend` save.
- `run_pipeline.sh` implemented and tested end-to-end.

## Validated End-to-End Run

Test command:

```bash
./run_pipeline.sh "input/2022-05-28 23.00.21.mp4" ja
```

Observed outputs:

- `output/2022-05-28 23.00.21.json`
- `output/2022-05-28 23.00.21.srt`
- `output/2022-05-28 23.00.21.vtt`
- `output/2022-05-28 23.00.21_intervals.json`
- `output/2022-05-28 23.00.21_edited.blend`

## Runtime Notes (Important)

1. WhisperX image CLI compatibility:
   - `--word_timestamps` is not accepted by this image tag and was removed.
2. WhisperX image entrypoint quirk:
   - Stage 1 currently passes a dummy `_` argument before the media path in `run_pipeline.sh`.
   - This avoids the first positional argument being dropped by the image entrypoint shell wrapper.
3. Container user mapping:
   - Stage 1 runs as `--user "0:0"` to avoid runtime errors seen with host UID mapping.
   - Side effect: transcript artifacts are owned by `root` on host.
4. Blender stability:
   - Stage 3 is invoked with `--factory-startup` to avoid failures from broken local add-ons.
   - Stage 3 uses Blender 5-compatible sequence API fallback (`sequence_editor.strips`).

## No-Diarization Policy

- Do not pass `--diarize`.
- Do not use `--hf_token`.
- Do not add diarization-specific dependencies or flows.

## Next Documentation Sync

Keep `README.md` and `AGENTS.md` aligned with:

- actual script flags
- tested command examples
- known runtime quirks above
