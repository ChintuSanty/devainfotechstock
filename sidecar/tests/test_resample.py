import numpy as np

from app.audio.resample import Resampler, to_mono


def test_passthrough_when_rates_match():
    resampler = Resampler(16_000, 16_000)
    samples = np.linspace(-1, 1, 1000, dtype=np.float32)
    assert np.array_equal(resampler.process(samples), samples)


def test_downsample_length_is_proportional_across_chunks():
    resampler = Resampler(48_000, 16_000)
    total = 0
    for _ in range(10):
        total += resampler.process(np.zeros(4800, dtype=np.float32)).size
    # 48000 input samples -> ~16000 out, allowing for filter warm-up.
    assert 15_800 <= total <= 16_050


def test_downsampled_tone_keeps_its_shape():
    src_rate, dst_rate, freq = 48_000, 16_000, 440.0
    t = np.arange(src_rate, dtype=np.float32) / src_rate
    tone = np.sin(2 * np.pi * freq * t).astype(np.float32)

    resampler = Resampler(src_rate, dst_rate)
    out = np.concatenate([resampler.process(chunk) for chunk in np.array_split(tone, 20)])

    # Skip the filter warm-up, then confirm the dominant bin is still 440 Hz.
    steady = out[dst_rate // 10 :]
    spectrum = np.abs(np.fft.rfft(steady * np.hanning(steady.size)))
    peak_hz = np.fft.rfftfreq(steady.size, 1 / dst_rate)[int(np.argmax(spectrum))]
    assert abs(peak_hz - freq) < 5.0


def test_to_mono_averages_interleaved_channels():
    interleaved = np.array([1.0, 0.0, 1.0, 0.0], dtype=np.float32)
    assert np.allclose(to_mono(interleaved, 2), [0.5, 0.5])
