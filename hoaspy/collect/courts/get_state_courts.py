"""Trial-court cases naming the associations we hold, state by state, from
each state's own public court-records portal.

    python -m hoaspy state-courts --list                                   # the adapters
    python -m hoaspy state-courts --portal ct_civil -v                     # one portal
    python -m hoaspy state-courts --portal md_casesearch --cookie-file md_cookie.txt
    python -m hoaspy state-courts --all                                    # every anonymous adapter

CourtListener gives federal dockets and state *appellate* opinions
nationwide (the courts command); re:SearchTX gives Texas trial courts
(tx-research). Every other state's trial courts live in that state's (or
county's) portal. This driver queries each portal **by association name**
— the names held in records/ (registries, corporate rosters, the IRS
roster) — through a per-portal adapter in court_portals/, and writes
courts/trial_<KEY>.jsonl in the shared docket shape. An adapter may add
`SWEEPS`, statewide prefixes queried after the names (Maryland's "council
of unit owners"), to catch communities no roster holds.

Each portal run is checkpointed per name (courts/.trial_<KEY>_partial.jsonl
and courts/.trial_<KEY>_done.txt), so it can be killed and resumed;
`--fresh` starts over. The output is written on every stop. Every portal
is best-effort by construction: name-match recall is partial and
unmeasured, and portals cap or page their results.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

from hoaspy import ROOT
from hoaspy.collect.courts import COURTS_DIR, court_portals
from hoaspy.collect.courts.court_portals import RateLimited
from hoaspy.collect.courts.court_portals._common import ASSOCIATION_RE
from hoaspy.lib.checkpoint import NameCheckpoint
from hoaspy.lib.jsonl import merge_json, now_iso, read_json, read_rows, write_rows

NAME_FILES = ("associations.jsonl", "state_corps.jsonl", "state_registries.jsonl", "irs_exempt_orgs.jsonl")
EXIT_COOKIE_NEEDED, EXIT_NO_NAMES, EXIT_EXPIRED, EXIT_RATE_LIMITED, EXIT_INTERRUPTED = 2, 1, 3, 4, 130

log = logging.getLogger(__name__)


# -- the names to query -------------------------------------------------------------

def association_names(state: str, limit: int | None = None, counties: set[str] | None = None) -> list[str]:
    """The distinct association-shaped names held for `state`, from every
    records file, longest first (a fuller name is a tighter query).
    `counties` (upper-cased) limits county-scoped portals to the
    associations that sit in those counties; rows without a county are
    then skipped."""
    seen: set[str] = set()
    names: list[str] = []
    for filename in NAME_FILES:
        for row in read_rows(ROOT / "records" / filename, skip_bad=True):
            if row.get("state") != state:
                continue
            if counties and (row.get("county") or "").upper() not in counties:
                continue
            name = " ".join((row.get("name") or "").split())
            if name and name.upper() not in seen and ASSOCIATION_RE.search(name):
                seen.add(name.upper())
                names.append(name)
    names.sort(key=len, reverse=True)
    return names[:limit] if limit else names


def portal_names(module, args: argparse.Namespace) -> list[str]:
    """What one portal run queries: `--names FILE` verbatim, else the names
    held for the portal's state followed by the adapter's SWEEPS
    (`--no-sweeps` leaves them out)."""
    if args.names:
        return [line.strip() for line in args.names.read_text().splitlines() if line.strip()]
    names = association_names(module.STATE, args.limit, getattr(module, "COUNTIES", None))
    if not getattr(args, "no_sweeps", False):
        names += [sweep for sweep in getattr(module, "SWEEPS", ()) if sweep not in names]
    return names


# -- the output ---------------------------------------------------------------------

def dedupe(rows: list[dict]) -> list[dict]:
    """Merge the records of one case (by case_data_id) found under several
    queried names: the associations and queries are unioned."""
    by_id: dict[str, dict] = {}
    for row in rows:
        key = f'{row.get("source")}:{row.get("case_data_id") or row.get("docket_number")}'
        current = by_id.get(key)
        if current is None:
            by_id[key] = dict(row, associations=list(row["associations"]), queries=list(row.get("queries", [])))
            continue
        for association in row["associations"]:
            if association not in current["associations"]:
                current["associations"].append(association)
        for query in row.get("queries", []):
            if query not in current["queries"]:
                current["queries"].append(query)
    return list(by_id.values())


def write_outputs(key: str, module, rows: list[dict], out_dir: Path) -> Path:
    """courts/trial_<KEY>.jsonl and the portal's entry in courts/sources.json.
    An empty result never clobbers an existing file."""
    out = out_dir / f"trial_{key}.jsonl"
    rows = dedupe(rows)
    if not rows:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no records this run — keeping existing %s untouched", out.name)
        return out
    write_rows(out, rows)
    entry = dict(module.INFO)
    entry.update({"state": module.STATE, "records": len(rows),
                  "associations": len({name for row in rows for name in row["associations"]}),
                  "courts": len({row["court"] for row in rows}), "retrieved_at": now_iso()})
    catalog = read_json(out_dir / "sources.json", {})
    catalog.setdefault("trial_courts", {})[key] = entry
    merge_json(out_dir / "sources.json", "trial_courts", catalog["trial_courts"])
    return out


# -- one portal run -----------------------------------------------------------------

def search_with_retries(client, name: str, key: str, wait_on_quota: bool) -> tuple[list[dict] | None, int]:
    """One portal search: (its rows, or None when the name was skipped; an
    exit code when the run must stop). A quota hit is slept off when asked,
    an expired session stops the run resumably, any other error skips the name."""
    while True:
        try:
            return client.search(name), 0
        except PermissionError as exc:
            print(f"\n{key}: {exc}\nProgress is checkpointed — rerun to resume "
                  "(with a fresh cookie if the portal needs one).", file=sys.stderr)
            return None, EXIT_EXPIRED
        except Exception as exc:
            # Adapters raise RateLimited; a bare HTTP 429 from requests counts too,
            # so a quota never burns names.
            if isinstance(exc, RateLimited) or " 429 " in f" {exc} ":
                retry_after = getattr(exc, "retry_after", 0) or 600
                if not wait_on_quota:
                    print(f"\n{key}: rate limited ({exc}). Progress is checkpointed — rerun to resume, "
                          "or add --wait-on-quota.", file=sys.stderr)
                    return None, EXIT_RATE_LIMITED
                nap = max(retry_after, 60) + 5
                log.warning("%s: rate limited — sleeping %ds then retrying", key, nap)
                time.sleep(nap)
                continue
            log.warning("  %s -> skipped (%s: %s)", name, type(exc).__name__, str(exc)[:120])
            return None, 0


def run_portal(key: str, module, args: argparse.Namespace) -> int:
    cookie = None
    if getattr(module, "NEEDS_COOKIE", False):
        if args.cookie_file and args.cookie_file.exists():
            cookie = args.cookie_file.read_text().strip()
        if not cookie:
            print(f"{key}: needs a Cookie header — pass --cookie-file (see the adapter's docstring, "
                  f"hoaspy/collect/courts/court_portals/{module.__name__.split('.')[-1]}.py).", file=sys.stderr)
            return EXIT_COOKIE_NEEDED
    names = portal_names(module, args)
    if not names:
        print(f"{key}: no {module.STATE} association names in records/", file=sys.stderr)
        return EXIT_NO_NAMES
    args.out.mkdir(parents=True, exist_ok=True)
    checkpoint = NameCheckpoint(args.out, f"trial_{key}")
    if args.fresh:
        checkpoint.clear()
    rows, done = checkpoint.load()
    todo = [name for name in names if name not in done]
    log.info("%s: %d %s names (%d done, %d to query)", key, len(names), module.STATE, len(names) - len(todo), len(todo))
    client = module.Client(cookie=cookie, pace=args.pace)
    code = 0
    try:
        with checkpoint:
            for i, name in enumerate(todo, 1):
                found, code = search_with_retries(client, name, key, args.wait_on_quota)
                if code:
                    break
                if found is None:
                    continue
                for row in found:
                    row.setdefault("source", key)
                    row.setdefault("state", module.STATE)
                rows.extend(found)
                checkpoint.add(name, found)
                if found or i % 25 == 0:
                    log.info("  [%d/%d] %s -> %d case(s)", i, len(todo), name, len(found))
                time.sleep(args.pace)
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed; rerun to resume.", file=sys.stderr)
        code = EXIT_INTERRUPTED
    out = write_outputs(key, module, rows, args.out)
    log.info("%s: wrote %s — %d cases, %d associations, %d courts", key, out.name, len(rows),
             len({name for row in rows for name in row["associations"]}), len({row["court"] for row in rows}))
    if code == 0:
        checkpoint.clear()
    return code


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--list", action="store_true", help="list the adapters and exit")
    parser.add_argument("--portal", action="append", help="adapter KEY to run (repeatable)")
    parser.add_argument("--all", action="store_true", help="run every adapter that needs no cookie")
    parser.add_argument("--cookie-file", type=Path, help="Cookie header file for portals that need one")
    parser.add_argument("--names", type=Path, help="override: a file of names, one per line")
    parser.add_argument("--limit", type=int, help="cap the number of names per portal")
    parser.add_argument("--no-sweeps", action="store_true",
                        help="skip the adapter's statewide SWEEPS prefixes, query held names only")
    parser.add_argument("--pace", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--out", type=Path, default=COURTS_DIR)
    parser.add_argument("--fresh", action="store_true", help="ignore checkpoints")
    parser.add_argument("--wait-on-quota", action="store_true")


def run(args: argparse.Namespace) -> int:
    adapters = court_portals.registry()
    if args.list or not (args.portal or args.all):
        for key, module in sorted(adapters.items()):
            access = "cookie" if getattr(module, "NEEDS_COOKIE", False) else "anon  "
            print(f"{key:22s} {module.STATE}  {access}  {module.INFO.get('name', '')}")
        return 0
    keys = args.portal or [key for key, module in adapters.items() if not getattr(module, "NEEDS_COOKIE", False)]
    worst = 0
    for key in keys:
        if key not in adapters:
            print(f"unknown portal {key!r}; --list shows the adapters", file=sys.stderr)
            return 2
        worst = max(worst, run_portal(key, adapters[key], args))
    return worst
