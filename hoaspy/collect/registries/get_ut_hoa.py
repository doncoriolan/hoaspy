"""Utah HOA Registry (Department of Commerce): every registered association.

    python -m hoaspy ut-hoa               # full sweep (resumable)
    python -m hoaspy ut-hoa --fresh       # ignore the checkpoint
    python -m hoaspy ut-hoa --limit 50    # smoke test

Registration is mandatory (Utah Code 57-8a-105 / 57-8-13.1). The public
search at https://services.commerce.utah.gov/hoa/ is backed by one
endpoint, assets/js/hoa-ajax.php:

    f=s&v=<text>   name search -> an HTML table, one <tr data-pid> per HOA
    f=d&v=<pid>    detail card -> registration number, type, status, expiry,
                   location, contact address, president, community manager,
                   payoff contact, board members

`v` is a SQL LIKE pattern, so `%` lists the whole registry (about 4,100
rows) in one response; each detail card is then fetched once, by a few
parallel sessions. Names and roles are kept; the phone numbers and e-mail
addresses on the cards are not.

Output: Utah's rows in records/state_registries.jsonl (replaced on each
run) and coverage.json collected["hoa_registry"]. Checkpoint:
.cache/ut/hoa_details.jsonl, one detail card per line, appended.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from hoaspy import ROOT
from hoaspy.collect.registries import REGISTRIES_PATH
from hoaspy.lib import http
from hoaspy.lib.contract import strip_contacts
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import append_row, now_iso, read_rows, replace_rows
from hoaspy.lib.records import Association, Officer

CACHE_DIR = ROOT / ".cache" / "ut"
CHECKPOINT_PATH = CACHE_DIR / "hoa_details.jsonl"
REGISTRY_URL = "https://services.commerce.utah.gov/hoa/"
ENDPOINT_URL = REGISTRY_URL + "assets/js/hoa-ajax.php"
SOURCE = "Utah Department of Commerce — HOA Registry"
TEST_RECORDS = {"13855665"}             # the state's own "DTS Smoke Test" row

LISTING_ROW_RE = re.compile(
    r'<tr data-pid="(\d+)" class="link-view">'
    r'<td class="text-left">(.*?)</td><td class="text-left">(.*?)</td>', re.S)
CARD_FIELDS = (("registration_no", r"Registration #: ([^<]*)"),
               ("registration_type", r"Registration Type: ([^<]*)"),
               ("status", r"Status: <span[^>]*>([^<]*)"),
               ("expires", r"Expires: ([^<]*)"))

log = logging.getLogger(__name__)


def html_text(fragment: str) -> str:
    """The text of an HTML fragment, tags gone, whitespace squashed."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def iso_date(text: str) -> str:
    """'3/21/2025' -> '2025-03-21'; '' when the text is not a date."""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return f"{match.group(3)}-{int(match.group(1)):02d}-{int(match.group(2)):02d}" if match else ""


def contact_name(block: str) -> str:
    """The name on a contact block: its first line. The phone, e-mail and
    address lines that follow are dropped; a block that starts with the
    phone number (no name line) gives ''."""
    lines = [html_text(part) for part in re.split(r"<br\s*/?>", block)]
    lines = [line for line in lines if line]
    return strip_contacts(lines[0]) if lines else ""


class Client:
    """One paced session against the registry's endpoint."""

    def __init__(self, pace: float):
        self.session = http.session(headers={"Referer": REGISTRY_URL, "X-Requested-With": "XMLHttpRequest"})
        self.pace = pace
        self._last = 0.0

    def call(self, function: str, value: str) -> str:
        """One endpoint call (`f`, `v`), retried on a network error."""
        wait = self._last + self.pace - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        for attempt in range(4):
            try:
                response = self.session.post(ENDPOINT_URL, data={"f": function, "v": value}, timeout=90)
                self._last = time.monotonic()
                response.raise_for_status()
                return response.text
            except requests.RequestException as exc:
                log.warning("%s %s: %s (attempt %d)", function, value, exc, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise RuntimeError(f"gave up on {function}={value}")

    def listing(self) -> list[dict]:
        """Every association the registry lists: {pid, name, dba, city, county}."""
        body = self.call("s", "%")
        rows = []
        for pid, name_cell, location in LISTING_ROW_RE.findall(body):
            name, dba = name_cell, ""
            if "DBA:" in name_cell:
                name, dba = name_cell.split("DBA:", 1)
            city, _, county = html_text(location).rpartition(", ")
            rows.append({"pid": pid, "name": html_text(name), "dba": html_text(dba),
                         "city": city, "county": county.replace(" County", "")})
        return rows

    def detail(self, pid: str) -> dict | None:
        """One detail card as a dict, or None when the registry has none for the pid."""
        body = self.call("d", pid)
        if "No Result Returned" in body:
            return None
        card: dict = {"pid": pid}
        match = re.search(r'<h1 class="mb-0">(.*?)</h1>', body, re.S)
        card["name"] = html_text(match.group(1)) if match else ""
        match = re.search(r"<b>DBA:</b>(.*?)</h3>", body, re.S)
        card["dba"] = html_text(match.group(1)) if match else ""
        for key, pattern in CARD_FIELDS:
            match = re.search(pattern, body)
            card[key] = html_text(match.group(1)) if match else ""
        match = re.search(r"Location:</h5><p[^>]*>(.*?)</p>", body, re.S)
        location = html_text(match.group(1)) if match else ""
        card["city"], _, county = location.rpartition(", ")
        card["county"] = county.replace(" County", "")
        match = re.search(r"Contact Info:</h5><p[^>]*>(.*?)</div>", body, re.S)
        card["contact_address"] = ", ".join(
            html_text(part) for part in re.split(r"<br\s*/?>", match.group(1)) if html_text(part)) if match else ""
        card["roles"] = {}
        for title, block in re.findall(r'<h4 class="mb-0">([^<]+)</h4><p class="mt-0 ml-3">(.*?)</p>', body, re.S):
            card["roles"][html_text(title)] = contact_name(block)
        card["board_members"] = []
        _, found, members = body.partition("Board Members</h4>")
        if found:
            for block in re.findall(r'<p class="ml-3">(.*?)</p>', members, re.S):
                name = contact_name(block)
                if name:
                    card["board_members"].append(name)
        return card


def to_record(card: dict, listing: dict, retrieved_at: str = "") -> Association:
    """A detail card (with its listing row as the fallback) as a registry record."""
    roles = card.get("roles") or {}
    manager = roles.get("HOA Community Manager") or roles.get("Community Manager") or ""
    officers = []
    president = roles.get("HOA President") or roles.get("President")
    if president:
        officers.append(Officer(name=president, title="President"))
    officers += [Officer(name=member, title="Board member") for member in card.get("board_members") or []
                 if member != president]
    kind = card.get("registration_type") or ""
    status = card.get("status") or ""
    contact_address = card.get("contact_address") or ""
    zip_match = re.search(r"\b(84\d{3})\b", contact_address)
    return Association(
        state="UT", source=SOURCE, source_url=f"{REGISTRY_URL}?p={card['pid']}",
        record_id=card.get("registration_no") or card["pid"],
        name=card.get("name") or listing.get("name", ""), dba=card.get("dba") or listing.get("dba", ""),
        status=f"{status} — {kind}".strip(" —") if kind else status,
        status_detail=f"expires {iso_date(card.get('expires'))}" if card.get("expires") else "",
        address=contact_address,
        city=(card.get("city") or listing.get("city") or "").title(),
        county=(card.get("county") or listing.get("county") or "").title(),
        zip=zip_match.group(1) if zip_match else "",
        manager_name=manager, officers=officers, registration_type=kind, retrieved_at=retrieved_at)


def load_checkpoint() -> dict[str, dict]:
    """{pid: detail card} fetched by earlier runs."""
    return {card["pid"]: card for card in read_rows(CHECKPOINT_PATH, skip_bad=True)}


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pace", type=float, default=0.7, help="seconds between requests per worker")
    parser.add_argument("--workers", type=int, default=3,
                        help="parallel detail fetchers, each with its own session (the server takes ~3 s per card)")
    parser.add_argument("--limit", type=int, help="stop after this many new details")
    parser.add_argument("--fresh", action="store_true", help="discard the checkpoint")


def run(args: argparse.Namespace) -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
    cards = load_checkpoint()

    client = Client(args.pace)
    listing = [row for row in client.listing() if row["pid"] not in TEST_RECORDS]
    by_pid = {row["pid"]: row for row in listing}
    todo = [row for row in listing if row["pid"] not in cards]
    log.info("registry lists %d associations (%d detail cards cached, %d to fetch)",
             len(listing), len(cards), len(todo))
    if args.limit:
        todo = todo[:args.limit]

    lock = threading.Lock()
    clients = [client] + [Client(args.pace) for _ in range(max(1, args.workers) - 1)]
    fetched = 0

    def fetch(job: tuple[int, dict]) -> None:
        nonlocal fetched
        index, row = job
        try:
            card = clients[index % len(clients)].detail(row["pid"])
        except Exception as exc:
            log.warning("%s (%s): %s", row["pid"], row["name"], exc)
            return
        if card is None:
            log.warning("%s (%s): no detail card", row["pid"], row["name"])
            card = {"pid": row["pid"], "name": row["name"], "missing": True}
        with lock:
            cards[row["pid"]] = card
            append_row(checkpoint, card)
            fetched += 1
            if fetched % 100 == 0:
                log.info("%d/%d details fetched", fetched, len(todo))

    with CHECKPOINT_PATH.open("a", encoding="utf-8") as checkpoint, ThreadPoolExecutor(len(clients)) as pool:
        list(pool.map(fetch, enumerate(todo)))

    retrieved_at = now_iso()
    records = [to_record(card, by_pid.get(pid, {}), retrieved_at) for pid, card in cards.items()
               if pid in by_pid or not card.get("missing")]
    records = [record for record in records if record.name]
    complete = all(pid in cards for pid in by_pid)
    total = replace_rows(REGISTRIES_PATH, (record.as_row() for record in records), {"UT"})
    update_coverage("UT", "hoa_registry", {
        "source": SOURCE, "records": len(records), "url": REGISTRY_URL,
        "note": ("mandatory statewide registry; current registrations only"
                 + ("" if complete else f"; sweep incomplete ({len(cards)}/{len(by_pid)} details)")),
    })
    log.info("%s: %d rows total, %d UT (%s)", REGISTRIES_PATH.name, total, len(records),
             "complete" if complete else "partial")
    print(f"  UT: {len(records):,} registry records")
    return 0
