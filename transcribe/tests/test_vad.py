import numpy as np
from nagare_transcribe import vad

PARAMS = {"threshold": 0.5, "min_speech": 0.25, "min_silence": 0.5, "speech_pad": 0.2}


def test_speech_regions_silence_is_empty():
    assert vad.speech_regions(np.zeros(16000 * 3, np.float32), PARAMS) == []
