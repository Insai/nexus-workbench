from __future__ import annotations

import pytest

from nexus_workbench.local.errors import LocalToolError
from nexus_workbench.local.formats import parse_events, parse_hypnogram


@pytest.mark.parametrize(
    "text",
    [
        "W\nN1\nN2\nN3\nR\n?\n",
        "W\nN1\nN2\nN3\nREM\nNS\n",
        "stage\nWake\nS1\nStage 2\nN3\nrem\nunscored\n",
        "epoch,stage\n0,W\n1,N1\n2,N2\n3,N3\n4,R\n5,?\n",
        "epoch;stage\n1;W\n2;N1\n3;N2\n4;N3\n5;R\n6;\n",
        "0,W\n1,N1\n2,N2\n3,N3\n4,R\n5,-1\n",
        "W N1 N2 N3 R ?\n",
        "0\n1\n2\n3\n5\n?\n",  # integer coding with 5 for REM
        "# a comment\nSleep stage W\nSleep stage 1\nSleep stage 2\nSleep stage 3\nSleep stage R\n"
        "Sleep stage ?\n",
    ],
)
def test_hypnogram_layouts(text: str) -> None:
    assert parse_hypnogram(text).tolist() == [0, 1, 2, 3, 4, -1]


def test_blank_lines_inside_are_unscored_and_trailing_ones_are_dropped() -> None:
    assert parse_hypnogram("\nW\n\nN2\n\n\n").tolist() == [0, -1, 2]


def test_old_stage_4_becomes_n3_when_5_is_rem() -> None:
    assert parse_hypnogram("3\n4\n5\n").tolist() == [3, 3, 4]


def test_ambiguous_4_is_refused() -> None:
    with pytest.raises(LocalToolError, match="number 4"):
        parse_hypnogram("0\n2\n4\n")


def test_epoch_gaps_are_unscored() -> None:
    assert parse_hypnogram("epoch,stage\n0,W\n2,N2\n").tolist() == [0, -1, 2]


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("", "empty"),
        ("W\nLights\n", "Line 2"),
        ("epoch,stage\n1,W\n1,N1\n", "do not increase"),
        ("foo,bar\nW,N1\n", "no 'stage' column"),
    ],
)
def test_bad_hypnograms(text: str, match: str) -> None:
    with pytest.raises(LocalToolError, match=match):
        parse_hypnogram(text)


def test_events_csv_with_and_without_header() -> None:
    a = parse_events("onset_sec,duration_sec,label\n10,1.5,spindle\n20.5,0.5,spindle\n")
    b = parse_events("10,1.5,spindle\n20.5,0.5,spindle\n")
    assert a == b
    assert (a[0].onset_sec, a[0].duration_sec, a[0].label) == (10.0, 1.5, "spindle")
    c = parse_events("label,start,end\nspindle,10,11.5\n")
    assert (c[0].onset_sec, c[0].duration_sec) == (10.0, 1.5)
    assert parse_events("5\n7\n")[1].duration_sec == 0.0


def test_events_json() -> None:
    a = parse_events('[{"onset_sec": 10, "duration_sec": 1.5, "label": "x"}]')
    b = parse_events('{"events": [{"onset": 10, "duration": 1.5, "label": "x"}]}')
    c = parse_events('[[10, 1.5, "x"]]')
    assert a == b == c
    assert parse_events("") == []


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ('[{"duration_sec": 1}]', "no onset"),
        ("[1, 2", "could not be parsed"),
        ("onset,duration\n-1,2\n", "negative"),
        ("onset,duration\n1,abc\n", "not a number"),
        ("what,ever\n1,2\n", "no onset column"),
        ('{"x": 1}', "JSON list"),
    ],
)
def test_bad_events(text: str, match: str) -> None:
    with pytest.raises(LocalToolError, match=match):
        parse_events(text)
