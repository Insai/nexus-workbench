"""``preprocess``: a new EDF, filtered as asked, the input untouched, bad settings refused."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from scipy.signal import butter, sosfiltfilt

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


def test_the_documented_command_works_on_the_sample_night(copy: Path) -> None:
    """``sample`` then ``preprocess ... --low 0.3 --high 35 --notch 50``: at 100 Hz there is
    nothing at 50 Hz, so the notch is skipped per channel and named in notes."""
    before = digest(copy)
    result = api.preprocess(str(copy), low_hz=0.3, high_hz=35, notch_hz=50)
    assert digest(copy) == before
    assert result["applied"] == ["bandpass"]  # the notch ran on no channel
    assert result["filters"]["notchHz"] == 50  # what was asked
    assert [c["filtered"] for c in result["channels"]] == [True] * 4
    assert [c["clippedSamples"] for c in result["channels"]] == [0] * 4
    labels = [c["label"] for c in result["channels"]]
    assert result["notes"] == [
        f"notch 50 Hz skipped on {label}: at or above half its 100 Hz sample rate"
        for label in labels
    ]


def test_notch_alone_above_nyquist_everywhere_is_refused(copy: Path) -> None:
    with pytest.raises(LocalToolError, match=r"notch_hz \(50 Hz\) is not below half"):
        api.preprocess(str(copy), notch_hz=50)  # nothing would change
    assert not copy.with_name("sample_night_preprocessed.edf").exists()


def test_high_edge_above_nyquist_leaves_a_high_pass(copy: Path) -> None:
    result = api.preprocess(str(copy), low_hz=0.3, high_hz=60)
    assert all(c["filtered"] for c in result["channels"])
    assert result["applied"] == ["highpass"]
    assert result["notes"][0].startswith("high 60 Hz skipped on ")
    assert result["notes"][0].endswith(
        "at or above half its 100 Hz sample rate, so only the 0.3 Hz high-pass applies"
    )


def test_a_256_hz_file_still_gets_the_notch(tmp_path: Path) -> None:
    fs = 256
    t = np.arange(fs * 120) / fs
    eeg = 20 * np.sin(2 * np.pi * 10 * t) + 40 * np.sin(2 * np.pi * 50 * t)
    path = write_test_edf(tmp_path / "fast.edf", [eeg], fs, ["C3-M2"])
    result = api.preprocess(str(path), low_hz=0.3, high_hz=35, notch_hz=50)
    assert result["notes"] == []
    assert result["filters"]["notchHz"] == 50
    y, out_fs = signal(path.with_name("fast_preprocessed.edf"))
    assert out_fs == fs
    assert amplitude(y, fs, 50) < 0.05 * 40


def test_mixed_rates_skip_the_notch_only_where_needed(tmp_path: Path) -> None:
    t_fast = np.arange(256 * 60) / 256
    t_slow = np.arange(100 * 60) / 100
    path = tmp_path / "mixed_rates.edf"
    write_edf(
        path,
        [
            OutSignal("C3-M2", 40 * np.sin(2 * np.pi * 50 * t_fast), 256, "uV", -500, 500),
            OutSignal("E1-M2", 20 * np.sin(2 * np.pi * 1 * t_slow), 100, "uV", -500, 500),
        ],
    )
    result = api.preprocess(str(path), notch_hz=50)
    assert result["applied"] == ["notch"]
    assert [c["filtered"] for c in result["channels"]] == [True, False]
    assert result["notes"] == [
        "notch 50 Hz skipped on E1-M2: at or above half its 100 Hz sample rate"
    ]


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


def test_filtering_past_the_range_widens_it_instead_of_clipping(tmp_path: Path) -> None:
    """A low-pass rings at each step of a +-490 uV square wave, past the +-500 uV input
    range. The output range widens to hold it; nothing is cut."""
    t = np.arange(FS * 120) / FS
    square = np.where(np.sin(2 * np.pi * 0.05 * t) >= 0, 490.0, -490.0)
    small = 20 * np.sin(2 * np.pi * 10 * t)
    spo2 = 95 + 2 * np.sin(2 * np.pi * 0.1 * t)
    path = tmp_path / "steps.edf"
    write_edf(
        path,
        [
            OutSignal("E1-M2", square, FS, "uV", -500, 500),
            OutSignal("C3-M2", small, FS, "uV", -500, 500),
            OutSignal("SpO2", spo2, FS, "%", 0, 100, dig_min=0, dig_max=1000),
        ],
    )
    result = api.preprocess(str(path), high_hz=5)
    assert [c["clippedSamples"] for c in result["channels"]] == [0, 0, 0]
    out = path.with_name("steps_preprocessed.edf")
    edf = EdfFile(str(out))
    h = edf.header
    # The square wave's channel: wider than the data, full 16-bit digital range.
    x, _ = signal(path, 0)
    expected = sosfiltfilt(butter(4, 5, btype="lowpass", fs=FS, output="sos"), x)
    assert expected.max() > 500 and expected.min() < -500
    assert h.phys_min[0] < expected.min() and h.phys_max[0] > expected.max()
    assert (h.dig_min[0], h.dig_max[0]) == (-32768, 32767)
    y, _ = signal(out, 0)
    step = (h.phys_max[0] - h.phys_min[0]) / (h.dig_max[0] - h.dig_min[0])
    assert np.max(np.abs(y - expected)) <= step  # within quantisation error
    # A channel that still fits, and a copied one, keep the input's ranges.
    src = EdfFile(str(path)).header
    for i in (1, 2):
        assert (h.phys_min[i], h.phys_max[i]) == (src.phys_min[i], src.phys_max[i])
        assert (h.dig_min[i], h.dig_max[i]) == (src.dig_min[i], src.dig_max[i])
    assert (h.dig_min[2], h.dig_max[2]) == (0, 1000)
    src_edf = EdfFile(str(path))
    with edf, src_edf:
        assert np.array_equal(edf.read_signal(2), src_edf.read_signal(2))


def test_a_range_written_the_other_way_round_stays_that_way(tmp_path: Path) -> None:
    t = np.arange(FS * 120) / FS
    square = np.where(np.sin(2 * np.pi * 0.05 * t) >= 0, 490.0, -490.0)
    path = tmp_path / "flipped.edf"
    write_edf(path, [OutSignal("E1-M2", square, FS, "uV", 500, -500)])
    result = api.preprocess(str(path), high_hz=5)
    assert result["channels"][0]["clippedSamples"] == 0
    h = EdfFile(str(path.with_name("flipped_preprocessed.edf"))).header
    assert h.phys_min[0] > 500 and h.phys_max[0] < -500
