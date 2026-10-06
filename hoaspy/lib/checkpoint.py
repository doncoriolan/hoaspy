"""Resume state for a collector that asks a source one name at a time.

A full run is thousands of requests over hours, so every name's rows are
written as they arrive and a killed run picks up at the next name:

    checkpoint = NameCheckpoint(out_dir, "tx_research")
    rows, done = checkpoint.load()
    with checkpoint:
        for name in names:
            if name in done:
                continue
            found = search(name)
            checkpoint.add(name, found)
    checkpoint.clear()          # only once the whole list finished

Two files sit beside the output: `.<prefix>_partial.jsonl`, one row per
line as found, and `.<prefix>_done.txt`, one finished name per line.
"""

from __future__ import annotations

from pathlib import Path

from hoaspy.lib.jsonl import append_row, read_rows


class NameCheckpoint:
    def __init__(self, directory: Path, prefix: str):
        self.partial_path = directory / f".{prefix}_partial.jsonl"
        self.done_path = directory / f".{prefix}_done.txt"
        self._partial = None
        self._done = None

    def load(self) -> tuple[list[dict], set[str]]:
        """(rows found so far, names finished). A torn last line from a
        hard kill is skipped, not fatal."""
        rows = read_rows(self.partial_path, skip_bad=True)
        names: set[str] = set()
        if self.done_path.exists():
            names = {line.strip() for line in self.done_path.read_text(encoding="utf-8").splitlines()
                     if line.strip()}
        return rows, names

    def clear(self) -> None:
        for path in (self.partial_path, self.done_path):
            path.unlink(missing_ok=True)

    def __enter__(self) -> NameCheckpoint:
        self.partial_path.parent.mkdir(parents=True, exist_ok=True)
        self._partial = self.partial_path.open("a", encoding="utf-8")
        self._done = self.done_path.open("a", encoding="utf-8")
        return self

    def __exit__(self, *exc) -> None:
        for handle in (self._partial, self._done):
            if handle is not None:
                handle.close()
        self._partial = self._done = None

    def add(self, name: str, rows: list[dict]) -> None:
        """Record one finished name and what it found, flushed to disk."""
        if self._partial is None or self._done is None:
            raise RuntimeError("add() needs the checkpoint opened with `with`")
        for row in rows:
            append_row(self._partial, row)
        self._done.write(name + "\n")
        self._done.flush()
