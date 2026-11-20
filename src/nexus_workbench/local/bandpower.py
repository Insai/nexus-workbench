"""Absolute and relative EEG band power per epoch, by Welch's method.

Each epoch's power spectral density is estimated with Welch's method (Hann window of
:data:`WINDOW_SEC` seconds, half overlap, mean removed per segment). Band power is the PSD
summed over the frequency bins in ``[low, high)`` times the bin width, so it is in the
signal's unit squared (µV² for EEG). Relative power is a band's power divided by the power
from the lowest band edge to the highest; with the default, contiguous bands the relative
powers sum to 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.signal import welch

from .errors import LocalToolError

#: Default bands as (name, low Hz, high Hz), each ``[low, high)``.
DEFAULT_BANDS: tuple[tuple[str, float, float], ...] = (
    ("delta", 0.5, 4.0),
    ("theta", 4.0, 8.0),
    ("alpha", 8.0, 12.0),
    ("sigma", 12.0, 16.0),
    ("beta", 16.0, 30.0),
)
#: Welch segment length in seconds (shortened to the epoch when the epoch is shorter).
WINDOW_SEC = 4.0
MAX_BANDS = 12

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,23}$")


@dataclass(frozen=True)
class Band:
    name: str
    low: float
    high: float


def parse_bands(bands: object) -> list[Band]:
    """Bands from ``None`` (the defaults), a mapping ``{name: [low, high]}`` or a list of
    ``{"name", "lowHz", "highHz"}`` objects (``[name, low, high]`` triples also work)."""
    if bands is None:
        return [Band(n, lo, hi) for n, lo, hi in DEFAULT_BANDS]
    items: list[tuple[object, object, object]] = []
    if isinstance(bands, Mapping):
        for k, v in bands.items():
            if not isinstance(v, Sequence) or isinstance(v, str) or len(v) != 2:
                raise LocalToolError(
                    f"Band {k!s:.24} must be given as [low Hz, high Hz], for example [0.5, 4]."
                )
            items.append((k, v[0], v[1]))
    elif isinstance(bands, Sequence) and not isinstance(bands, str):
        for b in bands:
            if isinstance(b, Mapping):
                items.append(
                    (b.get("name"), b.get("lowHz", b.get("low")), b.get("highHz", b.get("high")))
                )
            elif isinstance(b, Sequence) and not isinstance(b, str) and len(b) == 3:
                items.append((b[0], b[1], b[2]))
            else:
                raise LocalToolError(
                    'Each band must be {"name": ..., "lowHz": ..., "highHz": ...}.'
                )
    else:
        raise LocalToolError('Bands must be a mapping like {"delta": [0.5, 4], "theta": [4, 8]}.')
    if not items:
        raise LocalToolError("No bands were given. Leave bands out to use the defaults.")
    if len(items) > MAX_BANDS:
        raise LocalToolError(f"At most {MAX_BANDS} bands can be computed in one call.")
    out: list[Band] = []
    for name, lo, hi in items:
        if not isinstance(name, str) or not _NAME.match(name):
            raise LocalToolError(
                "Band names must start with a letter and use only letters, digits and "
                "underscores (at most 24 characters)."
            )
        try:
            lo_f, hi_f = float(lo), float(hi)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise LocalToolError(f"Band {name} needs numeric edges in Hz.") from None
        if not (np.isfinite(lo_f) and np.isfinite(hi_f) and 0.0 <= lo_f < hi_f):
            raise LocalToolError(
                f"Band {name} must have 0 <= low < high (got {lo_f:g} to {hi_f:g} Hz)."
            )
        out.append(Band(name, lo_f, hi_f))
    if len({b.name for b in out}) != len(out):
        raise LocalToolError("Band names must be unique.")
    return out


def epoch_band_powers(
    x: np.ndarray, fs: float, bands: Sequence[Band]
) -> tuple[np.ndarray, np.ndarray]:
    """Band powers for each row of ``x`` (shape ``(n_epochs, samples)``).

    Returns ``(absolute, total)``: ``absolute`` has shape ``(n_epochs, n_bands)`` with NaN for a
    band that starts at or above the Nyquist frequency; ``total`` is the power from the lowest
    band edge to the highest, shape ``(n_epochs,)``.
    """
    x = np.asarray(x, dtype=np.float64)
    spe = x.shape[-1]
    nperseg = max(2, min(spe, int(round(WINDOW_SEC * fs))))
    f, p = welch(
        x,
        fs=fs,
        window="hann",
        nperseg=nperseg,
        noverlap=nperseg // 2,
        detrend="constant",
        scaling="density",
        axis=-1,
    )
    df = float(f[1] - f[0])
    nyq = fs / 2.0
    absolute = np.full((x.shape[0], len(bands)), np.nan)
    for k, b in enumerate(bands):
        if b.low >= nyq:
            continue
        m = (f >= b.low) & (f < b.high)
        absolute[:, k] = p[:, m].sum(axis=1) * df
    lo = min(b.low for b in bands)
    hi = max(b.high for b in bands)
    m = (f >= lo) & (f < hi)
    total = p[:, m].sum(axis=1) * df
    return absolute, total


class BandAccumulator:
    """Running sums of per-epoch band powers, for a mean over epochs."""

    def __init__(self, n_bands: int) -> None:
        self.n = 0
        self.sum_abs = np.zeros(n_bands)
        self.sum_total = 0.0

    def add(self, absolute: np.ndarray, total: np.ndarray) -> None:
        if absolute.shape[0] == 0:
            return
        self.n += int(absolute.shape[0])
        self.sum_abs += absolute.sum(axis=0)
        self.sum_total += float(total.sum())

    def mean_absolute(self) -> np.ndarray:
        if self.n == 0:
            return np.full(self.sum_abs.shape, np.nan)
        return self.sum_abs / self.n

    def relative(self) -> np.ndarray:
        """Mean band power over mean total power (a ratio of means)."""
        if self.n == 0 or self.sum_total <= 0:
            return np.full(self.sum_abs.shape, np.nan)
        return self.sum_abs / self.sum_total
