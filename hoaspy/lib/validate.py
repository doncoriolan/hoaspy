"""Check collector output against the record contract (hoaspy/lib/contract.py).

    ./venv/bin/python -m hoaspy.lib.validate                         # every output file under records/ liens/ courts/ news/
    ./venv/bin/python -m hoaspy.lib.validate liens/liens_cook.jsonl  # some files
    ./venv/bin/python -m hoaspy.lib.validate --family lien /tmp/x.jsonl   # a file kept somewhere else
    ./venv/bin/python -m hoaspy.lib.validate --json report.json      # the same report, machine-readable

Run it after a collector finishes and before you hand its output to anyone.
For each file it prints the row count and one line per kind of problem with
the first line number it occurs on:

    records/associations.jsonl  [registry]  FAILED: 44,813 rows, 2 unusable, 37 with contact details
      error    empty `name`: 2 rows  e.g. line 31208
      contact  contact `manager_address`: 35 rows  e.g. line 912

Exit status: 0 when no file has an error or a contact detail (warnings do not
fail; `--strict` makes them), 1 otherwise, 2 when a file is not a known
collector output and no `--family` was given. A contact detail is reported by
row and key only — the report never repeats the phone number or the address.
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
            found += [p for p in sorted((root / name).glob("*.jsonl")) if contract.family_of(p.relative_to(root))]
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hoaspy.lib.validate", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path, help="JSON Lines files (default: every collector output file)")
    ap.add_argument("--family", choices=sorted(contract.FAMILIES),
                    help="the family of the files named, when their names do not say")
    ap.add_argument("--strict", action="store_true", help="fail on warnings too")
    ap.add_argument("--json", type=Path, metavar="PATH", help="also write the reports as JSON (- for stdout)")
    ap.add_argument("--examples", type=int, default=3, help="line numbers kept per kind of problem (default 3)")
    args = ap.parse_args(argv)

    files = args.files or output_files()
    if not files:
        print("no collector output found under " + ", ".join(f"{d}/" for d in DATA_DIRS), file=sys.stderr)
        return 2
    reports = []
    for path in files:
        if not path.is_file():
            print(f"{path}: no such file", file=sys.stderr)
            return 2
        family = args.family or contract.family_of(path.resolve())
        if family is None:
            print(f"{path}: not a known collector output — pass --family "
                  f"({', '.join(sorted(contract.FAMILIES))})", file=sys.stderr)
            return 2
        reports.append(contract.validate_file(path, family, examples=args.examples))

    to_stdout = args.json is not None and str(args.json) == "-"
    if not to_stdout:
        for report in reports:
            print("\n".join(report.lines()))
    failed = [r for r in reports if not r.ok or (args.strict and (r.total("warning") or r.duplicates))]
    if args.json is not None:
        text = json.dumps({"ok": not failed, "files": [r.to_dict() for r in reports]}, indent=2)
        if to_stdout:
            print(text)
        else:
            args.json.write_text(text + "\n")
    if not to_stdout:
        rows = sum(r.rows for r in reports)
        print(f"\n{len(reports)} files, {rows:,} rows: " + (f"{len(failed)} failed" if failed else "all conform"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
