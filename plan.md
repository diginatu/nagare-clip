# Video Editor AI Plan

## Goal

Build a semi-automated rough-cut pipeline for long-form recordings:

1. Stage 1: WhisperX in Docker -> transcript files (`json/srt/vtt/...`)
2. Stage 2: Text editing checkpoint -> `_edits.txt` (copy or LLM-filtered with `{{old->new}}` markers)
3. Stage 3: Patch application + interval processor -> `*_intervals.json`
4. Stage 4: Blender headless script -> pre-arranged `.blend` VSE project

Output is a reviewable `.blend`, not a final rendered export.

## Current Status

- Project refactored from flat layout to `src/` package layout with `hatchling` build backend.
- `docker-compose.yml` implemented for `ghcr.io/jim60105/whisperx:large-v3-ja` with GPU reservation and persistent cache.
- `src/nagare_clip/config.py` implements a centralised YAML config system: `DEFAULTS` dict, `load_config`, `deep_merge`, and `get_effective_config` (DEFAULTS ← file ← CLI overrides). `config.example.yml` documents all keys.
- Stage 2 is a mandatory text editing checkpoint. When `use_llm: false` (default), it copies Stage 1 `.txt` to `_edits.txt`. When enabled, it runs LLM-based correction and preserves `{{old->new}}` markers in the output for human review. Humans can edit `_edits.txt` then resume with `--from-stage 3`.
- Stage 3 applies `{{old->new}}` patches from `_edits.txt`, syncs corrected text back into WhisperX JSON timing data, then computes keep intervals. Interval logic uses silence exclusion on WhisperX word timings with a configurable per-word span cap (default 0.6s), merge/invert, min keep filtering, configurable pre/post keep margins (default 1s) with overlap merge, and GiNZA bunsetsu-based caption chunking split on silence gaps and keep-boundary crossings. Bunsetsu timing uses `ginza.bunsetu_spans(doc)` (GiNZA/spaCy) so particles and auxiliaries attach to the preceding content word. It detects large intra-bunsetsu character gaps from WhisperX misalignment and snaps the bunsetsu start forward to the later character cluster. Captions are preserved as transcript chunks, keep intervals are expanded to cover caption spans, and minimum keep duration is re-applied afterward to reduce tiny strips.
- Stage 4 modules implement Blender arg split (`--`), source metadata detection (with configurable default FPS), VSE strip packing, caption placement (style from config), and `.blend` save.
- `scripts/run_pipeline.sh` implemented and tested end-to-end with configurable input (`--input-videos-dir`, default `src_video`) and output (`--output-dir`, default `output`) directories shared with Docker Compose. Supports `--config FILE` with precedence logic: CLI > config file > defaults. Config-file values for `stage1` (`compute_type`, `batch_size`, `align_model`) and `pipeline` (`input_videos_dir`, `output_dir`) are read directly in the shell via Python/yaml; `stage3`/`stage4` config is forwarded as `--config` to the respective Python processes.

## Validated End-to-End Run

Test command:

```bash
./scripts/run_pipeline.sh "input/2022-05-28 23.00.21.mp4" ja
```

Observed outputs:

- `output/stage1/2022-05-28 23.00.21.json`
- `output/stage1/2022-05-28 23.00.21.txt`
- `output/stage2/2022-05-28 23.00.21_edits.txt`
- `output/stage3/2022-05-28 23.00.21_intervals.json`
- `output/stage4/2022-05-28 23.00.21_edited.blend`

## Runtime Notes

1. WhisperX image CLI compatibility:
   - `--word_timestamps` is not accepted by this image tag and was removed.
2. WhisperX image entrypoint quirk:
   - Stage 1 currently passes a dummy `_` argument before the media path in `scripts/run_pipeline.sh`.
   - This avoids the first positional argument being dropped by the image entrypoint shell wrapper.
3. Container user mapping:
   - Stage 1 runs as `--user "0:0"` to avoid runtime errors seen with host UID mapping.
   - Side effect: transcript artifacts are owned by `root` on host.
4. Blender stability:
   - Stage 4 uses Blender 5-compatible sequence API fallback (`sequence_editor.strips`).

## Next Documentation Sync

Keep `README.md` and `AGENTS.md` aligned.
