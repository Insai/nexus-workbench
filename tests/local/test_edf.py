from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from nexus_workbench.local.edf import EdfFile, OutSignal, read_header, write_edf
from nexus_workbench.local.errors import LocalToolError

from .conftest import write_test_edf


def _rows(n_sec: int = 60) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(1)
    return (
        rng.normal(0, 40, 100 * n_sec),
        rng.normal(0, 10, 50 * n_sec),
        np.linspace(-200, 200, 4 * n_sec),
    )


def test_round_trip_within_quantisation(tmp_path: Path) -> None:
    a, b, c = _rows()
    p = write_test_edf(tmp_path / "rt.edf", [a, b, c], [100, 50, 4], ["C3-M2", "E1-M2", "Pos"])
    with EdfFile(str(p)) as f:
        h = f.header
        assert h.variant == "EDF+C"
        assert h.labels == ("C3-M2", "E1-M2", "Pos", "EDF Annotations")
        assert [h.fs(i) for i in range(3)] == [100.0, 50.0, 4.0]
        assert h.n_records == 60 and h.record_sec == 1.0
        step = 1000.0 / 65535  # one digital step of the ±500 µV range
        for i, x in enumerate((a, b, c)):
            y = h.to_physical(i, f.read_signal(i))
            assert y.shape == x.shape
            assert np.max(np.abs(y - np.clip(x, -500, 500))) <= step / 2 + 1e-9


def test_values_outside_the_range_are_clipped(tmp_path: Path) -> None:
    p = write_test_edf(tmp_path / "clip.edf", [np.full(100, 900.0)], 100)
    with EdfFile(str(p)) as f:
        d = f.read_signal(0)
    assert set(d.tolist()) == {32767}


def test_annotations_round_trip(tmp_path: Path) -> None:
    ann = [(0.0, 0.0, "lights off"), (12.5, 2.25, "arousal"), (59.0, 0.0, "lights on")]
    p = write_test_edf(tmp_path / "ann.edf", [np.zeros(6000)], 100, annotations=ann)
    with EdfFile(str(p)) as f:
        assert f.annotations() == ann


def test_plain_edf_has_no_annotation_signal(tmp_path: Path) -> None:
    p = write_test_edf(tmp_path / "plain.edf", [np.zeros(3000)], 100, edf_plus=False)
    h = read_header(str(p))
    assert h.variant == "EDF" and h.annotation_indices == []
    with EdfFile(str(p)) as f:
        assert f.annotations() == []


def test_epoch_blocks_match_a_whole_read_for_odd_record_lengths(tmp_path: Path) -> None:
    rng = np.random.default_rng(2)
    x = rng.normal(0, 30, 100 * 95)
    y = rng.normal(0, 30, 20 * 95)
    sigs = [
        OutSignal("C3-M2", x, 100, "uV", -500, 500),
        OutSignal("C4-M1", y, 20, "uV", -500, 500),
    ]
    p = tmp_path / "odd.edf"
    write_edf(p, sigs, record_sec=2.5)  # an epoch of 10 s is 4 records
    with EdfFile(str(p)) as f:
        full = {0: f.read_signal(0), 1: f.read_signal(1)}
        n = f.n_epochs(10.0)
        got: dict[int, list[np.ndarray]] = {0: [], 1: []}
        for _, _, blocks in f.iter_epoch_blocks([0, 1], 10.0, block_bytes=1500):
            for i, d in blocks.items():
                got[i].append(d)
    assert n == 9  # 97.5 s written (whole records), 9 whole 10 s epochs
    for i, spe in ((0, 1000), (1, 200)):
        cat = np.concatenate(got[i]).reshape(-1)
        assert np.array_equal(cat, full[i][: n * spe])


def test_record_count_of_minus_one_is_read_from_the_file_size(tmp_path: Path) -> None:
    p = write_test_edf(tmp_path / "m1.edf", [np.zeros(1000)], 100)
    raw = bytearray(p.read_bytes())
    raw[236:244] = b"-1      "
    p.write_bytes(bytes(raw))
    assert read_header(str(p)).n_records == 10


def _broken(tmp_path: Path, name: str, mutate) -> str:  # type: ignore[no-untyped-def]
    p = write_test_edf(tmp_path / name, [np.zeros(1000)], 100)
    raw = bytearray(p.read_bytes())
    raw = mutate(raw)
    p.write_bytes(bytes(raw))
    return str(p)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda r: r[:-300], "truncated"),
        (lambda r: r[:200], "shorter than an EDF header"),
        (lambda r: r[:600], "shorter than its own header"),
        (lambda r: b"X" + r[1:], "version field"),
        (lambda r: b"\xff" + r[1:], "BDF"),
        (lambda r: r[:252] + b"ab  " + r[256:], "number of signals"),
        (lambda r: r[:184] + b"999     " + r[192:], "header size"),
        (lambda r: r[:244] + b"0       " + r[252:], "record duration"),
        (lambda r: r[:236] + b"0       " + r[244:], "no data records"),
    ],
)
def test_corrupt_files_give_a_plain_error(tmp_path: Path, mutate, match: str) -> None:  # type: ignore[no-untyped-def]
    p = _broken(tmp_path, "bad.edf", mutate)
    with pytest.raises(LocalToolError, match=match):
        EdfFile(p)


def test_random_bytes_are_refused_whatever_the_extension(tmp_path: Path) -> None:
    p = tmp_path / "random.edf"
    p.write_bytes(np.random.default_rng(0).integers(0, 256, 4096, dtype=np.uint8).tobytes())
    with pytest.raises(LocalToolError, match="not a valid EDF"):
        EdfFile(str(p))
    q = tmp_path / "text.edf"
    q.write_text("hello\n" * 100)
    with pytest.raises(LocalToolError):
        EdfFile(str(q))


def test_paths_that_are_not_regular_files(tmp_path: Path) -> None:
    with pytest.raises(LocalToolError, match="was not found"):
        EdfFile(str(tmp_path / "missing.edf"))
    with pytest.raises(LocalToolError, match="is a folder"):
        EdfFile(str(tmp_path))
    if os.path.exists("/dev/null"):
        with pytest.raises(LocalToolError, match="not a regular file"):
            EdfFile("/dev/null")
    loop = tmp_path / "loop.edf"
    os.symlink(loop, loop)
    with pytest.raises(LocalToolError, match="loops"):
        EdfFile(str(loop))
    with pytest.raises(LocalToolError, match="No EDF file path"):
        EdfFile("")


def test_errors_name_only_the_base_name(tmp_path: Path) -> None:
    d = tmp_path / "secret_folder"
    d.mkdir()
    with pytest.raises(LocalToolError) as exc:
        EdfFile(str(d / "night.edf"))
    assert "night.edf" in str(exc.value)
    assert "secret_folder" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_header_object_holds_no_identifying_fields(tmp_path: Path) -> None:
    p = write_test_edf(
        tmp_path / "id.edf",
        [np.zeros(1000)],
        100,
        patient="DOE^JANE F 01-JAN-1970 DOE^JANE",
        recording="Startdate 02-MAR-1971 DOE^JANE CLINIC^X",
        start_date="02.03.71",
        start_time="13.14.15",
    )
    h = read_header(str(p))
    text = repr(h)
    for s in ("DOE", "JANE", "1971", "02.03.71", "13.14.15", "CLINIC"):
        assert s not in text
