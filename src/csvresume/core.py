"""Byte-exact CSV cursor: records carry their byte span, so a parse can stop,
persist an offset, and resume without re-reading the file from the start.

The scanner is a faithful re-implementation of the RFC 4180 rules that
CPython's :mod:`csv` module implements for the *common* dialect subset:
``quoting=csv.QUOTE_MINIMAL``, ``doublequote=True``, single-character
``delimiter``/``quotechar``, and no ``escapechar``.  It works on bytes, which
makes byte offsets exact for ASCII-compatible encodings (UTF-8, latin-1,
cp1252, ...).  UTF-16/32 input is rejected rather than silently mis-parsed.
"""

from __future__ import annotations

import codecs
import io
import os
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator, List, Sequence, Tuple, Union

__all__ = [
    "Record",
    "Range",
    "UnsupportedDialect",
    "iter_records",
    "scan_bytes",
    "scan_file",
    "split_ranges",
    "naive_line_split_ranges",
    "CHUNK_SIZE",
]

CHUNK_SIZE = 1 << 20  # 1 MiB
_CR = 13
_LF = 10


class UnsupportedDialect(ValueError):
    """Raised when a dialect cannot be handled byte-exactly."""


@dataclass(frozen=True)
class Record:
    """One CSV record.

    ``start``/``end`` are absolute byte offsets in the source; ``end`` is the
    offset of the first byte of the next record (or EOF), so
    ``source[start:end] == raw`` and consecutive records tile the input.
    """

    index: int
    start: int
    end: int
    fields: List[str]
    raw: bytes

    @property
    def byte_length(self) -> int:
        return self.end - self.start

    @property
    def spans_lines(self) -> bool:
        """True when the record's raw bytes contain an embedded line break."""
        body = self.raw
        if body.endswith(b"\r\n"):
            body = body[:-2]
        elif body.endswith((b"\n", b"\r")):
            body = body[:-1]
        return b"\n" in body or b"\r" in body


@dataclass(frozen=True)
class Range:
    """A byte range that starts and ends on record boundaries."""

    start: int
    end: int

    @property
    def size(self) -> int:
        return self.end - self.start


def _encode_char(value: Union[str, bytes], name: str) -> int:
    if isinstance(value, str):
        data = value.encode("ascii", "strict")
    else:
        data = bytes(value)
    if len(data) != 1:
        raise UnsupportedDialect(f"{name} must be exactly one ASCII byte, got {value!r}")
    return data[0]


class _Dialect:
    __slots__ = ("delim", "quote")

    def __init__(self, delimiter: Union[str, bytes], quotechar: Union[str, bytes]):
        self.delim = _encode_char(delimiter, "delimiter")
        self.quote = _encode_char(quotechar, "quotechar")
        if self.delim == self.quote:
            raise UnsupportedDialect("delimiter and quotechar must differ")


def _check_encoding(encoding: str) -> None:
    try:
        info = codecs.lookup(encoding)
    except LookupError as exc:  # pragma: no cover - defensive
        raise UnsupportedDialect(f"unknown encoding {encoding!r}") from exc
    name = info.name
    if name.startswith(("utf-16", "utf-32", "utf-7")) or name in {"utf-16", "utf-32"}:
        raise UnsupportedDialect(
            f"encoding {encoding!r} is not ASCII-compatible; byte offsets would be ambiguous"
        )
    if not getattr(info, "_is_text_encoding", True):  # pragma: no cover
        raise UnsupportedDialect(f"encoding {encoding!r} is not a text encoding")


_NEED_MORE = object()


def _parse_record(
    buf: bytes,
    i: int,
    delim: int,
    quote: int,
    at_eof: bool,
) -> Union[Tuple[List[bytes], int], object]:
    """Parse one record starting at ``buf[i]``.

    Returns ``(fields, end_index)`` or ``_NEED_MORE`` when the record may
    continue past the end of ``buf`` and the stream is not exhausted.
    """

    n = len(buf)
    fields: List[bytes] = []
    field = bytearray()
    in_quotes = False
    at_field_start = True
    j = i
    while j < n:
        c = buf[j]
        if in_quotes:
            if c == quote:
                if j + 1 >= n and not at_eof:
                    return _NEED_MORE  # could be a doubled quote
                if j + 1 < n and buf[j + 1] == quote:
                    field.append(quote)
                    j += 2
                    continue
                in_quotes = False
                j += 1
                continue
            field.append(c)
            j += 1
            continue

        if c == delim:
            fields.append(bytes(field))
            field = bytearray()
            at_field_start = True
            j += 1
            continue

        if c == _CR or c == _LF:
            if c == _CR and j + 1 >= n and not at_eof:
                return _NEED_MORE  # CRLF may straddle the buffer end
            j += 1
            if c == _CR and j < n and buf[j] == _LF:
                j += 1
            if not fields and at_field_start and not field:
                # A bare line break is an empty record: stdlib csv yields [].
                return [], j
            fields.append(bytes(field))
            return fields, j

        if c == quote and at_field_start:
            in_quotes = True
            at_field_start = False
            j += 1
            continue

        field.append(c)
        at_field_start = False
        j += 1

    if not at_eof:
        return _NEED_MORE
    # EOF with no terminating line break: an unquoted tail (or an unclosed
    # quote, which swallows the rest of the input - same as stdlib).
    if j == i:
        return _NEED_MORE
    fields.append(bytes(field))
    return fields, j


def _iter_raw_records(source: Union[bytes, bytearray, BinaryIO, str, os.PathLike],
                      dialect: _Dialect, encoding: str,
                      chunk_size: int, offset: int = 0) -> Iterator[Tuple[int, int, List[bytes], bytes]]:
    _check_encoding(encoding)
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        if offset > len(data):
            raise ValueError(f"offset {offset} is past end of input ({len(data)} bytes)")
        chunks: Iterable[bytes] = (data[offset:],)
        closer = None
    elif isinstance(source, (str, os.PathLike)):
        size = os.path.getsize(os.fspath(source))
        if offset > size:
            raise ValueError(f"offset {offset} is past end of input ({size} bytes)")
        fh = open(os.fspath(source), "rb")
        fh.seek(offset)
        chunks = iter(lambda: fh.read(chunk_size), b"")
        closer = fh
    elif hasattr(source, "read"):
        fh = source  # type: ignore[assignment]
        if hasattr(fh, "seek"):
            fh.seek(offset)
        chunks = iter(lambda: fh.read(chunk_size), b"")
        closer = None
    else:  # pragma: no cover - defensive
        raise TypeError("source must be bytes, a path, or a binary file object")

    buf = bytearray()
    base = offset  # absolute offset of buf[0]
    eof = False
    try:
        it = iter(chunks)
        while True:
            # Try to parse as many records as the buffer allows.
            while True:
                if not buf:
                    if eof:
                        return
                    chunk = next(it, b"")
                    if not chunk:
                        eof = True
                        continue
                    buf += chunk
                    continue
                parsed = _parse_record(buf, 0, dialect.delim, dialect.quote, eof)
                if parsed is _NEED_MORE:
                    if eof:
                        # Unreachable for well-formed logic, but never spin.
                        return
                    chunk = next(it, b"")
                    if not chunk:
                        eof = True
                        continue
                    buf += chunk
                    continue
                fields, end = parsed  # type: ignore[misc]
                raw = bytes(buf[:end])
                yield base, base + end, fields, raw  # type: ignore[arg-type]
                del buf[:end]
                base += end
                if not buf and eof:
                    return
    finally:
        if closer is not None:
            closer.close()


def iter_records(source: Union[bytes, bytearray, BinaryIO, str, os.PathLike],
                 *,
                 delimiter: Union[str, bytes] = ",",
                 quotechar: Union[str, bytes] = '"',
                 encoding: str = "utf-8",
                 chunk_size: int = CHUNK_SIZE,
                 strict_encoding: bool = True,
                 offset: int = 0) -> Iterator[Record]:
    """Yield :class:`Record` objects with byte spans and decoded fields.

    ``offset`` must be a record boundary previously reported as ``Record.start``
    (or 0); scanning starts there without reading earlier bytes.
    """

    if chunk_size < 1:
        raise ValueError("chunk_size must be positive")
    dialect = _Dialect(delimiter, quotechar)
    errors = "strict" if strict_encoding else "replace"
    index = 0
    for start, end, raws, raw in _iter_raw_records(source, dialect, encoding, chunk_size, offset):
        decoded = [f.decode(encoding, errors) for f in raws]
        yield Record(index=index, start=start, end=end, fields=decoded, raw=raw)
        index += 1


def scan_bytes(data: bytes, **kwargs) -> List[Record]:
    """Convenience wrapper: scan an in-memory blob and materialise all records."""

    return list(iter_records(data, **kwargs))


def scan_file(path: Union[str, os.PathLike], **kwargs) -> Iterator[Record]:
    return iter_records(path, **kwargs)


def split_ranges(path: Union[str, os.PathLike],
                 parts: int,
                 *,
                 delimiter: Union[str, bytes] = ",",
                 quotechar: Union[str, bytes] = '"',
                 encoding: str = "utf-8") -> List[Range]:
    """Split ``path`` into at most ``parts`` record-aligned byte ranges.

    Every returned range starts and ends where a record starts, the ranges are
    contiguous, and they cover ``[0, filesize)`` with no overlap.  This is the
    operation a line-based ``split`` gets wrong whenever a quoted field contains
    a line break (see :func:`naive_line_split_ranges` for the contrast).
    """

    if parts < 1:
        raise ValueError("parts must be >= 1")
    size = os.path.getsize(os.fspath(path))
    if size == 0:
        return []
    dialect = _Dialect(delimiter, quotechar)
    starts = [0]
    for start, _end, _fields, _raw in _iter_raw_records(path, dialect, encoding, CHUNK_SIZE):
        if start:
            starts.append(start)
    starts = sorted(set(starts))
    if parts == 1:
        return [Range(0, size)]
    if len(starts) <= 1:
        return [Range(0, size)]
    targets = [round(size * k / parts) for k in range(1, parts)]
    # Map each target byte to the first record start at or after it.
    bounds = [0]
    lo = 0
    for t in targets:
        while lo < len(starts) and starts[lo] < t:
            lo += 1
        if lo >= len(starts):
            lo = len(starts) - 1
        if starts[lo] != bounds[-1]:
            bounds.append(starts[lo])
    if bounds[-1] != size:
        bounds.append(size)
    return [Range(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]


def naive_line_split_ranges(path: Union[str, os.PathLike], parts: int) -> List[Range]:
    """The contrasting baseline: split at roughly equal *line* boundaries.

    This mirrors what ``split -l`` / ``awk 'NR % k == 0'`` do: a boundary may
    land inside a quoted field, so the chunks are not independently parseable as
    the original records.
    """

    if parts < 1:
        raise ValueError("parts must be >= 1")
    with open(os.fspath(path), "rb") as fh:
        data = fh.read()
    if not data:
        return []
    size = len(data)
    targets = [round(size * k / parts) for k in range(1, parts)]
    bounds = [0]
    for t in targets:
        nl = data.find(b"\n", t)
        cut = size if nl == -1 else nl + 1
        if cut > bounds[-1]:
            bounds.append(cut)
    if bounds[-1] != size:
        bounds.append(size)
    return [Range(a, b) for a, b in zip(bounds, bounds[1:]) if b > a]
