from __future__ import annotations

import numpy as np
import pytest

from nexus_workbench.local import staging as st

W, N1, N2, N3, R = range(5)


def test_perfect_agreement() -> None:
    a = np.array([W, N1, N2, N3, R, N2, N2, N3])
    r = st.compare(a, a.copy())
    assert r["epochsCompared"] == 8
    assert r["agreement"] == 1.0 and r["kappa"] == 1.0
    assert all(v["f1"] == 1.0 for v in r["perStage"].values())


def test_unscored_epochs_are_dropped_on_either_side() -> None:
    a = np.array([W, N1, N2, -1, R])
    b = np.array([W, N1, N2, N3, -1])
    r = st.compare(a, b)
    assert r["epochsCompared"] == 3 and r["agreement"] == 1.0


def test_confusion_orientation_rows_a_columns_b() -> None:
    m = st.confusion_matrix(np.array([N2, N2, N2]), np.array([N2, N1, N2]))
    assert m[N2, N2] == 2 and m[N2, N1] == 1 and m.sum() == 3


def test_hand_computed_kappa() -> None:
    # a = W W N2 N2, b = W N2 N2 N2. Observed agreement 3/4. Row totals (a): W 2, N2 2;
    # column totals (b): W 1, N2 3. Chance agreement (2*1 + 2*3) / 16 = 0.5.
    # kappa = (0.75 - 0.5) / (1 - 0.5) = 0.5.
    r = st.compare(np.array([W, W, N2, N2]), np.array([W, N2, N2, N2]))
    assert r["agreement"] == 0.75
    assert r["kappa"] == pytest.approx(0.5)
    # W: tp 1, fn 1, fp 0 -> precision 1, recall 1/2, F1 2/3.
    # N2: tp 2, fn 0, fp 1 -> precision 2/3, recall 1, F1 4/5.
    w, n2 = r["perStage"]["W"], r["perStage"]["N2"]
    assert (w["precision"], w["recall"]) == (1.0, 0.5) and w["f1"] == pytest.approx(2 / 3)
    assert n2["precision"] == pytest.approx(2 / 3) and n2["f1"] == pytest.approx(4 / 5)
    # A stage absent from both sides has no defined rate.
    assert r["perStage"]["R"] == {
        "precision": None,
        "recall": None,
        "f1": None,
        "epochsA": 0,
        "epochsB": 0,
        "agreed": 0,
    }


def test_per_stage_f1_known_case() -> None:
    # 2 true W (1 agreed, 1 called N1); 2 true N1 both agreed. W F1 2/3, N1 F1 4/5.
    r = st.compare(np.array([W, W, N1, N1]), np.array([W, N1, N1, N1]))
    assert r["perStage"]["W"]["f1"] == pytest.approx(2 / 3)
    assert r["perStage"]["N1"]["f1"] == pytest.approx(4 / 5)


def test_kappa_never_exceeds_agreement_and_is_near_zero_for_independent_scorers() -> None:
    rng = np.random.default_rng(0)
    for _ in range(20):
        a = rng.integers(0, 5, size=200)
        b = a.copy()
        flip = rng.random(200) < 0.4
        b[flip] = rng.integers(0, 5, size=int(flip.sum()))
        r = st.compare(a, b)
        assert r["kappa"] <= r["agreement"] + 1e-9
    rng = np.random.default_rng(42)
    r = st.compare(rng.integers(0, 5, 5000), rng.integers(0, 5, 5000))
    assert abs(r["kappa"]) < 0.05


def test_edge_cases() -> None:
    empty = st.compare(np.array([], int), np.array([], int))
    assert empty["epochsCompared"] == 0 and empty["kappa"] is None and empty["agreement"] is None
    one = st.compare(np.full(10, N2), np.full(10, N2))
    assert one["agreement"] == 1.0 and one["kappa"] is None  # chance agreement is 1
    none = st.compare(np.full(10, N2), np.full(10, R))
    assert none["agreement"] == 0.0 and none["kappa"] == 0.0
    assert none["perStage"]["N2"]["f1"] == 0.0
    with pytest.raises(ValueError):
        st.compare(np.zeros(3, int), np.zeros(4, int))
