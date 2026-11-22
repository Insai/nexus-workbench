"""``preprocess``: a new EDF, filtered as asked, the input untouched, bad settings refused."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_workbench.local import LocalToolError, api
from nexus_workbench.local.edf import EdfFile, OutSignal, write_edf
from tests.local.conftest import write_test_edf

FS = 250


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def signal(path: str | Path, index: int = 0) -> tuple[np.ndarray, float]:
    edf = EdfFile(str(path))
    with edf:
        return edf.header.to_physical(index, edf.read_signal(index)), edf.header.fs(index)


def amplitude(x: np.ndarray, fs: float, hz: float) -> float:
    spectrum = np.abs(np.fft.rfft(x)) * 2 / len(x)
    return float(spectrum[np.argmin(np.abs(np.fft.rfftfreq(len(x), 1 / fs) - hz))])


@pytest.fixture
def copy(night: dict[str, Any], tmp_path: Path) -> Path:
    """The shared sample night, copied so each test writes next to its own file."""
    target = tmp_path / "sample_night.edf"
    shutil.copy(night["edf"], target)
    return target


@pytest.fixture
def mains(tmp_path: Path) -> Path:
    """Two minutes at 250 Hz: 20 uV at 10 Hz plus 40 uV of 50 Hz mains, and a 1 Hz channel."""
    t = np.arange(FS * 120) / FS
    eeg = 20 * np.sin(2 * np.pi * 10 * t) + 40 * np.sin(2 * np.pi * 50 * t)
    eog = 30 * np.sin(2 * np.pi * 1 * t)
    return write_test_edf(tmp_path / "mains.edf", [eeg, eog], FS, ["C3-M2", "E1-M2"])


def test_notch_attenuates_a_50_hz_sine_and_keeps_10_hz(mains: Path) -> None:
    before = digest(mains)
    result = api.preprocess(str(mains), notch_hz=50)
    out = mains.with_name("mains_preprocessed.edf")
    assert out.exists()
    assert digest(mains) == before  # the input is untouched
    x, _ = signal(mains)
    y, fs = signal(out)
    assert amplitude(x, fs, 50) > 35
    assert amplitude(y, fs, 50) < 0.05 * amplitude(x, fs, 50)
    assert amplitude(y, fs, 10) == pytest.approx(amplitude(x, fs, 10), rel=0.02)
    assert result["applied"] == ["notch"]
    assert result["filters"]["notchHz"] == 50 and result["filters"]["notchQ"] == 30
    assert result["output"] == "mains_preprocessed.edf"
    assert result["outputId"] != result["recordingId"]
    assert result["annotationsCopied"] is False


def test_band_pass_removes_what_is_outside_the_band(mains: Path) -> None:
    api.preprocess(str(mains), low_hz=0.5, high_hz=30)
    y, fs = signal(mains.with_name("mains_preprocessed.edf"))
    assert amplitude(y, fs, 50) < 1.0
    assert amplitude(y, fs, 10) == pytest.approx(20, rel=0.05)


def test_resample_and_channel_choice(mains: Path, tmp_path: Path) -> None:
    out = tmp_path / "out" / "small.edf"
    out.parent.mkdir()
    result = api.preprocess(
        str(mains), high_hz=40, resample_hz=100, channels=["C3-M2"], out=str(out)
    )
    assert result["output"] == "small.edf"
    assert [c["label"] for c in result["channels"]] == ["C3-M2"]
    assert result["channels"][0]["outputSampleRateHz"] == 100
    assert result["channelsWritten"] == 1 and result["channelsLeftOut"] == 1
    info = api.inspect_edf(str(out))
    assert info["nChannels"] == 1
    assert info["channels"][0]["sampleRateHz"] == 100
    assert info["durationSec"] == 120
    y, fs = signal(out)
    assert amplitude(y, fs, 10) == pytest.approx(20, rel=0.05)


def test_on_the_sample_night(copy: Path) -> None:
    before = digest(copy)
    result = api.preprocess(str(copy), low_hz=0.3, high_hz=35)
    assert digest(copy) == before
    assert result["format"] == "EDF+C"
    assert [c["filtered"] for c in result["channels"]] == [True] * 4
    assert result["durationSec"] == 3600
    quality = api.check_signal_quality(str(copy.with_name("sample_night_preprocessed.edf")))
    assert quality["nEpochs"] == 120  # the new file reads like any other


def test_output_never_replaces_a_file(mains: Path) -> None:
    api.preprocess(str(mains), notch_hz=50)
    out = mains.with_name("mains_preprocessed.edf")
    written = digest(out)
    with pytest.raises(LocalToolError, match="already exists"):
        api.preprocess(str(mains), notch_hz=50)
    with pytest.raises(LocalToolError, match="never overwrites its input"):
        api.preprocess(str(mains), notch_hz=50, out=str(mains))
    assert digest(out) == written


def test_out_may_be_a_folder(mains: Path, tmp_path: Path) -> None:
    folder = tmp_path / "derived"
    folder.mkdir()
    result = api.preprocess(str(mains), notch_hz=50, out=str(folder))
    assert result["output"] == "mains_preprocessed.edf"
    assert (folder / "mains_preprocessed.edf").exists()


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({}, "Nothing to do"),
        ({"low_hz": 30, "high_hz": 10}, "low_hz must be below high_hz"),
        ({"low_hz": -1}, "more than 0 Hz"),
        ({"high_hz": "fast"}, "number of hertz"),
        ({"notch_hz": 55}, "50 or 60"),
        ({"high_hz": 200}, "not below half the sample rate"),
        ({"notch_hz": 60, "channels": [1], "resample_hz": 3.5}, "whole number"),
        ({"high_hz": 60, "resample_hz": 100}, "below half of resample_hz"),
        ({"channels": ["Fz"]}, "No channel matches"),
        ({"notch_hz": 50, "out": "x.txt"}, "must end in .edf"),
    ],
)
def test_refusals(mains: Path, kwargs: dict[str, Any], needle: str) -> None:
    with pytest.raises(LocalToolError, match=needle):
        api.preprocess(str(mains), **kwargs)
    assert not mains.with_name("mains_preprocessed.edf").exists()


def test_notch_at_the_nyquist_rate_is_refused_on_the_sample_night(copy: Path) -> None:
    with pytest.raises(LocalToolError, match=r"notch_hz \(50 Hz\) is not below half"):
        api.preprocess(str(copy), notch_hz=50)  # 100 Hz: 50 Hz is the Nyquist frequency


def test_missing_folder_for_out(mains: Path, tmp_path: Path) -> None:
    with pytest.raises(LocalToolError, match="does not exist"):
        api.preprocess(str(mains), notch_hz=50, out=str(tmp_path / "nope" / "a.edf"))


def test_non_voltage_channels_are_copied_unfiltered(tmp_path: Path) -> None:
    t = np.arange(FS * 60) / FS
    spo2 = 95 + 2 * np.sin(2 * np.pi * 0.1 * t)
    path = tmp_path / "mixed.edf"
    write_edf(
        path,
        [
            OutSignal("C3-M2", 20 * np.sin(2 * np.pi * 10 * t), FS, "uV", -500, 500),
            OutSignal("SpO2", spo2, FS, "%", 0, 100),
        ],
    )
    result = api.preprocess(str(path), low_hz=1, resample_hz=125)
    assert [c["filtered"] for c in result["channels"]] == [True, False]
    assert [c["outputSampleRateHz"] for c in result["channels"]] == [125, FS]
    y, _ = signal(path.with_name("mixed_preprocessed.edf"), 1)
    x, _ = signal(path, 1)
    assert np.allclose(x, y)  # copied as it was
