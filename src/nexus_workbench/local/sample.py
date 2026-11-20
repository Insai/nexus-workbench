"""A fully synthetic night to try the local tools on.

Nothing here comes from a recording. The signal is seeded, band-limited noise whose band
amplitudes follow a made-up stage sequence, with spindle-like bursts in N2. A flat stretch
and a railed stretch are planted so the quality check has something to find, and the second
hypnogram and the candidate events differ from the reference by known amounts.
"""

from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
from scipy.signal import butter, sosfiltfilt

from .edf import OutSignal, write_edf
from .staging import STAGES

FS = 100
EPOCH_SEC = 30
LABELS = ("C3-M2", "C4-M1", "E1-M2", "Chin1-Chin2")
_W, _N1, _N2, _N3, _R = range(5)

#: RMS amplitude (µV) per stage (W, N1, N2, N3, R) of each band's noise in the EEG rows.
_EEG_BANDS: tuple[tuple[float, float, tuple[float, ...]], ...] = (
    (0.5, 4.0, (6.0, 10.0, 18.0, 50.0, 8.0)),
    (4.0, 8.0, (4.0, 10.0, 8.0, 8.0, 9.0)),
    (8.0, 12.0, (14.0, 4.0, 3.0, 2.0, 4.0)),
    (12.0, 16.0, (3.0, 3.0, 5.0, 2.0, 2.0)),
    (16.0, 30.0, (6.0, 3.0, 3.0, 2.0, 4.0)),
)
#: Eye movements (0.3 to 2 Hz) on the EOG row, RMS µV per stage.
_EYE = (25.0, 15.0, 3.0, 3.0, 40.0)
#: Chin EMG (10 to 45 Hz), RMS µV per stage.
_CHIN = (15.0, 8.0, 5.0, 4.0, 1.5)

FLAT_EPOCHS = 20
RAILED_EPOCHS = 10
UNSCORED_EPOCHS_B = 2
DISAGREE_FRACTION = 0.15
CANDIDATE_HIT_FRACTION = 0.8
CANDIDATE_EXTRA_FRACTION = 0.1
CANDIDATE_SHIFT_SEC = 0.2


def _stages(n: int, rng: np.random.Generator) -> np.ndarray:
    """A made-up stage sequence of ``n`` epochs with roughly 90 minute cycles."""
    seq: list[int] = [_W] * 20
    k = 0
    while len(seq) < n:
        n3 = max(0, 40 - 12 * k + int(rng.integers(-4, 5)))
        rem = min(50, 16 + 10 * k + int(rng.integers(-4, 5)))
        n2a = 30 + int(rng.integers(-5, 6))
        n2b = max(10, 180 - 6 - n2a - n3 - rem - 2)
        for st, m in ((_N1, 6), (_N2, n2a), (_N3, n3), (_N2, n2b), (_R, rem), (_W, 2)):
            seq.extend([st] * m)
        k += 1
    out = np.asarray(seq[:n], dtype=int)
    out[-min(10, n // 6) :] = _W
    return out


def _band_noise(n: int, lo: float, hi: float, rng: np.random.Generator) -> np.ndarray:
    sos = butter(4, [lo, min(hi, FS / 2 - 1)], btype="bandpass", fs=FS, output="sos")
    x = sosfiltfilt(sos, rng.standard_normal(n))
    return x / (x.std() or 1.0)


def _gain(stages: np.ndarray, per_stage: tuple[float, ...]) -> np.ndarray:
    return np.repeat(np.asarray(per_stage)[stages], EPOCH_SEC * FS)


def _spindles(stages: np.ndarray, rng: np.random.Generator) -> list[tuple[float, float]]:
    """Onsets and durations of spindle-like bursts, only in N2 epochs, at least 3 s apart."""
    out: list[tuple[float, float]] = []
    for e in np.flatnonzero(stages == _N2):
        t = e * EPOCH_SEC + 2.0
        for _ in range(int(rng.integers(0, 3))):
            t += float(rng.uniform(3.0, 9.0))
            dur = float(np.round(rng.uniform(0.5, 1.5), 2))
            if t + dur > (e + 1) * EPOCH_SEC - 1.0:
                break
            out.append((round(t, 2), dur))
            t += dur
    return out


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_sample_night(out_dir: str, hours: float = 8.0, seed: int = 0) -> dict[str, Any]:
    """Write a synthetic night into ``out_dir`` and return the file paths and what was planted.

    Files: ``sample_night.edf`` (EDF+, 4 channels at 100 Hz, about 2.9 MB per hour), a reference
    hypnogram, a second scorer's hypnogram, reference spindle events and candidate events.
    Same ``hours`` and ``seed`` give byte-identical files.
    """
    if not 0.5 <= float(hours) <= 24.0:
        raise ValueError("hours must be between 0.5 and 24")
    os.makedirs(out_dir, exist_ok=True)
    rng = np.random.default_rng(seed)
    n_ep = int(round(float(hours) * 3600 / EPOCH_SEC))
    n = n_ep * EPOCH_SEC * FS
    stages = _stages(n_ep, rng)
    t = np.arange(n) / FS

    rows: list[np.ndarray] = []
    for ch in range(2):  # C3-M2, C4-M1
        x = np.zeros(n)
        for lo, hi, amp in _EEG_BANDS:
            x += _band_noise(n, lo, hi, rng) * _gain(stages, amp)
        rows.append(x * (1.0 if ch == 0 else 0.95))
    eog = 0.4 * rows[0] + _band_noise(n, 0.3, 2.0, rng) * _gain(stages, _EYE)
    chin = _band_noise(n, 10.0, 45.0, rng) * _gain(stages, _CHIN)
    rows += [eog, chin]

    spindles = _spindles(stages, rng)
    for on, du in spindles:
        a, b = int(on * FS), int((on + du) * FS)
        burst = 30.0 * np.sin(2 * np.pi * 13.0 * t[a:b]) * np.hanning(b - a)
        rows[0][a:b] += burst
        rows[1][a:b] += 0.9 * burst

    f0 = int(round(0.25 * n_ep))
    r0 = int(round(0.6 * n_ep))
    spe = EPOCH_SEC * FS
    rows[1][f0 * spe : (f0 + FLAT_EPOCHS) * spe] = 0.0  # a dead contact
    seg = slice(r0 * spe, (r0 + RAILED_EPOCHS) * spe)
    rows[2][seg] = 3000.0 * np.sin(2 * np.pi * 1.0 * t[seg])  # clipped at the ±500 µV range

    signals = [
        OutSignal(LABELS[0], rows[0], FS, "uV", -500.0, 500.0),
        OutSignal(LABELS[1], rows[1], FS, "uV", -500.0, 500.0),
        OutSignal(LABELS[2], rows[2], FS, "uV", -500.0, 500.0),
        OutSignal(LABELS[3], rows[3], FS, "uV", -250.0, 250.0),
    ]
    dur = n_ep * EPOCH_SEC
    edf_path = os.path.join(out_dir, "sample_night.edf")
    write_edf(edf_path, signals, [(0.0, 0.0, "lights off"), (dur - 1.0, 0.0, "lights on")])

    # Reference hypnogram: "epoch,stage" with a header.
    hyp_a = os.path.join(out_dir, "sample_hypnogram.csv")
    _write_text(
        hyp_a,
        "epoch,stage\n" + "".join(f"{e},{STAGES[s]}\n" for e, s in enumerate(stages)),
    )
    # Second scorer: one stage per line, REM spelled out, the first epochs unscored.
    b = stages.copy()
    idx = np.sort(
        rng.choice(
            np.arange(UNSCORED_EPOCHS_B, n_ep),
            size=int(round(DISAGREE_FRACTION * (n_ep - UNSCORED_EPOCHS_B))),
            replace=False,
        )
    )
    neighbour = {_W: _N1, _N1: _N2, _N3: _N2, _R: _N1}
    for k, e in enumerate(idx):
        s = int(b[e])
        b[e] = (_N1 if k % 2 == 0 else _N3) if s == _N2 else neighbour[s]
    b_codes = b.copy()
    b_codes[:UNSCORED_EPOCHS_B] = -1
    names_b = {**{k: v for k, v in enumerate(STAGES)}, 4: "REM", -1: "?"}
    hyp_b = os.path.join(out_dir, "sample_hypnogram_scorer2.txt")
    _write_text(hyp_b, "".join(f"{names_b[int(s)]}\n" for s in b_codes))

    # Events: the reference is the planted spindles; the candidate finds most of them a little
    # late, misses the rest and adds some in wake, where there are none.
    ref_path = os.path.join(out_dir, "sample_events_reference.csv")
    _write_text(
        ref_path,
        "onset_sec,duration_sec,label\n"
        + "".join(f"{on:.2f},{du:.2f},spindle\n" for on, du in spindles),
    )
    n_ref = len(spindles)
    k_hit = int(round(CANDIDATE_HIT_FRACTION * n_ref))
    hits = np.sort(rng.choice(n_ref, size=k_hit, replace=False)) if n_ref else np.array([], int)
    cand = [(spindles[i][0] + CANDIDATE_SHIFT_SEC, spindles[i][1]) for i in hits]
    wake = np.flatnonzero(stages == _W)
    n_extra = int(round(CANDIDATE_EXTRA_FRACTION * n_ref))
    extra_ep = np.sort(rng.choice(wake, size=min(n_extra, wake.size), replace=False))
    cand += [(float(e * EPOCH_SEC + 10.0), 1.0) for e in extra_ep]
    cand.sort()
    cand_path = os.path.join(out_dir, "sample_events_candidate.json")
    _write_text(
        cand_path,
        json.dumps(
            [
                {"onset_sec": round(on, 2), "duration_sec": round(du, 2), "label": "spindle"}
                for on, du in cand
            ],
            indent=1,
        )
        + "\n",
    )
    return {
        "edf": edf_path,
        "hypnogram": hyp_a,
        "hypnogramScorer2": hyp_b,
        "eventsReference": ref_path,
        "eventsCandidate": cand_path,
        "planted": {
            "hours": float(hours),
            "seed": seed,
            "epochs": n_ep,
            "flat": {
                "channel": LABELS[1],
                "startSec": f0 * EPOCH_SEC,
                "endSec": (f0 + FLAT_EPOCHS) * EPOCH_SEC,
            },
            "railed": {
                "channel": LABELS[2],
                "startSec": r0 * EPOCH_SEC,
                "endSec": (r0 + RAILED_EPOCHS) * EPOCH_SEC,
            },
            "stagesA": stages.tolist(),
            "stagesB": b_codes.tolist(),
            "disagreementEpochs": int(idx.size),
            "unscoredEpochsB": UNSCORED_EPOCHS_B,
            "events": {
                "reference": n_ref,
                "matched": k_hit,
                "missed": n_ref - k_hit,
                "extra": int(extra_ep.size),
            },
        },
    }
