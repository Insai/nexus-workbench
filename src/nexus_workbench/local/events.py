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

import math
from bisect import bisect_left
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


def _find(parent: list[int], k: int) -> int:
    """Root of ``k`` in a skip list of used slots, with path compression."""
    root = k
    while parent[root] != root:
        root = parent[root]
    while parent[k] != root:
        parent[k], k = root, parent[k]
    return root


class MatchTooLong(Exception):
    """Matching would pass its work budget (see :meth:`_RefIndex.match`)."""


class _RefIndex:
    """Reference events grouped by duration class, each group sorted by position.

    Position is the midpoint (overlap matching) or the onset (onset matching). A reference
    event of duration at most ``L`` can only overlap a candidate whose midpoint is within
    ``(candidate length + 2 * tolerance + L) / 2`` of its own (less ``min_overlap_sec``), so
    each group is searched outward from the candidate's position up to that bound. Each group
    is held in two orders, by position then index and by position then reverse index, so both
    directions visit reference events in increasing (distance, index): the first one that
    overlaps is the group's best, and the search stops there. Used events are skipped in near
    constant time. This keeps matching close to linear in the number of events.
    """

    def __init__(self, truth: list[Event], require_overlap: bool, min_overlap_sec: float) -> None:
        self.truth = truth
        self.require_overlap = require_overlap
        groups: dict[int, list[int]] = {}
        for ti, t in enumerate(truth):
            d = t.duration_sec if require_overlap else 0.0
            if require_overlap and min_overlap_sec > 0 and d < min_overlap_sec:
                continue  # too short to ever overlap a candidate by min_overlap_sec
            key = -10_000 if d <= 0 else math.frexp(d)[1]
            groups.setdefault(key, []).append(ti)
        # Per group: longest duration, positions (sorted), ids by (position, index), ids by
        # (position, reverse index), and each id's slot in those two orders.
        self.groups: list[
            tuple[float, list[float], list[int], list[int], dict[int, int], dict[int, int]]
        ] = []
        for key in sorted(groups):
            members = groups[key]
            up = sorted(members, key=lambda ti: (self._pos(truth[ti]), ti))
            down = sorted(members, key=lambda ti: (self._pos(truth[ti]), -ti))
            pos = [self._pos(truth[ti]) for ti in up]
            longest = max(truth[ti].duration_sec for ti in members) if require_overlap else 0.0
            self.groups.append(
                (
                    longest,
                    pos,
                    up,
                    down,
                    {ti: k for k, ti in enumerate(up)},
                    {ti: k for k, ti in enumerate(down)},
                )
            )

    def _pos(self, e: Event) -> float:
        return (e.onset_sec + e.end_sec) / 2 if self.require_overlap else e.onset_sec

    def match(
        self,
        pred: list[Event],
        tolerance_sec: float,
        min_overlap_sec: float,
        budget: int | None = None,
    ) -> tuple[MatchResult, int]:
        """Greedy one-to-one match and the work it took (positions examined).

        Raises :class:`MatchTooLong` once the work passes ``budget``. That only happens when
        many reference events lie near candidates without overlapping them enough.
        """
        truth = self.truth
        # Per group: right[k] is the first unused slot at or after k in the "up" order
        # (len = none); left[k + 1] is the last unused slot at or before k in the "down"
        # order (0 = none).
        rights = [list(range(len(g[2]) + 1)) for g in self.groups]
        lefts = [list(range(len(g[3]) + 1)) for g in self.groups]
        used: set[int] = set()
        pairs: list[tuple[int, int]] = []
        work = len(truth) + len(pred)
        order = sorted(range(len(pred)), key=lambda i: (pred[i].onset_sec, i))
        inf = math.inf
        for pi in order:
            p = pred[pi]
            pm = self._pos(p)
            window = p.duration_sec + 2 * tolerance_sec
            if self.require_overlap and min_overlap_sec > 0 and window < min_overlap_sec:
                continue  # this candidate can never overlap anything by min_overlap_sec
            best: tuple[float, int] | None = None
            for g, (longest, pos, up, down, _, _) in enumerate(self.groups):
                if self.require_overlap:
                    bound = (window + longest) / 2 - min_overlap_sec
                    bound = bound * (1 + 1e-9) + 1e-9
                else:
                    bound = tolerance_sec
                right, left = rights[g], lefts[g]
                n = len(up)
                j = bisect_left(pos, pm)
                r = _find(right, j)
                lf = _find(left, j) - 1
                work += 1
                while True:
                    kr = (abs(pm - pos[r]), up[r]) if r < n else (inf, 0)
                    kl = (abs(pm - pos[lf]), down[lf]) if lf >= 0 else (inf, 0)
                    key = min(kr, kl)
                    if key[0] > bound or (best is not None and key >= best):
                        break
                    if key is kr:
                        r = _find(right, r + 1)
                    else:
                        lf = _find(left, lf) - 1
                    work += 1
                    if budget is not None and work > budget:
                        raise MatchTooLong
                    d, ti = key
                    if self.require_overlap:
                        ok = _overlaps(p, truth[ti], tolerance_sec, min_overlap_sec)
                    else:
                        ok = d <= tolerance_sec
                    if ok:
                        best = key
                        break
            if best is not None:
                ti = best[1]
                for g, (_, _, _, _, at_up, at_down) in enumerate(self.groups):
                    if ti in at_up:
                        k = at_up[ti]
                        rights[g][k] = k + 1
                        k = at_down[ti]
                        lefts[g][k + 1] = k
                        break
                used.add(ti)
                pairs.append((pi, ti))
        matched = {pi for pi, _ in pairs}
        result = MatchResult(
            pairs=pairs,
            extra=[i for i in range(len(pred)) if i not in matched],
            missed=[i for i in range(len(truth)) if i not in used],
        )
        return result, work


def match_events(
    candidate: Sequence[object],
    reference: Sequence[object],
    *,
    tolerance_sec: float = 0.0,
    min_overlap_sec: float = 0.0,
    require_overlap: bool = True,
    max_work: int | None = None,
) -> MatchResult:
    """Greedy one-to-one match of ``candidate`` to ``reference``. See the module docstring.

    With ``max_work``, raises :class:`MatchTooLong` once the matching work passes it.
    """
    index = _RefIndex(as_events(reference), require_overlap, min_overlap_sec)
    return index.match(as_events(candidate), tolerance_sec, min_overlap_sec, max_work)[0]


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
    max_work: int | None = None,
    tolerance_sec: float = 0.0,
    min_overlap_sec: float = 0.0,
    require_overlap: bool = True,
) -> dict[str, float | int | None]:
    """Chance level for F1 and recall: shift every candidate onset by one random offset
    (circularly, within ``span_sec``) and match again, ``n_shuffles`` times.

    ``span_sec`` defaults to the latest event end in either list. Deterministic for a seed.
    With ``max_work``, shuffles stop before the measured matching work (positions examined,
    summed over shuffles) would pass it. The first shuffle may use the whole budget; if even it
    would pass the budget, no shuffle runs and the means are ``None``. ``nShuffles`` is the
    number actually run.
    """
    pred = as_events(candidate)
    truth = as_events(reference)
    if not pred or not truth or n_shuffles <= 0:
        return {"f1Mean": None, "f1P95": None, "recallMean": None, "nShuffles": 0}
    span = span_sec if span_sec is not None else max(e.end_sec for e in pred + truth)
    index = _RefIndex(truth, require_overlap, min_overlap_sec)
    rng = np.random.default_rng(seed)
    f1s: list[float] = []
    recs: list[float] = []
    spent = 0
    for k in range(n_shuffles):
        if max_work is not None and k > 0 and spent + spent / k > max_work:
            break
        shift = float(rng.uniform(0, span)) if span > 0 else 0.0
        moved = [
            Event((e.onset_sec + shift) % span if span > 0 else e.onset_sec, e.duration_sec)
            for e in pred
        ]
        left = None if max_work is None else max_work - spent
        try:
            mr, work = index.match(moved, tolerance_sec, min_overlap_sec, left)
        except MatchTooLong:
            break
        spent += work
        tp = len(mr.pairs)
        precision, recall = tp / len(moved), tp / len(truth)
        f1s.append(f1_score(precision, recall))
        recs.append(recall)
    if not f1s:
        return {"f1Mean": None, "f1P95": None, "recallMean": None, "nShuffles": 0}
    return {
        "f1Mean": float(np.mean(f1s)),
        "f1P95": float(np.percentile(f1s, 95)),
        "recallMean": float(np.mean(recs)),
        "nShuffles": len(f1s),
    }
