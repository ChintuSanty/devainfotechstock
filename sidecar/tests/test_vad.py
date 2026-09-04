import numpy as np

from app.audio.vad import UtteranceSegmenter

RATE = 16_000


def tone(seconds: float, amplitude: float = 0.3) -> np.ndarray:
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), dtype=np.float32)


def test_speech_followed_by_silence_closes_one_utterance():
    segmenter = UtteranceSegmenter(RATE, silence_ms=400, min_speech_ms=200)
    utterances = segmenter.push(np.concatenate([silence(0.5), tone(1.2), silence(1.0)]))
    assert len(utterances) == 1
    assert utterances[0].audio.size > RATE  # at least a second of speech


def test_pure_silence_produces_nothing():
    segmenter = UtteranceSegmenter(RATE, silence_ms=400, min_speech_ms=200)
    assert segmenter.push(silence(3.0)) == []
    assert segmenter.flush() is None


def test_two_utterances_are_split_by_the_gap():
    segmenter = UtteranceSegmenter(RATE, silence_ms=400, min_speech_ms=200)
    stream = np.concatenate([tone(0.8), silence(1.0), tone(0.8), silence(1.0)])
    assert len(segmenter.push(stream)) == 2


def test_long_monologue_is_flushed_at_the_cap():
    segmenter = UtteranceSegmenter(RATE, silence_ms=2000, min_speech_ms=200, max_utterance_ms=1000)
    utterances = segmenter.push(tone(5.0))
    assert len(utterances) >= 4


def test_flush_returns_speech_still_open():
    segmenter = UtteranceSegmenter(RATE, silence_ms=2000, min_speech_ms=200)
    assert segmenter.push(tone(1.0)) == []
    trailing = segmenter.flush()
    assert trailing is not None and trailing.audio.size > 0
