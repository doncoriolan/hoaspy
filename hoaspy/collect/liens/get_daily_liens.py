"""New lien-family recordings, a day at a time — Broward and Miami-Dade —
for the lien alerts (docs/ALERTS.md): liens/daily/<county>/<YYYY-MM-DD>.jsonl.

    python -m hoaspy daily-liens                        # every recording day not yet fetched
    python -m hoaspy daily-liens --county broward       # one county
    python -m hoaspy daily-liens --days 14              # how far back to look (default 10)
    python -m hoaspy daily-liens --refetch 2026-10-20   # a day again

A recording day is done when its file exists; a rerun fetches only the
days that are missing, so a missed run is caught up by the next one.

Broward: the `Official_Records_Download` folder of the public SFTP server
(records_broward) holds the last ten or so recording days as
MM-DD-YYYY{doc,nme,lgl}-ver.txt in the yearly exports' layout, posted
one to four days after the recording day and at any hour; weekends have
no files. The yearly parser reads them (the association gate, the
lien-family document types) and `source` is `broward-or-daily-download`.

Miami-Dade: the Clerk's search API behind the public Official Records page
takes a Cloudflare Turnstile token on every search, so the day-window
query (`records_miamidade.enumerate_window`) runs through the page itself
in a local browser (hoaspy/lib/browser.py, DevTools on 127.0.0.1:9100;
README "Browser-driven collectors"), `records_miamidade.BrowserSession`.
When no browser answers the county is skipped and the summary says so; a
day is written only when every document type was searched, an empty day
included (a Sunday). Days up to yesterday are fetched: today's recordings
are still being indexed.

Every file holds Lien records (hoaspy/lib/records.py) in the shared
vocabulary (docs/LIENS.md); liens/daily/<county>/status.json records the
last run. The site build does not read these files — the yearly export
and the per-association sweep remain the record of the counties — the
alert sender (hoaspy/server/alerts.py) does.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from hoaspy import ROOT
from hoaspy.collect.liens import LIENS_DIR, records_broward, records_miamidade
from hoaspy.lib.jsonl import now_iso, read_json, write_json, write_rows

log = logging.getLogger(__name__)

DAILY_DIR = LIENS_DIR / "daily"
CACHE_DIR = ROOT / ".cache" / "broward_daily"
COUNTIES = ("broward", "miami-dade")
DEFAULT_DAYS = 10


def day_file(out_dir: Path, county: str, day: date) -> Path:
    return out_dir / county / f"{day.isoformat()}.jsonl"


def pending_days(out_dir: Path, county: str, candidates: list[date], refetch: set[date] = frozenset()) -> list[date]:
    """The candidate days without a file yet (plus the ones asked for
    again), oldest first."""
    return sorted(day for day in set(candidates) if day in refetch or not day_file(out_dir, county, day).exists())


def write_day(out_dir: Path, county: str, day: date, rows: list[dict]) -> Path:
    path = day_file(out_dir, county, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_rows(path, rows)
    return path


def write_status(out_dir: Path, county: str, **fields) -> None:
    path = out_dir / county / "status.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    status = read_json(path, {})
    status.update(fields)
    write_json(path, status)


def fetch_broward(out_dir: Path, cache_dir: Path, lookback: int, refetch: set[date], today: date) -> list[str]:
    """Every pending day the daily folder holds; what was written."""
    retrieved_at = now_iso()
    transport, sftp = records_broward.connect()
    written = []
    try:
        listed = records_broward.daily_days(sftp)
        oldest = today - timedelta(days=lookback)
        complete = [day for day, parts in listed.items() if {"doc", "nme"} <= parts and day >= oldest]
        for day in pending_days(out_dir, "broward", complete, refetch):
            records = records_broward.fetch_day(sftp, day, cache_dir, retrieved_at=retrieved_at)
            path = write_day(out_dir, "broward", day, [record.as_row() for record in records])
            log.info("broward %s: %d association lien-family documents -> %s", day, len(records), path.name)
            written.append(day.isoformat())
    finally:
        transport.close()
    write_status(out_dir, "broward", retrieved_at=retrieved_at, days_written=written, last_error="",
                 source="Broward County Records, Taxes and Treasury — Official Records daily index",
                 access=f"sftp://{records_broward.SFTP_HOST}/{records_broward.DAILY_DIR} (public credentials)",
                 page=records_broward.SOURCE_PAGE, folder_days=sorted(day.isoformat() for day in listed))
    return written


def fetch_miami_dade(out_dir: Path, lookback: int, refetch: set[date], today: date, pace: float,
                     devtools_url: str | None = None) -> list[str]:
    """Every pending day up to yesterday, through the browser; what was written."""
    retrieved_at = now_iso()
    candidates = [today - timedelta(days=n) for n in range(1, lookback + 1)]
    days = pending_days(out_dir, "miami-dade", candidates, refetch)
    if not days:
        return []
    session = records_miamidade.BrowserSession(devtools_url)
    written = []
    try:
        for day in days:
            rows, truncated = records_miamidade.enumerate_window(session, day, day, pace=pace,
                                                                 retrieved_at=retrieved_at)
            path = write_day(out_dir, "miami-dade", day, rows)
            log.info("miami-dade %s: %d association lien-family documents -> %s%s", day, len(rows), path.name,
                     " (TRUNCATED: " + ", ".join(truncated) + ")" if truncated else "")
            written.append(day.isoformat())
    finally:
        session.close()
    write_status(out_dir, "miami-dade", retrieved_at=retrieved_at, days_written=written, last_error="",
                 source="Miami-Dade Clerk of the Court & Comptroller — Official Records search, by recording day",
                 access="the public search page in a local browser (Turnstile token minted by the page's own widget)",
                 page=records_miamidade.SEARCH_PAGE)
    return written


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--county", choices=COUNTIES, help="one county (default: both)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="recording days back from today to consider")
    parser.add_argument("--refetch", type=date.fromisoformat, nargs="*", default=[], help="days to fetch again")
    parser.add_argument("--out", type=Path, default=DAILY_DIR, help="where the per-day files go")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR, help="where Broward's daily index files are kept")
    parser.add_argument("--pace", type=float, default=0.5, help="seconds between Miami-Dade searches")
    parser.add_argument("--devtools", help="the local browser's DevTools URL (default http://127.0.0.1:9100)")


def run(args: argparse.Namespace) -> int:
    started = time.time()
    today = date.today()
    refetch = set(args.refetch)
    counties = [args.county] if args.county else list(COUNTIES)
    failed = False
    summary = []
    if "broward" in counties:
        try:
            written = fetch_broward(args.out, args.cache, args.days, refetch, today)
            summary.append(f"broward: {len(written)} day(s) written" + (f" ({', '.join(written)})" if written else ""))
        except Exception as exc:
            failed = True
            write_status(args.out, "broward", last_error=f"{type(exc).__name__}: {exc}", failed_at=now_iso())
            summary.append(f"broward: FAILED — {type(exc).__name__}: {exc}")
    if "miami-dade" in counties:
        try:
            written = fetch_miami_dade(args.out, args.days, refetch, today, args.pace, args.devtools)
            summary.append(f"miami-dade: {len(written)} day(s) written"
                           + (f" ({', '.join(written)})" if written else ""))
        except PermissionError as exc:
            # No browser: not a failure of the source, so the exit status
            # stays clean when Broward went through; the summary says so.
            write_status(args.out, "miami-dade", last_error=str(exc), failed_at=now_iso())
            summary.append(f"miami-dade: SKIPPED — {exc}")
        except Exception as exc:
            failed = True
            write_status(args.out, "miami-dade", last_error=f"{type(exc).__name__}: {exc}", failed_at=now_iso())
            summary.append(f"miami-dade: FAILED — {type(exc).__name__}: {exc}")
    print("\n".join(summary))
    print(f"{time.time() - started:.0f}s; files under {args.out}/", file=sys.stderr)
    return 1 if failed else 0
