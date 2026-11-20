"""A small EDF / EDF+ reader and writer on numpy.

Reader
    Validates the fixed header fields, then reads the data in blocks of whole records, so memory
    follows the block size and the channels asked for, not the file size. Only 16-bit EDF is
    supported (not 24-bit BDF).

    The header's patient field (bytes 8 to 88), recording field (88 to 168) and start date and
    time (168 to 184) are never read: the reader seeks past them. Nothing in this module can
    return them.

Writer
    Writes EDF+C (with one ``EDF Annotations`` signal) or plain EDF, 16-bit samples. It writes
    only what it is given. By default the patient and recording fields hold the EDF+ "unknown"
    placeholders and the start date and time are the conventional placeholder 01.01.85 00.00.00.
"""

from __future__ import annotations

import io
import math
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import numpy as np

from .errors import LocalToolError
from .report import check_regular_file, display_name

ANNOT_LABEL = "EDF Annotations"
#: Read about this many bytes of data records at a time.
BLOCK_BYTES = 8 << 20

_INT = re.compile(r"^[+-]?\d+$")
_FLOAT = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class Header:
    """The parts of an EDF header this package reads. No identifying fields."""

    n_signals: int
    n_records: int
    record_sec: float
    variant: str  # "EDF", "EDF+C" or "EDF+D"
    labels: tuple[str, ...]
    units: tuple[str, ...]
    phys_min: tuple[float, ...]
    phys_max: tuple[float, ...]
    dig_min: tuple[int, ...]
    dig_max: tuple[int, ...]
    samples_per_record: tuple[int, ...]
    header_bytes: int

    def fs(self, i: int) -> float:
        return self.samples_per_record[i] / self.record_sec

    @property
    def duration_sec(self) -> float:
        return self.n_records * self.record_sec

    @property
    def record_samples(self) -> int:
        return int(sum(self.samples_per_record))

    @property
    def record_bytes(self) -> int:
        return 2 * self.record_samples

    @property
    def offsets(self) -> tuple[int, ...]:
        out = [0]
        for n in self.samples_per_record:
            out.append(out[-1] + n)
        return tuple(out)

    def is_annotation(self, i: int) -> bool:
        return self.labels[i] == ANNOT_LABEL

    @property
    def signal_indices(self) -> list[int]:
        """0-based positions of the data signals (everything but annotation signals)."""
        return [i for i in range(self.n_signals) if not self.is_annotation(i)]

    @property
    def annotation_indices(self) -> list[int]:
        return [i for i in range(self.n_signals) if self.is_annotation(i)]

    def gain(self, i: int) -> float:
        return (self.phys_max[i] - self.phys_min[i]) / (self.dig_max[i] - self.dig_min[i])

    def to_physical(self, i: int, d: np.ndarray) -> np.ndarray:
        """Digital samples of signal ``i`` to physical units, as float64."""
        return self.phys_min[i] + (d.astype(np.float64) - self.dig_min[i]) * self.gain(i)


def _bad(name: str, what: str) -> LocalToolError:
    return LocalToolError(
        f"{name} is not a valid EDF file ({what}). Check that it is an EDF or EDF+ recording "
        "and that it was copied completely."
    )


def _num_fields(raw: bytes, ns: int, offset: int, width: int) -> list[str]:
    return [
        raw[offset + i * width : offset + (i + 1) * width].decode("latin-1").strip()
        for i in range(ns)
    ]


def _parse_int(s: str, name: str, field: str) -> int:
    if not _INT.match(s):
        raise _bad(name, f"the {field} field is not a whole number")
    return int(s)


def _parse_float(s: str, name: str, field: str) -> float:
    if not _FLOAT.match(s):
        raise _bad(name, f"the {field} field is not a number")
    v = float(s)
    if not math.isfinite(v):
        raise _bad(name, f"the {field} field is not a finite number")
    return v


def read_header(path: str, file_size: int | None = None) -> Header:
    """Read and validate the header of the EDF file at ``path`` (already checked to be a file)."""
    name = display_name(path)
    size = os.stat(path).st_size if file_size is None else file_size
    if size < 256:
        raise _bad(name, "the file is shorter than an EDF header")
    try:
        with open(path, "rb") as f:
            version = f.read(8)
            # Bytes 8 to 184 hold the patient field, the recording field and the start date and
            # time. They are skipped, never read.
            f.seek(184)
            fixed = f.read(72)
            if version[:1] == b"\xff":
                raise LocalToolError(
                    f"{name} looks like a 24-bit BDF file. Only 16-bit EDF and EDF+ are "
                    "supported; convert it to EDF first."
                )
            if version.strip(b" ") != b"0":
                raise _bad(name, "the version field is not 0")
            try:
                text = fixed.decode("ascii")
            except UnicodeDecodeError:
                raise _bad(name, "the fixed header fields are not plain text") from None
            hdr_bytes = _parse_int(text[0:8].strip(), name, "header size")
            reserved = text[8:52].strip()
            n_rec = _parse_int(text[52:60].strip(), name, "number of records")
            rec_sec = _parse_float(text[60:68].strip(), name, "record duration")
            ns = _parse_int(text[68:72].strip(), name, "number of signals")
            if ns < 1:
                raise _bad(name, "it declares no signals")
            if hdr_bytes != 256 * (ns + 1):
                raise _bad(name, "the header size does not match the number of signals")
            if size < hdr_bytes:
                raise _bad(name, "the file is shorter than its own header")
            f.seek(256)
            raw = f.read(256 * ns)
    except OSError:
        raise LocalToolError(f"{name} could not be read. Check the file's permissions.") from None
    if len(raw) != 256 * ns:
        raise _bad(name, "the signal header is incomplete")
    if rec_sec <= 0:
        raise _bad(name, "the record duration is not positive")

    o = 0
    labels = _num_fields(raw, ns, o, 16)
    o += 16 * ns + 80 * ns  # labels, then transducer type (not read)
    units = _num_fields(raw, ns, o, 8)
    o += 8 * ns
    pmin = [_parse_float(s, name, "physical minimum") for s in _num_fields(raw, ns, o, 8)]
    o += 8 * ns
    pmax = [_parse_float(s, name, "physical maximum") for s in _num_fields(raw, ns, o, 8)]
    o += 8 * ns
    dmin = [_parse_int(s, name, "digital minimum") for s in _num_fields(raw, ns, o, 8)]
    o += 8 * ns
    dmax = [_parse_int(s, name, "digital maximum") for s in _num_fields(raw, ns, o, 8)]
    o += 8 * ns
    o += 80 * ns  # prefiltering (not read)
    nsamp = [_parse_int(s, name, "samples per record") for s in _num_fields(raw, ns, o, 8)]

    for i in range(ns):
        if nsamp[i] < 1:
            raise _bad(name, f"signal {i + 1} has no samples per record")
        if not (-32768 <= dmin[i] < dmax[i] <= 32767):
            raise _bad(name, f"signal {i + 1} has an invalid 16-bit digital range")
        if pmin[i] == pmax[i]:
            raise _bad(name, f"signal {i + 1} has an empty physical range")

    rec_bytes = 2 * sum(nsamp)
    if n_rec == -1:
        n_rec = (size - hdr_bytes) // rec_bytes
    if n_rec < 1:
        raise LocalToolError(f"{name} holds no data records. There is nothing to analyse.")
    if size < hdr_bytes + n_rec * rec_bytes:
        have = (size - hdr_bytes) // rec_bytes
        raise LocalToolError(
            f"{name} is truncated: its header promises {n_rec} data records but the file holds "
            f"{have}. Copy the file again or export it again from the recorder."
        )
    if reserved.startswith("EDF+C"):
        variant = "EDF+C"
    elif reserved.startswith("EDF+D"):
        variant = "EDF+D"
    else:
        variant = "EDF"
    return Header(
        n_signals=ns,
        n_records=n_rec,
        record_sec=rec_sec,
        variant=variant,
        labels=tuple(labels),
        units=tuple(units),
        phys_min=tuple(pmin),
        phys_max=tuple(pmax),
        dig_min=tuple(dmin),
        dig_max=tuple(dmax),
        samples_per_record=tuple(nsamp),
        header_bytes=hdr_bytes,
    )


def parse_tals(raw: bytes) -> list[tuple[float, float, str]]:
    """Parse EDF+ time-stamped annotation lists. Time-keeping entries (no text) are dropped."""
    out: list[tuple[float, float, str]] = []
    for tal in raw.split(b"\x00"):
        if not tal:
            continue
        parts = tal.split(b"\x14")
        head = parts[0]
        on, _, dur = head.partition(b"\x15")
        try:
            onset = float(on.decode("latin-1"))
            duration = float(dur.decode("latin-1")) if dur else 0.0
        except ValueError:
            continue
        for txt in parts[1:]:
            text = txt.decode("utf-8", "replace").strip()
            if text:
                out.append((onset, duration, text))
    return out


class EdfFile:
    """An open EDF file. Use as a context manager.

    ``EdfFile(path)`` checks that ``path`` is a regular file and validates the header; it raises
    :class:`LocalToolError` otherwise.
    """

    def __init__(self, path: str) -> None:
        self.path = check_regular_file(path, "EDF file")
        self.name = display_name(self.path)
        self.header = read_header(self.path)
        self._f: io.BufferedReader | None = None

    def __enter__(self) -> EdfFile:
        try:
            self._f = open(self.path, "rb")  # noqa: SIM115 (closed in __exit__)
        except OSError:
            raise LocalToolError(
                f"{self.name} could not be opened. Check the file's permissions."
            ) from None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None

    # -- low level -----------------------------------------------------------------------------

    def read_records(self, start: int, stop: int) -> np.ndarray:
        """Records ``[start, stop)`` as an int16 array of shape ``(n, record_samples)``."""
        if self._f is None:
            raise RuntimeError("EdfFile is not open; use it as a context manager")
        h = self.header
        n = stop - start
        self._f.seek(h.header_bytes + start * h.record_bytes)
        buf = self._f.read(n * h.record_bytes)
        if len(buf) != n * h.record_bytes:
            raise LocalToolError(
                f"{self.name} ended early while reading; the file may have changed or been "
                "truncated. Copy it again and retry."
            )
        return np.frombuffer(buf, dtype="<i2").reshape(n, h.record_samples)

    def samples_per_epoch(self, i: int, epoch_sec: float) -> int:
        k = epoch_sec * self.header.fs(i)
        if abs(k - round(k)) > 1e-6 or round(k) < 1:
            raise LocalToolError(
                f"An epoch of {epoch_sec:g} s is not a whole number of samples at "
                f"{self.header.fs(i):g} Hz on channel {i + 1}. Choose an epoch length that is."
            )
        return int(round(k))

    def n_epochs(self, epoch_sec: float) -> int:
        """Whole epochs of ``epoch_sec`` in the recording (a trailing partial epoch is dropped)."""
        return int(math.floor(self.header.duration_sec / epoch_sec + 1e-9))

    def iter_epoch_blocks(
        self, indices: Sequence[int], epoch_sec: float, block_bytes: int = BLOCK_BYTES
    ) -> Iterator[tuple[int, int, dict[int, np.ndarray]]]:
        """Yield ``(first_epoch, stop_epoch, {index: int16 array (n_epochs, samples)})``.

        Reads each block of records once for all the channels asked for.
        """
        h = self.header
        n = self.n_epochs(epoch_sec)
        if n == 0 or not indices:
            return
        spe = {i: self.samples_per_epoch(i, epoch_sec) for i in indices}
        off = h.offsets
        per_epoch_bytes = max(1.0, epoch_sec / h.record_sec) * h.record_bytes
        step = max(1, int(block_bytes // (per_epoch_bytes + h.record_bytes)))
        for e0 in range(0, n, step):
            e1 = min(n, e0 + step)
            spans: dict[int, tuple[int, int, int]] = {}
            for i in indices:
                spr = h.samples_per_record[i]
                a, b = e0 * spe[i], e1 * spe[i]
                spans[i] = (a, a // spr, -(-b // spr))
            ra = min(s[1] for s in spans.values())
            rb = min(h.n_records, max(s[2] for s in spans.values()))
            recs = self.read_records(ra, rb)
            out: dict[int, np.ndarray] = {}
            for i in indices:
                spr = h.samples_per_record[i]
                a, ra_i, rb_i = spans[i]
                seg = recs[ra_i - ra : rb_i - ra, off[i] : off[i] + spr].reshape(-1)
                k = a - ra_i * spr
                out[i] = seg[k : k + (e1 - e0) * spe[i]].reshape(e1 - e0, spe[i]).copy()
            yield e0, e1, out

    def read_signal(self, i: int) -> np.ndarray:
        """The whole of signal ``i`` as digital int16 (small files and tests only)."""
        h = self.header
        off = h.offsets
        parts = []
        step = max(1, BLOCK_BYTES // h.record_bytes)
        for r0 in range(0, h.n_records, step):
            recs = self.read_records(r0, min(h.n_records, r0 + step))
            parts.append(recs[:, off[i] : off[i + 1]].reshape(-1).copy())
        return np.concatenate(parts)

    def annotations(self) -> list[tuple[float, float, str]]:
        """All EDF+ annotations as ``(onset_sec, duration_sec, text)``. Internal use only."""
        h = self.header
        idx = h.annotation_indices
        if not idx:
            return []
        off = h.offsets
        out: list[tuple[float, float, str]] = []
        step = max(1, BLOCK_BYTES // h.record_bytes)
        for r0 in range(0, h.n_records, step):
            recs = self.read_records(r0, min(h.n_records, r0 + step))
            for i in idx:
                block = recs[:, off[i] : off[i + 1]]
                for r in range(block.shape[0]):
                    out.extend(parse_tals(block[r].tobytes()))
        return out


# ----------------------------------------------------------------------------- writer


@dataclass
class OutSignal:
    """One channel to write. ``data`` is in physical units and is clipped to the physical range."""

    label: str
    data: np.ndarray
    fs: float
    unit: str
    phys_min: float
    phys_max: float
    dig_min: int = -32768
    dig_max: int = 32767


def _pad(s: str, w: int) -> bytes:
    b = s.encode("latin-1", "replace")[:w]
    return b + b" " * (w - len(b))


def _num(x: float, w: int = 8) -> str:
    s = f"{x:.6g}"
    if len(s) > w:
        s = f"{x:.{max(0, w - 6)}e}"
    if len(s) > w:
        s = str(int(round(x)))[:w]
    return s


def digitise(
    x: np.ndarray, phys_min: float, phys_max: float, dig_min: int = -32768, dig_max: int = 32767
) -> np.ndarray:
    """Physical to int16 with the header's own 8-character range. NaN becomes 0 physical."""
    pmin, pmax = float(_num(phys_min)), float(_num(phys_max))
    y = np.asarray(x, np.float64)
    y = np.where(np.isfinite(y), y, 0.0)
    y = np.clip(y, min(pmin, pmax), max(pmin, pmax))
    g = (dig_max - dig_min) / (pmax - pmin)
    return np.clip(np.round((y - pmin) * g + dig_min), dig_min, dig_max).astype("<i2")


def _tal(onset: float, duration: float, text: str) -> bytes:
    on = f"{onset:+.4f}".rstrip("0").rstrip(".")
    du = f"\x15{duration:.4f}".rstrip("0").rstrip(".") if duration > 0 else ""
    clean = text.replace("\x14", " ").replace("\x15", " ").replace("\x00", " ")
    return (on + du + "\x14" + clean + "\x14\x00").encode("utf-8")


def write_edf(
    path: str | os.PathLike[str],
    signals: Sequence[OutSignal],
    annotations: Sequence[tuple[float, float, str]] = (),
    *,
    edf_plus: bool = True,
    record_sec: float = 1.0,
    patient: str = "X X X X",
    recording: str = "Startdate X X X X",
    start_date: str = "01.01.85",
    start_time: str = "00.00.00",
) -> None:
    """Write an EDF+C file (or plain EDF with ``edf_plus=False``).

    Every signal must have a whole number of samples per record. With ``edf_plus=False``,
    ``annotations`` must be empty.
    """
    if not signals:
        raise ValueError("at least one signal is needed")
    if not edf_plus and annotations:
        raise ValueError("plain EDF cannot hold annotations")
    if not re.fullmatch(r"\d\d\.\d\d\.\d\d", start_date) or not re.fullmatch(
        r"\d\d\.\d\d\.\d\d", start_time
    ):
        raise ValueError("start_date and start_time must look like dd.mm.yy and hh.mm.ss")
    spr: list[int] = []
    for s in signals:
        k = s.fs * record_sec
        if abs(k - round(k)) > 1e-9 or round(k) < 1:
            raise ValueError(f"{s.label}: {s.fs} Hz is not a whole number per {record_sec} s")
        spr.append(int(round(k)))
    n_rec = max(int(math.ceil(len(s.data) / n)) for s, n in zip(signals, spr, strict=True))

    labels = [s.label for s in signals] + ([ANNOT_LABEL] if edf_plus else [])
    if len(set(labels)) != len(labels):
        raise ValueError("duplicate channel labels")
    ns = len(labels)

    # Annotation records: the first TAL of each record is its time-keeping stamp; events go into
    # the earliest records with room for them.
    budget = 0
    per_rec: list[bytes] = []
    stamps: list[bytes] = []
    if edf_plus:
        events = [_tal(on, du, txt) for on, du, txt in sorted(annotations)]
        stamps = [f"+{r * record_sec:g}\x14\x14\x00".encode() for r in range(n_rec)]
        need = max(len(x) for x in stamps)
        budget = max(32, need + max((len(e) for e in events), default=0))
        budget += budget % 2
        per_rec = [b"" for _ in range(n_rec)]
        r = 0
        for e in events:
            while r < n_rec and len(stamps[r]) + len(per_rec[r]) + len(e) > budget:
                r += 1
            if r >= n_rec:
                raise ValueError("too many annotations for the records available")
            per_rec[r] += e

    hdr = io.BytesIO()
    hdr.write(_pad("0", 8))
    hdr.write(_pad(patient, 80))
    hdr.write(_pad(recording, 80))
    hdr.write(_pad(start_date, 8))
    hdr.write(_pad(start_time, 8))
    hdr.write(_pad(str(256 * (ns + 1)), 8))
    hdr.write(_pad("EDF+C" if edf_plus else "", 44))
    hdr.write(_pad(str(n_rec), 8))
    hdr.write(_pad(f"{record_sec:g}", 8))
    hdr.write(_pad(str(ns), 4))
    ann = [(ANNOT_LABEL, "", -1.0, 1.0, -32768, 32767, budget // 2)] if edf_plus else []
    cols = [
        (s.label, s.unit, s.phys_min, s.phys_max, s.dig_min, s.dig_max, n)
        for s, n in zip(signals, spr, strict=True)
    ] + ann
    for c in cols:
        hdr.write(_pad(c[0], 16))
    for _ in cols:
        hdr.write(_pad("", 80))  # transducer type: blank on purpose
    for c in cols:
        hdr.write(_pad(c[1], 8))
    for c in cols:
        hdr.write(_pad(_num(c[2]), 8))
    for c in cols:
        hdr.write(_pad(_num(c[3]), 8))
    for c in cols:
        hdr.write(_pad(str(c[4]), 8))
    for c in cols:
        hdr.write(_pad(str(c[5]), 8))
    for _ in cols:
        hdr.write(_pad("", 80))  # prefiltering: blank
    for c in cols:
        hdr.write(_pad(str(c[6]), 8))
    for _ in cols:
        hdr.write(_pad("", 32))
    head = hdr.getvalue()
    if len(head) != 256 * (ns + 1):
        raise AssertionError("header size")

    blocks: list[np.ndarray] = []
    for s, n in zip(signals, spr, strict=True):
        d = digitise(s.data, s.phys_min, s.phys_max, s.dig_min, s.dig_max)
        full = np.empty(n_rec * n, "<i2")
        full[: len(d)] = d
        full[len(d) :] = digitise(np.zeros(1), s.phys_min, s.phys_max, s.dig_min, s.dig_max)[0]
        blocks.append(full.reshape(n_rec, n))
    if edf_plus:
        ab = np.zeros((n_rec, budget), np.uint8)
        for r in range(n_rec):
            a = stamps[r] + per_rec[r]
            ab[r, : len(a)] = np.frombuffer(a, np.uint8)
        blocks.append(ab.view("<i2"))
    data = np.ascontiguousarray(np.hstack(blocks))
    with open(path, "wb") as f:
        f.write(head)
        f.write(data.tobytes())
