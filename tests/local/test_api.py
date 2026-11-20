"""The five API functions: JSON safety, privacy, size caps and errors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_workbench.local import api
from nexus_workbench.local.errors import LocalToolError
from nexus_workbench.local.labels import guess_type, safe_label

from .conftest import write_test_edf

FAKE = ("DOE", "JANE", "1971", "02.03.71", "13.14.15", "CLINIC", "SMITH")


def _dump(r: dict[str, Any]) -> str:
    return json.dumps(r, allow_nan=False)


def _all_outputs(night: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        api.inspect_edf(night["edf"]),
        api.check_signal_quality(night["edf"]),
        api.compute_band_power(night["edf"], hypnogram_path=night["hypnogram"]),
        api.compare_hypnograms(night["hypnogram"], night["hypnogramScorer2"]),
        api.compare_events(night["eventsReference"], night["eventsCandidate"], recording_hours=1.0),
    ]


def test_every_output_is_strict_json_and_names_no_path(night: dict[str, Any]) -> None:
    folder = str(Path(night["edf"]).parent)
    for r in _all_outputs(night):
        assert isinstance(r, dict)
        text = _dump(r)
        assert json.loads(text) == r  # only plain JSON types
        assert folder not in text
        assert "sample_" not in text and ".edf" not in text and ".csv" not in text
        assert "/" not in text.replace("uV^2", "")  # no path separators at all


def test_identifying_header_fields_never_reach_any_output_or_error(tmp_path: Path) -> None:
    rng = np.random.default_rng(0)
    rows = [rng.normal(0, 20, 100 * 300) for _ in range(3)]
    p = write_test_edf(
        tmp_path / "id.edf",
        rows,
        100,
        ["C3-M2", "SMITH JANE", "EOG DOE"],
        annotations=[(5.0, 0.0, "DOE^JANE said hello")],
        patient="DOE^JANE F 01-JAN-1970 DOE^JANE",
        recording="Startdate 02-MAR-1971 DOE^JANE CLINIC^X",
        start_date="02.03.71",
        start_time="13.14.15",
    )
    hyp = tmp_path / "h.txt"
    hyp.write_text("N2\n" * 10)
    outs = [
        api.inspect_edf(str(p)),
        api.check_signal_quality(str(p)),
        api.compute_band_power(str(p), hypnogram_path=str(hyp)),
        api.compute_band_power(str(p), channels=[2, "ch3", "C3-M2"]),
    ]
    errors = []
    for bad in (
        lambda: api.compute_band_power(str(p), channels=["F7"]),
        lambda: api.check_signal_quality(str(p), epoch_sec=7.005),
        lambda: api.compute_band_power(str(p), hypnogram_path=str(tmp_path / "h.csv")),
    ):
        with pytest.raises(LocalToolError) as exc:
            bad()
        errors.append(str(exc.value))
    truncated = tmp_path / "cut.edf"
    truncated.write_bytes(p.read_bytes()[:-500])
    with pytest.raises(LocalToolError) as exc:
        api.inspect_edf(str(truncated))
    errors.append(str(exc.value))
    text = " ".join([_dump(o) for o in outs] + errors)
    for s in FAKE:
        assert s not in text, s
    labels = [c["label"] for c in outs[0]["channels"]]
    assert labels == ["C3-M2", "ch2", "ch3"]
    assert [c["type"] for c in outs[0]["channels"]] == ["eeg", "other", "eog"]
    assert outs[0]["annotations"] == {"present": True, "count": 1}


def test_labels() -> None:
    passes = ["C3-M2", "EEG Fpz-Cz", "Fp1", "E1-M2", "EOG(L)", "Chin1-Chin2", "ECG", "EKG",
              "SpO2", "Thorax", "Position", "O2-A1", "EMG submental", "LOC", "AF7-AF8"]  # fmt: skip
    for k, lab in enumerate(passes, start=1):
        assert safe_label(lab, k) == lab, lab
    for k, lab in enumerate(["Jane", "C3-Jane", "Bed 4 left", "X-Y-Z", "C3--", "Fp11"], start=1):
        assert safe_label(lab, k) == f"ch{k}", lab
    assert guess_type("EEG Fpz-Cz") == "eeg"
    assert guess_type("E2-M1") == "eog"
    assert guess_type("Chin1-Chin2") == "emg"
    assert guess_type("EKG") == "ecg"
    assert guess_type("Thor") == "other"


def test_inspect_shape(night: dict[str, Any]) -> None:
    r = api.inspect_edf(night["edf"])
    assert r["format"] == "EDF+C"
    assert r["durationSec"] == 3600.0
    assert r["nChannels"] == 4
    assert len(r["recordingId"]) == 12 and int(r["recordingId"], 16) >= 0
    assert r["channels"][0] == {
        "index": 1,
        "label": "C3-M2",
        "type": "eeg",
        "sampleRateHz": 100.0,
        "unit": "uV",
        "physicalMin": -500.0,
        "physicalMax": 500.0,
        "sampleCount": 360000,
    }


def test_same_input_same_output(night: dict[str, Any]) -> None:
    assert _all_outputs(night) == _all_outputs(night)


@pytest.fixture(scope="module")
def long_file(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """12 hours, 32 channels (at 8 Hz to keep the file small), with many bad stretches."""
    d = tmp_path_factory.mktemp("long")
    fs, hours, n_ch = 8, 12, 32
    n = fs * 3600 * hours
    rng = np.random.default_rng(0)
    sites = ["Fp1", "Fp2", "F3", "F4", "C3", "C4", "P3", "P4", "O1", "O2", "F7", "F8", "T3",
             "T4", "T5", "T6", "Fz", "Cz", "Pz", "Oz", "FC1", "FC2", "CP1", "CP2", "FC5",
             "FC6", "CP5", "CP6", "AF3", "AF4", "PO3", "PO4"]  # fmt: skip
    rows = []
    for _ in range(n_ch):
        x = rng.normal(0, 20, n).astype(np.float64)
        for start in rng.integers(0, 1400, 6):
            x[start * 240 : (start + 3) * 240] = 0.0  # 3 flat epochs
        rows.append(x)
    p = write_test_edf(d / "long.edf", rows, fs, [f"{s}-M1" for s in sites])
    hyp = d / "long_hyp.txt"
    hyp.write_text("".join(["W\n", "N1\n", "N2\n", "N3\n", "R\n"][i % 5] for i in range(1440)))
    return str(p), str(hyp)


def test_outputs_stay_small_for_a_long_wide_file(long_file: tuple[str, str]) -> None:
    edf, hyp = long_file
    low = {"b1": [0.5, 1], "b2": [1, 1.5], "b3": [1.5, 2], "b4": [2, 3], "b5": [3, 4]}
    outs = {
        "inspect": api.inspect_edf(edf),
        "quality": api.check_signal_quality(edf),
        "power": api.compute_band_power(edf, hypnogram_path=hyp, bands=low),
    }
    q = outs["quality"]
    assert q["badSpansTotal"] > 20 and len(q["badSpans"]) == 20 and q["truncated"] is True
    assert len(outs["power"]["channels"]) == 32
    for name, r in outs.items():
        size = len(_dump(r))
        # About 4 characters per token: 15,000 tokens is about 60,000 characters.
        assert size < 40_000, (name, size)


def test_errors_are_for_people(night: dict[str, Any], tmp_path: Path) -> None:
    cases = [
        (lambda: api.inspect_edf(str(tmp_path / "nope.edf")), "was not found"),
        (lambda: api.inspect_edf(str(tmp_path)), "is a folder"),
        (lambda: api.inspect_edf(night["hypnogram"]), "not a valid EDF"),
        (lambda: api.check_signal_quality(night["edf"], epoch_sec=0), "between 1 and 3600"),
        (lambda: api.check_signal_quality(night["edf"], epoch_sec="x"), "number of seconds"),
        (lambda: api.check_signal_quality(night["edf"], channels=[9]), "no data channel"),
        (lambda: api.compute_band_power(night["edf"], bands={"x": [5, 1]}), "low < high"),
        (
            lambda: api.compare_events(
                night["eventsReference"], night["eventsCandidate"], recording_hours=0.1
            ),
            "after the end",
        ),
        (
            lambda: api.compare_events(
                night["eventsReference"], night["eventsCandidate"], n_shuffles=-1
            ),
            "n_shuffles",
        ),
    ]
    for fn, match in cases:
        with pytest.raises(LocalToolError, match=match):
            fn()


def test_hypnogram_length_mismatch(night: dict[str, Any], tmp_path: Path) -> None:
    short = tmp_path / "short.txt"
    short.write_text("W\n" * 100)
    with pytest.raises(LocalToolError, match="differ in length"):
        api.compare_hypnograms(night["hypnogram"], str(short))
    with pytest.raises(LocalToolError, match="hypnogram has 100 epochs"):
        api.compute_band_power(night["edf"], hypnogram_path=str(short))


def test_text_inputs_are_capped(night: dict[str, Any], tmp_path: Path) -> None:
    big = tmp_path / "big.csv"
    big.write_bytes(b"W\n" * (5 * 1024 * 1024 + 10))
    with pytest.raises(LocalToolError, match="limited to 10 MB"):
        api.compare_hypnograms(str(big), str(big))
    binary = tmp_path / "bin.csv"
    binary.write_bytes(b"\x00\x01\x02" * 100)
    with pytest.raises(LocalToolError, match="not a text file"):
        api.compare_events(str(binary), night["eventsCandidate"])


def test_label_filter_and_shuffle_reduction(night: dict[str, Any]) -> None:
    r = api.compare_events(night["eventsReference"], night["eventsCandidate"], label="nothing")
    assert r["reference"] == 0 and r["candidate"] == 0 and r["f1"] is None
    r = api.compare_events(night["eventsReference"], night["eventsCandidate"], n_shuffles=2000)
    assert r["shuffledNull"]["nShuffles"] <= 2000
