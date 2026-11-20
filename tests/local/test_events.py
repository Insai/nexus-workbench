from __future__ import annotations

import random
import time
from pathlib import Path

import pytest

from nexus_workbench.local import api
from nexus_workbench.local.events import (
    Event,
    _overlaps,
    as_events,
    match_events,
    score_events,
    shuffled_null,
)


def test_overlap_matching_with_a_tolerance() -> None:
    cand = [(10, 3), (30, 3), (100, 2)]
    ref = [(11, 2), (31, 4)]
    mr = match_events(cand, ref, tolerance_sec=3.0)
    assert mr.pairs == [(0, 0), (1, 1)]
    assert mr.extra == [2] and mr.missed == []


def test_identical_events_match_when_duration_equals_min_overlap() -> None:
    """Overlap is measured in floating point, so end minus onset can come out a hair below
    the duration; an event must still match an identical copy of itself."""
    rng = random.Random(0)  # noqa: S311 (test data, not security)
    for duration in (0.3, 1.1):
        events = [(round(rng.uniform(0, 28790), 2), duration) for _ in range(2153)]
        s = score_events(events, events, min_overlap_sec=duration)
        assert s.matched == 2153 and s.missed == 0 and s.extra == 0


def test_scores_hand_computed() -> None:
    # 2 of 3 candidates match, both references found: precision 2/3, recall 1,
    # F1 = 2 * (2/3) * 1 / (2/3 + 1) = 0.8, one extra in 8 h = 0.125 per hour.
    s = score_events([(10, 3), (30, 3), (100, 2)], [(11, 2), (31, 4)], 8.0, tolerance_sec=3.0)
    assert (s.matched, s.missed, s.extra) == (2, 0, 1)
    assert s.precision == pytest.approx(2 / 3)
    assert s.recall == 1.0
    assert s.f1 == pytest.approx(0.8)
    assert s.false_positives_per_hour == 0.125


def test_one_to_one() -> None:
    mr = match_events([(10, 2), (11, 2)], [(10, 2)])
    assert mr.pairs == [(0, 0)] and mr.extra == [1] and mr.missed == []


def test_onset_policy() -> None:
    mr = match_events(
        [(10, 1), (30, 1)], [(11, 5), (100, 5)], tolerance_sec=2.0, require_overlap=False
    )
    assert mr.pairs == [(0, 0)] and mr.extra == [1] and mr.missed == [1]


def test_min_overlap() -> None:
    cand, ref = [(10, 2)], [(11, 2)]  # they overlap by 1 s
    assert len(match_events(cand, ref, min_overlap_sec=0.5).pairs) == 1
    assert len(match_events(cand, ref, min_overlap_sec=1.0).pairs) == 1
    assert len(match_events(cand, ref, min_overlap_sec=1.5).pairs) == 0


def test_zero_length_events_inside_an_event_count_as_overlapping() -> None:
    assert len(match_events([(11, 0)], [(10, 2)]).pairs) == 1
    assert len(match_events([(13, 0)], [(10, 2)]).pairs) == 0
    assert len(match_events([(13, 0)], [(10, 2)], tolerance_sec=1.5).pairs) == 1


def test_no_overlap_and_empty_lists() -> None:
    s = score_events([(100, 1)], [(10, 1)], 1.0)
    assert (s.matched, s.missed, s.extra, s.f1) == (0, 1, 1, 0.0)
    e = score_events([], [(5, 2), (20, 2)], 1.0)
    assert e.recall == 0.0 and e.precision is None and e.f1 == 0.0
    both = score_events([], [], None)
    assert both.f1 is None and both.false_positives_per_hour is None
    perfect = score_events([Event(5, 2), Event(20, 2)], [Event(5, 2), Event(20, 2)], 1.0)
    assert perfect.f1 == 1.0


def _all_pairs(cand, ref, tol, mo):  # type: ignore[no-untyped-def]
    """Slow reference: every candidate checks every reference event."""
    p, t = as_events(cand), as_events(ref)
    used: set[int] = set()
    pairs = []
    for pi in sorted(range(len(p)), key=lambda i: (p[i].onset_sec, i)):
        best = None
        for ti in range(len(t)):
            if ti in used or not _overlaps(p[pi], t[ti], tol, mo):
                continue
            key = abs((p[pi].onset_sec + p[pi].end_sec) / 2 - (t[ti].onset_sec + t[ti].end_sec) / 2)
            if best is None or (key, ti) < best:
                best = (key, ti)
        if best is not None:
            used.add(best[1])
            pairs.append((pi, best[1]))
    return pairs


def test_windowed_matcher_equals_all_pairs() -> None:
    rng = random.Random(0)  # noqa: S311 (test data, not security)
    for _ in range(200):
        n_c, n_r = rng.randint(0, 30), rng.randint(0, 30)
        cand = [
            (rng.randint(0, 120) / 2, rng.choice([0.0, 0.5, 1.0, 2.0, 15.0])) for _ in range(n_c)
        ]
        ref = [
            (rng.randint(0, 120) / 2, rng.choice([0.0, 0.5, 1.0, 2.0, 30.0])) for _ in range(n_r)
        ]
        for tol in (0.0, 0.5, 3.0):
            for mo in (0.0, 0.5):
                got = match_events(cand, ref, tolerance_sec=tol, min_overlap_sec=mo).pairs
                assert got == _all_pairs(cand, ref, tol, mo)


def test_shuffled_null_is_deterministic_and_below_a_perfect_match() -> None:
    ref = [Event(t, 2) for t in range(0, 600, 60)]
    a = shuffled_null(ref, ref, n_shuffles=100, span_sec=600.0, seed=7)
    b = shuffled_null(ref, ref, n_shuffles=100, span_sec=600.0, seed=7)
    c = shuffled_null(ref, ref, n_shuffles=100, span_sec=600.0, seed=8)
    assert a == b
    assert a != c
    assert a["f1Mean"] < 1.0 and a["f1P95"] <= 1.0 and a["nShuffles"] == 100
    assert shuffled_null([], ref)["nShuffles"] == 0


def test_matching_stays_quick_with_a_long_reference_event(tmp_path: Path) -> None:
    """One reference event spans the night: matching must not become candidates times refs."""
    rng = random.Random(1)  # noqa: S311 (test data, not security)
    cand = tmp_path / "cand.csv"
    cand.write_text("".join(f"{rng.uniform(0, 28790):.2f},5\n" for _ in range(2000)))
    ref = tmp_path / "ref.csv"
    ref.write_text("0,28800\n" + "".join(f"{rng.uniform(0, 28790):.2f},5\n" for _ in range(2000)))
    t0 = time.perf_counter()
    r = api.compare_events(str(ref), str(cand))
    assert time.perf_counter() - t0 < 20
    assert r["shuffledNull"]["nShuffles"] >= 1


def test_matching_stays_quick_with_many_events_and_a_wide_tolerance(tmp_path: Path) -> None:
    rng = random.Random(2)  # noqa: S311 (test data, not security)
    cand = tmp_path / "cand.csv"
    cand.write_text("".join(f"{rng.uniform(0, 28790):.2f},5\n" for _ in range(20_000)))
    ref = tmp_path / "ref.csv"
    ref.write_text("".join(f"{rng.uniform(0, 28790):.2f},5\n" for _ in range(20_000)))
    t0 = time.perf_counter()
    r = api.compare_events(str(ref), str(cand), tolerance_sec=3600)
    assert time.perf_counter() - t0 < 20
    null = r["shuffledNull"]
    assert 1 <= null["nShuffles"] < 200 and null["reduced"] is True
    assert r["matched"] > 19_000


def test_default_shuffles_all_run_on_an_ordinary_night(tmp_path: Path) -> None:
    """3,000 reference events and a detector that finds each one a little off: all 200
    default shuffles run, and none is dropped for the work bound."""
    rng = random.Random(5)  # noqa: S311 (test data, not security)
    rows = [(rng.uniform(0, 28790), rng.uniform(0.5, 2.0)) for _ in range(3000)]
    ref = tmp_path / "ref.csv"
    ref.write_text("".join(f"{t:.3f},{d:.3f}\n" for t, d in rows))
    cand = tmp_path / "cand.csv"
    cand.write_text(
        "".join(
            f"{max(0.0, t + rng.gauss(0, 0.3)):.3f},{d * rng.uniform(0.8, 1.2):.3f}\n"
            for t, d in rows
        )
    )
    t0 = time.perf_counter()
    r = api.compare_events(str(ref), str(cand), recording_hours=8)
    assert time.perf_counter() - t0 < 20
    null = r["shuffledNull"]
    assert null["nShuffles"] == 200 and null["reduced"] is False


def test_shuffled_null_stops_at_the_work_bound() -> None:
    ref = [Event(t, 2) for t in range(0, 6000, 6)]
    full = shuffled_null(ref, ref, n_shuffles=50, span_sec=6000.0)
    capped = shuffled_null(ref, ref, n_shuffles=50, span_sec=6000.0, max_work=20_000)
    assert full["nShuffles"] == 50
    assert 1 <= int(capped["nShuffles"] or 0) < 50
    none = shuffled_null(ref, ref, n_shuffles=50, span_sec=6000.0, max_work=1)
    assert none["nShuffles"] == 0 and none["f1Mean"] is None


def test_onset_matcher_equals_all_pairs() -> None:
    rng = random.Random(3)  # noqa: S311 (test data, not security)
    for _ in range(200):
        cand = [(rng.randint(0, 120) / 2, 1.0) for _ in range(rng.randint(0, 30))]
        ref = [(rng.randint(0, 120) / 2, 2.0) for _ in range(rng.randint(0, 30))]
        for tol in (0.0, 0.5, 3.0):
            got = match_events(cand, ref, tolerance_sec=tol, require_overlap=False).pairs
            p, t = as_events(cand), as_events(ref)
            used: set[int] = set()
            want = []
            for pi in sorted(range(len(p)), key=lambda i: (p[i].onset_sec, i)):
                best = None
                for ti in range(len(t)):
                    key = abs(p[pi].onset_sec - t[ti].onset_sec)
                    if ti not in used and key <= tol and (best is None or (key, ti) < best):
                        best = (key, ti)
                if best is not None:
                    used.add(best[1])
                    want.append((pi, best[1]))
            assert got == want


def test_matching_stays_quick_with_many_identical_long_reference_events(tmp_path: Path) -> None:
    rng = random.Random(4)  # noqa: S311 (test data, not security)
    cand = tmp_path / "cand.csv"
    cand.write_text("".join(f"{rng.uniform(0, 28790):.2f},5\n" for _ in range(20_000)))
    ref = tmp_path / "ref.csv"
    ref.write_text("0,28800\n" * 20_000)
    t0 = time.perf_counter()
    r = api.compare_events(str(ref), str(cand))
    assert time.perf_counter() - t0 < 20
    assert r["matched"] == 20_000


def test_matching_that_would_take_too_long_is_a_plain_error(tmp_path: Path) -> None:
    """Reference events near every candidate that never overlap it enough: refused, quickly."""
    cand = tmp_path / "cand.csv"
    cand.write_text("10000,5\n" * 20_000)
    ref = tmp_path / "ref.csv"
    ref.write_text(
        "9000,2047\n" + "".join(f"{10187.5 + k * 1e-3:.3f},1025\n" for k in range(20_000))
    )
    t0 = time.perf_counter()
    with pytest.raises(api.LocalToolError, match="would take too long"):
        api.compare_events(str(ref), str(cand), min_overlap=5)
    assert time.perf_counter() - t0 < 20
