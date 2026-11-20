"""The synthetic night: deterministic, and every tool finds what was planted."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_workbench.local import api
from nexus_workbench.local.sample import make_sample_night

KEYS = ("edf", "hypnogram", "hypnogramScorer2", "eventsReference", "eventsCandidate")


def test_deterministic_with_a_seed(tmp_path: Path) -> None:
    a = make_sample_night(str(tmp_path / "a"), hours=0.5, seed=3)
    b = make_sample_night(str(tmp_path / "b"), hours=0.5, seed=3)
    c = make_sample_night(str(tmp_path / "c"), hours=0.5, seed=4)
    for k in KEYS:
        assert Path(a[k]).read_bytes() == Path(b[k]).read_bytes(), k
    assert Path(a["edf"]).read_bytes() != Path(c["edf"]).read_bytes()
    assert a["planted"] == b["planted"]


def test_default_size_is_small(night: dict[str, Any]) -> None:
    per_hour = os.path.getsize(night["edf"])
    assert per_hour * 8 < 30e6  # the default 8 hour night stays under 30 MB


def test_quality_finds_the_planted_stretches(night: dict[str, Any]) -> None:
    r = api.check_signal_quality(night["edf"])
    p = night["planted"]
    spans = {(s["channel"], s["class"]): (s["startSec"], s["endSec"]) for s in r["badSpans"]}
    assert spans == {
        (p["flat"]["channel"], "flat"): (p["flat"]["startSec"], p["flat"]["endSec"]),
        (p["railed"]["channel"], "railed"): (p["railed"]["startSec"], p["railed"]["endSec"]),
    }
    by = {c["label"]: c["counts"] for c in r["channels"]}
    assert by["C4-M1"]["flat"] == 20 and by["E1-M2"]["railed"] == 10
    assert by["C3-M2"]["ok"] == 120 and by["Chin1-Chin2"]["ok"] == 120


def _kappa(a: np.ndarray, b: np.ndarray) -> float:
    """Cohen's kappa written out directly, independent of the package."""
    keep = (a >= 0) & (b >= 0)
    a, b = a[keep], b[keep]
    n = a.size
    p_o = np.mean(a == b)
    p_e = sum(np.mean(a == k) * np.mean(b == k) for k in range(5))
    assert n > 0
    return float((p_o - p_e) / (1 - p_e))


def test_hypnogram_agreement_matches_the_planted_disagreement(night: dict[str, Any]) -> None:
    r = api.compare_hypnograms(night["hypnogram"], night["hypnogramScorer2"])
    p = night["planted"]
    n = p["epochs"] - p["unscoredEpochsB"]
    assert r["epochsCompared"] == n
    assert r["agreement"] == pytest.approx(1 - p["disagreementEpochs"] / n, abs=1e-4)
    expect = _kappa(np.array(p["stagesA"]), np.array(p["stagesB"]))
    assert r["kappa"] == pytest.approx(expect, abs=1e-4)


def test_event_scores_match_the_planted_overlap(night: dict[str, Any]) -> None:
    r = api.compare_events(night["eventsReference"], night["eventsCandidate"], recording_hours=1.0)
    e = night["planted"]["events"]
    assert (r["matched"], r["missed"], r["extra"]) == (e["matched"], e["missed"], e["extra"])
    f1 = 2 * e["matched"] / (2 * e["matched"] + e["missed"] + e["extra"])
    assert r["f1"] == pytest.approx(f1, abs=1e-4)
    assert r["falsePositivesPerHour"] == pytest.approx(e["extra"], abs=1e-3)
    assert r["shuffledNull"]["f1Mean"] < 0.5 * r["f1"]
    again = api.compare_events(
        night["eventsReference"], night["eventsCandidate"], recording_hours=1.0
    )
    assert again == r


def test_band_power_is_sensible(night: dict[str, Any]) -> None:
    r = api.compute_band_power(night["edf"], hypnogram_path=night["hypnogram"])
    by = {c["label"]: c for c in r["channels"]}
    assert set(by) == {"C3-M2", "C4-M1"}  # the EEG channels by default
    assert by["C3-M2"]["epochsUsed"] == 120
    assert by["C4-M1"]["epochsUsed"] == 100  # the 20 flat epochs are left out
    for c in by.values():
        assert sum(c["relative"]) == pytest.approx(1.0, abs=1e-3)
        st = c["byStage"]
        delta = {k: v["relative"][0] for k, v in st.items() if v["epochs"]}
        alpha = {k: v["relative"][2] for k, v in st.items() if v["epochs"]}
        # Built in: most delta in N3, most alpha in wake.
        assert max(delta, key=delta.__getitem__) == "N3"
        assert max(alpha, key=alpha.__getitem__) == "W"
        assert sum(v["epochs"] for v in st.values()) == c["epochsUsed"]
