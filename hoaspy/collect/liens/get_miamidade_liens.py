#!/usr/bin/env python
"""Collect HOA lien-family records from Miami-Dade County Official Records.

Miami-Dade does not sell a bulk index the way Broward's SFTP does, so this
queries the Clerk's public search API one association at a time — the mode that
stays under the API's 500-row result cap (see records_miamidade for the why).

    ./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie '<.PremierIDDade value>'
    ./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie ... --limit 200 --no-upload
    ./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie ... --names names.txt

The `.PremierIDDade` cookie is an anonymous session cookie (no login); copy its
value from a browser at onlineservices.miamidadeclerk.gov (DevTools → Application
→ Cookies). It is never committed.

Coverage is BEST-EFFORT, not complete, and the sources.json says so:
  * an association is found only when our name matches the Clerk's spelling
    (their "AMARETTO HOMEOWNERS ASSN INC" vs our "AMARETTO CONDO" misses);
  * a name broad enough to hit the 500-row cap is flagged, not silently cut.
For a complete historical index, buy the $110/mo Official Records FTP folder
(see docs/NEEDS.md). This collector is for targeted, per-association coverage.

Output (default ./liens/):
    liens_miamidade.jsonl   one record per lien-family document
    liens_miamidade.csv     flattened
    sources.json            provenance + coverage caveats (written per-region)
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from hoaspy.collect.liens import records_miamidade as md
from hoaspy.lib import s3_sync
from hoaspy import ROOT
DEFAULT_OUT = ROOT / "liens"
DEFAULT_PREFIX = "hoa-liens/"

log = logging.getLogger("liens.miamidade")

FIELDS = [
    "doc_id", "cfn", "year", "recorded_date", "doc_type", "doc_type_label",
    "state", "county", "association", "query_name", "respondent", "amount",
    "case_number", "parcel_id", "book_page", "address", "legal_description",
    "n_parties",
]
SYNCED = ["liens_miamidade.jsonl", "liens_miamidade.csv", "sources.json"]


def query_name(name: str) -> str:
    """Turn one of our association names into a party-name query the Clerk's
    index is likely to match: strip punctuation and filing noise, abbreviate the
    compass words the county abbreviates, and keep the distinctive head so the
    query neither misses nor broadens into a 500-row cap."""
    n = name.upper()
    n = re.sub(r"[,\.'\"]", " ", n)
    n = re.sub(r"\bCONDOMINIUM\b", "CONDO", n)
    n = re.sub(r"\bWEST\b", "W", n)
    n = re.sub(r"\bEAST\b", "E", n)
    n = re.sub(r"\bNORTH\b", "N", n)
    n = re.sub(r"\bSOUTH\b", "S", n)
    # trailing filing noise that the lien index usually omits
    n = re.sub(r"\b(A|AN|THE)\b", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def load_names(args) -> list[str]:
    if args.names:
        raw = [l.strip() for l in Path(args.names).read_text().splitlines() if l.strip()]
    else:
        raw = _miamidade_association_names(args.assoc_file, args.all_florida)
    seen, out = set(), []
    for n in raw:
        q = query_name(n)
        if len(q) < 4 or q in seen:
            continue
        seen.add(q)
        out.append(q)
    if args.limit:
        out = out[: args.limit]
    return out


def _miamidade_association_names(path: Path, all_florida: bool) -> list[str]:
    names = []
    with path.open() as fh:
        for line in fh:
            d = json.loads(line)
            if d.get("state") != "FL":
                continue
            county = (d.get("county") or "").strip().lower()
            if all_florida or county in ("miami-dade", "miami dade", "dade"):
                if d.get("name"):
                    names.append(d["name"])
    return names


def checkpoint_paths(out_dir: Path) -> tuple[Path, Path]:
    """(records-so-far, names-done). A full county run is thousands of requests
    over hours, so every name's results are appended as they arrive and the
    run resumes from where it stopped."""
    return out_dir / ".miamidade_partial.jsonl", out_dir / ".miamidade_done.txt"


def load_checkpoint(out_dir: Path) -> tuple[list[dict], set[str]]:
    part, done = checkpoint_paths(out_dir)
    records: list[dict] = []
    if part.exists():
        for line in part.open():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn final line from a hard kill
    names = set()
    if done.exists():
        names = {l.strip() for l in done.open() if l.strip()}
    return records, names


def clear_checkpoint(out_dir: Path) -> None:
    for p in checkpoint_paths(out_dir):
        p.unlink(missing_ok=True)


def write_outputs(records: list[dict], sources: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "liens_miamidade.jsonl").open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (out_dir / "liens_miamidade.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            w.writerow({**r, "respondent": "; ".join(r.get("respondents") or [])[:200]})
    # sources.json is per-region: merge, don't clobber Broward's entry.
    spath = out_dir / "sources.json"
    existing = {}
    if spath.exists():
        try:
            existing = json.loads(spath.read_text())
        except json.JSONDecodeError:
            existing = {}
        if existing and "miami_dade" not in existing and "source" in existing:
            existing = {"broward": existing}  # migrate the old single-region file
    existing["miami_dade"] = sources
    spath.write_text(json.dumps(existing, indent=2))


def print_summary(records: list[dict], truncated: list[str]) -> None:
    if not records:
        print("\nNo lien-family records matched any queried name.")
        return
    by_type = Counter(r["doc_type_label"] for r in records)
    filers = Counter(r["association"] or r["query_name"] for r in records
                     if r["doc_type"] in ("LIE",))
    with_parcel = sum(1 for r in records if r.get("parcel_id"))
    print(f"\n{len(records):,} lien-family documents")
    print(f"  {with_parcel:,} carry a folio/parcel id ({with_parcel/len(records)*100:.0f}%)")
    print("\nBy document type")
    for k, v in by_type.most_common():
        print(f"  {k:<28} {v:>6,}")
    print("\nMost active lien filers")
    for name, n in filers.most_common(12):
        print(f"  {n:>4}  {name[:64]}")
    if truncated:
        print(f"\n⚠ {len(truncated)} query(ies) hit the 500-row cap and are "
              f"incomplete — first few: {', '.join(truncated[:3])}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie", required=True,
                    help="the .PremierIDDade cookie value (with or without name=)")
    ap.add_argument("--names", type=Path, help="file of association names, one per line")
    ap.add_argument("--assoc-file", type=Path, default=ROOT / "records" / "associations.jsonl",
                    help="source of association names when --names is not given")
    ap.add_argument("--all-florida", action="store_true",
                    help="query every FL association, not just Miami-Dade county")
    ap.add_argument("--types", nargs="+", help="override Miami-Dade document types")
    ap.add_argument("--limit", type=int, help="cap the number of names queried")
    ap.add_argument("--pace", type=float, default=0.4, help="seconds between requests")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--config", type=Path, default=ROOT / "config.yml")
    ap.add_argument("--prefix", default=DEFAULT_PREFIX)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore any checkpoint and start the run over")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    if (env := ROOT / ".env").exists():
        load_dotenv(env)
    cfg = {}
    if args.config.exists():
        cfg = yaml.safe_load(args.config.read_text()) or {}

    try:
        names = load_names(args)
    except FileNotFoundError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    if not names:
        print("\nNo association names to query.\n", file=sys.stderr)
        return 1

    started = time.time()
    truncated: list[str] = []

    try:
        s = md.session(args.cookie)
    except md.MiamiDadeError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        clear_checkpoint(args.out)
    prior, done_names = load_checkpoint(args.out)
    if prior:
        log.info("resuming: %d records from %d names already collected",
                 len(prior), len(done_names))
    log.info("querying %d Miami-Dade association names (%d to go)",
             len(names), len([n for n in names if n not in done_names]))

    part_path, done_path = checkpoint_paths(args.out)
    part_fh = part_path.open("a")
    done_fh = done_path.open("a")

    def on_name_done(name, fresh):
        for r in fresh:
            part_fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        done_fh.write(name + "\n")
        part_fh.flush()
        done_fh.flush()

    def progress(i, total, name, found):
        if args.verbose or i % 50 == 0 or i == total:
            log.info("  %d/%d names, %d new records (…%s)", i, total, found, name[:30])

    try:
        new = md.fetch_for_names(s, names, doctypes=args.types, pace=args.pace,
                                 progress=progress, skip=done_names,
                                 seen_ids={r["doc_id"] for r in prior},
                                 on_name_done=on_name_done)
    except md.MiamiDadeError as exc:
        part_fh.close(); done_fh.close()
        print(f"\nMiami-Dade collection failed: {exc}", file=sys.stderr)
        print(f"Progress is checkpointed — rerun the same command to resume.\n",
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        part_fh.close(); done_fh.close()
        print("\nInterrupted — progress checkpointed; rerun to resume.\n",
              file=sys.stderr)
        return 130
    finally:
        if not part_fh.closed:
            part_fh.close()
        if not done_fh.closed:
            done_fh.close()

    records = prior + new

    now = datetime.now(timezone.utc).isoformat()
    for r in records:
        r["retrieved_at"] = now

    sources = {
        "retrieved_at": now,
        "source": "Miami-Dade Clerk of the Court & Comptroller — Official Records search API",
        "access": "public search API (anonymous .PremierIDDade cookie); no login, no bulk feed",
        "page": md.SEARCH_PAGE,
        "method": "per-association party-name query",
        "document_types": args.types or md.DEFAULT_TYPES,
        "names_queried": len(names),
        "records": len(records),
        "duration_seconds": round(time.time() - started, 1),
        "coverage": "best-effort, NOT complete",
        "caveat": (
            "Per-association name queries against the public search API. Coverage "
            "is bounded by (a) name-spelling agreement between our association "
            "list and the Clerk's party index, and (b) a 500-row result cap that "
            "truncates over-broad names and busy days. This is targeted coverage, "
            "not a full county index; the paid Official Records FTP folder is the "
            "complete source (see docs/NEEDS.md)."),
    }

    write_outputs(records, sources, args.out)
    clear_checkpoint(args.out)
    print_summary(records, truncated)
    print(f"\nWrote {len(records):,} records to {args.out}/liens_miamidade.jsonl")

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
