"""Command line interface: check / index / split / resume."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .core import (
    CHUNK_SIZE,
    Range,
    UnsupportedDialect,
    iter_records,
    naive_line_split_ranges,
    split_ranges,
)

EXIT_OK = 0
EXIT_CHECK_FAILED = 1
EXIT_USAGE = 2


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("path", help="CSV file (read as bytes)")
    p.add_argument("--delimiter", default=",", help="single-byte delimiter (default ',')")
    p.add_argument("--quotechar", default='"', help="single-byte quote char (default '\"')")
    p.add_argument("--encoding", default="utf-8", help="ASCII-compatible encoding (default utf-8)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csvresume",
        description="Byte-addressed, resumable CSV reading.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="verify record tiling and report stats")
    _add_common(p_check)
    p_check.add_argument("--json", action="store_true", help="machine-readable output")

    p_index = sub.add_parser("index", help="print record byte offsets")
    _add_common(p_index)
    p_index.add_argument("--limit", type=int, default=20, help="max records to print (0 = all)")
    p_index.add_argument("--json", action="store_true")

    p_split = sub.add_parser("split", help="split into record-aligned byte ranges")
    _add_common(p_split)
    p_split.add_argument("--parts", type=int, required=True)
    p_split.add_argument("--out", help="write chunk files into this directory")
    p_split.add_argument("--json", action="store_true")

    p_resume = sub.add_parser("resume", help="count records from a saved byte offset")
    _add_common(p_resume)
    p_resume.add_argument("--offset", type=int, required=True)
    p_resume.add_argument("--json", action="store_true")

    return parser


def _cmd_check(args) -> int:
    size = os.path.getsize(args.path)
    expected = 0
    records = 0
    empty_records = 0
    multiline = 0
    max_fields = 0
    first_fields: Optional[int] = None
    ragged = 0
    errors: List[str] = []
    with open(args.path, "rb") as mirror:
        for rec in iter_records(
            args.path,
            delimiter=args.delimiter,
            quotechar=args.quotechar,
            encoding=args.encoding,
        ):
            if rec.start != expected:
                errors.append(f"record {rec.index}: start {rec.start} != expected {expected}")
            expected = rec.end
            records += 1
            if not rec.fields:
                empty_records += 1
            if rec.spans_lines:
                multiline += 1
            max_fields = max(max_fields, len(rec.fields))
            if first_fields is None:
                first_fields = len(rec.fields)
            elif len(rec.fields) != first_fields:
                ragged += 1
            actual = mirror.read(len(rec.raw))
            if actual != rec.raw:
                errors.append(f"record {rec.index}: raw bytes do not match source at {rec.start}")
                if len(errors) > 5:
                    break
    if expected != size:
        errors.append(f"tiling stopped at {expected}, file size is {size}")

    result = {
        "path": os.path.abspath(args.path),
        "size_bytes": size,
        "records": records,
        "empty_records": empty_records,
        "multiline_records": multiline,
        "max_fields": max_fields,
        "ragged_records": ragged,
        "tiling_ok": not errors,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"{result['path']}: {records} records, {size} bytes, tiling_ok={not errors}")
        print(
            f"  empty={empty_records} multiline={multiline} "
            f"max_fields={max_fields} ragged={ragged}"
        )
        for err in errors:
            print(f"  ERROR: {err}")
    return EXIT_OK if not errors else EXIT_CHECK_FAILED


def _cmd_index(args) -> int:
    rows = []
    for rec in iter_records(
        args.path,
        delimiter=args.delimiter,
        quotechar=args.quotechar,
        encoding=args.encoding,
    ):
        if args.limit and len(rows) >= args.limit:
            break
        rows.append({"index": rec.index, "start": rec.start, "end": rec.end,
                     "fields": len(rec.fields), "bytes": rec.byte_length})
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            print(f"{row['index']:>6}  {row['start']:>12}..{row['end']:<12}  fields={row['fields']}")
    return EXIT_OK


def _range_dicts(ranges: List[Range]) -> List[dict]:
    return [{"index": i, "start": r.start, "end": r.end, "bytes": r.size}
            for i, r in enumerate(ranges)]


def _cmd_split(args) -> int:
    ranges = split_ranges(
        args.path,
        args.parts,
        delimiter=args.delimiter,
        quotechar=args.quotechar,
        encoding=args.encoding,
    )
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(args.path, "rb") as src:
            for i, rng in enumerate(ranges):
                src.seek(rng.start)
                chunk = src.read(rng.size)
                with open(os.path.join(args.out, f"chunk-{i:03d}.csv"), "wb") as dst:
                    dst.write(chunk)
    if args.json:
        print(json.dumps(_range_dicts(ranges), indent=2))
    else:
        for row in _range_dicts(ranges):
            print(f"chunk-{row['index']:03d}  {row['start']}..{row['end']}  ({row['bytes']} bytes)")
    return EXIT_OK


def _cmd_resume(args) -> int:
    count = 0
    first_fields = None
    total_bytes = 0
    for rec in iter_records(
        args.path,
        delimiter=args.delimiter,
        quotechar=args.quotechar,
        encoding=args.encoding,
        offset=args.offset,
    ):
        if first_fields is None:
            first_fields = len(rec.fields)
        count += 1
        total_bytes += rec.byte_length
    payload = {"offset": args.offset, "records_from_offset": count,
               "fields_of_first_record": first_fields, "bytes_parsed": total_bytes}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"from byte {args.offset}: {count} records, "
            f"{total_bytes} bytes, first record has {first_fields} fields"
        )
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return _cmd_check(args)
        if args.command == "index":
            return _cmd_index(args)
        if args.command == "split":
            return _cmd_split(args)
        if args.command == "resume":
            return _cmd_resume(args)
    except UnsupportedDialect as exc:
        print(f"csvresume: unsupported dialect: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"csvresume: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BrokenPipeError:  # pragma: no cover
        return EXIT_OK
    parser.error("unknown command")
    return EXIT_USAGE
