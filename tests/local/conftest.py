from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from nexus_workbench.local.edf import OutSignal, write_edf
from nexus_workbench.local.sample import make_sample_night


def write_test_edf(
    path: Path,
    rows: Sequence[np.ndarray],
    fs: float | Sequence[float],
    labels: Sequence[str] | None = None,
    *,
    phys: float = 500.0,
    unit: str = "uV",
    **kw: Any,
) -> Path:
    """Write ``rows`` (µV) as an EDF+ file with a symmetric physical range of ``±phys``."""
    rates = [fs] * len(rows) if isinstance(fs, (int, float)) else list(fs)
    names = labels or [f"C{k + 3}-M{k % 2 + 1}" for k in range(len(rows))]
    sigs = [
        OutSignal(lab, np.asarray(x, float), r, unit, -phys, phys)
        for lab, x, r in zip(names, rows, rates, strict=True)
    ]
    write_edf(path, sigs, **kw)
    return path


@pytest.fixture(scope="session")
def night(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """A one-hour synthetic night shared by the tests."""
    return make_sample_night(str(tmp_path_factory.mktemp("night")), hours=1.0, seed=0)
