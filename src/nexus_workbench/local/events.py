"""Event-level agreement: one-to-one matching of candidate events to reference events, precision,
recall, F1, false positives per hour, and a shuffled null.

Matching is greedy and one-to-one. Candidates are taken in onset order; each takes the closest
unused reference event (by distance between midpoints, ties to the lower reference index)
among those it overlaps. ``tolerance_sec`` widens each candidate by that much on both sides
before the overlap is measured; ``min_overlap_sec`` is the shortest overlap that counts (0 means
any overlap; a zero-length event counts as overlapping an event that contains it).

With ``require_overlap=False`` matching uses onsets only: a candidate matches a reference event
whose onset is within ``tolerance_sec`` of its own.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Event:
    onset_sec: float
    duration_sec: float
    label: str = ""

    @property
    def end_sec(self) -> float:
        return self.onset_sec + self.duration_sec


@dataclass
class MatchResult:
    pairs: list[tuple[int, int]]  # (candidate index, reference index)
    extra: list[int]  # candidate indices with no match
    missed: list[int]  # reference indices with no match


def as_events(seq: Iterable[object]) -> list[Event]:
    """Events from :class:`Event` objects or ``(onset, duration)`` pairs."""
    out: list[Event] = []
    for e in seq:
        if isinstance(e, Event):
            out.append(e)
        else:
            on, du = e  # type: ignore[misc]
            out.append(Event(float(on), float(du)))
    return out


def _overlaps(p: Event, t: Event, tol: float, min_overlap: float) -> bool:
    a0, a1 = p.onset_sec - tol, p.end_sec + tol
    b0, b1 = t.onset_sec, t.end_sec
    length = min(a1, b1) - max(a0, b0)
    if min_overlap > 0:
        return length >= min_overlap
    if length > 0:
        return True
    # A zero-length event touching or inside the other one counts as overlapping.
    return length == 0 and (a1 == a0 or b1 == b0)


def match_events(
    candidate: Sequence[object],
    reference: Sequence[object],
    *,
    tolerance_sec: float = 0.0,
    min_overlap_sec: float = 0.0,
    require_overlap: bool = True,
) -> MatchResult:
    """Greedy one-to-one match of ``candidate`` to ``reference``. See the module docstring."""
    pred = as_events(candidate)
    truth = as_events(reference)
    order = sorted(range(len(pred)), key=lambda i: (pred[i].onset_sec, i))
    by_onset = sorted(range(len(truth)), key=lambda i: (truth[i].onset_sec, i))
    onsets = [truth[i].onset_sec for i in by_onset]
    # A reference event reaches back at most its own duration, so the longest bounds the window.
    reach = max((t.duration_sec for t in truth), default=0.0)
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for pi in order:
        p = pred[pi]
        if require_overlap:
            lo = bisect_left(onsets, p.onset_sec - tolerance_sec - reach)
            hi = bisect_right(onsets, p.end_sec + tolerance_sec)
        else:
            lo = bisect_left(onsets, p.onset_sec - tolerance_sec)
            hi = bisect_right(onsets, p.onset_sec + tolerance_sec)
        best: tuple[float, int] | None = None
        for ti in by_onset[lo:hi]:
            if ti in used:
                continue
            t = truth[ti]
            if require_overlap:
                ok = _overlaps(p, t, tolerance_sec, min_overlap_sec)
                key = abs((p.onset_sec + p.end_sec) / 2 - (t.onset_sec + t.end_sec) / 2)
            else:
                key = abs(p.onset_sec - t.onset_sec)
                ok = key <= tolerance_sec
            if ok and (best is None or (key, ti) < best):
                best = (key, ti)
        if best is not None:
            used.add(best[1])
            pairs.append((pi, best[1]))
    matched = {pi for pi, _ in pairs}
    return MatchResult(
        pairs=pairs,
        extra=[i for i in range(len(pred)) if i not in matched],
        missed=[i for i in range(len(truth)) if i not in used],
    )


@dataclass
class EventScore:
    matched: int
    missed: int
    extra: int
    n_candidate: int
    n_reference: int
    precision: float | None
    recall: float | None
    f1: float | None
    false_positives_per_hour: float | None


def f1_score(precision: float | None, recall: float | None) -> float:
    if not precision or not recall:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def score_events(
    candidate: Sequence[object],
    reference: Sequence[object],
    hours: float | None = None,
    **match_kw: object,
) -> EventScore:
    """Counts, precision, recall, F1 and false positives per hour (when ``hours`` is known).

    Precision is ``None`` with no candidate events, recall ``None`` with no reference events,
    and F1 ``None`` when both lists are empty (0 when only one is).
    """
    pred = as_events(candidate)
    truth = as_events(reference)
    mr = match_events(pred, truth, **match_kw)  # type: ignore[arg-type]
    tp = len(mr.pairs)
    precision = tp / len(pred) if pred else None
    recall = tp / len(truth) if truth else None
    f1 = None if not pred and not truth else f1_score(precision, recall)
    fph = len(mr.extra) / hours if hours is not None and hours > 0 else None
    return EventScore(
        matched=tp,
        missed=len(mr.missed),
        extra=len(mr.extra),
        n_candidate=len(pred),
        n_reference=len(truth),
        precision=precision,
        recall=recall,
        f1=f1,
        false_positives_per_hour=fph,
    )


def shuffled_null(
    candidate: Sequence[object],
    reference: Sequence[object],
    *,
    n_shuffles: int = 200,
    span_sec: float | None = None,
    seed: int = 0,
    **match_kw: object,
) -> dict[str, float | int | None]:
    """Chance level for F1 and recall: shift every candidate onset by one random offset
    (circularly, within ``span_sec``) and match again, ``n_shuffles`` times.

    ``span_sec`` defaults to the latest event end in either list. Deterministic for a seed.
    """
    pred = as_events(candidate)
    truth = as_events(reference)
    if not pred or not truth or n_shuffles <= 0:
        return {"f1Mean": None, "f1P95": None, "recallMean": None, "nShuffles": 0}
    span = span_sec if span_sec is not None else max(e.end_sec for e in pred + truth)
    rng = np.random.default_rng(seed)
    f1s: list[float] = []
    recs: list[float] = []
    for _ in range(n_shuffles):
        shift = float(rng.uniform(0, span)) if span > 0 else 0.0
        moved = [
            Event((e.onset_sec + shift) % span if span > 0 else e.onset_sec, e.duration_sec)
            for e in pred
        ]
        s = score_events(moved, truth, **match_kw)
        f1s.append(s.f1 or 0.0)
        recs.append(s.recall or 0.0)
    return {
        "f1Mean": float(np.mean(f1s)),
        "f1P95": float(np.percentile(f1s, 95)),
        "recallMean": float(np.mean(recs)),
        "nShuffles": n_shuffles,
    }
