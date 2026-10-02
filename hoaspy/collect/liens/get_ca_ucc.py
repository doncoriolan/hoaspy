#!/usr/bin/env python3
"""California judgment liens naming an HOA, from the SOS bizfile UCC search.

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

The search matches debtor and secured-party names, so an HOA keyword finds
both kinds of record that matter here: judgments the association holds
against owners (association = secured party — the CA counterpart of the
county claim-of-lien indexes used for FL/NY), and judgments held *against*
the association (association = debtor). Each row is shaped into the
liens/*.jsonl schema build_site.add_liens already ingests, with
`hoa_role` saying which it is and doc_type JL (held by the HOA) or JLX
(against the HOA) so the report counts them apart.

Unlike the business search this endpoint reports a paging edge; whether it
honours an offset is discovered at run time (see sweep_term), with the
filing-date bisection from get_ca_sos as the fallback. Auth, pacing, the
pause-for-fresh-credentials loop and the checkpoint are get_ca_sos's.

Government source; the index is referenced, not re-hosted.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from hoaspy.collect.registries import get_ca_sos as sos
from hoaspy.collect.registries import records_sunbiz
from hoaspy import ROOT
OUT_DEFAULT = ROOT / "liens" / "liens_ca_ucc.jsonl"
SOURCES_DEFAULT = ROOT / "liens" / "sources_ca_ucc.json"
RAW_DEFAULT = ROOT / "liens" / "ca_ucc_raw.jsonl"
CKPT_DONE = ROOT / "liens" / ".ca_ucc_done.txt"
CKPT_PARTIAL = ROOT / "liens" / ".ca_ucc_partial.jsonl"
WORDS_FILE = ROOT / "liens" / ".ca_ucc_words.json"

UCC_URL = "https://bizfileonline.sos.ca.gov/api/Records/uccsearch"
SOURCE = "ca-sos-ucc"
SOURCE_URL = "https://bizfileonline.sos.ca.gov/search/ucc"
RECORD_TYPES = {"2154": "Judgment Lien"}
PAGE = 100
DATE_MIN = "1980-01-01"

# HOA-shaped words; the engine prefix-matches, so CONDO also finds
# CONDOMINIUM and HOA also finds HOANG (dropped by the name gate).
KEYWORDS = [
    "HOA", "HOMEOWNERS", "HOMEOWNER", "HOME OWNERS", "OWNERS ASSOCIATION",
    "CONDOMINIUM", "CONDO", "PROPERTY OWNERS", "COMMUNITY ASSOCIATION",
    "MASTER ASSOCIATION", "RESIDENTS ASSOCIATION", "MAINTENANCE ASSOCIATION",
    "MAINTENANCE CORPORATION", "TOWNHOMES", "TOWNHOME", "TOWNHOUSE", "VILLAS",
]

log = logging.getLogger("ca_ucc")

_PARTY_RE = re.compile(r"^(?P<name>.+?)\s+-\s+(?P<city>[^-]*?),\s*(?P<st>[A-Z]{2})\s*$")
# Entity descriptors the UCC index appends to a party name:
#   ", A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION", " A CALIFORNIA
#   CORPORATION DBA ...", ", AN INDIVIDUAL", " C/O ...". Cut at the earliest.
_DESCRIPTOR_RES = [
    re.compile(r",?\s+\b(?:A|AN)\s+(?:[A-Z]+\s+){0,3}?(?:NON-?\s?PROFIT|NONPROFIT|"
               r"MUTUAL BENEFIT|PUBLIC BENEFIT|CORPORATION|CORP\.?|LIMITED|BANKING|"
               r"PROFESSIONAL|INDIVIDUAL|L\.?L\.?C\.?|L\.?P\.?|PARTNERSHIP|COMPANY|"
               r"TRUST|MUNICIPAL|GOVERNMENTAL|PUBLIC AGENCY)\b", re.I),
    re.compile(r"\s+(?:DBA|D/B/A|AKA|A/K/A|FKA|F/K/A|C/O|SUCCESSOR)\b", re.I),
    re.compile(r",?\s+ET\s+AL\.?$", re.I),
]


def clean_party_name(name: str) -> str:
    """'RIO VISTA WALK HOA, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION'
    -> 'RIO VISTA WALK HOA'."""
    s = " ".join((name or "").split())
    cut = len(s)
    for rx in _DESCRIPTOR_RES:
        m = rx.search(s)
        if m and m.start() < cut and m.start() > 0:
            cut = m.start()
    return s[:cut].strip(" ,;")


def parse_party(line: str) -> dict:
    """'<NAME> - <CITY>, <ST>' -> {name, city, state}; the raw line is the
    name when the suffix is absent."""
    line = " ".join((line or "").split())
    m = _PARTY_RE.match(line)
    if not m:
        return {"name": clean_party_name(line), "city": "", "state": ""}
    return {"name": clean_party_name(m.group("name")),
            "city": m.group("city").strip().title(), "state": m.group("st")}


_INDIVIDUAL_RE = re.compile(r"\bAN?\s+INDIVIDUAL\b", re.I)
_STRONG_ASSOC_RE = re.compile(
    r"\b(ASSOCIATION|ASSN|HOMEOWNERS?|HOME OWNERS?|CONDOMINIUMS?|CONDO|OWNERS|"
    r"TOWNHOMES?|TOWNHOUSES?|VILLAS|COMMUNITY|MASTER|MAINTENANCE)\b", re.I)
# Businesses that borrow an HOA word ("HOMEOWNERS MARKETING SERVICES, INC.",
# "HOA PROPERTY MANAGEMENT"): a trade word with no ASSOCIATION/ASSN/HOA
# token is a vendor, not a community.
_BUSINESS_RE = re.compile(
    r"\b(MARKETING|SERVICES?|MANAGEMENT|INSURANCE|MORTGAGE|LENDING|BANK|REALTY|"
    r"FINANCIAL|ROOFING|CONSTRUCTION|SUPPLY|PLUMBING|LANDSCAP\w*|WARRANTY)\b", re.I)
_ASSOC_TOKEN_RE = re.compile(r"\b(ASSOCIATION|ASSN|HOA)\b", re.I)
_KNOWN: set[str] | None = None


def known_names() -> set[str]:
    """Normalised names of the CA entities in records/state_corps.jsonl that
    are common-interest developments by entity type (is_association) — the
    strongest evidence a UCC party is an association. Keyword-named LLCs
    and stock corporations are deliberately left out: they include vendors
    like "HOMEOWNERS MARKETING SERVICES, INC." that must still face the
    business-word check."""
    global _KNOWN
    if _KNOWN is None:
        _KNOWN = set()
        p = ROOT / "records" / "state_corps.jsonl"
        if p.exists():
            for ln in p.read_text().splitlines():
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if r.get("state") == "CA" and r.get("is_association"):
                    _KNOWN.add(norm_name(r.get("name", "")))
    return _KNOWN


def norm_name(s: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()


def is_association(name: str, raw: str = "", known: set | None = None) -> bool:
    """Association-shaped party. Two UCC-specific guards on top of the shared
    name gate: the index marks people as 'AN INDIVIDUAL', and HOA is a common
    Vietnamese given name, so a bare leading 'HOA <surname>' with no other
    association word is a person unless it is a CA corporation we know."""
    if _INDIVIDUAL_RE.search(raw or ""):
        return False
    if known is None:
        known = known_names()
    if norm_name(name) in known:
        return True            # a CA association we hold, keyword or not
    if not records_sunbiz.ASSOCIATION_NAME_RE.search(name or ""):
        return False
    if _BUSINESS_RE.search(name or "") and not _ASSOC_TOKEN_RE.search(name or ""):
        return False
    tokens = (name or "").upper().split()
    if tokens and tokens[0] == "HOA" and len(tokens) > 1 \
            and tokens[1] not in ("OF", "AT", "FOR", "@", "-") \
            and not _STRONG_ASSOC_RE.search(name):
        return False
    return True


def to_record(row: dict) -> dict | None:
    """Shape one UCC row into the liens/*.jsonl schema, or None when no
    party is association-shaped (a person named Hoang, a bank)."""
    raw_d = [x for x in (row.get("TITLE") or []) if x]
    raw_c = [x for x in (row.get("SEC_PARTY") or []) if x]
    debtors = [parse_party(x) for x in raw_d]
    creditors = [parse_party(x) for x in raw_c]
    hoa = next((p for p, r in zip(creditors, raw_c) if is_association(p["name"], r)), None)
    role = "creditor" if hoa else ""
    if not hoa:
        hoa = next((p for p, r in zip(debtors, raw_d) if is_association(p["name"], r)), None)
        role = "debtor" if hoa else ""
    if not hoa:
        return None
    filed = row.get("FILING_DATE") or ""
    ymd = sos.iso_date(filed).replace("-", "")
    return {
        "doc_id": row.get("RECORD_NUM") or str(row.get("ID", "")),
        "bizfile_id": str(row.get("ID", "")),
        "doc_type": "JL" if role == "creditor" else "JLX",
        "doc_type_label": ("judgment_lien" if role == "creditor"
                           else "judgment_lien_against_association"),
        "recorded_date": filed, "recorded_ymd": ymd,
        "year": int(ymd[:4]) if ymd[:4].isdigit() else 0,
        "amount": "", "case_number": "",
        "state": "CA", "county": "", "city": hoa["city"],
        "source": SOURCE, "source_page": SOURCE_URL,
        "association": hoa["name"],
        "filers": [p["name"] for p in creditors],
        "respondents": [p["name"] for p in debtors],
        "n_parties": len(creditors) + len(debtors),
        "legal_description": "", "parcel_id": "", "property_address": "",
        "lapse_date": row.get("LAPSE_DATE") or "",
        "status": row.get("STATUS") or "",
        "record_type": row.get("RECORD_TYPE") or "",
        "hoa_role": role,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_response(payload: dict) -> tuple[list[dict], int]:
    rows = payload.get("rows") or {}
    out = [r for r in (to_record(row) for row in rows.values()) if r]
    return out, len(rows)


def ucc_body(term: str, record_type: str = "2154", start: str | None = None,
             end: str | None = None, offset: int = 0, limit: int = PAGE) -> dict:
    body = {
        "SEARCH_VALUE": term, "STATUS": "ACTIVE_LAPSED_UNLAPSED",
        "RECORD_TYPE_ID": record_type or "2154",
        "FILING_DATE": {"start": sos.mdy_date(start) if start else None,
                        "end": sos.mdy_date(end) if end else None},
        "LAPSE_DATE": {"start": None, "end": None},
    }
    if offset:
        # Two spellings of the same request: the response's own "edge"
        # shape, and flat keys. Whichever the server reads, it will echo
        # the offset back in edge.offset — sweep_term checks that.
        body["edge"] = {"offset": offset, "limit": limit}
        body["OFFSET"], body["LIMIT"] = offset, limit
    return body


def sweep_term(client, ckpt: sos.Checkpoint, term: str, rtype: str,
               date_min: str = DATE_MIN) -> tuple[int, str]:
    """Collect every row for one (term, record type). Returns (new rows,
    how): 'single' page held everything, 'paged' if the server honoured
    offsets, 'bisected' if it did not and the filing-date window had to be
    split instead."""
    key = f"{term}|{rtype}|all"
    if key in ckpt.done:
        return 0, "done"
    search_fn = client.search if hasattr(client, "search") else client   # Client or a fake
    recs, raw, rows = search_fn(term, rtype, None, None)
    edge = rows.pop("__edge__", {}) or {}
    total = int(edge.get("total") or raw)
    new = ckpt.add(recs, rows)
    log.info("%-24s type=%s -> %d rows (total %d), %d new", term[:24], rtype, raw, total, new)
    # The server counts result *hits* (total, limit) while the row dict is
    # keyed by record ID, so a page of 100 hits can come back as 70 rows
    # when a record matches under several party names. Step by the server's
    # page size, never by the row count, or pages overlap.
    limit = int(edge.get("limit") or PAGE)
    if total <= limit or raw == 0:
        ckpt.mark_done(key)
        return new, "single"
    offset = limit
    paged = True
    while offset < total:
        recs2, raw2, rows2 = search_fn(term, rtype, None, None, offset=offset, limit=limit)
        edge2 = rows2.pop("__edge__", {}) or {}
        if int(edge2.get("offset") or 0) != offset:
            paged = False          # server ignored the offset: same page again
            break
        n2 = ckpt.add(recs2, rows2)
        new += n2
        log.info("%-24s type=%s   page @%d/%d -> %d rows, %d new",
                 term[:24], rtype, offset, total, raw2, n2)
        if raw2 == 0:
            break                  # ran off the end early; nothing more to page
        offset += limit
    if paged:
        ckpt.mark_done(key)
        return new, "paged"
    log.info("%-24s type=%s   offset not honoured — bisecting filing dates (cap %d)",
             term[:24], rtype, raw)
    sw = sos.Sweeper(search_fn, ckpt, date_min=date_min, cap=raw)
    new += sw.run_term(term, rtype)
    ckpt.mark_done(key)
    return new, "bisected"


def write_outputs(records: list[dict], out: Path, sources: Path, requests_made: int) -> int:
    if not records:
        log.warning("no CA judgment-lien records collected — leaving %s untouched", out.name)
        return 0
    records.sort(key=lambda r: (r["recorded_ymd"], r["doc_id"]), reverse=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with tmp.open("w") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(out)
    by_role = Counter(r["hoa_role"] for r in records)
    by_status = Counter(r["status"] for r in records)
    sources.write_text(json.dumps({
        "source": "California Secretary of State — bizfile UCC search, judgment liens",
        "source_url": SOURCE_URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "records": len(records), "api_calls": requests_made,
        "by_role": dict(by_role), "by_status": dict(by_status),
        "years": [min(r["year"] for r in records if r["year"]),
                  max(r["year"] for r in records if r["year"])] if any(r["year"] for r in records) else [],
        "note": "Judgment liens filed with the SOS against personal property; "
                "role says whether the association holds the judgment (creditor) "
                "or owes it (debtor). Real-property judgment liens live in county "
                "recorder indexes, which are not covered here.",
    }, indent=2))
    return len(records)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cookie-file", type=Path, default=sos.COOKIE_FILE)
    ap.add_argument("--token-file", type=Path, default=sos.TOKEN_FILE)
    ap.add_argument("--keywords", help="comma-separated override of the keyword list")
    ap.add_argument("--record-types", default=",".join(RECORD_TYPES),
                    help="comma-separated RECORD_TYPE_IDs (default 2154 = Judgment Lien)")
    ap.add_argument("--pace", type=float, default=1.5)
    ap.add_argument("--wait-minutes", type=float, default=480)
    ap.add_argument("--max-requests", type=int, default=0)
    ap.add_argument("--date-min", default=DATE_MIN)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--sources", type=Path, default=SOURCES_DEFAULT)
    ap.add_argument("--raw", type=Path, default=RAW_DEFAULT)
    ap.add_argument("--probe", action="store_true",
                    help="one keyword, first two pages, print the paging edge; no writes")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s\t%(message)s", datefmt="%H:%M:%S")

    try:
        client = sos.Client(args.cookie_file, args.token_file, pace=args.pace,
                            wait_minutes=args.wait_minutes, max_requests=args.max_requests,
                            url=UCC_URL, body_fn=ucc_body, parse_fn=parse_response)
    except ValueError as e:
        log.error("%s", e)
        return 2

    keywords = ([k.strip() for k in args.keywords.split(",") if k.strip()]
                if args.keywords else KEYWORDS)
    rtypes = [t.strip() for t in args.record_types.split(",") if t.strip()]

    if args.probe:
        term = keywords[0]
        recs, raw, rows = client.search(term, rtypes[0], None, None)
        edge = rows.pop("__edge__", {})
        print(f"page 1: {raw} rows, {len(recs)} association-shaped, edge={edge}")
        for r in recs[:5]:
            print("  ", r["hoa_role"], r["doc_type"], r["recorded_date"], r["association"],
                  "<-" if r["hoa_role"] == "creditor" else "->", r["respondents"][:1] or r["filers"][:1])
        if raw and int(edge.get("total") or 0) > raw:
            recs2, raw2, rows2 = client.search(term, rtypes[0], None, None, offset=raw)
            edge2 = rows2.pop("__edge__", {})
            print(f"page 2 @{raw}: {raw2} rows, edge={edge2}, "
                  f"{'NEW ids' if set(rows2) - set(rows) else 'SAME ids (offset ignored)'}")
        return 0

    if args.fresh:
        sos.Checkpoint(CKPT_DONE, CKPT_PARTIAL).clear()
    ckpt = sos.Checkpoint(CKPT_DONE, CKPT_PARTIAL, args.raw)
    log.info("checkpoint: %d done, %d rows so far", len(ckpt.done), len(ckpt.by_id))

    completed = True
    hows: Counter = Counter()
    try:
        for rtype in rtypes:
            for i, term in enumerate(keywords, 1):
                new, how = sweep_term(client, ckpt, term, rtype, args.date_min)
                hows[how] += 1
                log.info("== [%d/%d] %r type=%s: +%d new (%s; total %d, %d requests)",
                         i, len(keywords), term, rtype, new, how, len(ckpt.by_id), client.requests)
    except KeyboardInterrupt:
        log.warning("interrupted — checkpoint kept; rerun to resume")
        completed = False
    except (sos.BlockedError, RuntimeError) as e:
        log.error("stopped: %s — checkpoint kept; rerun to resume", e)
        completed = False

    n = write_outputs(list(ckpt.by_id.values()), args.out, args.sources, client.requests)
    log.info("wrote %s — %d CA judgment-lien rows (%s)", args.out.name, n, dict(hows))
    if completed:
        ckpt.clear()
        log.info("sweep complete — checkpoint cleared")
    else:
        log.info("run paused before finishing — checkpoint kept; rerun to resume")
    return 0 if completed else 3


if __name__ == "__main__":
    sys.exit(main())
