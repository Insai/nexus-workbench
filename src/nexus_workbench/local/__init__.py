"""Local EEG file tools: no account, no network.

The tools in :mod:`nexus_workbench.local.api` take paths as text and return JSON-safe
dictionaries. Expected failures raise :class:`LocalToolError` with a message for a person.
Five only read; ``preprocess`` writes a new EDF file and never changes its input.
"""

from .api import (
    check_signal_quality,
    compare_events,
    compare_hypnograms,
    compute_band_power,
    inspect_edf,
    preprocess,
)
from .errors import LocalToolError

__all__ = [
    "LocalToolError",
    "check_signal_quality",
    "compare_events",
    "compare_hypnograms",
    "compute_band_power",
    "inspect_edf",
    "preprocess",
]
