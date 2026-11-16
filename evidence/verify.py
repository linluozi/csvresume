#!/usr/bin/env python3
"""Produce evidence/differential.json from the same corpus the test suite uses.

The corpus builder lives in tests/test_diff.py; importing it here keeps the
test and the published evidence on exactly one implementation.  No number in
README is typed by hand: this script writes the JSON that README cites.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from csvresume import iter_records, scan_bytes  # noqa: E402

spec = importlib.util.spec_from_file_location("diff_corpus", os.path.join(ROOT, "tests", "test_diff.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def main() -> int:
    corpus = mod.build_corpus(1500)
    mismatches = 0
    checked = 0
    skipped = 0
    resume_checks = 0
    resume_mismatches = 0
    coverage_failures = 0
    hashes = []
    sample_hashes = []
    rng = random.Random(424242)
    for delimiter, quote, text in corpus:
        data = text.encode("utf-8")
        hashes.append(hashlib.sha256(data).hexdigest())
        sample_hashes.append(hashlib.sha256(
            f"{delimiter!r}|{quote!r}|{text}".encode("utf-8", "surrogatepass")).hexdigest())
        try:
            expected = list(csv.reader(io.StringIO(text, newline=""),
                                       delimiter=delimiter, quotechar=quote))
        except csv.Error:
            skipped += 1
            continue
        records = scan_bytes(data, delimiter=delimiter, quotechar=quote)
        if [r.fields for r in records] != expected:
            mismatches += 1
        if b"".join(r.raw for r in records) != data:
            coverage_failures += 1
        checked += 1
        if records:
            cut = rng.randrange(len(records))
            tail = list(iter_records(data, delimiter=delimiter, quotechar=quote,
                                     offset=records[cut].start))
            resume_checks += 1
            if [r.fields for r in tail] != expected[cut:]:
                resume_mismatches += 1

    corpus_digest = hashlib.sha256("".join(sorted(sample_hashes)).encode()).hexdigest()
    payload = {
        "corpus": "tests/test_diff.py::build_corpus",
        "seed": mod.SEED,
        "unique_samples": len(set(sample_hashes)),
        "unique_input_byte_strings": len(set(hashes)),
        "checked_samples": checked,
        "skipped_stdlib_errors": skipped,
        "mismatches": mismatches,
        "coverage_failures": coverage_failures,
        "resume_checks": resume_checks,
        "resume_mismatches": resume_mismatches,
        "corpus_sha256": corpus_digest,
        "dialects": [list(d) for d in mod.DIALECTS],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(os.path.join(HERE, "differential.json"), "w") as fh:
        json.dump(payload, fh, indent=2)
    print(json.dumps(payload, indent=2))
    ok = (mismatches == 0 and coverage_failures == 0 and resume_mismatches == 0
          and payload["unique_samples"] >= 1400)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
