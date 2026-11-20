"""Agreement between two hypnograms: raw agreement, Cohen's kappa, per-stage precision, recall
and F1, and the confusion matrix.

Stages are coded ``0=W 1=N1 2=N2 3=N3 4=R``. Any negative code means unscored; an epoch
unscored on either side is left out of every number.
"""

from __future__ import annotations

from typing import Any

import numpy as np

STAGES = ("W", "N1", "N2", "N3", "R")
UNSCORED = -1


def confusion_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """``C[i, j]`` = number of epochs where ``a`` says stage ``i`` and ``b`` says stage ``j``.

    ``a`` and ``b`` must have the same length; epochs negative on either side are dropped.
    """
    a = np.asarray(a, dtype=int)
    b = np.asarray(b, dtype=int)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.size} and {b.size} epochs")
    keep = (a >= 0) & (b >= 0)
    a, b = a[keep], b[keep]
    if a.size and (a.max() >= len(STAGES) or b.max() >= len(STAGES)):
        raise ValueError("stage codes must be 0 to 4, or negative for unscored")
    m = np.zeros((len(STAGES), len(STAGES)), dtype=int)
    np.add.at(m, (a, b), 1)
    return m


def kappa_from_confusion(m: np.ndarray) -> float | None:
    """Cohen's kappa from a confusion matrix. ``None`` when undefined (no epochs, or chance
    agreement is 1 because both sides use one and the same single stage)."""
    m = np.asarray(m, dtype=float)
    n = m.sum()
    if n == 0:
        return None
    p_o = np.trace(m) / n
    p_e = float((m.sum(axis=1) * m.sum(axis=0)).sum()) / (n * n)
    if np.isclose(p_e, 1.0):
        return None
    return float((p_o - p_e) / (1.0 - p_e))


def agreement_from_confusion(m: np.ndarray) -> float | None:
    """Share of compared epochs where both sides agree; ``None`` with no epochs."""
    n = int(np.asarray(m).sum())
    return float(np.trace(m)) / n if n else None


def per_stage_from_confusion(m: np.ndarray) -> dict[str, dict[str, Any]]:
    """Per stage, with ``a`` as the reference: precision, recall, F1 and the counts behind them.

    A rate whose denominator is zero is ``None`` (undefined), not 0.
    """
    m = np.asarray(m, dtype=int)
    out: dict[str, dict[str, Any]] = {}
    for i, name in enumerate(STAGES):
        tp = int(m[i, i])
        support = int(m[i, :].sum())
        predicted = int(m[:, i].sum())
        fn, fp = support - tp, predicted - tp
        denom = 2 * tp + fp + fn
        out[name] = {
            "precision": tp / predicted if predicted else None,
            "recall": tp / support if support else None,
            "f1": 2 * tp / denom if denom else None,
            "epochsA": support,
            "epochsB": predicted,
            "agreed": tp,
        }
    return out


def compare(a: np.ndarray, b: np.ndarray) -> dict[str, Any]:
    """All agreement numbers for two equal-length hypnograms (codes as in this module)."""
    a = np.asarray(a, dtype=int)
    b = np.asarray(b, dtype=int)
    m = confusion_matrix(a, b)
    n = int(m.sum())
    return {
        "epochsCompared": n,
        "agreement": agreement_from_confusion(m),
        "kappa": kappa_from_confusion(m),
        "perStage": per_stage_from_confusion(m),
        "confusion": m,
    }
