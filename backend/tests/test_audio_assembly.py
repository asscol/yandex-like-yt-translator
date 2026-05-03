"""Tests for the timed-track assembler in app.services.audio."""

from __future__ import annotations

import numpy as np

from app.services.audio import TimedClip, _resample_linear, assemble_timed_track


def test_resample_identity():
    x = np.array([1.0, -1.0, 0.5], dtype=np.float32)
    assert np.allclose(_resample_linear(x, 1000, 1000), x)


def test_resample_doubles_length_when_upsampling():
    x = np.array([0.0, 1.0, 0.0, -1.0], dtype=np.float32)
    y = _resample_linear(x, 100, 200)
    # length doubles (within 1 sample)
    assert abs(y.shape[0] - x.shape[0] * 2) <= 1


def test_assemble_lays_clips_at_target_starts():
    sr = 1000
    a = np.ones(int(0.1 * sr), dtype=np.float32)        # 100 ms of "1"
    b = -np.ones(int(0.1 * sr), dtype=np.float32) * 0.5  # 100 ms of "-0.5"
    track, out_sr = assemble_timed_track(
        [
            TimedClip(start=0.0, samples=a, sample_rate=sr),
            TimedClip(start=0.5, samples=b, sample_rate=sr),
        ],
        total_duration=1.0,
        sample_rate=sr,
    )
    assert out_sr == sr
    # First clip occupies [0, 0.1)
    assert np.allclose(track[: int(0.1 * sr)], 1.0)
    # Silence in [0.1, 0.5)
    assert np.allclose(track[int(0.1 * sr) : int(0.5 * sr)], 0.0)
    # Second clip occupies [0.5, 0.6)
    assert np.allclose(track[int(0.5 * sr) : int(0.6 * sr)], -0.5)
    # Silence after
    assert np.allclose(track[int(0.6 * sr) : int(1.0 * sr)], 0.0)


def test_assemble_extends_track_if_clip_overflows():
    sr = 1000
    a = np.ones(int(2.0 * sr), dtype=np.float32)  # 2 s clip
    track, _ = assemble_timed_track(
        [TimedClip(start=0.5, samples=a, sample_rate=sr)],
        total_duration=1.0,  # but track requested only 1 s
        sample_rate=sr,
    )
    # Track should now be at least 2.5 s (start + clip)
    assert track.shape[0] >= int(2.5 * sr)


def test_assemble_no_clipping_after_normalization():
    sr = 1000
    a = np.ones(int(0.1 * sr), dtype=np.float32)
    b = np.ones(int(0.1 * sr), dtype=np.float32)
    # Force overlap so the sum exceeds 1.0
    track, _ = assemble_timed_track(
        [
            TimedClip(start=0.0, samples=a, sample_rate=sr),
            TimedClip(start=0.05, samples=b, sample_rate=sr),
        ],
        total_duration=0.2,
        sample_rate=sr,
    )
    assert float(np.max(np.abs(track))) <= 1.0 + 1e-6
