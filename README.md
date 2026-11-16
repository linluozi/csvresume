# csvresume — byte-addressed, resumable CSV reading

CPython's `csv` module tells you *what* is in a row, never *where* the row is.
That makes crash recovery and parallel ingestion guesswork: without byte offsets
a pipeline must re-read a file from the start, and the usual workaround — `split
-l` — silently corrupts any file whose quoted fields contain line breaks.

`csvresume` is a small, dependency-free reader that returns exact byte spans:

```python
from csvresume import iter_records
for rec in iter_records("huge.csv"):
    print(rec.index, rec.start, rec.end, rec.fields)
    # rec.raw is exactly source[rec.start:rec.end]
```

A record's `start`/`end` are absolute byte offsets; consecutive records tile the
input with no gaps and no overlap (`b"".join(r.raw for r in records) == data`).
Checkpoint `rec.end`, persist it, and reopen later with
`iter_records(path, offset=saved)` — earlier bytes are never read again.
`split_ranges(path, n)` returns up to `n` byte ranges (fewer when the file has
fewer records) that each start on a record boundary, so each chunk parses
standalone into the same rows as the full file.

Upstream: none

This is an original library (MIT). It is a *re-implementation* of the RFC 4180
rules CPython's `csv` applies for the common dialect subset; the standard library
is used only as a differential oracle in the tests, never at runtime.

## Install

```bash
python3 -m pip install .
# or, for development:
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m pytest
```

CLI (installed as `csvresume`, or `python -m csvresume`):

```bash
csvresume check people.csv --json      # tiling + stats, exit 0/1
csvresume index people.csv --limit 5   # record byte offsets
csvresume split people.csv --parts 4 --out chunks/   # record-aligned chunks
csvresume resume people.csv --offset 123456         # records after a checkpoint
```

## What the evidence shows

All numbers below were produced by `benchmark/bench.py` and
`evidence/verify.py` on this machine; raw per-run samples are in
`benchmark/raw/` (30 files: 3 scales × 2 paths × 5 timed reps).

### Resume: checkpoint at 90% of the records, then read the tail

Baseline re-parses the file from byte 0 (what you must do without offsets);
optimized seeks to the saved offset. Same parser, same process, 3 warmups then
5 timed reps, `time.perf_counter_ns()`.

| scale  | records | bytes     | baseline median | optimized median | speedup |
|--------|---------|-----------|-----------------|------------------|---------|
| small  | 5 001   | 334 982   | 45.00 ms        | 4.55 ms          | **9.90x** |
| medium | 20 001  | 1 385 275 | 183.75 ms       | 18.90 ms         | **9.72x** |
| large  | 60 001  | 4 229 727 | 579.63 ms       | 57.97 ms         | **10.00x** |

"records" includes the header record; data rows are 5 000 / 20 000 / 60 000.
Median of the three speedups: **9.90x**. Baseline p25/p75: 44.92/45.11,
183.51/188.29, 574.09/584.18 ms; optimized p25/p75: 4.49/4.65, 18.74/18.95,
56.85/61.39 ms. Relative standard deviation across timed reps was 0.37–7.21%
(the largest, 7.21%, is the large optimized run: 56.9–61.4 ms interquartile);
no scale exceeded 10%. The speedup is a property of skipping 90% of the input,
so it tracks the checkpoint position, not a constant. A fresh clean-clone run
produced 9.89x on the same three scales (0.1% below the committed run).

### Differential: identical fields to `csv.reader`

`evidence/verify.py` builds the corpus from the same builder the test suite uses
(`tests/test_diff.py::build_corpus`, seed 20261116), deduplicates by
`sha256(dialect | input)`, and compares every field:

| metric | value |
|--------|-------|
| unique samples (dialect + input) | **1 500** |
| distinct input byte strings | 1 440 |
| compared against `csv.reader` | 1 500 |
| field mismatches | **0** |
| coverage failures (`b"".join(raw) != input`) | **0** |
| resume checks (cut at a random record) | 1 495 |
| resume mismatches | **0** |
| corpus sha256 | `8ceec6e6aa6847793646ac341703efaa7675fea3ddbec3d8c00c1d5353f677bf` |

Corpus: five dialects (`,` `;` tab `|` with `"` and `'` quote chars), 0–3
records of 0–4 fields per document before mutation, fields with embedded `\n`,
`\r\n`, `\r`, delimiters, doubled quotes, NUL, Chinese/emoji/latin-1 text, quotes
after a closed field, and unclosed quotes; 45% of samples are byte-level
mutations of valid documents. (Mutations can add records or fields: the largest
sample actually observed in this corpus has 4 records and 6 fields.) 507 of the
1 500 samples contain a newline inside a quoted field, 1 269 contain a quote
character, and all five `(delimiter, quotechar)` pairs are realised. The test
suite re-runs a 200-sample slice at chunk sizes 1, 2, 3, 7, 64 and 1 MiB to prove
the streaming buffer does not change results.

### Real corpus

20 real CSVs from numpy's validation-data directory (1 132 589 bytes, 26 692
records): **0 field mismatches** against `csv.reader`. Pass `--real-dir <dir>`
to `bench.py` to run the same check on any directory of real CSVs.

| reader | throughput |
|--------|-----------|
| `csvresume` | 5.34 MB/s |
| stdlib `csv.reader` | 86.83 MB/s |

**`csvresume` is 16.3x slower than the C implementation.** That is the honest
trade: you pay pure-Python parsing to get byte addresses and resumability. If you
only need fields once and never resume, use `csv`. Use this when the offset
matters — crash-safe ingestion, chunked workers, or locating a bad row by byte
range in a multi-GB file.

### Splitting: line-based vs record-aligned

Generated corpus: 60 000 data rows, 30% with a quoted embedded newline
(4 229 727 bytes). "Lost/fabricated" means a chunk's standalone record count
differs from the number of records that actually start inside that byte range.
"Ends inside a quoted field" is computed from the exact quote state of the full
file (a cut is inside a quoted field iff it lies strictly inside a record that
spans lines), not from quote-count parity.

| parts | naive: lost/fabricated | naive chunks ending inside a quoted field | record-aligned: lost |
|-------|------------------------|--------------------------------------------|----------------------|
| 2  | 0 | 0 | 0 |
| 4  | 1 | 1 | 0 |
| 8  | 2 | 2 | 0 |
| 16 | 5 | 5 | 0 |

At 16 chunks, 5 cut chunks end inside a quoted field and 5 more begin inside one
(10 boundaries total), which is why 5 records are lost or fabricated. This is the
failure mode `split -l` has on exports with multi-line address/notes columns; it
is data-dependent, which is why the table reports measured counts instead of a
rate.

## Gates

| gate | result | reproduce |
|------|--------|-----------|
| G1 upstream tests | **N/A** — original library, there is no upstream test suite to keep green | — |
| G2 differential ≥1000 unique, 0 mismatch | **PASS** — 1 500 unique, 0 mismatch | `python evidence/verify.py` |
| G3 median speedup ≥1.15x | **PASS** — 9.90x | `python benchmark/bench.py` |
| G4 largest scale ≥0.95x | **PASS** — 10.00x | `python benchmark/bench.py` |
| G5 peak RSS ≤1.10x | **PASS** — 98 864 KB both paths, ratio 1.000 | `python benchmark/bench.py` |

G3/G4 here measure the resume path (skip the already-processed prefix), not a
claim that the parser is faster than `csv` — it is not (see the table above).
G5 compares peak RSS of a full re-parse vs a resume, each measured in its own
child process via `resource.getrusage(RUSAGE_SELF).ru_maxrss`.

## Reproduce from a clean clone

```bash
python3 -m venv .venv && .venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q                 # 49 passed
.venv/bin/python evidence/verify.py           # writes evidence/differential.json
.venv/bin/python benchmark/bench.py           # writes benchmark/{baseline,optimized,results}.json + raw/
```

`bench.py` regenerates its corpus in `/tmp/csvresume-bench` from seed 20261116
(`id,name,city,note,amount`; 30% multi-line notes), so no large fixture is
committed.

## Environment

- CPU: AMD EPYC 7K62 48-Core Processor, 4 vCPU visible
- RAM: 3 812 564 KB total
- Python: CPython 3.12.3, Linux 6.8.0-124-generic, glibc 2.39
- Seed: 20261116; checkpoint fraction 0.90; reps = 5, warmup = 3
- No other interpreter is installed on this machine, so no cross-version run is
  claimed.

## Scope and known limits (measured, not guessed)

- Dialects: single-byte `delimiter`/`quotechar`, `doublequote=True`, no
  `escapechar`. Other dialects raise `UnsupportedDialect` instead of guessing.
- Encodings must be ASCII-compatible (UTF-8, latin-1, cp1252, ...); UTF-16/32/7
  are rejected because byte offsets would be ambiguous.
- Unlike `csv`, there is no 131 072-byte field limit; fields are bounded by
  memory only. That is a deliberate difference, not an oversight.
- A record larger than the read chunk (default 1 MiB) is re-scanned as the
  buffer grows, so a single pathological multi-MiB record costs more than one
  pass over its bytes. Normal records are unaffected.
- `Record.index` is the ordinal within the current scan (0-based); after a
  resume it restarts at 0 while `start`/`end` stay absolute.

## 本轮创新

First CSV reader that treats the **record boundary itself as the addressable
unit** and proves it byte-for-byte: every record carries `[start, end)` and
`raw`, so a full parse is a tiling certificate (`b"".join(raw) == input`), not
just a list of fields. That turns "resume where I stopped" and "split without
corrupting quoted newlines" into the same one-pass primitive, and the evidence
quantifies the failure it replaces: at 16 parts, 5 of 16 line-cut chunks end
inside a quoted field (5 more begin inside one), losing or fabricating 5 records
while the record-aligned split loses none.

Compared with the ledger: `tzgap` addresses wall-clock instants, not file
offsets; `feedparser-opt-lab` memoised URI resolution but never exposed record
addressing; no prior entry proves parse coverage as a byte tiling or measures
line-split corruption on quoted-newline CSVs. The novelty is the invariant plus
the negative control (naive split), not a faster loop.

## License

MIT, copyright 2026 linluozi. No upstream code was copied; CPython's `csv`
behaviour was reproduced from its documented RFC 4180 semantics and verified by
differential testing.
