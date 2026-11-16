import os
import io

import pytest

from csvresume import (
    Record,
    Range,
    UnsupportedDialect,
    iter_records,
    naive_line_split_ranges,
    scan_bytes,
    scan_file,
    split_ranges,
)


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path


def test_record_spans_and_fields():
    data = b"id,name\r\n1,\"multi\r\nline\"\r\n2,bare"
    records = scan_bytes(data)
    assert [r.fields for r in records] == [["id", "name"], ["1", "multi\r\nline"], ["2", "bare"]]
    assert records[0].start == 0 and records[0].end == 9
    assert records[1].start == 9
    assert records[0].byte_length == 9
    assert records[1].spans_lines is True
    assert records[0].spans_lines is False
    assert b"".join(r.raw for r in records) == data


def test_scan_file_matches_scan_bytes(tmp_path):
    text = 'a,b\n"x\ny",z\n\nlast,row'
    path = write(tmp_path, "data.csv", text)
    from_file = list(scan_file(path))
    from_bytes = scan_bytes(text.encode("utf-8"))
    assert [(r.fields, r.start, r.end) for r in from_file] == [
        (r.fields, r.start, r.end) for r in from_bytes
    ]


def test_binary_file_object_streams(tmp_path):
    text = "a,b\nc,d\n"
    path = write(tmp_path, "data.csv", text)
    with open(path, "rb") as fh:
        records = list(iter_records(fh, chunk_size=2))
    assert [r.fields for r in records] == [["a", "b"], ["c", "d"]]


def test_offset_must_be_record_boundary(tmp_path):
    text = "aa,bb\ncc,dd\n"
    path = write(tmp_path, "data.csv", text)
    records = list(iter_records(path))
    tail = list(iter_records(path, offset=records[1].start))
    assert [r.fields for r in tail] == [["cc", "dd"]]
    with pytest.raises(ValueError):
        list(iter_records(path, offset=10_000))


def test_dialects(tmp_path):
    text = "'a;b';c\n1;2\n"
    records = scan_bytes(text.encode(), delimiter=";", quotechar="'")
    assert [r.fields for r in records] == [["a;b", "c"], ["1", "2"]]


def test_tab_dialect():
    records = scan_bytes(b"a\tb\tc\n", delimiter="\t")
    assert records[0].fields == ["a", "b", "c"]


def test_encoding_latin1_and_utf8():
    raw = "café,olé\n".encode("latin-1")
    records = scan_bytes(raw, encoding="latin-1")
    assert records[0].fields == ["café", "olé"]


def test_unsupported_dialects():
    with pytest.raises(UnsupportedDialect):
        scan_bytes(b"a,b\n", delimiter="::")
    with pytest.raises(UnsupportedDialect):
        scan_bytes(b"a,b\n", delimiter=",", quotechar=",")
    with pytest.raises(UnsupportedDialect):
        scan_bytes(b"a,b\n", encoding="utf-16")


def test_non_ascii_offsets_are_byte_offsets():
    text = "中文,😀\nx,y\n"
    data = text.encode("utf-8")
    records = scan_bytes(data)
    assert records[0].raw == "中文,😀\n".encode("utf-8")
    assert records[1].start == len(records[0].raw)
    assert records[0].byte_length == len("中文,😀\n".encode("utf-8"))


def test_split_ranges_are_record_aligned(tmp_path):
    lines = ['name,address\n']
    for i in range(50):
        lines.append(f'{i},"line one\nline two, {i}"\n')
    path = write(tmp_path, "multi.csv", "".join(lines))
    records = list(iter_records(path))
    starts = {r.start for r in records}
    size = os.path.getsize(path)

    ranges = split_ranges(path, 5)
    assert ranges[0].start == 0 and ranges[-1].end == size
    for a, b in zip(ranges, ranges[1:]):
        assert a.end == b.start
    for rng in ranges:
        assert rng.start in starts or rng.start == 0
        assert rng.end in starts or rng.end == size
    # Each chunk parses standalone into a contiguous slice of the full records.
    data = path.read_bytes()
    all_fields = [r.fields for r in records]
    rebuilt = []
    for rng in ranges:
        rebuilt.extend(r.fields for r in scan_bytes(data[rng.start:rng.end]))
    assert rebuilt == all_fields


def test_split_more_parts_than_records(tmp_path):
    path = write(tmp_path, "tiny.csv", "a,b\n")
    ranges = split_ranges(path, 10)
    assert ranges == [Range(0, 4)]


def test_naive_line_split_is_observably_wrong(tmp_path):
    lines = ['id,address\n']
    for i in range(40):
        lines.append(f'{i},"first line\nsecond line {i}"\n')
    path = write(tmp_path, "multi.csv", "".join(lines))
    data = path.read_bytes()
    good = split_ranges(path, 4)
    naive = naive_line_split_ranges(path, 4)
    assert good != naive

    def chunk_record_counts(ranges):
        return [len(scan_bytes(data[r.start:r.end])) for r in ranges]

    # A chunk cut inside a quoted field either merges records or ends mid-field,
    # so the standalone record count differs from the record-aligned split.
    assert chunk_record_counts(good) != chunk_record_counts(naive)


def test_empty_file(tmp_path):
    path = write(tmp_path, "empty.csv", "")
    assert list(iter_records(path)) == []
    assert split_ranges(path, 4) == []


def test_chunk_size_validation():
    with pytest.raises(ValueError):
        list(iter_records(b"a,b\n", chunk_size=0))


def test_record_index_is_ordinal_of_scan():
    records = scan_bytes(b"a\nb\nc\n")
    assert [r.index for r in records] == [0, 1, 2]
    assert isinstance(records[0], Record)
