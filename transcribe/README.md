# nagare-transcribe

Thin worker subprocess for nagare-clip: Haas-ja ASR and Silero VAD, kept in its
own uv environment (heavy torch/NeMo pins) so the main package never imports them.

- Setup: `uv sync --project transcribe`
- Run: `uv run --project transcribe python -m nagare_transcribe {vad,asr} REQUEST.json`
- `asr` needs a CUDA GPU; `vad` runs on CPU.
- Tests: `make test-transcribe`
