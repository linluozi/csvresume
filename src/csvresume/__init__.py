"""csvresume - byte-addressed, resumable CSV reading for Python.

Public API:
    iter_records(source, ...)   -> iterator of Record(index, start, end, fields, raw)
    scan_bytes(data, ...)       -> list[Record]
    split_ranges(path, parts)   -> byte ranges aligned to record boundaries
    naive_line_split_ranges     -> the line-based baseline, for comparison
"""

from .core import (
    CHUNK_SIZE,
    Range,
    Record,
    UnsupportedDialect,
    iter_records,
    naive_line_split_ranges,
    scan_bytes,
    scan_file,
    split_ranges,
)

__version__ = "0.1.0"
__all__ = [
    "CHUNK_SIZE",
    "Range",
    "Record",
    "UnsupportedDialect",
    "iter_records",
    "naive_line_split_ranges",
    "scan_bytes",
    "scan_file",
    "split_ranges",
    "__version__",
]
