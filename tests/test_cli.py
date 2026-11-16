import json
import os
import subprocess
import sys

import pytest

from csvresume import scan_bytes

SRC = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def run(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(SRC, "src") + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "csvresume", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )


@pytest.fixture()
def csvfile(tmp_path):
    lines = ['id,name,note\n']
    for i in range(30):
        lines.append(f'{i},"name {i}","first\nsecond {i}"\n')
    path = tmp_path / "people.csv"
    path.write_text("".join(lines), encoding="utf-8", newline="")
    return path


def test_check_json_reports_clean_tiling(csvfile):
    res = run("check", str(csvfile), "--json")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["tiling_ok"] is True
    assert payload["records"] == 31
    assert payload["multiline_records"] == 30
    assert payload["errors"] == []


def test_check_detects_truncated_quote(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text('a,b\n"unclosed\n', encoding="utf-8", newline="")
    res = run("check", str(path), "--json")
    assert res.returncode == 0  # an unclosed quote is a legal field, like csv.reader
    payload = json.loads(res.stdout)
    assert payload["records"] == 2
    assert payload["tiling_ok"] is True


def test_index_limit_and_offsets(csvfile):
    res = run("index", str(csvfile), "--limit", "3", "--json")
    assert res.returncode == 0, res.stderr
    rows = json.loads(res.stdout)
    assert [r["index"] for r in rows] == [0, 1, 2]
    assert rows[0]["start"] == 0
    assert rows[1]["start"] == rows[0]["end"]


def test_split_writes_chunks_that_parse(csvfile, tmp_path):
    out = tmp_path / "chunks"
    res = run("split", str(csvfile), "--parts", "4", "--out", str(out), "--json")
    assert res.returncode == 0, res.stderr
    ranges = json.loads(res.stdout)
    assert len(ranges) == 4
    full = [r.fields for r in scan_bytes(csvfile.read_bytes())]
    rebuilt = []
    for chunk in sorted(out.iterdir()):
        rebuilt.extend(r.fields for r in scan_bytes(chunk.read_bytes()))
    assert rebuilt == full


def test_resume_from_offset_matches_tail(csvfile, tmp_path):
    data = csvfile.read_bytes()
    records = scan_bytes(data)
    cut = 10
    offset = records[cut].start
    res = run("resume", str(csvfile), "--offset", str(offset), "--json")
    assert res.returncode == 0, res.stderr
    payload = json.loads(res.stdout)
    assert payload["records_from_offset"] == len(records) - cut
    assert payload["bytes_parsed"] == len(data) - offset


def test_missing_file_exit_code(tmp_path):
    res = run("check", str(tmp_path / "nope.csv"))
    assert res.returncode == 2
    assert "csvresume" in res.stderr


def test_unsupported_dialect_exit_code(csvfile):
    res = run("check", str(csvfile), "--encoding", "utf-16")
    assert res.returncode == 2
    assert "unsupported dialect" in res.stderr.lower()
