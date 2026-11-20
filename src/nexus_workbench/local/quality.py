"""Per-epoch, per-channel signal quality: ``ok``, ``suspect``, ``flat`` or ``railed``.

All thresholds are in microvolts and are applied at the file's own sample rate (resampling
would hide constant runs). They are default thresholds, not tuned for any device.

Per epoch, worst class wins:

* ``railed``: peak-to-peak above :data:`SATURATED_PTP_UV` (EEG and EOG only); or a quarter or
  more of the samples sit on the channel's own minimum or maximum (within :data:`EXTREME_TOL` of
  its range); or the epoch is flat, or holds a constant run of :data:`CONSTANT_RUN_SEC` or more,
  at a rail level (at least :data:`RAIL_LEVEL_UV` from zero, or at the file's digital limit).
* ``flat``: standard deviation below :data:`FLAT_STD_UV` at a level that is not a rail (a dead or
  zero-filled contact).
* ``suspect``: a constant run of :data:`CONSTANT_RUN_SEC` or more; or more than
  :data:`SUSPECT_EXTREME_FRAC` of samples on the channel's extremes; or RMS about the epoch
  mean (the standard deviation, so a DC offset does not count) above :data:`SUSPECT_RMS_UV`
  (EEG and EOG only).
* ``ok``: none of the above.

The two amplitude rules (peak-to-peak and RMS) are set for EEG and EOG. They are not applied
to ECG, where R peaks reach several millivolts, nor to EMG, where chin and leg bursts are
large by nature, nor to channels whose type cannot be told from the label. Flat, rail,
constant-run and extremes rules apply to every channel.

Per channel, an overall class for the whole recording: ``railed`` (railed throughout),
``flat`` (flat throughout), ``suspect`` (more than :data:`SUSPECT_BAD_EPOCH_FRAC` of epochs
classed anything but ``ok``, or more than :data:`SUSPECT_EXTREME_FRAC` of all samples on the
extremes) or ``ok``. So a channel is never ``ok`` overall when none of its epochs is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

EPOCH_SEC = 30.0
#: An epoch with a standard deviation below this is flat (µV).
FLAT_STD_UV = 0.5
#: An epoch with a peak-to-peak range above this is saturated (µV).
SATURATED_PTP_UV = 2000.0
#: A run of identical samples at least this long marks the epoch (seconds).
CONSTANT_RUN_SEC = 1.0
#: "On the extremes" means within this fraction of the channel's range of its min or max.
EXTREME_TOL = 1e-3
#: More than this share of samples on the extremes is suspect.
SUSPECT_EXTREME_FRAC = 0.01
#: At least this share of an epoch's samples on the extremes means the epoch is clipped.
RAILED_EXTREME_FRAC = 0.25
#: A flat stretch or constant run held at least this far from 0 µV is a rail, not a dead contact.
RAIL_LEVEL_UV = 1000.0
#: An epoch with RMS about its mean (its standard deviation) above this is suspect (µV).
SUSPECT_RMS_UV = 250.0
#: Channel types the amplitude rules (peak-to-peak and RMS) apply to.
AMPLITUDE_RULE_TYPES = ("eeg", "eog")
#: A channel with more than this share of bad epochs is suspect overall.
SUSPECT_BAD_EPOCH_FRAC = 0.10

CLASSES = ("ok", "suspect", "flat", "railed")
OK, SUSPECT, FLAT, RAILED = 0, 1, 2, 3

THRESHOLDS: dict[str, float] = {
    "flatStdUv": FLAT_STD_UV,
    "saturatedPeakToPeakUv": SATURATED_PTP_UV,
    "constantRunSec": CONSTANT_RUN_SEC,
    "extremeTolerance": EXTREME_TOL,
    "suspectExtremeFraction": SUSPECT_EXTREME_FRAC,
    "railedExtremeFraction": RAILED_EXTREME_FRAC,
    "railLevelUv": RAIL_LEVEL_UV,
    "suspectRmsUv": SUSPECT_RMS_UV,
    "suspectBadEpochFraction": SUSPECT_BAD_EPOCH_FRAC,
}


def amplitude_rules_apply(channel_type: str) -> bool:
    """Whether the peak-to-peak and RMS rules apply to a channel of this type."""
    return channel_type in AMPLITUDE_RULE_TYPES


def _longest_runs(x: np.ndarray, at_limit: np.ndarray | None, thr: int) -> tuple[np.ndarray, ...]:
    """Per epoch (row of ``x``): longest run of identical samples, whether any run of ``thr`` or
    more samples sits at a rail level."""
    n_ep, spe = x.shape
    flat = x.reshape(-1)
    change = np.empty(flat.size, dtype=bool)
    change[0] = True
    change[1:] = flat[1:] != flat[:-1]
    change[::spe] = True  # runs never cross an epoch boundary
    starts = np.flatnonzero(change)
    lengths = np.diff(np.append(starts, flat.size))
    ep = starts // spe
    first = np.searchsorted(ep, np.arange(n_ep))
    max_run = np.maximum.reduceat(lengths, first)
    rail_run = np.zeros(n_ep, dtype=bool)
    long = lengths >= thr
    if long.any():
        vals = flat[starts[long]]
        rail = np.abs(vals) >= RAIL_LEVEL_UV
        if at_limit is not None:
            rail |= at_limit.reshape(-1)[starts[long]]
        rail_run[ep[long][rail]] = True
    return max_run, rail_run


@dataclass
class EpochEvidence:
    """Per-epoch evidence for a block of epochs of one channel."""

    classes: np.ndarray
    saturated: np.ndarray
    flat: np.ndarray
    constant_run: np.ndarray
    extreme_count: np.ndarray
    level: np.ndarray


def epoch_evidence(
    x_uv: np.ndarray,
    fs: float,
    lo: float,
    hi: float,
    at_limit: np.ndarray | None = None,
    *,
    amplitude_rules: bool = True,
) -> EpochEvidence:
    """Classify each row of ``x_uv`` (shape ``(n_epochs, samples_per_epoch)``, µV).

    ``lo`` and ``hi`` are the channel's minimum and maximum over the whole recording.
    ``at_limit`` marks samples at the file's digital minimum or maximum, when known.
    ``amplitude_rules`` turns the peak-to-peak and RMS rules on (EEG and EOG) or off.
    """
    x = np.asarray(x_uv, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] == 0:
        raise ValueError("x_uv must be 2-D (epochs, samples)")
    spe = x.shape[1]
    std = x.std(axis=1)
    ptp = x.max(axis=1) - x.min(axis=1)
    level = x.mean(axis=1)
    thr = max(1, int(round(CONSTANT_RUN_SEC * fs)))
    max_run, rail_run = _longest_runs(x, at_limit, thr)
    if hi > lo:
        tol = EXTREME_TOL * (hi - lo)
        on_ext = (np.abs(x - lo) <= tol) | (np.abs(x - hi) <= tol)
        ext_count = on_ext.sum(axis=1)
    else:
        ext_count = np.zeros(x.shape[0], dtype=np.int64)
    ext_frac = ext_count / spe

    sat = ptp > SATURATED_PTP_UV if amplitude_rules else np.zeros(x.shape[0], dtype=bool)
    loud = std > SUSPECT_RMS_UV if amplitude_rules else np.zeros(x.shape[0], dtype=bool)
    is_flat = std < FLAT_STD_UV
    run = max_run >= thr
    all_at_limit = at_limit.all(axis=1) if at_limit is not None else np.zeros_like(is_flat)
    flat_rail = is_flat & ((np.abs(level) >= RAIL_LEVEL_UV) | all_at_limit)

    cls = np.full(x.shape[0], OK, dtype=np.int8)
    cls[run | (ext_frac > SUSPECT_EXTREME_FRAC) | loud] = SUSPECT
    cls[is_flat & ~flat_rail] = FLAT
    cls[sat | flat_rail | rail_run | (ext_frac >= RAILED_EXTREME_FRAC)] = RAILED
    return EpochEvidence(
        classes=cls,
        saturated=sat,
        flat=is_flat,
        constant_run=run,
        extreme_count=ext_count.astype(np.int64),
        level=level,
    )


@dataclass
class ChannelQuality:
    """Evidence for one channel, accumulated over blocks of epochs."""

    samples_per_epoch: int
    parts: list[EpochEvidence] = field(default_factory=list)
    lo: float = 0.0
    hi: float = 0.0

    def add(self, ev: EpochEvidence) -> None:
        self.parts.append(ev)

    def _cat(self, name: str) -> np.ndarray:
        if not self.parts:
            return np.zeros(0)
        return np.concatenate([getattr(p, name) for p in self.parts])

    @property
    def classes(self) -> np.ndarray:
        return self._cat("classes").astype(np.int8)

    @property
    def n_epochs(self) -> int:
        return int(sum(p.classes.size for p in self.parts))

    def counts(self) -> dict[str, int]:
        c = np.bincount(self.classes, minlength=4) if self.n_epochs else np.zeros(4, int)
        return {name: int(c[k]) for k, name in enumerate(CLASSES)}

    def overall(self) -> str | None:
        """Overall class for the whole recording; ``None`` with no whole epoch to judge."""
        n = self.n_epochs
        if n == 0:
            return None
        sat = self._cat("saturated").astype(bool)
        flat = self._cat("flat").astype(bool)
        run = self._cat("constant_run").astype(bool)
        ext = self._cat("extreme_count")
        ext_ep = ext / self.samples_per_epoch
        all_flat = bool(flat.all())
        level = abs(float(np.median(self._cat("level"))))
        has_range = self.hi > self.lo
        ext_total = float(ext.sum()) / (n * self.samples_per_epoch) if has_range else 1.0
        if bool((sat | run).all()) and not all_flat:
            return "railed"
        if all_flat and level >= RAIL_LEVEL_UV:
            return "railed"
        if self.hi > self.lo and bool((ext_ep >= RAILED_EXTREME_FRAC).all()):
            return "railed"
        if all_flat:
            return "flat"
        not_ok = self.classes != OK
        if ext_total > SUSPECT_EXTREME_FRAC or not_ok.sum() / n > SUSPECT_BAD_EPOCH_FRAC:
            return "suspect"
        return "ok"

    def bad_spans(self, epoch_sec: float) -> list[tuple[float, float, str]]:
        """Runs of consecutive epochs with the same non-ok class, as (start s, end s, class)."""
        cls = self.classes
        out: list[tuple[float, float, str]] = []
        i = 0
        n = cls.size
        while i < n:
            c = int(cls[i])
            j = i + 1
            while j < n and int(cls[j]) == c:
                j += 1
            if c != OK:
                out.append((i * epoch_sec, j * epoch_sec, CLASSES[c]))
            i = j
        return out


def assess(
    x_uv: np.ndarray, fs: float, epoch_sec: float = EPOCH_SEC, *, amplitude_rules: bool = True
) -> dict[str, Any]:
    """Quality of a whole 1-D signal in µV held in memory (for tests and small inputs).

    Returns the per-epoch classes, counts and the overall class (``None`` when the signal is
    shorter than one epoch or has no finite sample).
    """
    x = np.asarray(x_uv, dtype=np.float64)
    spe = int(round(epoch_sec * fs))
    finite = x[np.isfinite(x)]
    n = x.size // spe if spe > 0 else 0
    cq = ChannelQuality(samples_per_epoch=max(spe, 1))
    if n == 0 or finite.size == 0:
        return {"overall": None, "classes": [], "counts": cq.counts()}
    cq.lo, cq.hi = float(finite.min()), float(finite.max())
    ep = np.where(np.isfinite(x[: n * spe]), x[: n * spe], 0.0).reshape(n, spe)
    cq.add(epoch_evidence(ep, fs, cq.lo, cq.hi, amplitude_rules=amplitude_rules))
    ev = cq.parts[0]
    return {
        "overall": cq.overall(),
        "classes": [CLASSES[int(c)] for c in cq.classes],
        "counts": cq.counts(),
        "n_saturated": int(ev.saturated.sum()),
        "extreme_fraction": float(ev.extreme_count.sum()) / (n * spe),
    }
