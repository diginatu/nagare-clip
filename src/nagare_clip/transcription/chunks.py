"""Pack VAD speech regions into ASR chunks (pure)."""


def pack_regions(
    regions: list[tuple[float, float]], max_seconds: float
) -> list[tuple[float, float]]:
    """Greedily merge consecutive regions while the chunk stays within max_seconds.

    A region longer than the limit becomes a chunk of its own.
    """
    chunks: list[tuple[float, float]] = []
    for start, end in regions:
        if chunks and end - chunks[-1][0] <= max_seconds:
            chunks[-1] = (chunks[-1][0], end)
        else:
            chunks.append((start, end))
    return chunks
