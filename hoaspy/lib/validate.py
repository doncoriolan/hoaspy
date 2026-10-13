"""Check collector output against the record contract (hoaspy/lib/contract.py).

    python -m hoaspy validate                          # every output file under records/ liens/ courts/ news/
    python -m hoaspy validate liens/liens_cook.jsonl   # some files
    python -m hoaspy validate --family lien /tmp/x.jsonl   # a file kept somewhere else
    python -m hoaspy validate --json report.json       # the same report, machine-readable

Run it after a collector finishes and before you hand its output to anyone.
For each file it prints the row count and one line per kind of problem with
the first line number it occurs on:

    records/associations.jsonl  [registry]  FAILED: 44,813 rows, 2 unusable, 37 with contact details
      error    empty `name`: 2 rows  e.g. line 31208
      contact  contact `manager_address`: 35 rows  e.g. line 912

Exit status: 0 when no file has an error or a contact detail (warnings do
not fail; `--strict` makes them), 1 otherwise, 2 when a file is not a known
collector output and no `--family` was given. A contact detail is reported
by row and key only — the report never repeats the phone number or the
address.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hoaspy import ROOT
from hoaspy.lib import contract

DATA_DIRS = ("records", "liens", "courts", "news")


def output_files(root: Path = ROOT) -> list[Path]:
    """Every collector output file under `root`, in a stable order."""
    found = []
    for name in DATA_DIRS:
        if (root / name).is_dir():
            found += [path for path in sorted((root / name).glob("*.jsonl"))
                      if contract.family_of(path.relative_to(root))]
    return found


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("files", nargs="*", type=Path, help="JSON Lines files (default: every collector output file)")
    parser.add_argument("--family", choices=sorted(contract.FAMILIES),
                        help="the family of the files named, when their names do not say")
    parser.add_argument("--strict", action="store_true", help="fail on warnings too")
    parser.add_argument("--json", type=Path, metavar="PATH", help="also write the reports as JSON (- for stdout)")
    parser.add_argument("--examples", type=int, default=3, help="line numbers kept per kind of problem (default 3)")


def run(args: argparse.Namespace) -> int:
    files = args.files or output_files()
    if not files:
        print("no collector output found under " + ", ".join(f"{name}/" for name in DATA_DIRS), file=sys.stderr)
        return 2
    reports = []
    for path in files:
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            return 2
        family = args.family or contract.family_of(path.resolve())
        if family is None:
            print(f"{path}: not a known collector output — pass --family ({', '.join(sorted(contract.FAMILIES))})",
                  file=sys.stderr)
            return 2
        reports.append(contract.validate_file(path, family, examples=args.examples))

    to_stdout = args.json is not None and str(args.json) == "-"
    if not to_stdout:
        for report in reports:
            print("\n".join(report.lines()))
    failed = [report for report in reports
              if not report.ok or (args.strict and (report.total("warning") or report.duplicates))]
    if args.json is not None:
        text = json.dumps({"ok": not failed, "files": [report.to_dict() for report in reports]}, indent=2)
        if to_stdout:
            print(text)
        else:
            args.json.write_text(text + "\n")
    if not to_stdout:
        rows = sum(report.rows for report in reports)
        print(f"\n{len(reports)} files, {rows:,} rows: " + (f"{len(failed)} failed" if failed else "all conform"))
    return 1 if failed else 0
