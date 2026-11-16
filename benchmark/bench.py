#!/usr/bin/env python3
"""Benchmark: checkpointed resume vs re-parsing from byte 0.

Two things are measured, both on the same machine, same process, same parser:

1. resume cost: for a checkpoint at 90% of the records, the baseline re-parses
   the whole file and discards the already-processed prefix; the optimized run
   starts at the saved byte offset.  This is the operation a pipeline performs
   after a crash or when it processes a file in scheduled slices.
2. split correctness: record-aligned ranges vs naive line-based ranges on a
   corpus whose fields contain embedded line breaks.

Everything is written to ``benchmark/raw/<impl>-<scale>-<i>.json`` first; the
JSON summaries are derived from those files, never typed in by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from csvresume import (  # noqa: E402
    iter_records,
    naive_line_split_ranges,
    scan_bytes,
    split_ranges,
)

SEED = 20261116
SCALES = {
    # rows, fraction of rows whose note contains an embedded newline
    "small": (5_000, 0.30),
    "medium": (20_000, 0.30),
    "large": (60_000, 0.30),
}
CUT = 0.90  # checkpoint position, as a fraction of records


def make_csv(path: str, rows: int, multiline_fraction: float, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    out = ['id,name,city,note,amount\n']
    for i in range(rows):
        if rng.random() < multiline_fraction:
            note = f"first line {i}\nsecond line, with comma {i}"
        else:
            note = f"single line note {i}"
        city = rng.choice(["上海", "Zürich", "São Paulo", "Springfield", "東京"])
        out.append(
            f'{i},"person {i}","{city}","{note}",{rng.randrange(100000) / 100:.2f}\n'
        )
    data = "".join(out).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return {
        "path": path,
        "bytes": len(data),
        "rows": rows,
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def timed(fn, reps: int, warmup: int):
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(reps):
        t0 = time.perf_counter_ns()
        value = fn()
        t1 = time.perf_counter_ns()
        samples.append(t1 - t0)
    return samples, value


def stats(samples):
    ordered = sorted(samples)
    med = statistics.median(ordered)
    p25 = ordered[len(ordered) // 4]
    p75 = ordered[(3 * len(ordered)) // 4]
    mean = statistics.fmean(ordered)
    sd = statistics.stdev(ordered) if len(ordered) > 1 else 0.0
    return {
        "samples_ns": ordered,
        "median_ns": int(med),
        "p25_ns": int(p25),
        "p75_ns": int(p75),
        "mean_ns": int(mean),
        "rsd_pct": round(100.0 * sd / mean, 3) if mean else 0.0,
        "n": len(ordered),
    }


def dump_raw(impl: str, scale: str, i: int, payload: dict, raw_dir: str) -> None:
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, f"{impl}-{scale}-{i}.json"), "w") as fh:
        json.dump(payload, fh, indent=2)


def env_info() -> dict:
    cpu = "unknown"
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    mem_kb = None
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    mem_kb = int(line.split()[1])
                    break
    except OSError:
        pass
    return {
        "cpu": cpu,
        "cpu_count": os.cpu_count(),
        "mem_total_kb": mem_kb,
        "python": sys.version.split()[0],
        "python_impl": platform.python_implementation(),
        "platform": platform.platform(),
        "seed": SEED,
        "cut_fraction": CUT,
        "command": " ".join(sys.argv),
    }


def mem_probe(mode: str, path: str, cut: float) -> dict:
    import resource

    records = list(iter_records(path))
    offset = records[int(len(records) * cut)].start
    del records
    count = 0
    if mode == "full":
        for _ in iter_records(path):
            count += 1
    else:
        for _ in iter_records(path, offset=offset):
            count += 1
    peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"mode": mode, "records_counted": count, "peak_rss_kb": peak_kb}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--corpus-dir", default="/tmp/csvresume-bench")
    parser.add_argument("--real-dir", default=None,
                        help="directory of real .csv files for the equivalence check")
    parser.add_argument("--raw-dir", default=os.path.join(HERE, "raw"))
    parser.add_argument("--out-dir", default=HERE)
    parser.add_argument("--mem-probe", choices=["full", "resume"], help=argparse.SUPPRESS)
    parser.add_argument("--cut", type=float, default=CUT)
    args = parser.parse_args(argv)

    if args.mem_probe:
        print(json.dumps(mem_probe(args.mem_probe, os.path.join(args.corpus_dir, "large.csv"), args.cut)))
        return 0

    os.makedirs(args.corpus_dir, exist_ok=True)
    os.makedirs(args.raw_dir, exist_ok=True)
    corpora = {}
    for name, (rows, frac) in SCALES.items():
        corpora[name] = make_csv(os.path.join(args.corpus_dir, f"{name}.csv"), rows, frac)

    result = {
        "env": env_info(),
        "corpora": corpora,
        "resume": {},
        "split": {},
        "real_corpus": None,
        "memory": {},
    }

    for scale, corpus in corpora.items():
        path = corpus["path"]
        records = list(iter_records(path))
        cut_index = int(len(records) * args.cut)
        offset = records[cut_index].start
        expected_tail = len(records) - cut_index
        del records

        def baseline():
            n = 0
            for rec in iter_records(path):
                if rec.start >= offset:
                    n += 1
            return n

        def optimized():
            n = 0
            for _ in iter_records(path, offset=offset):
                n += 1
            return n

        base_samples, base_n = timed(baseline, args.reps, args.warmup)
        opt_samples, opt_n = timed(optimized, args.reps, args.warmup)
        assert base_n == expected_tail == opt_n, (base_n, opt_n, expected_tail)

        for i, sample in enumerate(base_samples):
            dump_raw("restart", scale, i, {"scale": scale, "impl": "restart", "sample_ns": sample,
                                           "offset": offset, "records": base_n}, args.raw_dir)
        for i, sample in enumerate(opt_samples):
            dump_raw("resume", scale, i, {"scale": scale, "impl": "resume", "sample_ns": sample,
                                          "offset": offset, "records": opt_n}, args.raw_dir)

        base_stats = stats(base_samples)
        opt_stats = stats(opt_samples)
        speedup = base_stats["median_ns"] / opt_stats["median_ns"]
        result["resume"][scale] = {
            "bytes": corpus["bytes"],
            "records": base_n + cut_index,
            "checkpoint_record": cut_index,
            "checkpoint_offset": offset,
            "records_after_checkpoint": expected_tail,
            "restart": base_stats,
            "resume": opt_stats,
            "speedup": round(speedup, 3),
        }
        print(f"resume {scale}: {base_stats['median_ns']/1e6:.2f} ms -> "
              f"{opt_stats['median_ns']/1e6:.2f} ms = {speedup:.2f}x")

    # Split correctness: a naive line-based split cuts inside quoted newlines.
    large_path = corpora["large"]["path"]
    import bisect

    full = list(iter_records(large_path))
    full_starts = [r.start for r in full]
    full_ends = [r.end for r in full]
    full_multiline = [r.spans_lines for r in full]

    def cut_inside_quoted_field(offset: int) -> bool:
        """A cut is inside a quoted field iff it is strictly inside a record
        that spans lines (record is the only place a raw newline can hide)."""
        idx = bisect.bisect_right(full_starts, offset) - 1
        return (0 <= idx < len(full) and full_starts[idx] < offset < full_ends[idx]
                and full_multiline[idx])
    for parts in (2, 4, 8, 16):
        aligned = split_ranges(large_path, parts)
        naive = naive_line_split_ranges(large_path, parts)

        def misparse(ranges):
            lost = 0
            parity = 0
            ends_inside = 0
            starts_inside = 0
            boundaries_inside = 0
            data_all = open(large_path, "rb").read()
            for rng in ranges:
                expected = sum(1 for s in full_starts if rng.start <= s < rng.end)
                data = data_all[rng.start:rng.end]
                got = len(scan_bytes(data))
                lost += abs(got - expected)
                if data.count(b'"') % 2 == 1:
                    parity += 1
                if rng.end < len(data_all) and cut_inside_quoted_field(rng.end):
                    ends_inside += 1
                    boundaries_inside += 1
                if rng.start > 0 and cut_inside_quoted_field(rng.start):
                    starts_inside += 1
                    boundaries_inside += 1
            return lost, parity, ends_inside, starts_inside, boundaries_inside

        a_lost, a_parity, a_end, a_start, a_bounds = misparse(aligned)
        n_lost, n_parity, n_end, n_start, n_bounds = misparse(naive)
        result["split"][str(parts)] = {
            "aligned_chunks": len(aligned),
            "aligned_records_lost_or_fabricated": a_lost,
            "aligned_chunks_with_unbalanced_quotes": a_parity,
            "aligned_chunks_ending_inside_quoted_field": a_end,
            "aligned_chunks_starting_inside_quoted_field": a_start,
            "naive_chunks": len(naive),
            "naive_records_lost_or_fabricated": n_lost,
            "naive_chunks_with_unbalanced_quotes": n_parity,
            "naive_chunks_ending_inside_quoted_field": n_end,
            "naive_chunks_starting_inside_quoted_field": n_start,
            "naive_boundaries_inside_quoted_field": n_bounds,
        }
        print(f"split parts={parts}: aligned lost={a_lost}, naive lost={n_lost}, "
              f"naive boundaries inside a quoted field={n_bounds} "
              f"({n_end} end / {n_start} start)")

    # Peak RSS: full re-parse vs resume, measured in child processes.
    for mode in ("full", "resume"):
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--mem-probe", mode,
             "--corpus-dir", args.corpus_dir, "--cut", str(args.cut)],
            capture_output=True, text=True, check=True,
        )
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        result["memory"][mode] = payload
    full_rss = result["memory"]["full"]["peak_rss_kb"]
    resume_rss = result["memory"]["resume"]["peak_rss_kb"]
    result["memory"]["ratio_resume_over_full"] = round(resume_rss / full_rss, 3)

    # Real corpus: field-level equivalence against the stdlib csv module.
    if args.real_dir and os.path.isdir(args.real_dir):
        import csv
        import io

        rows = []
        preloaded = []
        total_bytes = 0
        total_records = 0
        mismatches = 0
        for name in sorted(os.listdir(args.real_dir)):
            if not name.endswith(".csv"):
                continue
            path = os.path.join(args.real_dir, name)
            data = open(path, "rb").read()
            text = data.decode("utf-8")
            expected = list(csv.reader(io.StringIO(text, newline="")))
            got = [r.fields for r in scan_bytes(data)]
            mismatches += sum(1 for a, b in zip(expected, got) if a != b) + abs(len(expected) - len(got))
            rows.append({"file": name, "bytes": len(data), "records": len(got),
                         "sha256": hashlib.sha256(data).hexdigest()})
            preloaded.append((data, text))
            total_bytes += len(data)
            total_records += len(got)
        t0 = time.perf_counter()
        for _ in range(3):
            for data, _text in preloaded:
                scan_bytes(data)
        t1 = time.perf_counter()
        t2 = time.perf_counter()
        for _ in range(3):
            for _data, text in preloaded:
                list(csv.reader(io.StringIO(text, newline="")))
        t3 = time.perf_counter()
        result["real_corpus"] = {
            "dir": os.path.abspath(args.real_dir),
            "files": rows,
            "total_bytes": total_bytes,
            "total_records": total_records,
            "field_mismatches_vs_stdlib": mismatches,
            "csvresume_mb_per_s": round(total_bytes * 3 / (t1 - t0) / 1e6, 2),
            "stdlib_mb_per_s": round(total_bytes * 3 / (t3 - t2) / 1e6, 2),
            "csvresume_vs_stdlib_slowdown": round((t1 - t0) / (t3 - t2), 2),
        }
        print(f"real corpus: {len(rows)} files, {total_records} records, "
              f"{mismatches} mismatches vs stdlib")

    # Summaries derived from raw samples (not hand-written numbers).
    speeds = [v["speedup"] for v in result["resume"].values()]
    g3 = statistics.median(speeds)
    g4 = result["resume"]["large"]["speedup"]
    g5 = result["memory"]["ratio_resume_over_full"]
    diff_path = os.path.join(ROOT, "evidence", "differential.json")
    if os.path.exists(diff_path):
        with open(diff_path) as fh:
            diff = json.load(fh)
        g2 = {"unique_samples": diff["unique_samples"],
              "checked_samples": diff["checked_samples"],
              "mismatches": diff["mismatches"],
              "corpus_sha256": diff["corpus_sha256"],
              "test": "evidence/verify.py + tests/test_diff.py"}
    else:
        g2 = {"unique_samples": None, "mismatches": None,
              "test": "run evidence/verify.py first"}
    result["gates"] = {
        "G1_upstream_tests": "N/A - original library, no upstream",
        "G2_differential": g2,
        "G3_median_speedup": round(g3, 3),
        "G4_large_scale_speedup": round(g4, 3),
        "G5_resume_peak_rss_ratio": round(g5, 3),
        "G3_pass": g3 >= 1.15,
        "G4_pass": g4 >= 0.95,
        "G5_pass": g5 <= 1.10,
    }

    with open(os.path.join(args.out_dir, "baseline.json"), "w") as fh:
        json.dump({
            "impl": "restart-from-byte-0",
            "env": result["env"],
            "corpora": result["corpora"],
            "measurements": {k: v["restart"] for k, v in result["resume"].items()},
        }, fh, indent=2)
    with open(os.path.join(args.out_dir, "optimized.json"), "w") as fh:
        json.dump({
            "impl": "resume-from-saved-offset",
            "env": result["env"],
            "corpora": result["corpora"],
            "measurements": {k: v["resume"] for k, v in result["resume"].items()},
        }, fh, indent=2)
    with open(os.path.join(args.out_dir, "results.json"), "w") as fh:
        json.dump(result, fh, indent=2)
    print(json.dumps(result["gates"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
