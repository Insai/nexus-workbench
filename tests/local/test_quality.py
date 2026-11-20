"""Signal quality on constructed signals. All signals are synthetic."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_workbench.local import api
from nexus_workbench.local.errors import LocalToolError
from nexus_workbench.local.quality import assess

from .conftest import write_test_edf

FS = 256.0
N_EP = 10
SPE = int(FS * 30)


def _eeg(seed: int = 0) -> np.ndarray:
    """10 µV noise with a 13 Hz, 40 µV burst every 20 s."""
    rng = np.random.default_rng(seed)
    n = SPE * N_EP
    t = np.arange(n) / FS
    x = rng.normal(0, 10, n)
    for on in np.arange(12.0, n / FS - 5, 20.0):
        s, e = int(on * FS), int((on + 1.0) * FS)
        x[s:e] += 40 * np.sin(2 * np.pi * 13 * t[s:e]) * np.hanning(e - s)
    return x


def _railed() -> np.ndarray:
    """A large oscillation clipped at ±3,700 µV throughout, with noise riding below the clip."""
    n = SPE * N_EP
    t = np.arange(n) / FS
    x = 9000 * np.sin(2 * np.pi * 1.3 * t) + np.random.default_rng(1).normal(0, 30, n)
    return np.clip(x, -3700, 3700)


def test_clean_eeg_is_ok() -> None:
    r = assess(_eeg(), FS)
    assert r["overall"] == "ok"
    assert r["counts"] == {"ok": N_EP, "suspect": 0, "flat": 0, "railed": 0}
    assert r["extreme_fraction"] < 1e-3


def test_clipped_throughout_is_railed() -> None:
    r = assess(_railed(), FS)
    assert r["overall"] == "railed"
    assert r["n_saturated"] == N_EP
    assert r["counts"]["railed"] == N_EP


def test_pinned_at_a_rail_is_railed_and_zero_fill_is_flat() -> None:
    n = SPE * N_EP
    pinned = assess(np.full(n, 3700.0), FS)
    assert pinned["overall"] == "railed" and pinned["counts"]["railed"] == N_EP
    zero = assess(np.zeros(n), FS)
    assert zero["overall"] == "flat" and zero["counts"]["flat"] == N_EP


@pytest.mark.parametrize("n_clipped", [3, 5, 9])
def test_partly_clipped_is_suspect_overall_and_railed_per_epoch(n_clipped: int) -> None:
    x = _eeg()
    k = SPE * n_clipped
    x[:k] = _railed()[:k]
    r = assess(x, FS)
    assert r["overall"] == "suspect"
    assert r["n_saturated"] == n_clipped
    assert r["classes"] == ["railed"] * n_clipped + ["ok"] * (N_EP - n_clipped)


def test_low_level_clip_is_caught_by_the_extremes_share() -> None:
    """Clipped at ±150 µV: far below the saturation threshold, but on its rails a lot."""
    r = assess(np.clip(_eeg() * 20, -150, 150), FS)
    assert r["n_saturated"] == 0
    assert r["extreme_fraction"] >= 0.25
    assert r["overall"] == "railed"


def test_nothing_to_judge() -> None:
    assert assess(np.zeros(100), FS)["overall"] is None  # shorter than one epoch
    assert assess(np.full(SPE * 2, np.nan), FS)["overall"] is None


def test_constructed_epochs_each_get_their_class() -> None:
    """One epoch of each kind, side by side in one channel."""
    rng = np.random.default_rng(3)
    clean = rng.normal(0, 10, SPE)
    flat = np.zeros(SPE)
    railed = np.full(SPE, 1500.0)  # held at a rail level
    burst = rng.normal(0, 10, SPE)
    burst[1000:1010] = [1500, -1500] * 5  # peak-to-peak 3000 µV: saturated
    dropout = rng.normal(0, 10, SPE)
    dropout[2000 : 2000 + int(1.5 * FS)] = 0.0  # a 1.5 s constant run at 0 µV
    rail_run = rng.normal(0, 10, SPE)
    rail_run[3000 : 3000 + int(1.5 * FS)] = 1200.0  # a 1.5 s constant run at a rail level
    loud = rng.normal(0, 300, SPE)  # RMS above 250 µV, peak-to-peak under 2000 µV
    loud = np.clip(loud, -950, 950) * 0.999 + rng.normal(0, 0.01, SPE)
    x = np.concatenate([clean, flat, railed, burst, dropout, rail_run, loud, clean])
    r = assess(x, FS)
    assert r["classes"] == [
        "ok",
        "flat",
        "railed",
        "railed",
        "suspect",
        "railed",
        "suspect",
        "ok",
    ]


def test_edf_level_quality_finds_the_planted_stretches(tmp_path: Path) -> None:
    fs = 100
    n_ep = 20
    rng = np.random.default_rng(4)
    x = rng.normal(0, 15, fs * 30 * n_ep)
    y = rng.normal(0, 15, fs * 30 * n_ep)
    x[fs * 30 * 4 : fs * 30 * 7] = 0.0  # 3 flat epochs
    y[fs * 30 * 10 : fs * 30 * 12] = 500.0  # 2 epochs pinned at the top of the ±500 range
    p = write_test_edf(tmp_path / "q.edf", [x, y], fs, ["C3-M2", "C4-M1"])
    r = api.check_signal_quality(str(p))
    by = {c["label"]: c for c in r["channels"]}
    assert by["C3-M2"]["counts"] == {"ok": 17, "suspect": 0, "flat": 3, "railed": 0}
    # 500 µV is below the 1000 µV rail level, but it is the file's digital maximum.
    assert by["C4-M1"]["counts"] == {"ok": 18, "suspect": 0, "flat": 0, "railed": 2}
    assert r["badSpans"] == [
        {"channel": "C3-M2", "startSec": 120.0, "endSec": 210.0, "class": "flat"},
        {"channel": "C4-M1", "startSec": 300.0, "endSec": 360.0, "class": "railed"},
    ]
    assert r["usablePercent"] == pytest.approx(100 * 35 / 40)
    assert r["allChannelsOkPercent"] == pytest.approx(100 * 15 / 20)


def test_channels_in_other_units_are_converted_or_skipped(tmp_path: Path) -> None:
    rng = np.random.default_rng(5)
    x_v = rng.normal(0, 15e-6, 100 * 60)  # 15 µV noise written in volts
    p = write_test_edf(tmp_path / "v.edf", [x_v], 100, ["C3-M2"], phys=500e-6, unit="V")
    r = api.check_signal_quality(str(p))
    assert r["channels"][0]["counts"]["ok"] == 2
    q = write_test_edf(tmp_path / "pct.edf", [np.full(6000, 95.0)], 100, ["SpO2"], unit="%")
    with pytest.raises(LocalToolError, match="voltage"):
        api.check_signal_quality(str(q))


def test_dc_offset_does_not_make_epochs_suspect(tmp_path: Path) -> None:
    x = _eeg() + 300.0  # a 300 µV electrode offset on clean EEG
    r = assess(x, FS)
    assert r["overall"] == "ok" and r["counts"]["ok"] == N_EP
    p = write_test_edf(tmp_path / "dc.edf", [x[: 100 * 300]], 100, ["C3-M2"], phys=1000.0)
    q = api.check_signal_quality(str(p))
    assert q["channels"][0]["counts"]["ok"] == 10
    bp = api.compute_band_power(str(p))
    assert bp["channels"][0]["epochsUsed"] == 10


def test_overall_is_never_ok_when_no_epoch_is_ok() -> None:
    rng = np.random.default_rng(9)
    loud = np.clip(rng.normal(0, 300, SPE * N_EP), -950, 950)  # every epoch suspect (std)
    r = assess(loud, FS)
    assert r["counts"]["ok"] == 0
    assert r["overall"] == "suspect"


def test_overall_follows_the_epoch_counts() -> None:
    """A 1 s constant run (not at a rail level) in every epoch makes every epoch suspect, so
    the channel is suspect overall, not railed: flat and railed overall need every epoch so."""
    x = _eeg()
    for k in range(N_EP):
        x[k * SPE + 1000 : k * SPE + 1000 + int(FS)] = 5.0
    r = assess(x, FS)
    assert r["counts"] == {"ok": 0, "suspect": N_EP, "flat": 0, "railed": 0}
    assert r["overall"] == "suspect"
    # A dead channel held at a rail level in most epochs and at a low level in the others is
    # neither flat nor railed throughout.
    levels = [3700.0] * 6 + [10.0] * 3 + [5.0]
    m = assess(np.concatenate([np.full(SPE, v) for v in levels]), FS)
    assert m["counts"] == {"ok": 0, "suspect": 0, "flat": 3, "railed": 7}
    assert m["overall"] == "suspect"


def _ecg(fs: int, seconds: int, seed: int = 0) -> np.ndarray:
    """A synthetic ECG: 2.5 mV R peaks at 60 beats per minute on small noise."""
    rng = np.random.default_rng(seed)
    t = np.arange(fs * seconds) / fs
    x = rng.normal(0, 10, t.size)
    for beat in np.arange(0.5, seconds, 1.0):
        x += 2500 * np.exp(-0.5 * ((t - beat) / 0.012) ** 2)  # R wave
        x -= 300 * np.exp(-0.5 * ((t - beat - 0.04) / 0.015) ** 2)  # S wave
    return x


def test_ecg_r_peaks_are_not_railed(tmp_path: Path) -> None:
    fs = 256
    ecg = _ecg(fs, 300)
    eeg = np.random.default_rng(1).normal(0, 15, fs * 300)
    big = np.random.default_rng(2).normal(0, 15, fs * 300)
    big[fs * 30 : fs * 60] += 2500 * np.sin(2 * np.pi * 1.0 * np.arange(fs * 30) / fs)
    p = write_test_edf(
        tmp_path / "ecg.edf", [ecg, eeg, big], fs, ["ECG", "C3-M2", "E1-M2"], phys=5000.0
    )
    r = api.check_signal_quality(str(p))
    by = {c["label"]: c for c in r["channels"]}
    assert by["ECG"]["type"] == "ecg"
    assert by["ECG"]["counts"]["ok"] == 10 and by["ECG"]["overall"] == "ok"
    assert by["C3-M2"]["counts"]["ok"] == 10
    # The amplitude rules still apply to EOG: a 5 mV swing in epoch 2 is railed.
    assert by["E1-M2"]["counts"]["railed"] == 1
