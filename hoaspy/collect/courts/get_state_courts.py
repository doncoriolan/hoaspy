#!/usr/bin/env python3
"""Collect trial-court cases naming our community associations, state by
state, from each state's own public court-records portal.

CourtListener gives us federal dockets and state *appellate* opinions
nationwide (get_courts.py); re:SearchTX gives Texas trial courts
(get_tx_research.py). Every other state's trial courts live in that state's
(or county's) portal. This driver queries each portal **by association
name** — the names we already hold in records/ — through a per-portal
adapter in hoaspy/collect/courts/court_portals/, and writes courts/trial_<KEY>.jsonl in the same
docket shape build_site.py already folds in.

    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --list                 # adapters
    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal ct_civil -v    # one portal
    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal ny_webcivil --cookie-file ny_cookie.txt
    ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --all                  # every adapter

Each portal run is checkpointed per name (courts/.trial_<KEY>_done.txt and
courts/.trial_<KEY>_partial.jsonl), so it can be killed and resumed; --fresh
starts over. Output is written on every stop so build_site can use partial
results. Every portal is best-effort by construction — name-match recall is
partial and unmeasured, and portals cap or page their results — so
build_site's per-state "not checked" footers stay in force; see docs/DATA.md.

Government sources only; --no-upload semantics (nothing leaves the machine).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from hoaspy.collect.courts import court_portals
from hoaspy.collect.courts.court_portals import RateLimited
from hoaspy.collect.courts.court_portals._common import ASSOCIATION_RE

from hoaspy import ROOT
OUT_DIR = ROOT / "courts"
log = logging.getLogger("state_courts")


# -- association names per state ---------------------------------------------
def association_names(state: str, limit: int | None = None,
                      counties: set[str] | None = None) -> list[str]:
    """Distinct association-shaped names we hold for `state`, from every
    records file, longest first (a fuller name is a tighter query).
    `counties` (upper-case) limits county-scoped portals to the associations
    that sit in that county; rows without a county are then skipped."""
    seen: set[str] = set()
    names: list[str] = []
    for fn in ("associations.jsonl", "state_corps.jsonl", "state_registries.jsonl"):
        p = ROOT / "records" / fn
        if not p.exists():
            continue
        with p.open() as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("state") != state:
                    continue
                if counties and (d.get("county") or "").upper() not in counties:
                    continue
                nm = " ".join((d.get("name") or "").split())
                if nm and nm.upper() not in seen and ASSOCIATION_RE.search(nm):
                    seen.add(nm.upper())
                    names.append(nm)
    names.sort(key=len, reverse=True)
    return names[:limit] if limit else names


# -- checkpointing ---------------------------------------------------------------
def ckpt_paths(key: str, out_dir: Path) -> tuple[Path, Path]:
    return out_dir / f".trial_{key}_partial.jsonl", out_dir / f".trial_{key}_done.txt"


def load_ckpt(key: str, out_dir: Path) -> tuple[list[dict], set[str]]:
    part, done = ckpt_paths(key, out_dir)
    records: list[dict] = []
    if part.exists():
        for line in part.open():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue            # torn final line from a hard kill
    names = {l.strip() for l in done.open() if l.strip()} if done.exists() else set()
    return records, names


def clear_ckpt(key: str, out_dir: Path) -> None:
    for p in ckpt_paths(key, out_dir):
        p.unlink(missing_ok=True)


def dedupe(records: list[dict]) -> list[dict]:
    """Merge records for the same case (by case_data_id) collected under
    several queried names: union the associations and queries."""
    by_id: dict[str, dict] = {}
    for r in records:
        k = f'{r.get("source")}:{r.get("case_data_id") or r.get("docket_number")}'
        cur = by_id.get(k)
        if cur is None:
            by_id[k] = dict(r, associations=list(r["associations"]),
                            queries=list(r.get("queries", [])))
            continue
        for a in r["associations"]:
            if a not in cur["associations"]:
                cur["associations"].append(a)
        for q in r.get("queries", []):
            if q not in cur["queries"]:
                cur["queries"].append(q)
    return list(by_id.values())


def write_outputs(key: str, mod, records: list[dict], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"trial_{key}.jsonl"
    records = dedupe(records)
    if not records:
        if out.exists() and out.stat().st_size > 0:
            log.warning("no records this run — keeping existing %s untouched", out.name)
        return out
    with out.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    src = out_dir / "sources.json"
    catalog = {}
    if src.exists():
        try:
            catalog = json.loads(src.read_text())
        except json.JSONDecodeError:
            catalog = {}
    entry = dict(mod.INFO)
    entry.update({"state": mod.STATE, "records": len(records),
                  "associations": len({a for r in records for a in r["associations"]}),
                  "courts": len({r["court"] for r in records}),
                  "retrieved_at": datetime.now(timezone.utc).isoformat()})
    catalog.setdefault("trial_courts", {})[key] = entry
    src.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n")
    return out


# -- one portal run -----------------------------------------------------------------
def run_portal(key: str, mod, args) -> int:
    cookie = None
    if getattr(mod, "NEEDS_COOKIE", False):
        if args.cookie_file and args.cookie_file.exists():
            cookie = args.cookie_file.read_text().strip()
        if not cookie:
            print(f"{key}: needs a Cookie header — pass --cookie-file (see "
                  f"hoaspy/collect/courts/court_portals/{mod.__name__.split('.')[-1]}.py docstring).", file=sys.stderr)
            return 2
    names = association_names(mod.STATE, args.limit, getattr(mod, "COUNTIES", None))
    if args.names:
        names = [l.strip() for l in args.names.read_text().splitlines() if l.strip()]
    if not names:
        print(f"{key}: no {mod.STATE} association names in records/", file=sys.stderr)
        return 1
    if args.fresh:
        clear_ckpt(key, args.out)
    records, done = load_ckpt(key, args.out)
    part_path, done_path = ckpt_paths(key, args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    todo = [n for n in names if n not in done]
    log.info("%s: %d %s names (%d done, %d to query)", key, len(names), mod.STATE,
             len(names) - len(todo), len(todo))
    client = mod.Client(cookie=cookie, pace=args.pace)
    code = 0
    try:
        with part_path.open("a") as part_fh, done_path.open("a") as done_fh:
            for i, name in enumerate(todo, 1):
                found = None
                while True:                       # retry loop for quota hits
                    try:
                        found = client.search(name)
                        break
                    except PermissionError as exc:
                        print(f"\n{key}: {exc}\nProgress is checkpointed — rerun to "
                              "resume (with a fresh cookie if the portal needs one).",
                              file=sys.stderr)
                        code = 3
                        break
                    except Exception as exc:
                        # Adapters raise RateLimited; a bare HTTP 429 from
                        # requests counts too so a quota never burns names.
                        if isinstance(exc, RateLimited) or " 429 " in f" {exc} ":
                            ra = getattr(exc, "retry_after", 0) or 600
                            if not args.wait_on_quota:
                                print(f"\n{key}: rate limited ({exc}). Progress is "
                                      "checkpointed — rerun to resume, or add "
                                      "--wait-on-quota.", file=sys.stderr)
                                code = 4
                                break
                            nap = max(ra, 60) + 5
                            log.warning("%s: rate limited — sleeping %ds then retrying", key, nap)
                            time.sleep(nap)
                            continue
                        log.warning("  [%d/%d] %s -> skipped (%s: %s)", i, len(todo),
                                    name, type(exc).__name__, str(exc)[:120])
                        break
                if code:
                    break
                if found is None:
                    continue
                for rec in found:
                    rec.setdefault("source", key)
                    rec.setdefault("state", mod.STATE)
                    part_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    records.append(rec)
                part_fh.flush()
                done_fh.write(name + "\n")
                done_fh.flush()
                if found or i % 25 == 0:
                    log.info("  [%d/%d] %s -> %d case(s)", i, len(todo), name, len(found))
                time.sleep(args.pace)
    except KeyboardInterrupt:
        print("\nInterrupted — progress checkpointed; rerun to resume.", file=sys.stderr)
        code = 130
    out = write_outputs(key, mod, records, args.out)
    log.info("%s: wrote %s — %d cases, %d associations, %d courts", key, out.name,
             len(records), len({a for r in records for a in r["associations"]}),
             len({r["court"] for r in records}))
    if code == 0 and not todo[len(todo):]:
        pass
    if code == 0:
        clear_ckpt(key, args.out)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list adapters and exit")
    ap.add_argument("--portal", action="append", help="adapter KEY to run (repeatable)")
    ap.add_argument("--all", action="store_true", help="run every adapter that needs no cookie")
    ap.add_argument("--cookie-file", type=Path, help="Cookie header file for portals that need one")
    ap.add_argument("--names", type=Path, help="override: file of names, one per line")
    ap.add_argument("--limit", type=int, help="cap the number of names per portal")
    ap.add_argument("--pace", type=float, default=1.0, help="seconds between requests")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--fresh", action="store_true", help="ignore checkpoints")
    ap.add_argument("--wait-on-quota", action="store_true")
    ap.add_argument("--no-upload", action="store_true", help="(always; kept for parity)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    reg = court_portals.registry()
    if args.list or not (args.portal or args.all):
        for k, m in sorted(reg.items()):
            print(f"{k:22s} {m.STATE}  {'cookie' if getattr(m, 'NEEDS_COOKIE', False) else 'anon  '}  {m.INFO.get('name', '')}")
        return 0
    keys = args.portal or [k for k, m in reg.items() if not getattr(m, "NEEDS_COOKIE", False)]
    worst = 0
    for k in keys:
        if k not in reg:
            print(f"unknown portal {k!r}; --list shows adapters", file=sys.stderr)
            return 2
        worst = max(worst, run_portal(k, reg[k], args))
    return worst


if __name__ == "__main__":
    sys.exit(main())
