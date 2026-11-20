# Local tools

Five tools that run on files on your own machine. They need no account and no network.
A coding agent calls them through the MCP server; from Python they are in
`nexus_workbench.local.api`.

Every tool takes paths as text and returns a JSON object made only of objects, lists, text,
numbers, `true`, `false` and `null`. A number that cannot be computed is `null`, never NaN.
Every expected failure (missing file, not an EDF, mismatched lengths, unreadable hypnogram)
raises `LocalToolError` with a message that says what happened and what to do.

Rules shared by all five:

- The EDF header's patient field, recording field and start date and time are never read.
  The reader skips those bytes.
- A file is named in results by an opaque id: the first 12 hex characters of the SHA-256 of
  its bytes (`recordingId` for an EDF, `fileId` for a text file). Results never contain a path
  or a file name. Error messages may contain the base name of the file you passed, never the
  folder.
- Channel labels are passed through only when they are standard electrode or sensor names
  (10-20 and 10-10 sites, optionally as an `X-Y` derivation and with an `EEG `, `EOG `, `EMG `
  or `ECG ` prefix; `E1`, `E2`, `LOC`, `ROC`; chin and leg EMG names; `ECG`, `EKG`; common
  respiratory, oximetry and position names). Any other label is shown as `ch<N>`, where N is
  the channel's 1-based position in the file. Units are passed through only when they are
  common units, otherwise `other`.
- No tool returns raw samples or per-epoch arrays. Lists that can grow have a cap, and the
  output says when a cap was hit (`"truncated": true`).
- Same input, same output.
- Only regular files are read. Folders, devices, pipes and symbolic link loops are refused.
  An EDF must have a valid header whatever its extension. Text inputs are limited to 10 MB.

## inspect_edf

Describes an EDF or EDF+ file: how long it is, its channels and whether it holds EDF+
annotations. Use it first, to see channel labels and positions for the other tools.

Arguments:

- `path` (text): the EDF or EDF+ file.

Output (two of four channels shown):

```json
{
  "recordingId": "0401b485d4af",
  "format": "EDF+C",
  "durationSec": 28800.0,
  "recordSec": 1.0,
  "nRecords": 28800,
  "nChannels": 4,
  "channels": [
    {"index": 1, "label": "C3-M2", "type": "eeg", "sampleRateHz": 100.0, "unit": "uV",
     "physicalMin": -500.0, "physicalMax": 500.0, "sampleCount": 2880000},
    {"index": 4, "label": "Chin1-Chin2", "type": "emg", "sampleRateHz": 100.0, "unit": "uV",
     "physicalMin": -250.0, "physicalMax": 250.0, "sampleCount": 2880000}
  ],
  "channelsListed": 4,
  "annotations": {"present": true, "count": 2},
  "discontinuous": false,
  "truncated": false
}
```

- `format` is `EDF`, `EDF+C` (continuous) or `EDF+D` (discontinuous).
- `type` is a guess from the label: `eeg`, `eog`, `emg`, `ecg` or `other`.
- `nChannels` counts data channels; the EDF+ annotation signal is not a channel. Positions
  (`index`) are positions in the file, so they can skip the annotation signal.
- `durationSec` is the number of data records times the record length. For `EDF+D` files the
  recording has gaps, and this is the recorded time, not the wall-clock span.
- Annotations are counted. Their text is never returned.

Limits: at most 128 channels are listed (`channelsListed`, `truncated`). Only 16-bit EDF is
read; 24-bit BDF is refused.

## check_signal_quality

Classes every epoch of every voltage channel as `ok`, `suspect`, `flat` or `railed`, and
reports counts, percentages and the longest bad stretches. Use it before trusting any number
computed from a recording.

Arguments:

- `path` (text): the EDF file.
- `channels` (list, optional): labels as `inspect_edf` shows them, `ch<N>`, or 1-based
  positions. Default: every data channel.
- `epoch_sec` (number, default 30): epoch length in seconds, 1 to 3600. It must be a whole
  number of samples at each channel's rate.

Output (two of four channels shown):

```json
{
  "recordingId": "0401b485d4af",
  "epochSec": 30.0,
  "nEpochs": 960,
  "secondsNotInEpochs": 0.0,
  "thresholds": {
    "flatStdUv": 0.5, "saturatedPeakToPeakUv": 2000.0, "constantRunSec": 1.0,
    "extremeTolerance": 0.001, "suspectExtremeFraction": 0.01, "railedExtremeFraction": 0.25,
    "railLevelUv": 1000.0, "suspectRmsUv": 250.0, "suspectBadEpochFraction": 0.1
  },
  "channels": [
    {"index": 2, "label": "C4-M1", "type": "eeg", "epochs": 960,
     "counts": {"ok": 940, "suspect": 0, "flat": 20, "railed": 0},
     "percent": {"ok": 97.92, "suspect": 0.0, "flat": 2.08, "railed": 0.0},
     "overall": "ok"},
    {"index": 3, "label": "E1-M2", "type": "eog", "epochs": 960,
     "counts": {"ok": 950, "suspect": 0, "flat": 0, "railed": 10},
     "percent": {"ok": 98.96, "suspect": 0.0, "flat": 0.0, "railed": 1.04},
     "overall": "ok"}
  ],
  "skipped": [],
  "usablePercent": 99.22,
  "allChannelsOkPercent": 96.88,
  "badSpans": [
    {"channel": "C4-M1", "startSec": 7200.0, "endSec": 7800.0, "class": "flat"},
    {"channel": "E1-M2", "startSec": 17280.0, "endSec": 17580.0, "class": "railed"}
  ],
  "badSpansTotal": 2,
  "badSpansCap": 20,
  "truncated": false,
  "channelsTruncated": false
}
```

- `usablePercent`: `ok` epochs over all epochs of all checked channels.
- `allChannelsOkPercent`: epochs in which every checked channel is `ok`.
- `overall`: one class for the channel over the whole recording. `railed` or `flat` mean
  railed or flat throughout; `suspect` means more than 10 % of epochs are saturated, flat or
  hold a constant run, or more than 1 % of all samples sit on the channel's extremes;
  otherwise `ok`. A channel can be `ok` overall and still have some bad epochs.
- `badSpans`: runs of consecutive epochs with the same non-ok class, longest first.
- `skipped`: chosen channels whose unit is not a voltage, with the reason.
- `secondsNotInEpochs`: a trailing part shorter than one epoch is not checked.

Method. All thresholds are in microvolts and are applied at the file's own sample rate. An
epoch is `railed` when its peak-to-peak range is above 2000 µV, when a quarter or more of its
samples sit on the channel's own minimum or maximum (within 0.1 % of the channel's range), or
when it is flat or holds a constant run of 1 s or more at a rail level (1000 µV or more from
zero, or the file's digital limit). It is `flat` when its standard deviation is below 0.5 µV
at a level that is not a rail, as from a dead or zero-filled contact. It is `suspect` when it
holds a constant run of 1 s or more, has more than 1 % of samples on the extremes, or has an
RMS above 250 µV. These are default thresholds, not tuned for any device.

Limits: at most 64 channels per call (`channelsTruncated`); at most 20 bad spans
(`badSpansTotal` gives the full count). Channels whose unit is not V, mV, µV or nV are skipped.

## compute_band_power

Mean absolute and relative power in the delta, theta, alpha, sigma and beta bands per
channel, over artifact-free epochs only, and per sleep stage when a hypnogram is given.

Arguments:

- `path` (text): the EDF file.
- `channels` (list, optional): as for `check_signal_quality`. Default: the channels whose label
  looks like EEG; when none does, every voltage channel.
- `hypnogram_path` (text, optional): a hypnogram for the same night with the same epoch
  length (format below). It must cover every whole epoch of the recording; up to two extra
  trailing epochs are ignored.
- `epoch_sec` (number, default 30).
- `bands` (optional): `{"name": [low Hz, high Hz], ...}` or
  `[{"name": ..., "lowHz": ..., "highHz": ...}, ...]`, up to 12 bands. Default:

| Band | Range (Hz) |
|---|---|
| delta | 0.5 to 4 |
| theta | 4 to 8 |
| alpha | 8 to 12 |
| sigma | 12 to 16 |
| beta | 16 to 30 |

Each range includes its lower edge and excludes its upper edge.

Output (one of two channels and two of five stages shown):

```json
{
  "recordingId": "0401b485d4af",
  "epochSec": 30.0,
  "nEpochs": 960,
  "bands": [
    {"name": "delta", "lowHz": 0.5, "highHz": 4.0},
    {"name": "theta", "lowHz": 4.0, "highHz": 8.0},
    {"name": "alpha", "lowHz": 8.0, "highHz": 12.0},
    {"name": "sigma", "lowHz": 12.0, "highHz": 16.0},
    {"name": "beta", "lowHz": 16.0, "highHz": 30.0}
  ],
  "unit": "uV^2",
  "method": {
    "psd": "welch", "window": "hann", "windowSec": 4.0, "overlap": 0.5,
    "bandPower": "sum of PSD bins in [lowHz, highHz) times bin width",
    "relative": "band power over power from the lowest to the highest band edge",
    "average": "mean over epochs classed ok by check_signal_quality"
  },
  "channelsChosen": "channels that look like EEG",
  "channels": [
    {"index": 2, "label": "C4-M1", "type": "eeg", "epochsTotal": 960, "epochsUsed": 940,
     "absolute": [360.4, 72.38, 18.03, 19.6, 10.21],
     "relative": [0.7499, 0.1506, 0.0375, 0.0408, 0.0212],
     "byStage": {
       "W": {"epochs": 40, "absolute": [31.05, 16.65, 173.5, 13.77, 32.21],
             "relative": [0.1162, 0.0623, 0.6493, 0.0516, 0.1206]},
       "N3": {"epochs": 71, "absolute": [2109.0, 140.6, 5.398, 3.636, 3.627],
              "relative": [0.9323, 0.0621, 0.0024, 0.0016, 0.0016]}
     }}
  ],
  "skipped": [],
  "hypnogram": {"fileId": "44a879051ff1", "epochs": 960, "unscored": 0, "extraEpochsIgnored": 0},
  "truncated": false
}
```

- `absolute` and `relative` are in the order of `bands`. Absolute power is in µV² (other
  voltage units are converted to µV first), to four significant digits.
- `epochsUsed` is the number of epochs behind each number: epochs classed `ok` on that channel.
  `byStage` gives the same per stage (`W`, `N1`, `N2`, `N3`, `R`); unscored epochs count in
  the overall numbers but in no stage. A stage with no usable epochs has `epochs: 0` and
  `null` powers.
- `byStage` and `hypnogram` are present only with a hypnogram (`hypnogram` is `null` without).
- `bandsAboveNyquist` lists bands whose upper edge is above half the sample rate; a band that
  starts at or above it is `null`.

Method. Each epoch's power spectral density is estimated with Welch's method: Hann windows of
4 s (shortened to the epoch when it is shorter), half overlap, mean removed per window. Band
power is the density summed over the bins in the band times the bin width. Numbers are means
over the usable epochs; relative power is mean band power over mean power from the lowest
band edge to the highest, so with the default contiguous bands the relative powers sum to 1.
Epochs a quality check classes as anything but `ok` are left out, channel by channel.

Limits: at most 32 channels per call (`truncated`). Only voltage channels.

## compare_hypnograms

Epoch-by-epoch agreement between two hypnograms of the same night: raw agreement, Cohen's
kappa, per-stage precision, recall and F1, and the confusion matrix. `a` is treated as the
reference for precision and recall.

Arguments:

- `path_a`, `path_b` (text): two hypnograms (format below) with the same number of epochs.
- `epoch_sec` (number, default 30): used only for `stageMinutes`.

Output:

```json
{
  "fileIdA": "44a879051ff1",
  "fileIdB": "c2d447733f64",
  "epochSec": 30.0,
  "epochsTotal": 960,
  "epochsCompared": 958,
  "epochsUnscored": 2,
  "agreement": 0.8497,
  "kappa": 0.7421,
  "perStage": {
    "W": {"precision": 1.0, "recall": 0.7368, "f1": 0.8485, "epochsA": 38, "epochsB": 28, "agreed": 28},
    "N1": {"precision": 0.2759, "recall": 0.8889, "f1": 0.4211, "epochsA": 36, "epochsB": 116, "agreed": 32},
    "N2": {"precision": 0.9744, "recall": 0.8514, "f1": 0.9088, "epochsA": 626, "epochsB": 547, "agreed": 533},
    "N3": {"precision": 0.629, "recall": 0.8864, "f1": 0.7358, "epochsA": 88, "epochsB": 124, "agreed": 78},
    "R": {"precision": 1.0, "recall": 0.8412, "f1": 0.9137, "epochsA": 170, "epochsB": 143, "agreed": 143}
  },
  "confusion": {
    "rows": "a",
    "columns": "b",
    "labels": ["W", "N1", "N2", "N3", "R"],
    "matrix": [[28, 10, 0, 0, 0], [0, 32, 4, 0, 0], [0, 47, 533, 46, 0], [0, 0, 10, 78, 0], [0, 27, 0, 0, 143]]
  },
  "stageMinutes": {
    "a": {"W": 20.0, "N1": 18.0, "N2": 313.0, "N3": 44.0, "R": 85.0, "unscored": 0.0},
    "b": {"W": 14.0, "N1": 58.0, "N2": 273.5, "N3": 62.0, "R": 71.5, "unscored": 1.0}
  }
}
```

Method. An epoch unscored in either hypnogram is left out of every number. Agreement is the
share of compared epochs with the same stage. Kappa is (observed agreement minus chance
agreement) over (1 minus chance agreement), with chance agreement from the two sides' stage
proportions. Per stage, precision is agreed epochs over epochs `b` gives that stage, recall
is agreed epochs over epochs `a` gives it, and F1 is 2 × agreed over the sum of the two. A
rate whose denominator is zero is `null`; kappa is `null` when there are no epochs or both
sides use one and the same stage throughout.

Limits: the two files must have the same number of epochs (a mismatch is an error, never a
silent trim). At most 200,000 epochs.

## compare_events

Matches candidate events (for example from a detector) to reference events (for example
from a scorer) one to one, and scores the agreement. Also gives a shuffled null: the F1 you
get by chance from events with the same count and durations at unrelated times.

Arguments:

- `reference_path`, `candidate_path` (text): two event files (format below).
- `min_overlap` (number, default 0): the shortest overlap, in seconds, that counts as a
  match. 0 means any overlap.
- `recording_hours` (number, optional): the recording length. Needed for false positives
  per hour, and used as the span of the shuffled null.
- `n_shuffles` (integer, default 200, 0 to 2000): shuffles for the null.
- `seed` (integer, default 0): seed for the null. Same seed, same null.
- `tolerance_sec` (number, default 0): widens each candidate by this much on both sides
  before overlap is measured. Use it for zero-length events or when onsets are imprecise.
- `label` (text, optional): compare only events with this label (case is ignored).

Output:

```json
{
  "fileIdReference": "1123f08dec77",
  "fileIdCandidate": "7daa561153bb",
  "eventsInFiles": {"reference": 616, "candidate": 533},
  "labelFilter": false,
  "reference": 616,
  "candidate": 533,
  "matched": 493,
  "missed": 123,
  "extra": 40,
  "precision": 0.925,
  "recall": 0.8003,
  "f1": 0.8581,
  "recordingHours": 8.0,
  "falsePositivesPerHour": 5.0,
  "matching": {"rule": "greedy one-to-one, closest midpoint first", "minOverlapSec": 0.0, "toleranceSec": 0.0},
  "shuffledNull": {
    "method": "circular shift of all candidate onsets by one random offset",
    "seed": 0, "nShuffles": 200, "reduced": false,
    "f1Mean": 0.0334, "f1P95": 0.0923, "recallMean": 0.0312
  }
}
```

- `matched` pairs, `missed` reference events with no match, `extra` candidate events with no
  match. Precision is `matched / candidate`, recall `matched / reference`.
- `falsePositivesPerHour` is `extra / recordingHours`, `null` without `recording_hours`.
- Precision is `null` with no candidate events, recall `null` with no reference events, F1
  `null` when both are empty.

Method. Candidates are taken in onset order. Each takes the closest unused reference event
(by distance between midpoints) among those it overlaps by at least `min_overlap` seconds,
after widening by `tolerance_sec`. A zero-length event counts as overlapping an event that
contains it. For the null, all candidate onsets are shifted by one random offset, wrapping
around the end of the recording (or the last event end when `recording_hours` is not given),
and matched again; `f1Mean` and `f1P95` are the mean and 95th percentile over the shuffles.

Limits: at most 200,000 events per file. To keep the null quick, shuffles times events is
kept under 4,000,000; when that lowers the number of shuffles, `reduced` is `true` and
`nShuffles` says how many ran. An event that starts after `recording_hours` is an error.

## File formats

### Hypnogram

Plain text or CSV, one epoch per row, in order. Any of these layouts:

```
W
N1
N2
?
```

```
epoch,stage
0,W
1,N1
2,N2
```

- Stages: `W`, `N1`, `N2`, `N3`, `R`. Also accepted: `Wake`, `REM`, `S1` to `S4` (`S3` and
  `S4` both become N3), `N4`, `Stage 2`, `Sleep stage R`, and any case.
- Integer coding: `0`=W, `1`=N1, `2`=N2, `3`=N3, `5`=REM. `4` is accepted as N3 only when `5`
  also appears; on its own it is refused, because some tools use 4 for REM.
- Unscored: `?`, an empty cell or line, `-1`, `U`, `unscored`, `unknown`, `MT`, `movement`,
  `A`, `artifact`, `NS`.
- Separators: comma, semicolon or tab; or one column; or stages separated by spaces on one
  line. Lines starting with `#` are ignored, as are blank lines at the start and end.
- Header: optional. With several columns, a header must name the stage column `stage` (or
  `label`, `sleep_stage`, `hypnogram`, `score`); an `epoch` column, when present, gives the
  epoch number (0- or 1-based, increasing; missing epochs are unscored). Without a header the
  last column is the stage and a first column of whole numbers is the epoch number.
- Epoch length is not in the file: pass `epoch_sec` if it is not 30 s.

### Events

CSV with `onset_sec,duration_sec[,label]`, seconds from the start of the recording, with or
without a header:

```
onset_sec,duration_sec,label
787.33,1.10,spindle
792.33,1.00,spindle
```

Or JSON: a list of objects with the same keys, or an object with an `events` list:

```json
[{"onset_sec": 787.53, "duration_sec": 1.1, "label": "spindle"}]
```

- Header names accepted: onset (`onset_sec`, `onset`, `start`, `start_sec`, `time`), duration
  (`duration_sec`, `duration`, `dur`), end (`end_sec`, `end`, used when there is no duration),
  label (`label`, `description`, `type`, `kind`, `event`, `name`).
- Without a header the columns are onset, then duration (0 when missing), then label.
- Onsets and durations must be finite and not negative.

## Practical limits

- EDF files are read in blocks of about 8 MB of data records, so memory does not grow with
  the file. Peak memory for one call stays at a few hundred MB whatever the file size; a 2 GB
  file does not need 2 GB of memory.
- `check_signal_quality` reads the data twice (once for each channel's range, once to
  classify), `compute_band_power` twice, and every EDF tool reads the whole file once more to
  compute `recordingId`. Time grows with file size.
- Outputs stay well under 15,000 tokens for a 12-hour, 32-channel file. The caps are stated
  in each section.
- Supported: 16-bit EDF and EDF+ (continuous and discontinuous). Not supported: 24-bit BDF.

For research use only. Not for clinical decision-making.
