#!/usr/bin/env python
"""Utah HOA Registry (Utah Dept of Commerce) — every registered association.

    ./venv/bin/python -m hoaspy.collect.registries.get_ut_hoa            # full sweep (resumable)
    ./venv/bin/python -m hoaspy.collect.registries.get_ut_hoa --fresh    # ignore the checkpoint
    ./venv/bin/python -m hoaspy.collect.registries.get_ut_hoa --limit 50 # smoke test

Registration is mandatory (Utah Code 57-8a-105 / 57-8-13.1). The public UI at
https://services.commerce.utah.gov/hoa/ is a name search backed by one
endpoint, `assets/js/hoa-ajax.php`:

    f=s&v=<text>   name search -> HTML table, one <tr data-pid> per HOA
    f=d&v=<pid>    detail card -> registration #, type, status, expiry,
                   location, contact address, president, community manager,
                   payoff contact, board members

`v` is a SQL LIKE pattern, so `%` lists the whole registry (about 4,100 rows)
in one response; each detail is then fetched once. Names and roles are kept;
the phone numbers and e-mail addresses on the detail cards are NOT stored.

Output: records/state_registries.jsonl (UT rows replaced) in the registry
shape build_site.py folds in, plus coverage.json collected["hoa_registry"].
Checkpoint: .cache/ut/hoa_details.jsonl (one detail per line, appended).
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from hoaspy.collect.registries.get_states import REGS, update_coverage, write_merged

from hoaspy import ROOT
from hoaspy.lib.contract import strip_contacts
CACHE = ROOT / ".cache" / "ut"
CKPT = CACHE / "hoa_details.jsonl"
BASE = "https://services.commerce.utah.gov/hoa/"
ENDPOINT = BASE + "assets/js/hoa-ajax.php"
USER_AGENT = "macos:hoa-public-records:0.1 (academic research)"
SOURCE = "Utah Department of Commerce — HOA Registry"
TEST_RECORDS = {"13855665"}          # the state's own "DTS Smoke Test" row

log = logging.getLogger("ut")

ROW_RE = re.compile(
    r'<tr data-pid="(\d+)" class="link-view">'
    r'<td class="text-left">(.*?)</td><td class="text-left">(.*?)</td>', re.S)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


class Client:
    def __init__(self, pace: float):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Referer": BASE,
                               "X-Requested-With": "XMLHttpRequest"})
        self.pace = pace
        self._last = 0.0

    def call(self, f: str, v: str) -> str:
        wait = self._last + self.pace - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            try:
                r = self.s.post(ENDPOINT, data={"f": f, "v": v}, timeout=90)
                self._last = time.monotonic()
                r.raise_for_status()
                return r.text
            except requests.RequestException as exc:
                log.warning("%s %s: %s (attempt %d)", f, v, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {f}={v}")

    def list_all(self) -> list[dict]:
        body = self.call("s", "%")
        rows = []
        for pid, namecell, loc in ROW_RE.findall(body):
            name, dba = namecell, ""
            if "DBA:" in namecell:
                name, dba = namecell.split("DBA:", 1)
            city, _, county = clean(loc).rpartition(", ")
            rows.append({"pid": pid, "name": clean(name), "dba": clean(dba),
                         "city": city, "county": county.replace(" County", "")})
        return rows

    def detail(self, pid: str) -> dict | None:
        body = self.call("d", pid)
        if "No Result Returned" in body:
            return None
        d: dict = {"pid": pid}
        m = re.search(r'<h1 class="mb-0">(.*?)</h1>', body, re.S)
        d["name"] = clean(m.group(1)) if m else ""
        m = re.search(r"<b>DBA:</b>(.*?)</h3>", body, re.S)
        d["dba"] = clean(m.group(1)) if m else ""
        for key, pat in (("registration_no", r"Registration #: ([^<]*)"),
                         ("registration_type", r"Registration Type: ([^<]*)"),
                         ("status", r"Status: <span[^>]*>([^<]*)"),
                         ("expires", r"Expires: ([^<]*)")):
            m = re.search(pat, body)
            d[key] = clean(m.group(1)) if m else ""
        m = re.search(r"Location:</h5><p[^>]*>(.*?)</p>", body, re.S)
        loc = clean(m.group(1)) if m else ""
        d["city"], _, county = loc.rpartition(", ")
        d["county"] = county.replace(" County", "")
        m = re.search(r"Contact Info:</h5><p[^>]*>(.*?)</div>", body, re.S)
        d["contact_address"] = ", ".join(
            clean(x) for x in re.split(r"<br\s*/?>", m.group(1)) if clean(x)) if m else ""
        d["roles"] = {}
        for title, frag in re.findall(
                r'<h4 class="mb-0">([^<]+)</h4><p class="mt-0 ml-3">(.*?)</p>', body, re.S):
            d["roles"][clean(title)] = _block_name(frag)
        d["board_members"] = []
        bm = body.split("Board Members</h4>", 1)
        if len(bm) == 2:
            for frag in re.findall(r'<p class="ml-3">(.*?)</p>', bm[1], re.S):
                name = _block_name(frag)
                if name:
                    d["board_members"].append(name)
        return d


def _block_name(frag: str) -> str:
    """First line of a contact block is the name; phone/e-mail/address lines
    that follow are deliberately dropped. A block with no name line starts
    with the phone number instead, which is dropped too."""
    parts = [clean(p) for p in re.split(r"<br\s*/?>", frag)]
    parts = [p for p in parts if p]
    return strip_contacts(parts[0]) if parts else ""


def _iso(v: str) -> str:
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", v or "")
    return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}" if m else ""


def to_record(d: dict, listing: dict) -> dict:
    roles = d.get("roles") or {}
    manager = roles.get("HOA Community Manager") or roles.get("Community Manager") or ""
    officers = []
    pres = roles.get("HOA President") or roles.get("President")
    if pres:
        officers.append({"name": pres, "title": "President"})
    for b in d.get("board_members") or []:
        if b != pres:
            officers.append({"name": b, "title": "Board member"})
    kind = d.get("registration_type") or ""
    status = d.get("status") or ""
    return {
        "state": "UT",
        "source": SOURCE,
        "source_url": f"{BASE}?p={d['pid']}",
        "record_id": d.get("registration_no") or d["pid"],
        "name": d.get("name") or listing.get("name", ""),
        "dba": d.get("dba") or listing.get("dba", ""),
        "status": f"{status} — {kind}".strip(" —") if kind else status,
        "status_detail": f"expires {_iso(d.get('expires'))}" if d.get("expires") else "",
        "recorded_date": "",
        "address": d.get("contact_address") or "",
        "city": (d.get("city") or listing.get("city") or "").title(),
        "county": (d.get("county") or listing.get("county") or "").title(),
        "zip": (re.search(r"\b(84\d{3})\b", d.get("contact_address") or "") or [None, ""])[1],
        "units": None,
        "manager_name": manager,
        "officers": officers,
        "registration_type": kind,
    }


def load_ckpt() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if CKPT.exists():
        for line in CKPT.read_text().splitlines():
            if line.strip():
                d = json.loads(line)
                out[d["pid"]] = d
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pace", type=float, default=0.7, help="seconds between requests per worker")
    ap.add_argument("--workers", type=int, default=3,
                    help="parallel detail fetchers, each its own session (server takes ~3 s per card)")
    ap.add_argument("--limit", type=int, help="stop after this many new details")
    ap.add_argument("--fresh", action="store_true", help="discard the checkpoint")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    CACHE.mkdir(parents=True, exist_ok=True)
    if args.fresh and CKPT.exists():
        CKPT.unlink()
    have = load_ckpt()

    client = Client(args.pace)
    listing = client.list_all()
    listing = [r for r in listing if r["pid"] not in TEST_RECORDS]
    by_pid = {r["pid"]: r for r in listing}
    todo = [r for r in listing if r["pid"] not in have]
    log.info("registry lists %d associations (%d detail cards cached, %d to fetch)",
             len(listing), len(have), len(todo))

    if args.limit:
        todo = todo[:args.limit]
    lock = threading.Lock()
    clients = [client] + [Client(args.pace) for _ in range(max(1, args.workers) - 1)]
    counter = {"n": 0}

    def fetch(job):
        idx, r = job
        c = clients[idx % len(clients)]
        try:
            d = c.detail(r["pid"])
        except Exception as exc:
            log.warning("%s (%s): %s", r["pid"], r["name"], exc)
            return
        if d is None:
            log.warning("%s (%s): no detail card", r["pid"], r["name"])
            d = {"pid": r["pid"], "name": r["name"], "missing": True}
        with lock:
            have[r["pid"]] = d
            fh.write(json.dumps(d, ensure_ascii=False) + "\n")
            fh.flush()
            counter["n"] += 1
            if counter["n"] % 100 == 0:
                log.info("%d/%d details fetched", counter["n"], len(todo))

    with CKPT.open("a") as fh, ThreadPoolExecutor(max_workers=len(clients)) as pool:
        list(pool.map(fetch, enumerate(todo)))

    rows = [to_record(d, by_pid.get(pid, {})) for pid, d in have.items()
            if pid in by_pid or not d.get("missing")]
    rows = [r for r in rows if r["name"]]
    complete = all(pid in have for pid in by_pid)
    total = write_merged(REGS, rows, {"UT"})
    update_coverage("UT", "hoa_registry", {
        "source": SOURCE, "records": len(rows), "url": BASE,
        "note": ("mandatory statewide registry; current registrations only"
                 + ("" if complete else f"; sweep incomplete ({len(have)}/{len(by_pid)} details)")),
    })
    log.info("state_registries.jsonl: %d rows total, %d UT (%s)", total, len(rows),
             "complete" if complete else "partial")
    print(f"  UT: {len(rows):,} registry records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
