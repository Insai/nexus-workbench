from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from nexus_workbench.local import api
from nexus_workbench.local.bandpower import epoch_band_powers, parse_bands
from nexus_workbench.local.errors import LocalToolError

from .conftest import write_test_edf

FS = 100.0
SPE = int(30 * FS)


@pytest.mark.parametrize(("freq", "band"), [(2.0, 0), (6.0, 1), (10.0, 2), (14.0, 3), (22.0, 4)])
def test_a_sinusoid_lands_in_its_band_with_power_a2_over_2(freq: float, band: int) -> None:
    amp = 20.0
    t = np.arange(SPE) / FS
    x = (amp * np.sin(2 * np.pi * freq * t))[None, :]
    absolute, total = epoch_band_powers(x, FS, parse_bands(None))
    assert absolute[0, band] == pytest.approx(amp**2 / 2, rel=0.01)
    others = np.delete(absolute[0], band)
    assert others.max() < 1e-3 * amp**2
    assert total[0] == pytest.approx(amp**2 / 2, rel=0.01)


def test_two_sinusoids_split_by_their_powers() -> None:
    t = np.arange(SPE) / FS
    x = (30 * np.sin(2 * np.pi * 2 * t) + 10 * np.sin(2 * np.pi * 10 * t))[None, :]
    absolute, total = epoch_band_powers(x, FS, parse_bands(None))
    assert absolute[0, 0] == pytest.approx(450, rel=0.01)
    assert absolute[0, 2] == pytest.approx(50, rel=0.01)
    assert absolute[0, 0] / total[0] == pytest.approx(0.9, abs=0.005)


def test_relative_powers_sum_to_one_with_contiguous_bands() -> None:
    x = np.random.default_rng(0).normal(0, 20, (5, SPE))
    absolute, total = epoch_band_powers(x, FS, parse_bands(None))
    assert np.allclose(absolute.sum(axis=1) / total, 1.0)


def test_a_band_above_nyquist_is_undefined() -> None:
    x = np.random.default_rng(0).normal(0, 20, (1, 30 * 50))
    bands = parse_bands({"low": [1, 4], "high": [30, 40]})
    absolute, _ = epoch_band_powers(x, 50.0, bands)
    assert np.isfinite(absolute[0, 0]) and np.isnan(absolute[0, 1])


def test_band_arguments() -> None:
    assert [b.name for b in parse_bands(None)] == ["delta", "theta", "alpha", "sigma", "beta"]
    assert parse_bands([{"name": "spindle", "lowHz": 11, "highHz": 16}])[0].low == 11.0
    assert parse_bands([["slow", 0.3, 1.0]])[0].high == 1.0
    for bad in ({"x": [4, 1]}, {"bad name": [1, 2]}, {"a": [1]}, [], "delta"):
        with pytest.raises(LocalToolError):
            parse_bands(bad)


def test_artifact_epochs_are_excluded(tmp_path: Path) -> None:
    """A 10 Hz, 20 µV sine with two flat epochs and one saturated epoch: only the clean
    epochs count, so the mean alpha power is the clean value, not diluted or inflated."""
    n_ep = 10
    t = np.arange(SPE * n_ep) / FS
    x = 20 * np.sin(2 * np.pi * 10 * t) + np.random.default_rng(1).normal(0, 1, t.size)
    x[SPE * 2 : SPE * 4] = 0.0
    x[SPE * 6 : SPE * 7] = 1800 * np.sign(np.sin(2 * np.pi * 1 * t[SPE * 6 : SPE * 7]))
    p = write_test_edf(tmp_path / "a.edf", [x], FS, ["C3-M2"], phys=2000.0)
    r = api.compute_band_power(str(p))
    ch = r["channels"][0]
    assert ch["epochsTotal"] == 10 and ch["epochsUsed"] == 7
    assert ch["absolute"][2] == pytest.approx(200, rel=0.02)
    assert sum(ch["relative"]) == pytest.approx(1.0, abs=1e-3)
