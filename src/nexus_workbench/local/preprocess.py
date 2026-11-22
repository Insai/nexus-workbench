"""``preprocess``: filter, notch, resample and pick channels of a local EDF into a new EDF.

The input is never changed or replaced. The output is a new EDF+C file. Filters apply to
voltage channels only (units V, mV, uV or nV); other channels are copied unchanged. The
output's patient and recording fields hold the EDF+ "unknown" placeholders and its start
date and time are 01.01.85 00.00.00, because this package never reads the input's. EDF+
annotations are not copied.
"""

from __future__ import annotations

import contextlib
import math
import os
from fractions import Fraction
from typing import Any

import numpy as np
from scipy.signal import butter, iirnotch, resample_poly, sosfiltfilt, tf2sos

from .edf import OutSignal, write_edf
from .errors import LocalToolError
from .labels import guess_type, safe_label, to_microvolts
from .report import content_id, display_name, json_safe, rnd

FILTER_ORDER = 4
NOTCH_Q = 30.0
NOTCH_CHOICES = (50.0, 60.0)
MIN_RESAMPLE_HZ = 1.0
MAX_RESAMPLE_HZ = 4096.0
#: Largest up or down factor of a resample, so a strange ratio cannot run for minutes.
MAX_RESAMPLE_FACTOR = 1000
#: Samples written in all, over every channel, so one call stays within a few GB of memory.
MAX_OUTPUT_SAMPLES = 150_000_000
DEFAULT_SUFFIX = "_preprocessed.edf"
METHOD = "Butterworth, order 4, run forward and backward (zero phase)"


def _hz(value: object, name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        value = None
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise LocalToolError(f"{name} must be a number of hertz, or left out.") from None
    if not math.isfinite(v) or v <= 0:
        raise LocalToolError(f"{name} must be more than 0 Hz.")
    return v


def _output_path(path: str, src: str, out: object) -> str:
    """Where to write: ``out``, or ``<name>_preprocessed.edf`` next to the input. Never the
    input, never a file that exists."""
    if out is None:
        stem, _ = os.path.splitext(src)
        target = stem + DEFAULT_SUFFIX
    elif isinstance(out, str) and out.strip():
        target = os.path.expanduser(out.strip())
        if os.path.isdir(target):
            stem, _ = os.path.splitext(os.path.basename(src))
            target = os.path.join(target, stem + DEFAULT_SUFFIX)
    else:
        raise LocalToolError("out must be a path to a new .edf file, or left out.")
    name = display_name(target)
    if not target.lower().endswith(".edf"):
        raise LocalToolError(f"{name} must end in .edf.")
    if os.path.realpath(target) == os.path.realpath(src):
        raise LocalToolError("out is the input file. preprocess never overwrites its input.")
    if os.path.lexists(target):
        raise LocalToolError(
            f"{name} already exists. preprocess never replaces a file; pass another out, or "
            "move that file first."
        )
    parent = os.path.dirname(os.path.abspath(target))
    if not os.path.isdir(parent):
        raise LocalToolError(f"The folder for {name} does not exist. Create it first.")
    return target


def _ratio(fs: float, target: float) -> tuple[int, int]:
    frac = (Fraction(target) / Fraction(fs)).limit_denominator(MAX_RESAMPLE_FACTOR)
    return frac.numerator, frac.denominator


def preprocess(
    path: str,
    low_hz: float | None = None,
    high_hz: float | None = None,
    notch_hz: float | None = None,
    resample_hz: float | None = None,
    channels: list[str | int] | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """Write a filtered copy of an EDF file and describe what was done."""
    from .api import _need_continuous, _open_edf, _select  # noqa: PLC0415 (shared helpers)

    low = _hz(low_hz, "low_hz")
    high = _hz(high_hz, "high_hz")
    if low is not None and high is not None and low >= high:
        raise LocalToolError("low_hz must be below high_hz.")
    notch = _hz(notch_hz, "notch_hz")
    if notch is not None and notch not in NOTCH_CHOICES:
        raise LocalToolError("notch_hz must be 50 or 60 (the mains frequency), or left out.")
    rate = _hz(resample_hz, "resample_hz")
    if rate is not None and (rate != int(rate) or not MIN_RESAMPLE_HZ <= rate <= MAX_RESAMPLE_HZ):
        raise LocalToolError(
            f"resample_hz must be a whole number from {MIN_RESAMPLE_HZ:g} to "
            f"{MAX_RESAMPLE_HZ:g} Hz."
        )
    if rate is not None and high is not None and high >= rate / 2:
        raise LocalToolError(
            f"high_hz must be below half of resample_hz ({rate / 2:g} Hz), or nothing above "
            "it survives the resample."
        )
    if low is None and high is None and notch is None and rate is None and channels is None:
        raise LocalToolError(
            "Nothing to do. Give low_hz, high_hz, notch_hz, resample_hz or channels."
        )

    edf = _open_edf(path)
    _need_continuous(edf)
    h = edf.header
    idx = h.signal_indices if channels is None else _select(h, channels)
    if not idx:
        raise LocalToolError(f"{edf.name} has no data channels, only annotations.")
    if rate is not None and abs(rate * h.record_sec - round(rate * h.record_sec)) > 1e-9:
        raise LocalToolError(
            f"resample_hz must give a whole number of samples per {h.record_sec:g} s data "
            "record. Choose another rate."
        )

    plan = []
    total = 0
    for i in idx:
        fs = h.fs(i)
        voltage = to_microvolts(h.units[i]) is not None
        nyq = fs / 2
        label = safe_label(h.labels[i], i + 1)
        if voltage:
            for name, f in (("low_hz", low), ("high_hz", high), ("notch_hz", notch)):
                if f is not None and f >= nyq:
                    raise LocalToolError(
                        f"{name} ({f:g} Hz) is not below half the sample rate of {label} "
                        f"({fs:g} Hz, so {nyq:g} Hz). Choose a lower value or other channels."
                    )
        out_fs = rate if (voltage and rate is not None) else fs
        if voltage and rate is not None:
            up, down = _ratio(fs, rate)
            if abs(fs * up / down - rate) > 1e-6:
                raise LocalToolError(
                    f"{label} at {fs:g} Hz cannot be resampled to {rate:g} Hz exactly."
                )
        n_out = int(round(h.n_records * h.samples_per_record[i] * out_fs / fs))
        total += n_out
        plan.append((i, fs, out_fs, voltage, label))
    if total > MAX_OUTPUT_SAMPLES:
        raise LocalToolError(
            f"The output would hold {total:,} samples, more than this tool writes in one go "
            f"({MAX_OUTPUT_SAMPLES:,}). Choose fewer channels or a lower resample_hz."
        )
    target = _output_path(path, edf.path, out)

    signals: list[OutSignal] = []
    rows: list[dict[str, Any]] = []
    with edf:
        for i, fs, out_fs, voltage, label in plan:
            x = h.to_physical(i, edf.read_signal(i))
            filtered = False
            if voltage and (low is not None or high is not None):
                if low is not None and high is not None:
                    sos = butter(FILTER_ORDER, [low, high], btype="bandpass", fs=fs, output="sos")
                elif low is not None:
                    sos = butter(FILTER_ORDER, low, btype="highpass", fs=fs, output="sos")
                else:
                    sos = butter(FILTER_ORDER, high, btype="lowpass", fs=fs, output="sos")
                x = _filt(sos, x, label)
                filtered = True
            if voltage and notch is not None:
                b, a = iirnotch(notch, NOTCH_Q, fs=fs)
                x = _filt(tf2sos(b, a), x, label)
                filtered = True
            if voltage and rate is not None and out_fs != fs:
                up, down = _ratio(fs, rate)
                x = resample_poly(x, up, down)
            lo, hi = min(h.phys_min[i], h.phys_max[i]), max(h.phys_min[i], h.phys_max[i])
            clipped = int(np.count_nonzero((x < lo) | (x > hi)))
            signals.append(
                OutSignal(
                    label=h.labels[i].strip() or f"ch{i + 1}",
                    data=x.astype(np.float32),
                    fs=out_fs,
                    unit=h.units[i].strip(),
                    phys_min=h.phys_min[i],
                    phys_max=h.phys_max[i],
                    dig_min=h.dig_min[i],
                    dig_max=h.dig_max[i],
                )
            )
            rows.append(
                {
                    "index": i + 1,
                    "label": label,
                    "type": guess_type(h.labels[i]),
                    "sampleRateHz": rnd(fs, 6),
                    "outputSampleRateHz": rnd(out_fs, 6),
                    "filtered": filtered,
                    "clippedSamples": clipped,
                }
            )
    labels = [s.label for s in signals]
    if len(set(labels)) != len(labels):
        raise LocalToolError(
            "Two chosen channels have the same label, which an EDF file cannot hold. Choose "
            "channels with different labels."
        )
    _write_new(target, signals, h.record_sec)
    applied = []
    if low is not None or high is not None:
        applied.append(
            "bandpass"
            if low is not None and high is not None
            else ("highpass" if low is not None else "lowpass")
        )
    if notch is not None:
        applied.append("notch")
    if rate is not None:
        applied.append("resample")
    if channels is not None:
        applied.append("channels")
    return json_safe(
        {
            "recordingId": content_id(edf.path),
            "output": display_name(target),
            "outputId": content_id(target),
            "format": "EDF+C",
            "durationSec": rnd(h.duration_sec, 3),
            "applied": applied,
            "filters": {
                "lowHz": low,
                "highHz": high,
                "method": METHOD if (low is not None or high is not None) else None,
                "notchHz": notch,
                "notchQ": NOTCH_Q if notch is not None else None,
                "resampleHz": rate,
            },
            "channels": rows,
            "channelsWritten": len(rows),
            "channelsLeftOut": len(h.signal_indices) - len(rows),
            "annotationsCopied": False,
        }
    )


def _filt(sos: np.ndarray, x: np.ndarray, label: str) -> np.ndarray:
    try:
        return sosfiltfilt(sos, x)
    except ValueError:
        raise LocalToolError(
            f"{label} is too short to filter at these settings. Use a longer recording."
        ) from None


def _write_new(target: str, signals: list[OutSignal], record_sec: float) -> None:
    """Create ``target`` (it must not exist) and write the EDF into it."""
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise LocalToolError(
            f"{display_name(target)} already exists. preprocess never replaces a file."
        ) from None
    except OSError:
        raise LocalToolError(
            f"{display_name(target)} could not be created. Check that you can write to the folder."
        ) from None
    os.close(fd)
    try:
        write_edf(target, signals, record_sec=record_sec)
    except (OSError, ValueError) as exc:
        with contextlib.suppress(OSError):
            os.unlink(target)
        if isinstance(exc, OSError):
            raise LocalToolError(
                f"{display_name(target)} could not be written. Check the free space and the "
                "folder's permissions."
            ) from None
        raise LocalToolError(
            "The output could not be laid out as EDF records at these settings. Try another "
            "resample_hz."
        ) from None
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(target)
        raise
