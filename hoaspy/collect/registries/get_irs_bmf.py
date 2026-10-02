#!/usr/bin/env python
"""Homeowners / condominium associations in the IRS Exempt Organizations
Business Master File (EO BMF) — every state.

    ./venv/bin/python -m hoaspy.collect.registries.get_irs_bmf                # all states
    ./venv/bin/python -m hoaspy.collect.registries.get_irs_bmf --state OK WY  # a few

The BMF is the IRS's public roster of organizations that hold a ruling of
tax exemption. Most HOAs file Form 1120-H and never appear here, but the
ones that sought 501(c)(4)/(c)(7) status do — with EIN, address, ruling
date, NTEE code (L50 = "Homeowners Associations") and the latest reported
income/assets. It is the only free, nationwide, government-published list
of associations, so it is the floor for the 30+ states that publish no
registry or corporate bulk file.

Selection: NTEE code L50, or an association-looking name (same regex as
get_states.py). Regional files eo1/eo2/eo3.csv (US) are cached under
.cache/irs/ and downloaded when missing.

Output: records/irs_exempt_orgs.jsonl — registry-shaped rows (one per EIN)
that build_site.py folds in exactly like a state HOA registry, with
source "irs-eo-bmf". coverage.json gets a collected["irs_exempt"] entry
per state.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import requests

from hoaspy.collect.registries.get_states import ASSOC_RE, update_coverage, write_merged

from hoaspy import ROOT
CACHE = ROOT / ".cache" / "irs"
OUT = ROOT / "records" / "irs_exempt_orgs.jsonl"
PAGE = "https://www.irs.gov/charities-non-profits/exempt-organizations-business-master-file-extract-eo-bmf"
FILES = {n: f"https://www.irs.gov/pub/irs-soi/{n}.csv" for n in ("eo1", "eo2", "eo3")}
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
SOURCE = "irs-eo-bmf"

SUBSECTION = {
    "03": "501(c)(3)", "04": "501(c)(4)", "05": "501(c)(5)", "06": "501(c)(6)",
    "07": "501(c)(7)", "08": "501(c)(8)", "10": "501(c)(10)", "12": "501(c)(12)",
    "13": "501(c)(13)", "19": "501(c)(19)",
}
STATUS = {"01": "exempt", "02": "conditional exemption", "12": "trust (Form 990-PF)",
          "25": "terminated (990-N filer)"}
STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

log = logging.getLogger("irs")


def ensure_files(session: requests.Session) -> list[Path]:
    CACHE.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, url in FILES.items():
        p = CACHE / f"{name}.csv"
        if not p.exists() or p.stat().st_size < 1_000_000:
            log.info("downloading %s", url)
            resp = session.get(url, timeout=900)
            resp.raise_for_status()
            p.write_bytes(resp.content)
        paths.append(p)
    return paths


def _ruling(v: str) -> str:
    v = (v or "").strip()
    return f"{v[:4]}-{v[4:6]}" if len(v) >= 6 and v.isdigit() else ""


def _amt(v: str):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def is_association(row: dict) -> bool:
    if (row.get("NTEE_CD") or "").startswith("L50"):
        return True
    return bool(ASSOC_RE.search(row.get("NAME") or "")
                or ASSOC_RE.search(row.get("SORT_NAME") or ""))


def to_record(row: dict) -> dict:
    name = " ".join((row.get("NAME") or "").split())
    sort_name = " ".join((row.get("SORT_NAME") or "").split())
    if sort_name and not ASSOC_RE.search(name) and ASSOC_RE.search(sort_name):
        name = sort_name           # e.g. NAME is the management agent, SORT_NAME the HOA
    sub = SUBSECTION.get(row.get("SUBSECTION") or "", f"501(c)({int(row['SUBSECTION'])})"
                         if (row.get("SUBSECTION") or "").isdigit() else "")
    status = STATUS.get(row.get("STATUS") or "", row.get("STATUS") or "")
    ruling = _ruling(row.get("RULING") or "")
    ein = (row.get("EIN") or "").strip()
    return {
        "state": row["STATE"],
        "source": SOURCE,
        "source_url": PAGE,
        "record_id": ein,
        "ein": f"{ein[:2]}-{ein[2:]}" if len(ein) == 9 else ein,
        "name": name,
        "status": f"{sub} {status}".strip(),
        "status_detail": f"IRS ruling {ruling}" if ruling else "",
        "recorded_date": ruling,
        "address": " ".join((row.get("STREET") or "").split()),
        "city": " ".join((row.get("CITY") or "").split()).title(),
        "county": "",
        "zip": (row.get("ZIP") or "")[:5],
        "in_care_of": " ".join((row.get("ICO") or "").replace("%", "").split()),
        "ntee": row.get("NTEE_CD") or "",
        "subsection": sub,
        "tax_period": row.get("TAX_PERIOD") or "",
        "income_amt": _amt(row.get("INCOME_AMT")),
        "asset_amt": _amt(row.get("ASSET_AMT")),
        "units": None,
        "manager_name": "",
    }


def collect(paths: list[Path], states: set[str] | None) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        with p.open(newline="", encoding="latin-1") as fh:
            for row in csv.DictReader(fh):
                st = (row.get("STATE") or "").strip()
                if st not in STATES or (states and st not in states):
                    continue
                if not is_association(row):
                    continue
                rec = to_record(row)
                if not rec["name"] or rec["record_id"] in seen:
                    continue
                seen.add(rec["record_id"])
                out.append(rec)
        log.info("%s: %d association rows so far", p.name, len(out))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", nargs="+", help="limit to these state codes")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    started = time.time()
    paths = ensure_files(session)
    states = {s.upper() for s in args.state} if args.state else None
    rows = collect(paths, states)

    by_state = Counter(r["state"] for r in rows)
    total = write_merged(OUT, rows, set(by_state) | (states or set()))
    for st, n in sorted(by_state.items()):
        update_coverage(st, "irs_exempt", {
            "source": "IRS Exempt Organizations Business Master File",
            "records": n, "url": PAGE,
            "note": "501(c) HOAs/condos only (NTEE L50 or association name); 1120-H filers absent",
        })
        print(f"  {st}: {n:,} tax-exempt associations")
    log.info("%s: %d rows total (%d fresh) in %.0fs", OUT.name, total, len(rows),
             time.time() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
