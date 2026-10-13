"""California judgment liens naming an HOA, from the Secretary of State's
bizfile UCC search.

    python -m hoaspy ca-ucc --probe      # one keyword, two pages, shows the paging edge; no writes
    python -m hoaspy ca-ucc              # the full sweep (resumable)

California records judgment liens against personal property with the
Secretary of State (Code Civ. Proc. § 697.510), and the bizfile UCC search
indexes them alongside financing statements:

    POST https://bizfileonline.sos.ca.gov/api/Records/uccsearch
    body {"SEARCH_VALUE": <term>, "STATUS": "ACTIVE_LAPSED_UNLAPSED",
          "RECORD_TYPE_ID": "2154",            # Judgment Lien
          "FILING_DATE": {"start": "M/D/YYYY"|null, "end": ...},
          "LAPSE_DATE": {"start": null, "end": null}}
    -> {"rows": {<id>: {TITLE: ["<debtor> - <CITY>, <ST>"],
                        SEC_PARTY: ["<creditor> - <CITY>, <ST>"],
                        RECORD_NUM, RECORD_TYPE, FILING_DATE, LAPSE_DATE,
                        STATUS}}, "edge": {"offset", "limit", "total"}}

The search matches debtor and secured-party names, so an HOA keyword
finds both kinds of record that matter: judgments the association holds
against owners (association = secured party, the California counterpart of
the county claim-of-lien indexes) and judgments held *against* the
association (association = debtor). `hoa_role` says which, and the
document code is JL (held by the HOA) or JLX (against the HOA) so the two
are counted apart.

Unlike the business search this endpoint reports a paging edge; whether
it honours an offset is discovered at run time (see sweep_term), with the
filing-date bisection of the ca-sos command as the fallback. Auth, pacing,
the pause-for-fresh-credentials loop and the checkpoint are the ca-sos
command's.

Output: liens/liens_ca_ucc.jsonl in the shared lien shape plus `city`,
`bizfile_id`, `lapse_date`, `status`, `record_type` and `hoa_role`;
liens/sources_ca_ucc.json; raw API rows in liens/ca_ucc_raw.jsonl.
Checkpoint: liens/.ca_ucc_done.txt and liens/.ca_ucc_partial.jsonl.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections import Counter
from pathlib import Path

from hoaspy.collect.liens import LIENS_DIR
from hoaspy.collect.registries import CORPORATIONS_PATH
from hoaspy.collect.registries import get_ca_sos as sos
from hoaspy.lib.jsonl import now_iso, read_rows, write_json, write_rows
from hoaspy.lib.names import CORPORATION_NAME_RE
from hoaspy.lib.records import Lien

DEFAULT_OUTPUT_PATH = LIENS_DIR / "liens_ca_ucc.jsonl"
DEFAULT_SOURCES_PATH = LIENS_DIR / "sources_ca_ucc.json"
DEFAULT_RAW_PATH = LIENS_DIR / "ca_ucc_raw.jsonl"
DONE_PATH = LIENS_DIR / ".ca_ucc_done.txt"
PARTIAL_PATH = LIENS_DIR / ".ca_ucc_partial.jsonl"

UCC_URL = "https://bizfileonline.sos.ca.gov/api/Records/uccsearch"
SOURCE = "ca-sos-ucc"
SOURCE_PAGE = "https://bizfileonline.sos.ca.gov/search/ucc"
RECORD_TYPES = {"2154": "Judgment Lien"}
JUDGMENT_LIEN_TYPE = "2154"
PAGE_SIZE = 100
DATE_MIN = "1980-01-01"

# HOA-shaped words; the engine prefix-matches, so CONDO also finds
# CONDOMINIUM and HOA also finds HOANG (dropped by the name gate).
KEYWORDS = [
    "HOA", "HOMEOWNERS", "HOMEOWNER", "HOME OWNERS", "OWNERS ASSOCIATION",
    "CONDOMINIUM", "CONDO", "PROPERTY OWNERS", "COMMUNITY ASSOCIATION",
    "MASTER ASSOCIATION", "RESIDENTS ASSOCIATION", "MAINTENANCE ASSOCIATION",
    "MAINTENANCE CORPORATION", "TOWNHOMES", "TOWNHOME", "TOWNHOUSE", "VILLAS",
]

PARTY_RE = re.compile(r"^(?P<name>.+?)\s+-\s+(?P<city>[^-]*?),\s*(?P<st>[A-Z]{2})\s*$")
# Entity descriptors the UCC index appends to a party name:
#   ", A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION", " A CALIFORNIA
#   CORPORATION DBA ...", ", AN INDIVIDUAL", " C/O ...". Cut at the earliest.
DESCRIPTOR_RES = [
    re.compile(r",?\s+\b(?:A|AN)\s+(?:[A-Z]+\s+){0,3}?(?:NON-?\s?PROFIT|NONPROFIT|"
               r"MUTUAL BENEFIT|PUBLIC BENEFIT|CORPORATION|CORP\.?|LIMITED|BANKING|"
               r"PROFESSIONAL|INDIVIDUAL|L\.?L\.?C\.?|L\.?P\.?|PARTNERSHIP|COMPANY|"
               r"TRUST|MUNICIPAL|GOVERNMENTAL|PUBLIC AGENCY)\b", re.I),
    re.compile(r"\s+(?:DBA|D/B/A|AKA|A/K/A|FKA|F/K/A|C/O|SUCCESSOR)\b", re.I),
    re.compile(r",?\s+ET\s+AL\.?$", re.I),
]
INDIVIDUAL_RE = re.compile(r"\bAN?\s+INDIVIDUAL\b", re.I)
STRONG_ASSOCIATION_RE = re.compile(
    r"\b(ASSOCIATION|ASSN|HOMEOWNERS?|HOME OWNERS?|CONDOMINIUMS?|CONDO|OWNERS|"
    r"TOWNHOMES?|TOWNHOUSES?|VILLAS|COMMUNITY|MASTER|MAINTENANCE)\b", re.I)
# Businesses that borrow an HOA word ("HOMEOWNERS MARKETING SERVICES, INC.",
# "HOA PROPERTY MANAGEMENT"): a trade word with no ASSOCIATION/ASSN/HOA
# token is a vendor, not a community.
BUSINESS_RE = re.compile(
    r"\b(MARKETING|SERVICES?|MANAGEMENT|INSURANCE|MORTGAGE|LENDING|BANK|REALTY|"
    r"FINANCIAL|ROOFING|CONSTRUCTION|SUPPLY|PLUMBING|LANDSCAP\w*|WARRANTY)\b", re.I)
ASSOCIATION_TOKEN_RE = re.compile(r"\b(ASSOCIATION|ASSN|HOA)\b", re.I)
# A bare leading "HOA <surname>" is a person (HOA is a common Vietnamese
# given name) unless one of these follows.
HOA_CONNECTIVES = ("OF", "AT", "FOR", "@", "-")

log = logging.getLogger(__name__)

# The normalized names of the CA entities that are common-interest
# developments by entity type, loaded on first use; a test may set it.
KNOWN_NAMES: set[str] | None = None


def clean_party_name(name: str) -> str:
    """'RIO VISTA WALK HOA, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION'
    -> 'RIO VISTA WALK HOA'."""
    text = " ".join((name or "").split())
    cut = len(text)
    for pattern in DESCRIPTOR_RES:
        match = pattern.search(text)
        if match and 0 < match.start() < cut:
            cut = match.start()
    return text[:cut].strip(" ,;")


def parse_party(line: str) -> dict:
    """'<NAME> - <CITY>, <ST>' -> {name, city, state}; the raw line is the
    name when the suffix is absent."""
    line = " ".join((line or "").split())
    match = PARTY_RE.match(line)
    if not match:
        return {"name": clean_party_name(line), "city": "", "state": ""}
    return {"name": clean_party_name(match.group("name")), "city": match.group("city").strip().title(),
            "state": match.group("st")}


def normalized(name: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).strip()


def known_names() -> set[str]:
    """The normalized names of the CA entities in records/state_corps.jsonl
    that are common-interest developments by entity type (is_association) —
    the strongest evidence a UCC party is an association. Keyword-named
    LLCs and stock corporations are deliberately left out: they include
    vendors like "HOMEOWNERS MARKETING SERVICES, INC." that must still face
    the business-word check."""
    global KNOWN_NAMES
    if KNOWN_NAMES is None:
        KNOWN_NAMES = {normalized(row.get("name", "")) for row in read_rows(CORPORATIONS_PATH, skip_bad=True)
                       if row.get("state") == "CA" and row.get("is_association")}
    return KNOWN_NAMES


def is_association(name: str, raw: str = "", known: set | None = None) -> bool:
    """An association-shaped party. Two UCC-specific guards on top of the
    shared name gate: the index marks people as "AN INDIVIDUAL", and a bare
    leading "HOA <surname>" with no other association word is a person
    unless it is a CA corporation we know."""
    if INDIVIDUAL_RE.search(raw or ""):
        return False
    if known is None:
        known = known_names()
    if normalized(name) in known:
        return True            # a CA association we hold, keyword or not
    if not CORPORATION_NAME_RE.search(name or ""):
        return False
    if BUSINESS_RE.search(name or "") and not ASSOCIATION_TOKEN_RE.search(name or ""):
        return False
    tokens = (name or "").upper().split()
    if tokens and tokens[0] == "HOA" and len(tokens) > 1 and tokens[1] not in HOA_CONNECTIVES \
            and not STRONG_ASSOCIATION_RE.search(name):
        return False
    return True


def to_record(row: dict, retrieved_at: str = "") -> Lien | None:
    """One UCC row as a lien record, or None when no party is
    association-shaped (a person named Hoang, a bank)."""
    raw_debtors = [text for text in (row.get("TITLE") or []) if text]
    raw_creditors = [text for text in (row.get("SEC_PARTY") or []) if text]
    debtors = [parse_party(text) for text in raw_debtors]
    creditors = [parse_party(text) for text in raw_creditors]
    hoa = next((party for party, raw in zip(creditors, raw_creditors, strict=True)
                if is_association(party["name"], raw)), None)
    role = "creditor" if hoa else ""
    if not hoa:
        hoa = next((party for party, raw in zip(debtors, raw_debtors, strict=True)
                    if is_association(party["name"], raw)), None)
        role = "debtor" if hoa else ""
    if not hoa:
        return None
    filed = row.get("FILING_DATE") or ""
    ymd = sos.iso_date(filed).replace("-", "")
    return Lien(
        doc_id=row.get("RECORD_NUM") or str(row.get("ID", "")),
        doc_type="JL" if role == "creditor" else "JLX",
        doc_type_label="judgment_lien" if role == "creditor" else "judgment_lien_against_association",
        recorded_date=filed, recorded_ymd=ymd, year=int(ymd[:4]) if ymd[:4].isdigit() else 0,
        state="CA", county="", source=SOURCE, source_page=SOURCE_PAGE, association=hoa["name"],
        filers=[party["name"] for party in creditors], respondents=[party["name"] for party in debtors],
        n_parties=len(creditors) + len(debtors), retrieved_at=retrieved_at or now_iso(),
        extras={"city": hoa["city"], "bizfile_id": str(row.get("ID", "")), "lapse_date": row.get("LAPSE_DATE") or "",
                "status": row.get("STATUS") or "", "record_type": row.get("RECORD_TYPE") or "", "hoa_role": role})


def parse_response(payload: dict) -> tuple[list[dict], int]:
    """(the kept rows, shaped; the raw row count)."""
    rows = payload.get("rows") or {}
    kept = [record.as_row() for record in (to_record(row) for row in rows.values()) if record]
    return kept, len(rows)


def ucc_body(term: str, record_type: str = JUDGMENT_LIEN_TYPE, start: str | None = None,
             end: str | None = None, offset: int = 0, limit: int = PAGE_SIZE) -> dict:
    """The uccsearch request body for one query (and one page of it)."""
    body = {
        "SEARCH_VALUE": term, "STATUS": "ACTIVE_LAPSED_UNLAPSED",
        "RECORD_TYPE_ID": record_type or JUDGMENT_LIEN_TYPE,
        "FILING_DATE": {"start": sos.mdy_date(start) if start else None,
                        "end": sos.mdy_date(end) if end else None},
        "LAPSE_DATE": {"start": None, "end": None},
    }
    if offset:
        # Two spellings of the same request: the response's own "edge"
        # shape, and flat keys. Whichever the server reads, it echoes the
        # offset back in edge.offset — sweep_term checks that.
        body["edge"] = {"offset": offset, "limit": limit}
        body["OFFSET"], body["LIMIT"] = offset, limit
    return body


def sweep_term(client, checkpoint: sos.Checkpoint, term: str, record_type: str,
               date_min: str = DATE_MIN) -> tuple[int, str]:
    """Collect every row for one (term, record type). Returns (new rows,
    how): "single" when one page held everything, "paged" when the server
    honoured offsets, "bisected" when it did not and the filing-date window
    had to be split instead."""
    key = f"{term}|{record_type}|all"
    if key in checkpoint.done:
        return 0, "done"
    search_fn = client.search if hasattr(client, "search") else client   # a Client or a fake
    rows, raw_count, raw_rows = search_fn(term, record_type, None, None)
    edge = raw_rows.pop("__edge__", {}) or {}
    total = int(edge.get("total") or raw_count)
    new = checkpoint.add(rows, raw_rows)
    log.info("%-24s type=%s -> %d rows (total %d), %d new", term[:24], record_type, raw_count, total, new)
    # The server counts result *hits* (total, limit) while the row dict is
    # keyed by record id, so a page of 100 hits can come back as 70 rows
    # when a record matches under several party names. Step by the server's
    # page size, never by the row count, or pages overlap.
    limit = int(edge.get("limit") or PAGE_SIZE)
    if total <= limit or raw_count == 0:
        checkpoint.mark_done(key)
        return new, "single"
    offset = limit
    paged = True
    while offset < total:
        rows, raw_count, raw_rows = search_fn(term, record_type, None, None, offset=offset, limit=limit)
        edge = raw_rows.pop("__edge__", {}) or {}
        if int(edge.get("offset") or 0) != offset:
            paged = False          # the server ignored the offset: the same page again
            break
        added = checkpoint.add(rows, raw_rows)
        new += added
        log.info("%-24s type=%s   page @%d/%d -> %d rows, %d new",
                 term[:24], record_type, offset, total, raw_count, added)
        if raw_count == 0:
            break                  # ran off the end early; nothing more to page
        offset += limit
    if paged:
        checkpoint.mark_done(key)
        return new, "paged"
    log.info("%-24s type=%s   offset not honoured — bisecting filing dates (cap %d)", term[:24], record_type, raw_count)
    sweeper = sos.Sweeper(search_fn, checkpoint, date_min=date_min, cap=raw_count)
    new += sweeper.run_term(term, record_type)
    checkpoint.mark_done(key)
    return new, "bisected"


def write_outputs(rows: list[dict], out: Path, sources: Path, requests_made: int) -> int:
    if not rows:
        log.warning("no CA judgment-lien records collected — leaving %s untouched", out.name)
        return 0
    rows.sort(key=lambda row: (row["recorded_ymd"], row["doc_id"]), reverse=True)
    write_rows(out, rows)
    years = [row["year"] for row in rows if row["year"]]
    write_json(sources, {
        "source": "California Secretary of State — bizfile UCC search, judgment liens",
        "source_url": SOURCE_PAGE,
        "retrieved_at": now_iso(),
        "records": len(rows), "api_calls": requests_made,
        "by_role": dict(Counter(row["hoa_role"] for row in rows)),
        "by_status": dict(Counter(row["status"] for row in rows)),
        "years": [min(years), max(years)] if years else [],
        "note": "Judgment liens filed with the SOS against personal property; "
                "role says whether the association holds the judgment (creditor) "
                "or owes it (debtor). Real-property judgment liens live in county "
                "recorder indexes, which are not covered here.",
    })
    return len(rows)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cookie-file", type=Path, default=sos.COOKIE_PATH)
    parser.add_argument("--token-file", type=Path, default=sos.TOKEN_PATH)
    parser.add_argument("--keywords", help="comma-separated override of the keyword list")
    parser.add_argument("--record-types", default=",".join(RECORD_TYPES),
                        help="comma-separated RECORD_TYPE_IDs (default 2154 = Judgment Lien)")
    parser.add_argument("--pace", type=float, default=1.5)
    parser.add_argument("--wait-minutes", type=float, default=480)
    parser.add_argument("--max-requests", type=int, default=0)
    parser.add_argument("--date-min", default=DATE_MIN)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES_PATH)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW_PATH)
    parser.add_argument("--probe", action="store_true",
                        help="one keyword, the first two pages, print the paging edge; no writes")
    parser.add_argument("--fresh", action="store_true")


def probe(client: sos.Client, term: str, record_type: str) -> None:
    """Two pages of one keyword, printed with the paging edge."""
    rows, raw_count, raw_rows = client.search(term, record_type, None, None)
    edge = raw_rows.pop("__edge__", {})
    print(f"page 1: {raw_count} rows, {len(rows)} association-shaped, edge={edge}")
    for row in rows[:5]:
        print("  ", row["hoa_role"], row["doc_type"], row["recorded_date"], row["association"],
              "<-" if row["hoa_role"] == "creditor" else "->", row["respondents"][:1] or row["filers"][:1])
    if raw_count and int(edge.get("total") or 0) > raw_count:
        _, raw_count2, raw_rows2 = client.search(term, record_type, None, None, offset=raw_count)
        edge2 = raw_rows2.pop("__edge__", {})
        print(f"page 2 @{raw_count}: {raw_count2} rows, edge={edge2}, "
              f"{'NEW ids' if set(raw_rows2) - set(raw_rows) else 'SAME ids (offset ignored)'}")


def run(args: argparse.Namespace) -> int:
    try:
        client = sos.Client(args.cookie_file, args.token_file, pace=args.pace, wait_minutes=args.wait_minutes,
                            max_requests=args.max_requests, url=UCC_URL, body_fn=ucc_body, parse_fn=parse_response)
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    keywords = [word.strip() for word in args.keywords.split(",") if word.strip()] if args.keywords else KEYWORDS
    record_types = [code.strip() for code in args.record_types.split(",") if code.strip()]
    if args.probe:
        probe(client, keywords[0], record_types[0])
        return 0

    if args.fresh:
        sos.Checkpoint(DONE_PATH, PARTIAL_PATH).clear()
    checkpoint = sos.Checkpoint(DONE_PATH, PARTIAL_PATH, args.raw)
    log.info("checkpoint: %d done, %d rows so far", len(checkpoint.done), len(checkpoint.by_id))

    completed = True
    hows: Counter = Counter()
    try:
        for record_type in record_types:
            for i, term in enumerate(keywords, 1):
                new, how = sweep_term(client, checkpoint, term, record_type, args.date_min)
                hows[how] += 1
                log.info("== [%d/%d] %r type=%s: +%d new (%s; total %d, %d requests)",
                         i, len(keywords), term, record_type, new, how, len(checkpoint.by_id), client.requests)
    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint kept; rerun to resume")
        completed = False
    except (sos.BlockedError, RuntimeError) as exc:
        log.error("stopped: %s — checkpoint kept; rerun to resume", exc)
        completed = False

    count = write_outputs(list(checkpoint.by_id.values()), args.out, args.sources, client.requests)
    log.info("wrote %s — %d CA judgment-lien rows (%s)", args.out.name, count, dict(hows))
    if completed:
        checkpoint.clear()
        log.info("sweep complete — checkpoint cleared")
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    return 0 if completed else 3
