"""Differential test: csvresume vs CPython's csv module.

The corpus is generated with a documented seed, deduplicated by sha256, and
compared field-by-field.  It mixes valid dialects, hand-built pathological
records (quotes after a closed field, quotes inside unquoted fields, CRLF
inside quoted fields, non-ASCII) and byte-level mutations of valid documents.

Run with ``-s`` to see the unique-sample count printed by the test.
"""

from __future__ import annotations

import hashlib
import io
import csv
import random

import pytest

from csvresume import scan_bytes, iter_records

SEED = 20261116

DIALECTS = [(",", '"'), (";", '"'), ("\t", '"'), ("|", "'"), (",", "'")]

FIELD_POOL = [
    "",
    "alice",
    "0",
    "NULL",
    "  padded  ",
    "a,b",
    "a;b",
    "a|b",
    'a"b',
    "a'b",
    "line1\nline2",
    "crlf\r\ninside",
    "cr\ronly",
    "tab\there",
    "中文值",
    "café",
    "emoji 😀",
    "\\N",
    "x" * 40,
]

MUTATION_BYTES = b'ab,"\'\n\r\t; |\x00= 09'


def _quote(value: str, quote: str) -> str:
    return quote + value.replace(quote, quote + quote) + quote


def _build_valid(rng: random.Random, delimiter: str, quote: str) -> str:
    out = []
    for _ in range(rng.randrange(0, 4)):
        fields = [rng.choice(FIELD_POOL) for _ in range(rng.randrange(0, 5))]
        rendered = []
        for value in fields:
            needs = (
                delimiter in value
                or quote in value
                or "\n" in value
                or "\r" in value
                or rng.random() < 0.35
            )
            rendered.append(_quote(value, quote) if needs else value)
        eol = rng.choice(["\n", "\r\n", "\n", "\r"])
        out.append(delimiter.join(rendered) + eol)
    text = "".join(out)
    if out and rng.random() < 0.25:
        text = text[: -len(rng.choice(["\n", "\r\n", "\r"]))]
    return text


def _mutate(rng: random.Random, text: str) -> str:
    """Character-level mutation, so multi-byte characters stay valid UTF-8."""
    chars = list(text)
    alphabet = MUTATION_BYTES.decode("latin-1")
    for _ in range(rng.randrange(1, 4)):
        choice = rng.randrange(3)
        if choice == 0 and chars:
            chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        elif choice == 1:
            chars.insert(rng.randrange(len(chars) + 1), rng.choice(alphabet))
        elif chars:
            del chars[rng.randrange(len(chars))]
    return "".join(chars)


def build_corpus(target_unique: int = 1500):
    rng = random.Random(SEED)
    unique = {}
    attempts = 0
    while len(unique) < target_unique and attempts < target_unique * 60:
        attempts += 1
        delimiter, quote = rng.choice(DIALECTS)
        text = _build_valid(rng, delimiter, quote)
        if rng.random() < 0.45:
            text = _mutate(rng, text)
        key = hashlib.sha256(
            f"{delimiter!r}|{quote!r}|{text}".encode("utf-8", "surrogatepass")
        ).hexdigest()
        unique.setdefault(key, (delimiter, quote, text))
    return list(unique.values())


CORPUS = build_corpus(1500)


def _stdlib(text, delimiter, quote):
    return list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, quotechar=quote))


def test_corpus_is_unique_and_large():
    assert len(CORPUS) >= 1400, f"only {len(CORPUS)} unique samples"
    print(f"\nunique samples: {len(CORPUS)} (sha256 dedup, seed={SEED})")


def test_differential_fields_and_bytes():
    checked = 0
    skipped = 0
    for delimiter, quote, text in CORPUS:
        data = text.encode("utf-8")
        try:
            expected = _stdlib(text, delimiter, quote)
        except csv.Error:
            skipped += 1
            continue
        records = scan_bytes(data, delimiter=delimiter, quotechar=quote)
        got = [r.fields for r in records]
        assert got == expected, (
            f"field mismatch for dialect {delimiter!r}/{quote!r}: {text!r}\n"
            f"expected={expected!r}\n got={got!r}"
        )
        # Coverage invariant: records tile the input byte-for-byte.
        assert b"".join(r.raw for r in records) == data
        cursor = 0
        for rec in records:
            assert rec.start == cursor
            assert rec.raw == data[rec.start:rec.end]
            cursor = rec.end
        assert cursor == len(data)
        checked += 1
    print(f"compared {checked} samples, skipped {skipped} (stdlib csv.Error)")
    assert checked >= 1400


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 64, 1 << 20])
def test_chunk_boundaries_do_not_change_results(chunk_size):
    subset = CORPUS[:200]
    for delimiter, quote, text in subset:
        data = text.encode("utf-8")
        try:
            expected = _stdlib(text, delimiter, quote)
        except csv.Error:
            continue
        records = list(
            iter_records(data, delimiter=delimiter, quotechar=quote, chunk_size=chunk_size)
        )
        assert [r.fields for r in records] == expected
        assert b"".join(r.raw for r in records) == data


@pytest.mark.parametrize(
    "text,expected",
    [
        ("", []),
        ("\n", [[]]),
        ("\n\n", [[], []]),
        ("a,b", [["a", "b"]]),
        ("a,b\n", [["a", "b"]]),
        ("a,b\r\n", [["a", "b"]]),
        ("a,b\r", [["a", "b"]]),
        ('"a\r\nb",c\n', [["a\r\nb", "c"]]),
        ('"a""b",c\n', [['a"b', "c"]]),
        ('"a"b,c\n', [["ab", "c"]]),
        ('""x,y\n', [["x", "y"]]),
        ('"a"b"c,d\n', [['ab"c', "d"]]),
        ('"unclosed,b\n', [["unclosed,b\n"]]),
        ("a\x00b\n", [["a\x00b"]]),
        (",\n", [["", ""]]),
        ('"",""\n', [["", ""]]),
        ("中文,😀\n", [["中文", "😀"]]),
    ],
)
def test_edge_cases_match_stdlib(text, expected):
    assert _stdlib(text, ",", '"') == expected
    records = scan_bytes(text.encode("utf-8"))
    assert [r.fields for r in records] == expected
    assert b"".join(r.raw for r in records) == text.encode("utf-8")


def test_offset_equals_suffix_scan():
    rng = random.Random(SEED + 1)
    for _ in range(120):
        delimiter, quote = rng.choice(DIALECTS)
        text = _mutate(rng, _build_valid(rng, delimiter, quote))
        data = text.encode("utf-8")
        try:
            expected = _stdlib(text, delimiter, quote)
        except csv.Error:
            continue
        full = scan_bytes(data, delimiter=delimiter, quotechar=quote)
        if not full:
            continue
        cut = rng.randrange(len(full))
        offset = full[cut].start
        tail = list(
            iter_records(data, delimiter=delimiter, quotechar=quote, offset=offset)
        )
        assert [r.fields for r in tail] == expected[cut:]
        assert [r.start for r in tail] == [r.start for r in full[cut:]]


def test_truncated_inputs_match_stdlib():
    """Every prefix of a sample is also a legal input; offsets must stay exact."""
    rng = random.Random(SEED + 2)
    checked = 0
    for delimiter, quote, text in CORPUS[:120]:
        data = text.encode("utf-8")
        try:
            expected_full = _stdlib(text, delimiter, quote)
        except csv.Error:
            continue
        for _ in range(3):
            cut = rng.randrange(len(data) + 1)
            piece = data[:cut]
            try:
                expected = _stdlib(piece.decode("utf-8"), delimiter, quote)
            except (csv.Error, UnicodeDecodeError):
                continue
            records = scan_bytes(piece, delimiter=delimiter, quotechar=quote)
            assert [r.fields for r in records] == expected, (delimiter, quote, piece)
            assert b"".join(r.raw for r in records) == piece
            checked += 1
    assert checked >= 200
    print(f"truncated inputs checked: {checked}")
