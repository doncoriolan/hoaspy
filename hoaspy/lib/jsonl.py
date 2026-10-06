"""Reading and writing collector output: the JSON Lines record files and
the JSON side files (sources.json, checkpoints, coverage).

Every write goes to a temporary file first and replaces the target in one
step, so a run that dies mid-write leaves the previous file whole.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


def now_iso() -> str:
    """The moment, as the `retrieved_at` column carries it: UTC, ISO 8601."""
    return datetime.now(UTC).isoformat()


def iter_rows(path: Path, skip_bad: bool = False) -> Iterator[dict]:
    """The rows of a JSON Lines file, one at a time. `skip_bad` passes over
    a line that is not JSON (the torn last line of a hard-killed run)
    instead of raising."""
    bad = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                if not skip_bad:
                    raise
                bad += 1
    if bad:
        log.warning("%s: %d line(s) were not JSON and were skipped", path, bad)


def read_rows(path: Path, skip_bad: bool = False) -> list[dict]:
    """Every row of a JSON Lines file; [] when the file does not exist."""
    return list(iter_rows(path, skip_bad)) if path.exists() else []


def write_rows(path: Path, rows: Iterable[dict]) -> int:
    """Write `rows` as a JSON Lines file (atomically); returns how many."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    count = 0
    with tmp.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    tmp.replace(path)
    return count


def append_row(fh, row: dict) -> None:
    """One row onto an open JSON Lines file, flushed so a checkpoint survives a kill."""
    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    fh.flush()


def replace_rows(path: Path, fresh: Iterable[dict], states: set[str]) -> int:
    """Replace the rows of `states` in a merged file with `fresh`, keeping
    every other state's rows. Returns the total number of rows written."""
    kept = [row for row in read_rows(path, skip_bad=True) if row.get("state") not in states]
    return write_rows(path, kept + list(fresh))


def read_json(path: Path, default=None):
    """The JSON document at `path`, or `default` when the file is missing or unreadable."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("%s is not valid JSON — starting over", path)
        return default


def write_json(path: Path, data) -> None:
    """Write a JSON document (atomically, indented, one trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def merge_json(path: Path, key: str, entry) -> None:
    """Set one top-level key of a JSON document, leaving the others as they
    are — how several collectors share one sources.json."""
    data = read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data[key] = entry
    write_json(path, data)
