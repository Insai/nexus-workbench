"""The local tools. Each takes plain arguments, returns a JSON-safe ``dict`` and raises
:class:`~nexus_workbench.local.errors.LocalToolError` for every expected failure.

Results never contain a path, a file name, raw samples or EDF header text other than standard
channel names. Files are named by an opaque id: the first 12 hex characters of the SHA-256 of
their bytes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import bandpower as bp
from . import quality as q
from . import staging
from .edf import EdfFile, Header
from .errors import LocalToolError
from .events import Event, MatchTooLong, score_events, shuffled_null
from .formats import parse_events, parse_hypnogram, read_hypnogram
from .labels import guess_type, safe_label, safe_unit, to_microvolts
from .report import content_id, display_name, json_safe, pct, read_text_file, rnd, sig

#: Channels listed by ``inspect_edf``.
MAX_LIST_CHANNELS = 128
#: Channels analysed in one ``check_signal_quality`` call.
MAX_QUALITY_CHANNELS = 64
#: Channels analysed in one ``compute_band_power`` call.
MAX_POWER_CHANNELS = 32
#: Bad spans returned by ``check_signal_quality``.
MAX_SPANS = 20
#: Skipped (non-voltage) channels listed by ``check_signal_quality`` and ``compute_band_power``.
MAX_SKIPPED = 20
#: Matching work (event positions examined) allowed for the match itself; past it the call is
#: refused, so a pathological pair of files cannot run for minutes.
MAX_MATCH_WORK = 2_000_000
#: Matching work summed over all shuffles is kept under this, so the null stays quick whatever
#: the events look like.
MAX_SHUFFLE_WORK = 10_000_000
MAX_SHUFFLES = 2000
#: The end of the message for a recording shorter than one epoch (the CLI swaps it for its own).
SHORT_FILE_HINT = "Use a shorter epoch_sec or a longer recording."
#: A hypnogram may run this many epochs past the end of the EDF (the extra ones are ignored).
HYPNOGRAM_SLACK_EPOCHS = 2


# ----------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class Supplied:
    """Text given in place of a file, for example a workspace recording's machine staging.

    ``file_id`` names it in results where a file's id would be; ``name`` names it in messages.
    """

    text: str
    file_id: str
    name: str


def _text_input(source: str | Supplied, what: str) -> tuple[str, str, str]:
    """``(text, id, name)`` of a path or a :class:`Supplied` text."""
    if isinstance(source, Supplied):
        return source.text, source.file_id, source.name
    text, fid = read_text_file(source, what)
    return text, fid, display_name(source)


def _epoch_sec(epoch_sec: object) -> float:
    if isinstance(epoch_sec, bool):
        epoch_sec = None
    try:
        v = float(epoch_sec)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise LocalToolError("epoch_sec must be a number of seconds, for example 30.") from None
    if not math.isfinite(v) or not (1.0 <= v <= 3600.0):
        raise LocalToolError("epoch_sec must be between 1 and 3600 seconds (30 is usual).")
    return v


def _channel_row(h: Header, i: int) -> dict[str, Any]:
    return {
        "index": i + 1,
        "label": safe_label(h.labels[i], i + 1),
        "type": guess_type(h.labels[i]),
        "sampleRateHz": rnd(h.fs(i), 6),
        "unit": safe_unit(h.units[i]),
        "physicalMin": sig(h.phys_min[i], 8),
        "physicalMax": sig(h.phys_max[i], 8),
        "sampleCount": h.n_records * h.samples_per_record[i],
    }


def _as_list(channels: object) -> list[object]:
    if isinstance(channels, str):
        return [c.strip() for c in channels.split(",") if c.strip()]
    if isinstance(channels, (int, np.integer)) and not isinstance(channels, bool):
        return [int(channels)]
    if isinstance(channels, Sequence):
        return list(channels)
    raise LocalToolError(
        'channels must be a list of channel labels or 1-based positions, for example ["C3-M2", 2].'
    )


def _positions(signals: list[int]) -> str:
    """Where the data channels are, in 1-based positions (the annotation signal is not one)."""
    pos = [i + 1 for i in signals]
    if pos == list(range(1, len(pos) + 1)):
        return f"Positions run from 1 to {len(pos)}"
    shown = ", ".join(str(k) for k in pos[:40]) + (", ..." if len(pos) > 40 else "")
    return f"The data channels are at positions {shown}"


def _select(h: Header, channels: object) -> list[int]:
    """0-based indices for ``channels`` (labels as ``inspect_edf`` shows them, ``ch<N>``, the
    file's own label, or 1-based positions)."""
    signals = h.signal_indices
    shown = {i: safe_label(h.labels[i], i + 1) for i in signals}
    out: list[int] = []
    for c in _as_list(channels):
        if isinstance(c, bool):
            c = None
        if isinstance(c, (int, np.integer)):
            k = int(c) - 1
            if k not in shown:
                raise LocalToolError(
                    f"There is no data channel at position {int(c)}. {_positions(signals)}; "
                    "run inspect_edf to see them."
                )
            out.append(k)
            continue
        if not isinstance(c, str) or not c.strip():
            raise LocalToolError("Each channel must be a label or a 1-based position.")
        want = c.strip().lower()
        hits = [i for i in signals if shown[i].lower() == want]
        if not hits:
            hits = [i for i in signals if h.labels[i].strip().lower() == want]
        if not hits and want.startswith("ch") and want[2:].isdigit():
            k = int(want[2:]) - 1
            hits = [k] if k in shown else []
        if not hits:
            avail = ", ".join(shown[i] for i in signals[:40])
            raise LocalToolError(
                f"No channel matches the name given. Channels in this file: {avail}"
                + (" …" if len(signals) > 40 else "")
                + ". Use one of these labels or a 1-based position."
            )
        if len(hits) > 1:
            raise LocalToolError(
                "More than one channel matches that name. Use the 1-based position instead "
                f"(one of {', '.join(str(i + 1) for i in hits)})."
            )
        out.append(hits[0])
    if not out:
        raise LocalToolError("channels was empty. Leave it out to use the default channels.")
    return list(dict.fromkeys(out))


def _uv_scale(h: Header, i: int) -> float | None:
    return to_microvolts(h.units[i])


def _minmax(edf: EdfFile, idx: list[int], epoch_sec: float) -> dict[int, tuple[float, float]]:
    """Each channel's physical minimum and maximum (µV) over its whole epochs."""
    lo = {i: 32767 for i in idx}
    hi = {i: -32768 for i in idx}
    for _, _, blocks in edf.iter_epoch_blocks(idx, epoch_sec):
        for i, d in blocks.items():
            lo[i] = min(lo[i], int(d.min()))
            hi[i] = max(hi[i], int(d.max()))
    h = edf.header
    out: dict[int, tuple[float, float]] = {}
    for i in idx:
        s = _uv_scale(h, i) or 1.0
        a = float(h.to_physical(i, np.array([lo[i]]))[0]) * s
        b = float(h.to_physical(i, np.array([hi[i]]))[0]) * s
        out[i] = (min(a, b), max(a, b))
    return out


def _evidence(
    h: Header, i: int, d: np.ndarray, lo: float, hi: float
) -> tuple[q.EpochEvidence, np.ndarray]:
    """Quality evidence for a block of digital samples, and the block in µV."""
    x = h.to_physical(i, d) * (_uv_scale(h, i) or 1.0)
    at_limit = (d == h.dig_min[i]) | (d == h.dig_max[i])
    amp = q.amplitude_rules_apply(guess_type(h.labels[i]))
    return q.epoch_evidence(x, h.fs(i), lo, hi, at_limit, amplitude_rules=amp), x


def _open_edf(path: object) -> EdfFile:
    return EdfFile(path)  # type: ignore[arg-type]


def _need_continuous(edf: EdfFile) -> None:
    """Refuse EDF+D: its records have gaps, and the epoch maths assumes they do not."""
    if edf.header.variant == "EDF+D":
        raise LocalToolError(
            f"{edf.name} is a discontinuous EDF+D recording: its data records have gaps between "
            "them. Discontinuous recordings are not supported yet by this tool; inspect_edf can "
            "still describe the file. Export the recording as continuous EDF or EDF+C."
        )


def _need_epochs(edf: EdfFile, epoch_sec: float) -> int:
    n = edf.n_epochs(epoch_sec)
    if n == 0:
        raise LocalToolError(
            f"{edf.name} is {edf.header.duration_sec:g} s long, shorter than one {epoch_sec:g} s "
            f"epoch. {SHORT_FILE_HINT}"
        )
    return n


def _split_voltage(h: Header, idx: list[int]) -> tuple[list[int], list[dict[str, Any]]]:
    keep, skipped = [], []
    for i in idx:
        if _uv_scale(h, i) is None:
            skipped.append(
                {
                    "index": i + 1,
                    "label": safe_label(h.labels[i], i + 1),
                    "reason": "not a voltage channel (unit is not V, mV, uV or nV)",
                }
            )
        else:
            keep.append(i)
    return keep, skipped


# ----------------------------------------------------------------------------- inspect_edf


def inspect_edf(path: str) -> dict[str, Any]:
    """Describe an EDF or EDF+ file: duration, channels and whether annotations exist.

    Never returns the header's patient field, recording field, or start date and time.
    """
    edf = _open_edf(path)
    h = edf.header
    with edf:
        n_ann = len(edf.annotations()) if h.annotation_indices else 0
    rows = [_channel_row(h, i) for i in h.signal_indices]
    return json_safe(
        {
            "recordingId": content_id(edf.path),
            "format": h.variant,
            "durationSec": rnd(h.duration_sec, 3),
            "recordSec": rnd(h.record_sec, 6),
            "nRecords": h.n_records,
            "nChannels": len(rows),
            "channels": rows[:MAX_LIST_CHANNELS],
            "channelsListed": min(len(rows), MAX_LIST_CHANNELS),
            "annotations": {"present": n_ann > 0, "count": n_ann},
            "discontinuous": h.variant == "EDF+D",
            "truncated": len(rows) > MAX_LIST_CHANNELS,
        }
    )


# ----------------------------------------------------------------------------- quality


def _quality(
    edf: EdfFile, idx: list[int], epoch_sec: float
) -> tuple[dict[int, q.ChannelQuality], int]:
    h = edf.header
    n = _need_epochs(edf, epoch_sec)
    ranges = _minmax(edf, idx, epoch_sec)
    out = {
        i: q.ChannelQuality(
            samples_per_epoch=edf.samples_per_epoch(i, epoch_sec),
            lo=ranges[i][0],
            hi=ranges[i][1],
        )
        for i in idx
    }
    for _, _, blocks in edf.iter_epoch_blocks(idx, epoch_sec):
        for i, d in blocks.items():
            out[i].add(_evidence(h, i, d, ranges[i][0], ranges[i][1])[0])
    return out, n


def check_signal_quality(
    path: str, channels: list[str | int] | None = None, epoch_sec: float = 30.0
) -> dict[str, Any]:
    """Classify every epoch of every voltage channel as ok, suspect, flat or railed."""
    ep = _epoch_sec(epoch_sec)
    edf = _open_edf(path)
    _need_continuous(edf)
    h = edf.header
    idx = h.signal_indices if channels is None else _select(h, channels)
    if not idx:
        raise LocalToolError(f"{edf.name} has no data channels, only annotations.")
    volt, skipped = _split_voltage(h, idx)
    truncated = len(volt) > MAX_QUALITY_CHANNELS
    volt = volt[:MAX_QUALITY_CHANNELS]
    if not volt:
        raise LocalToolError(
            "None of the chosen channels is a voltage channel, so the quality thresholds (in "
            "microvolts) do not apply. Choose EEG, EOG, EMG or ECG channels."
        )
    with edf:
        res, n = _quality(edf, volt, ep)
    rows = []
    spans: list[tuple[float, int, float, float, str]] = []
    total = ok_total = 0
    all_ok = np.ones(n, dtype=bool)
    for i in volt:
        cq = res[i]
        counts = cq.counts()
        m = cq.n_epochs
        total += m
        ok_total += counts["ok"]
        all_ok &= cq.classes == q.OK
        label = safe_label(h.labels[i], i + 1)
        rows.append(
            {
                "index": i + 1,
                "label": label,
                "type": guess_type(h.labels[i]),
                "epochs": m,
                "counts": counts,
                "percent": {k: pct(v, m) for k, v in counts.items()},
                "overall": cq.overall(),
            }
        )
        for a, b, c in cq.bad_spans(ep):
            spans.append((b - a, i, a, b, c))
    spans.sort(key=lambda s: (-s[0], s[1], s[2]))
    bad = [
        {"channel": safe_label(h.labels[i], i + 1), "startSec": a, "endSec": b, "class": c}
        for _, i, a, b, c in spans[:MAX_SPANS]
    ]
    return json_safe(
        {
            "recordingId": content_id(edf.path),
            "epochSec": ep,
            "nEpochs": n,
            "secondsNotInEpochs": rnd(h.duration_sec - n * ep, 3),
            "thresholds": q.THRESHOLDS,
            "channels": rows,
            "skipped": skipped[:MAX_SKIPPED],
            "usablePercent": pct(ok_total, total),
            "allChannelsOkPercent": pct(int(all_ok.sum()), n),
            "badSpans": bad,
            "badSpansTotal": len(spans),
            "badSpansCap": MAX_SPANS,
            "truncated": truncated or len(spans) > MAX_SPANS or len(skipped) > MAX_SKIPPED,
            "channelsTruncated": truncated,
        }
    )


# ----------------------------------------------------------------------------- band power


def _load_hypnogram(path: str, n_epochs: int) -> tuple[np.ndarray, str, int]:
    text, fid = read_text_file(path, "hypnogram")
    hyp = parse_hypnogram(text, display_name(path))
    if len(hyp) < n_epochs:
        raise LocalToolError(
            f"The hypnogram has {len(hyp)} epochs but the recording has {n_epochs} whole "
            "epochs. They must cover the same night with the same epoch length; check "
            "epoch_sec and that the files belong together."
        )
    if len(hyp) > n_epochs + HYPNOGRAM_SLACK_EPOCHS:
        raise LocalToolError(
            f"The hypnogram has {len(hyp)} epochs but the recording only {n_epochs} whole "
            "epochs. Check epoch_sec and that the files belong together."
        )
    return hyp[:n_epochs], fid, len(hyp) - n_epochs


def compute_band_power(
    path: str,
    channels: list[str | int] | None = None,
    hypnogram_path: str | None = None,
    epoch_sec: float = 30.0,
    bands: dict[str, list[float]] | list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Mean absolute and relative band power per channel over artifact-free (``ok``) epochs,
    and per sleep stage when a hypnogram is given."""
    ep = _epoch_sec(epoch_sec)
    band_list = bp.parse_bands(bands)
    edf = _open_edf(path)
    _need_continuous(edf)
    h = edf.header
    chosen = "as requested"
    if channels is None:
        idx = [i for i in h.signal_indices if guess_type(h.labels[i]) == "eeg"]
        chosen = "channels that look like EEG"
        if not [i for i in idx if _uv_scale(h, i) is not None]:
            idx = h.signal_indices
            chosen = "all voltage channels (none looked like EEG)"
    else:
        idx = _select(h, channels)
    volt, skipped = _split_voltage(h, idx)
    truncated = len(volt) > MAX_POWER_CHANNELS
    volt = volt[:MAX_POWER_CHANNELS]
    if not volt:
        raise LocalToolError(
            "None of the chosen channels is a voltage channel. Choose EEG channels, by label "
            "or position (run inspect_edf to see them)."
        )
    n = _need_epochs(edf, ep)
    hyp: np.ndarray | None = None
    hyp_info: dict[str, Any] | None = None
    if hypnogram_path is not None:
        hyp, fid, extra = _load_hypnogram(hypnogram_path, n)
        hyp_info = {
            "fileId": fid,
            "epochs": n,
            "unscored": int((hyp < 0).sum()),
            "extraEpochsIgnored": extra,
        }

    nb = len(band_list)
    overall = {i: bp.BandAccumulator(nb) for i in volt}
    # One accumulator per stage code (0=W ... 4=R) per channel.
    per_stage = {i: [bp.BandAccumulator(nb) for _ in staging.STAGES] for i in volt}
    with edf:
        ranges = _minmax(edf, volt, ep)
        for e0, e1, blocks in edf.iter_epoch_blocks(volt, ep):
            for i, d in blocks.items():
                ev, x = _evidence(h, i, d, *ranges[i])
                ok = ev.classes == q.OK
                if not ok.any():
                    continue
                absolute, total = bp.epoch_band_powers(x[ok], h.fs(i), band_list)
                overall[i].add(absolute, total)
                if hyp is not None:
                    st = hyp[e0:e1][ok]
                    for code in range(len(staging.STAGES)):
                        m = st == code
                        if m.any():
                            per_stage[i][code].add(absolute[m], total[m])

    def pack(acc: bp.BandAccumulator) -> dict[str, Any]:
        return {
            "epochs": acc.n,
            "absolute": [sig(v) for v in acc.mean_absolute()],
            "relative": [rnd(v, 4) for v in acc.relative()],
        }

    rows = []
    for i in volt:
        nyq = h.fs(i) / 2
        row: dict[str, Any] = {
            "index": i + 1,
            "label": safe_label(h.labels[i], i + 1),
            "type": guess_type(h.labels[i]),
            "epochsTotal": n,
            "epochsUsed": overall[i].n,
            **{k: v for k, v in pack(overall[i]).items() if k != "epochs"},
        }
        limited = [b.name for b in band_list if b.high > nyq]
        if limited:
            row["bandsAboveNyquist"] = limited
        if hyp is not None:
            row["byStage"] = {
                name: pack(per_stage[i][code]) for code, name in enumerate(staging.STAGES)
            }
        rows.append(row)
    return json_safe(
        {
            "recordingId": content_id(edf.path),
            "epochSec": ep,
            "nEpochs": n,
            "bands": [{"name": b.name, "lowHz": b.low, "highHz": b.high} for b in band_list],
            "unit": "uV^2",
            "method": {
                "psd": "welch",
                "window": "hann",
                "windowSec": bp.WINDOW_SEC,
                "overlap": 0.5,
                "bandPower": "sum of PSD bins in [lowHz, highHz) times bin width",
                "relative": "band power over power from the lowest to the highest band edge",
                "average": "mean over epochs classed ok by check_signal_quality",
            },
            "channelsChosen": chosen,
            "channels": rows,
            "skipped": skipped[:MAX_SKIPPED],
            "hypnogram": hyp_info,
            "truncated": truncated or len(skipped) > MAX_SKIPPED,
        }
    )


# ----------------------------------------------------------------------------- hypnograms


def compare_hypnograms(
    path_a: str | Supplied, path_b: str | Supplied, epoch_sec: float = 30.0
) -> dict[str, Any]:
    """Epoch-by-epoch agreement between two hypnograms of the same night (``a`` as reference).

    Either side may be a :class:`Supplied` text instead of a path.
    """
    ep = _epoch_sec(epoch_sec)
    text_a, id_a, name_a = _text_input(path_a, "hypnogram")
    text_b, id_b, name_b = _text_input(path_b, "hypnogram")
    a, first_a = read_hypnogram(text_a, name_a)
    b, first_b = read_hypnogram(text_b, name_b)
    if len(a) != len(b):
        raise LocalToolError(
            f"The hypnograms differ in length: {name_a} has {len(a)} epochs and "
            f"{name_b} has {len(b)}. They must cover the same epochs of the same "
            "night; trim or pad one with ? for unscored epochs."
        )
    res = staging.compare(a, b)
    per = {
        name: {
            "precision": rnd(v["precision"]),
            "recall": rnd(v["recall"]),
            "f1": rnd(v["f1"]),
            "epochsA": v["epochsA"],
            "epochsB": v["epochsB"],
            "agreed": v["agreed"],
        }
        for name, v in res["perStage"].items()
    }

    def minutes(h: np.ndarray) -> dict[str, float | None]:
        out: dict[str, float | None] = {
            s: rnd(float((h == k).sum()) * ep / 60, 2) for k, s in enumerate(staging.STAGES)
        }
        out["unscored"] = rnd(float((h < 0).sum()) * ep / 60, 2)
        return out

    return json_safe(
        {
            "fileIdA": id_a,
            "fileIdB": id_b,
            "epochSec": ep,
            "epochsTotal": len(a),
            "epochsCompared": res["epochsCompared"],
            "epochsUnscored": len(a) - res["epochsCompared"],
            "agreement": rnd(res["agreement"]),
            "kappa": rnd(res["kappa"]),
            "perStage": per,
            "confusion": {
                "rows": "a",
                "columns": "b",
                "labels": list(staging.STAGES),
                "matrix": res["confusion"],
            },
            "stageMinutes": {"a": minutes(a), "b": minutes(b)},
            "firstColumn": {"a": first_a, "b": first_b},
        }
    )


# ----------------------------------------------------------------------------- events


def _nonneg(v: object, name: str, upper: float | None = None) -> float:
    if isinstance(v, bool):
        v = None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise LocalToolError(f"{name} must be a number.") from None
    if not math.isfinite(x) or x < 0 or (upper is not None and x > upper):
        hint = f" and at most {upper:g}" if upper is not None else ""
        raise LocalToolError(f"{name} must be zero or more{hint}.")
    return x


def compare_events(
    reference_path: str | Supplied,
    candidate_path: str | Supplied,
    min_overlap: float = 0.0,
    recording_hours: float | None = None,
    n_shuffles: int = 200,
    seed: int = 0,
    tolerance_sec: float = 0.0,
    label: str | None = None,
) -> dict[str, Any]:
    """Match candidate events to reference events one to one and score the agreement.

    Either side may be a :class:`Supplied` text instead of a path.
    """
    mo = _nonneg(min_overlap, "min_overlap (seconds)", 3600)
    tol = _nonneg(tolerance_sec, "tolerance_sec", 3600)
    hours = None
    if recording_hours is not None:
        hours = _nonneg(recording_hours, "recording_hours", 1000)
        if hours == 0:
            raise LocalToolError("recording_hours must be more than zero, or left out.")
    if isinstance(n_shuffles, bool) or not isinstance(n_shuffles, (int, np.integer)):
        raise LocalToolError("n_shuffles must be a whole number, for example 200.")
    if not 0 <= int(n_shuffles) <= MAX_SHUFFLES:
        raise LocalToolError(f"n_shuffles must be between 0 and {MAX_SHUFFLES}.")
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)) or int(seed) < 0:
        raise LocalToolError("seed must be a whole number of zero or more.")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise LocalToolError("label must be text, or left out to use every event.")

    text_r, id_r, name_r = _text_input(reference_path, "event file")
    text_c, id_c, name_c = _text_input(candidate_path, "event file")
    ref = parse_events(text_r, name_r)
    cand = parse_events(text_c, name_c)
    n_ref_all, n_cand_all = len(ref), len(cand)
    if label is not None:
        want = label.strip().lower()

        def keep(es: list[Event]) -> list[Event]:
            return [e for e in es if e.label.lower() == want]

        ref, cand = keep(ref), keep(cand)
    if hours is not None:
        end = hours * 3600.0
        late = sum(1 for e in ref + cand if e.onset_sec >= end)
        if late:
            raise LocalToolError(
                f"{late} event(s) start at or after the end of a {hours:g} h recording. Check "
                "recording_hours and that onsets are in seconds from the start."
            )
    kw: dict[str, object] = {"tolerance_sec": tol, "min_overlap_sec": mo}
    try:
        s = score_events(cand, ref, hours, max_work=MAX_MATCH_WORK, **kw)
    except MatchTooLong:
        raise LocalToolError(
            "Matching these events would take too long: many reference events lie close to "
            "candidate events without overlapping them by min_overlap. Lower min_overlap or "
            "tolerance_sec, or compare one label at a time."
        ) from None
    shuffles = int(n_shuffles)
    null = shuffled_null(
        cand,
        ref,
        n_shuffles=shuffles,
        span_sec=hours * 3600.0 if hours is not None else None,
        seed=int(seed),
        max_work=MAX_SHUFFLE_WORK,
        tolerance_sec=tol,
        min_overlap_sec=mo,
    )
    reduced = bool(cand and ref) and int(null["nShuffles"] or 0) < shuffles
    return json_safe(
        {
            "fileIdReference": id_r,
            "fileIdCandidate": id_c,
            "eventsInFiles": {"reference": n_ref_all, "candidate": n_cand_all},
            "labelFilter": label is not None,
            "reference": s.n_reference,
            "candidate": s.n_candidate,
            "matched": s.matched,
            "missed": s.missed,
            "extra": s.extra,
            "precision": rnd(s.precision),
            "recall": rnd(s.recall),
            "f1": rnd(s.f1),
            "recordingHours": hours,
            "falsePositivesPerHour": rnd(s.false_positives_per_hour, 3),
            "matching": {
                "rule": "greedy one-to-one, closest midpoint first",
                "minOverlapSec": mo,
                "toleranceSec": tol,
            },
            "shuffledNull": {
                "method": "circular shift of all candidate onsets by one random offset",
                "seed": int(seed),
                "nShuffles": null["nShuffles"],
                "reduced": reduced,
                "f1Mean": rnd(null["f1Mean"]),
                "f1P95": rnd(null["f1P95"]),
                "recallMean": rnd(null["recallMean"]),
            },
        }
    )


def preprocess(
    path: str,
    low_hz: float | None = None,
    high_hz: float | None = None,
    notch_hz: float | None = None,
    resample_hz: float | None = None,
    channels: list[str | int] | None = None,
    out: str | None = None,
) -> dict[str, Any]:
    """Band-pass, notch, resample and pick channels of an EDF into a new EDF (see
    :mod:`nexus_workbench.local.preprocess`). The input is never changed."""
    from .preprocess import preprocess as run  # noqa: PLC0415

    return run(
        path,
        low_hz=low_hz,
        high_hz=high_hz,
        notch_hz=notch_hz,
        resample_hz=resample_hz,
        channels=channels,
        out=out,
    )


__all__ = [
    "Supplied",
    "check_signal_quality",
    "compare_events",
    "compare_hypnograms",
    "compute_band_power",
    "inspect_edf",
    "preprocess",
]
