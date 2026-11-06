"""Silero VAD speech regions (CPU)."""

import numpy as np

from .audio import SAMPLE_RATE


def speech_regions(audio: np.ndarray, params: dict) -> list[tuple[float, float]]:
    """Speech spans in seconds. ``params`` values are seconds (threshold is a probability)."""
    import torch
    from silero_vad import get_speech_timestamps, load_silero_vad

    model = load_silero_vad()
    ts = get_speech_timestamps(
        torch.from_numpy(audio),
        model,
        sampling_rate=SAMPLE_RATE,
        threshold=params["threshold"],
        min_speech_duration_ms=round(params["min_speech"] * 1000),
        min_silence_duration_ms=round(params["min_silence"] * 1000),
        speech_pad_ms=round(params["speech_pad"] * 1000),
        return_seconds=True,
    )
    return [(t["start"], t["end"]) for t in ts]
