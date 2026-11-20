"""Channel labels that are safe to return, a guessed channel type, and unit handling.

EDF header text can hold anything, including names. A label is passed through only when it
is a standard electrode or sensor name; any other label becomes ``ch<N>`` (1-based position).
"""

from __future__ import annotations

import re

#: 10-20 and 10-10 electrode sites, plus reference sites. Upper case.
_SITE = re.compile(
    r"^(?:FP[12Z]|AF[3-478Z]|F(?:[1-9]|10|Z)|FC[1-6Z]|FT(?:[789]|10)|C[1-6Z]|CP[1-6Z]"
    r"|T(?:[3-9]|10)|TP(?:[789]|10)|P(?:[1-9]|10|Z)|PO[3-478Z]|O[12Z]|OZ|IZ|NZ|A[12]|M[12])$"
)
_EOG_TOKENS = {
    "E1", "E2", "EOG", "LOC", "ROC", "EOGL", "EOGR", "EOG1", "EOG2", "LEOG", "REOG",
    "HEOG", "VEOG", "EOGH", "EOGV", "HORIZONTAL", "VERTICAL",
}  # fmt: skip
_EMG_TOKENS = {
    "EMG", "CHIN", "CHIN1", "CHIN2", "CHIN3", "CHINZ", "CHINL", "CHINR", "EMG1", "EMG2", "EMG3",
    "LAT", "RAT", "LLEG", "RLEG", "LEG", "LEGL", "LEGR", "TIB", "LTIB", "RTIB", "SUBMENTAL",
}  # fmt: skip
_ECG_TOKENS = {"ECG", "EKG", "ECG1", "ECG2", "ECG3", "EKG1", "EKG2", "ECGI", "ECGII", "ECGIII"}
_OTHER_NAMES = {
    "AIRFLOW", "FLOW", "NASAL", "CANNULA", "PRESSURE", "NASALPRESSURE", "THERM", "THERMISTOR",
    "THOR", "THORAX", "CHEST", "ABD", "ABDO", "ABDOMEN", "RESP", "RESPTHORAX", "RESPABDOMEN",
    "SPO2", "SAO2", "SPO2%", "PULSE", "PLETH", "HR", "HEARTRATE", "SNORE", "POSITION", "POS",
    "BODY", "TEMP", "TEMPERATURE", "LIGHT", "ACCX", "ACCY", "ACCZ", "ACCELX", "ACCELY",
    "ACCELZ", "X", "Y", "Z", "CO2", "ETCO2", "PAP", "CPAP", "EVENTMARKER", "MARKER",
}  # fmt: skip
_PREFIXES = ("EEG", "EOG", "EMG", "ECG", "EKG", "RESP")

_VOLT_SCALE_TO_UV = {"UV": 1.0, "µV": 1.0, "μV": 1.0, "MV": 1e3, "V": 1e6, "NV": 1e-3}
_KNOWN_UNITS = {
    "uV", "µV", "μV", "mV", "V", "nV", "%", "bpm", "BPM", "degC", "°C", "C", "mmHg", "cmH2O",
    "Ohm", "kOhm", "g", "mg", "m/s2", "m/s^2", "L/s", "l/s", "L/min", "lux", "Hz", "s", "ms",
    "mbar", "Pa", "a.u.", "au", "AU",
}  # fmt: skip


def _squash(s: str) -> str:
    return re.sub(r"[\s_().]+", "", s).upper()


def _token_kind(tok: str) -> str | None:
    t = _squash(tok)
    if _SITE.match(t):
        return "eeg"
    if t in _EOG_TOKENS:
        return "eog"
    if t in _EMG_TOKENS:
        return "emg"
    if t in _ECG_TOKENS:
        return "ecg"
    if t in _OTHER_NAMES:
        return "other"
    return None


def _split(label: str) -> tuple[str | None, str]:
    """Split an optional type prefix such as ``EEG `` off a label."""
    s = label.strip()
    m = re.match(r"^([A-Za-z]{3,4})[\s_:]+(.+)$", s)
    if m and m.group(1).upper() in _PREFIXES:
        return m.group(1).upper(), m.group(2).strip()
    return None, s


def _kinds(label: str) -> tuple[str | None, list[str | None]]:
    prefix, core = _split(label)
    parts = re.split(r"\s*[-:/|]\s*", core) if core else []
    if not parts or len(parts) > 2 or any(not p for p in parts):
        return prefix, [None]
    return prefix, [_token_kind(p) for p in parts]


def is_standard(label: str) -> bool:
    """True when ``label`` is a standard electrode or sensor name (or a derivation of two)."""
    if not label or len(label) > 16 or not label.isascii() or not label.isprintable():
        return False
    _, kinds = _kinds(label)
    return all(k is not None for k in kinds)


def safe_label(label: str, position: int) -> str:
    """``label`` when it is standard, else ``ch<position>`` (``position`` is 1-based)."""
    return label.strip() if is_standard(label) else f"ch{position}"


def guess_type(label: str) -> str:
    """``eeg``, ``eog``, ``emg``, ``ecg`` or ``other``, from the label alone."""
    prefix, kinds = _kinds(label)
    if prefix in ("EOG", "EMG", "ECG"):
        return prefix.lower()
    if prefix == "EKG":
        return "ecg"
    known = [k for k in kinds if k is not None]
    for k in ("eog", "emg", "ecg"):
        if k in known:
            return k
    if known and all(k == "eeg" for k in known):
        return "eeg"
    if prefix == "EEG":
        return "eeg"
    if known and "other" in known:
        return "other"
    # Fall back on a loose read of a non-standard label (never returned, only used to guess).
    up = label.upper()
    for k, words in (
        ("eog", ("EOG", "LOC", "ROC")),
        ("emg", ("EMG", "CHIN")),
        ("ecg", ("ECG", "EKG")),
        ("eeg", ("EEG",)),
    ):
        if any(w in up for w in words):
            return k
    return "other"


def safe_unit(unit: str) -> str:
    """The physical unit when it is a common one, else ``other`` (header text is not echoed)."""
    u = unit.strip()
    if u in _KNOWN_UNITS:
        return u
    if u.upper() == "UV":
        return "uV"
    return "other" if u else ""


def to_microvolts(unit: str) -> float | None:
    """Factor that turns values in ``unit`` into microvolts; ``None`` when not a voltage."""
    u = unit.strip()
    if u in ("µV", "μV"):
        return 1.0
    return _VOLT_SCALE_TO_UV.get(u.upper())
