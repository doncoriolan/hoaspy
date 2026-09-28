#!/usr/bin/env python
"""Collect named community associations with locations from Florida and Texas
public records, and mirror them to S3.

Unlike the Reddit corpus, every record here carries a real association name and
a real location, so it can be joined to county land records, management
companies, or each other.

    ./venv/bin/python -m hoaspy.collect.registries.get_records                 # both states, push to S3
    ./venv/bin/python -m hoaspy.collect.registries.get_records --state FL      # one state
    ./venv/bin/python -m hoaspy.collect.registries.get_records --no-upload     # local only
    ./venv/bin/python -m hoaspy.collect.registries.get_records --county Harris --name-contains oaks

Output (default ./records/):
    associations.jsonl   one record per association, unified schema
    associations.csv     same, flattened for a spreadsheet
    sources.json         provenance: URLs fetched, row counts, timestamp
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

from hoaspy.collect.registries import records_fl
from hoaspy.collect.registries import records_tx
from hoaspy.lib import s3_sync
from hoaspy import ROOT
DEFAULT_OUT = ROOT / "records"
DEFAULT_PREFIX = "hoa-records/"

USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"

log = logging.getLogger("records")

FIELDS = [
    "id", "state", "source", "name", "type", "address", "city", "county", "zip",
    "units", "status", "status_detail", "recorded_date",
    "manager_name", "manager_city", "manager_state", "manager_zip",
    "document_url", "source_url", "retrieved_at",
]

SYNCED = ["associations.jsonl", "associations.csv", "sources.json"]


def record_id(rec: dict) -> str:
    """Stable id from the fields that identify a community.

    The state registries have no shared key, so this is derived rather than
    given — which also makes re-runs idempotent.
    """
    basis = "|".join([
        rec.get("state", ""),
        rec.get("source", ""),
        rec.get("record_id", "") or rec.get("name", "").upper(),
        rec.get("zip", ""),
    ])
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


def collect(states: list[str], timeout: float) -> tuple[list[dict], dict]:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    now = datetime.now(timezone.utc).isoformat()

    records: list[dict] = []
    sources: dict = {"retrieved_at": now, "states": {}}

    if "FL" in states:
        log.info("Florida: fetching DBPR condominium extracts ...")
        fl = records_fl.fetch(session, timeout=timeout)
        records.extend(fl)
        sources["states"]["FL"] = {
            "source": "DBPR Division of Florida Condominiums, Timeshares and Mobile Homes",
            "page": records_fl.SOURCE_PAGE,
            "files": list(records_fl.EXTRACTS),
            "rows": len(fl),
        }

    if "TX" in states:
        log.info("Texas: fetching TREC management certificates ...")
        tx = records_tx.fetch(session, timeout=timeout)
        records.extend(tx)
        sources["states"]["TX"] = {
            "source": "Texas Real Estate Commission HOA management certificates",
            "page": records_tx.SOURCE_PAGE,
            "endpoint": records_tx.ENDPOINT,
            "rows": len(tx),
        }

    for rec in records:
        rec["id"] = record_id(rec)
        rec["retrieved_at"] = now

    # Same association can appear twice if an extract overlaps a region boundary.
    seen: set[str] = set()
    deduped = []
    for rec in records:
        if rec["id"] in seen:
            continue
        seen.add(rec["id"])
        deduped.append(rec)
    if len(deduped) != len(records):
        log.info("dropped %d duplicate rows", len(records) - len(deduped))

    sources["total_records"] = len(deduped)
    return deduped, sources


def apply_filters(records: list[dict], args) -> list[dict]:
    out = records
    if args.county:
        want = {c.lower() for c in args.county}
        out = [r for r in out if r.get("county", "").lower() in want]
    if args.name_contains:
        needle = args.name_contains.lower()
        out = [r for r in out if needle in r.get("name", "").lower()]
    if args.status:
        want = {s.lower() for s in args.status}
        out = [r for r in out
               if r.get("status_detail", "").lower() in want
               or r.get("status", "").lower() in want]
    if args.with_address:
        out = [r for r in out if r.get("address")]
    return out


def write_outputs(records: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    with (out_dir / "associations.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    with (out_dir / "associations.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)

    (out_dir / "sources.json").write_text(json.dumps(sources, indent=2))


def print_summary(records: list[dict]) -> None:
    if not records:
        print("\nNo records matched.")
        return

    by_state = Counter(r["state"] for r in records)
    with_addr = sum(1 for r in records if r.get("address"))
    with_mgr = sum(1 for r in records if r.get("manager_name"))
    counties = Counter(r["county"] for r in records if r.get("county"))
    flags = Counter(r["status_detail"] for r in records if r.get("status_detail"))
    mgrs = Counter(r["manager_name"] for r in records if r.get("manager_name"))

    print(f"\n{len(records)} associations  "
          f"({', '.join(f'{s} {n}' for s, n in sorted(by_state.items()))})")
    print(f"  {with_addr} with a street address, {with_mgr} with a named manager")

    def show(title: str, counter: Counter, n: int = 8) -> None:
        if not counter:
            return
        print(f"\n{title}")
        width = max(len(str(k)) for k, _ in counter.most_common(n))
        for key, count in counter.most_common(n):
            print(f"  {key:<{width}}  {count}")

    show("Counties", counties)
    show("Registration flags", flags)
    show("Managing entities", mgrs)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", action="append", choices=["FL", "TX"],
                    help="limit to one state (repeatable); default both")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--config", type=Path, default=ROOT / "config.yml")
    ap.add_argument("--county", action="append", help="filter by county (repeatable)")
    ap.add_argument("--name-contains", help="filter by substring of the association name")
    ap.add_argument("--status", action="append",
                    help="filter by registration status, e.g. Delinquent")
    ap.add_argument("--with-address", action="store_true",
                    help="keep only records that have a street address")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX, help="S3 key prefix")
    ap.add_argument("--no-upload", action="store_true", help="skip the S3 push")
    ap.add_argument("--force", action="store_true", help="overwrite an S3 conflict")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    cfg = {}
    if args.config.exists():
        cfg = yaml.safe_load(args.config.read_text()) or {}

    states = args.state or ["FL", "TX"]
    started = time.time()

    try:
        records, sources = collect(states, args.timeout)
    except requests.RequestException as exc:
        print(f"\nFetch failed: {exc}\n", file=sys.stderr)
        return 1

    if not records:
        print("\nNothing collected — both sources returned no rows.\n", file=sys.stderr)
        return 1

    filtered = apply_filters(records, args)
    sources["filters_applied"] = {
        k: v for k, v in {
            "county": args.county, "name_contains": args.name_contains,
            "status": args.status, "with_address": args.with_address or None,
        }.items() if v
    }
    sources["records_written"] = len(filtered)
    sources["duration_seconds"] = round(time.time() - started, 1)

    write_outputs(filtered, sources, args.out)
    print_summary(filtered)
    print(f"\nWrote {len(filtered)} records to {args.out}/")

    bucket, _ = s3_sync.resolve_target(cfg)
    if not args.no_upload and bucket:
        prefix = args.prefix if args.prefix.endswith("/") else args.prefix + "/"
        try:
            s3 = s3_sync.get_client(cfg)
            for warning in s3_sync.check_access(s3, bucket, prefix):
                log.warning(warning)
            pushed = s3_sync.push(s3, bucket, prefix, args.out,
                                  sse=(cfg.get("s3") or {}).get("sse", "AES256"),
                                  force=args.force, files=SYNCED)
            print(f"Mirrored {len(pushed)} file(s) to s3://{bucket}/{prefix}")
        except s3_sync.S3Error as exc:
            log.error("S3 push failed: %s", exc)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
