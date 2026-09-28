"""Miami-Dade County Official Records index, via the Clerk's public search API.

Broward hands out its whole index over SFTP; Miami-Dade does not. What it does
expose is the JSON API behind the public Official Records search at
<https://onlineservices.miamidadeclerk.gov/officialrecords/>. Two calls:

    POST /api/home/standardsearch?partyName=&documentType=<T>&dateRangeFrom=..&
         dateRangeTo=..&searchT=<T>&firstQuery=y&searchtype=Name/Document
         -> {"isValidSearch": true, "qs": "<opaque encrypted criteria blob>"}
    GET  /api/SearchResults/getStandardRecords?qs=<blob>
         -> {"searchCritiriea": {...}, "recordingModels": [ <row>, ... ]}

`partyName` empty enumerates every recording of that document type in the date
range — the same "pull the index, filter to associations" shape as Broward.

Three things shape the collector:

1. Auth is a single opaque cookie, `.PremierIDDade`. No login, no CAPTCHA: the
   `x-recaptcha-token` header the site sends is not validated server-side. The
   cookie is anonymous and issued by the site's SSO; grab it once from a browser
   (DevTools -> Application -> Cookies) and pass it in. It is not committed.

2. Results are hard-capped at 500 rows, ordered by (recorded_date, sequence)
   ascending. A window wider than 500 rows silently returns only its earliest
   dates, so we page: request [from, to], and if capped, continue from the last
   date seen. A single day over the cap is sub-sliced by party initial so no
   filings are dropped (see enumerate_type).

3. Every document is returned once per party (a D "direct/filer" row and an R
   "reverse/respondent" row), so rows are collapsed to one record per clerk file
   number (CFN), gathering all parties.

Unlike Broward, the Miami-Dade index carries the folio (parcel), legal
description and often the street address on the same row, so these records DO
locate the property.
"""

from __future__ import annotations

import logging
import re
import time
import urllib.parse
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("records.miamidade")

# The association-name filter, kept identical to records_broward.ASSOCIATION_RE
# so both counties classify the same way. Copied rather than imported because
# records_broward pulls in paramiko (SFTP) and this collector is pure HTTP.
ASSOCIATION_RE = re.compile(
    r"(HOMEOWNER|CONDOMINIUM|\bCONDO\b|PROPERTY OWNER|MASTER ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|\bHOA\b|TOWNHOUSE|TOWNHOME|VILLAS? OF|"
    r"ASSOCIATION,? INC|ASSN,? INC|\bASSN\b)", re.I)

BASE = "https://onlineservices.miamidadeclerk.gov/officialrecords"
SEARCH_PAGE = f"{BASE}/StandardSearch"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

ROW_CAP = 500  # server-side hard limit on rows per result set

# The Miami-Dade document-type strings (exact, from /api/home/documentTypes)
# mapped to Broward's short codes so both counties share one vocabulary.
# NB: the federal-tax-lien label carries a double space, kept verbatim.
LIEN_TYPES = {
    "LIEN - LIE": ("LIE", "claim_of_lien"),
    "LIS PENDENS - LIS": ("LP", "lis_pendens"),
    "NOTICE OF CONTEST OF LIEN - NCT": ("NCL", "notice_of_contest_of_lien"),
    "CANCELLATION OF LIS PENDENS - CLP": ("CLP", "cancellation_of_lis_pendens"),
    "FEDERAL TAX LIEN  - FTL": ("FTL", "federal_tax_lien"),
    "NOTICE OF TAX LIEN - NTL": ("NTL", "notice_of_tax_lien"),
    "SATISFACTION OF JUDGMENT - SJU": ("SJU", "satisfaction_of_judgment"),
}

# The lien itself plus escalation, mirroring Broward's DEFAULT_TYPES. The tax
# liens and satisfactions are opt-in: huge and mostly not association matters.
DEFAULT_TYPES = [
    "LIEN - LIE",
    "LIS PENDENS - LIS",
    "NOTICE OF CONTEST OF LIEN - NCT",
]


class MiamiDadeError(RuntimeError):
    pass


def session(premier_id_cookie: str) -> requests.Session:
    """A ready-to-use session. `premier_id_cookie` is the value of the
    `.PremierIDDade` cookie (with or without the `.PremierIDDade=` prefix)."""
    val = premier_id_cookie.strip()
    if val.startswith(".PremierIDDade="):
        val = val.split("=", 1)[1]
    if not val:
        raise MiamiDadeError("empty .PremierIDDade cookie")
    s = requests.Session()
    s.headers.update({
        "user-agent": UA,
        "accept": "application/json",
        "referer": f"{BASE}/StandardSearch",
        "origin": "https://onlineservices.miamidadeclerk.gov",
    })
    s.cookies.set(".PremierIDDade", val,
                  domain="onlineservices.miamidadeclerk.gov")
    return s


def _mint_qs(s: requests.Session, doctype: str, party: str,
             frm: str, to: str, timeout: int = 30) -> str | None:
    """Turn search criteria into the encrypted `qs` blob, or None if the server
    reports no matching records (isValidSearch false)."""
    params = {
        "partyName": party,
        "dateRangeFrom": frm,
        "dateRangeTo": to,
        "documentType": doctype,
        "searchT": doctype,
        "firstQuery": "y",
        "searchtype": "Name/Document",
    }
    url = f"{BASE}/api/home/standardsearch?" + urllib.parse.urlencode(params)
    r = s.post(url, data="", headers={"content-type": "application/json; charset=utf-8"},
               timeout=timeout)
    if r.status_code != 200:
        raise MiamiDadeError(f"standardsearch HTTP {r.status_code}")
    body = r.json()
    if not body.get("isValidSearch") or not body.get("qs"):
        return None
    return body["qs"]


def _fetch_rows(s: requests.Session, qs: str, timeout: int = 60) -> list[dict]:
    url = (f"{BASE}/api/SearchResults/getStandardRecords?qs="
           + urllib.parse.quote(qs, safe=""))
    r = s.get(url, timeout=timeout)
    if r.status_code != 200:
        raise MiamiDadeError(f"getStandardRecords HTTP {r.status_code}")
    body = r.json()
    # A minted qs that decrypts to null criteria (bad/expired cookie) returns an
    # empty set with null criteria; treat that as an auth failure, not "no data".
    crit = body.get("searchCritiriea") or {}
    rows = body.get("recordingModels") or []
    if rows and crit.get("documentType") is None:
        raise MiamiDadeError(
            "results returned with null criteria — the .PremierIDDade cookie is "
            "stale or wrong; refresh it from the browser")
    return rows


def _query(s: requests.Session, doctype: str, frm: str, to: str,
           party: str = "") -> tuple[list[dict], bool]:
    """One search. Returns (rows, capped) where capped means the row count hit
    the server limit and the window's later dates were truncated."""
    qs = _mint_qs(s, doctype, party, frm, to)
    if qs is None:
        return [], False
    rows = _fetch_rows(s, qs)
    return rows, len(rows) >= ROW_CAP


def _collapse(rows: list[dict]) -> dict[int, dict]:
    """Group the per-party rows into one record per clerk file number."""
    by_cfn: dict[int, dict] = {}
    parties: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for m in rows:
        cfn = m.get("cfN_MASTER_ID")
        if cfn is None:
            continue
        parties[cfn].append((str(m.get("firsT_PARTY") or "").strip(),
                             str(m.get("partY_CODE") or "").strip()))
        by_cfn.setdefault(cfn, m)
    for cfn, m in by_cfn.items():
        m["_parties"] = parties[cfn]
    return by_cfn


def _iso(rec_date: str) -> str:
    """'8/30/2004 12:00:00 AM' -> '2004-08-30'."""
    if not rec_date:
        return ""
    try:
        return datetime.strptime(rec_date.split(" ")[0], "%m/%d/%Y").date().isoformat()
    except ValueError:
        return ""


def to_record(m: dict, doctype_str: str) -> dict:
    """One collapsed row -> a Broward-shaped lien record."""
    code, label = LIEN_TYPES.get(doctype_str, (doctype_str, doctype_str))
    people = m.get("_parties") or [(str(m.get("firsT_PARTY") or "").strip(),
                                    str(m.get("partY_CODE") or "").strip())]
    filers = [n for n, role in people if role == "D" and n]
    respondents = [n for n, role in people if role == "R" and n]
    # The association is normally the filer (lienor) but occasionally indexed on
    # the reverse side — check both, filer first, exactly like Broward.
    association = next((n for n in filers if ASSOCIATION_RE.search(n)), "")
    if not association:
        association = next((n for n in respondents if ASSOCIATION_RE.search(n)), "")

    folio = m.get("foliO_NUMBER") or 0
    return {
        "doc_id": (m.get("clerk_File") or f"{m.get('cfN_YEAR')}-{m.get('cfN_SEQ')}").strip(),
        "cfn": (m.get("clerk_File") or "").strip(),
        "year": m.get("cfN_YEAR"),
        "recorded_date": _iso(m.get("reC_DATE") or ""),
        # build_site.py sorts a community's recent filings on recorded_ymd and
        # reads property_address; ISO dates sort correctly as plain strings.
        "recorded_ymd": _iso(m.get("reC_DATE") or ""),
        "property_address": (m.get("address") or "").strip(),
        "doc_type": code,
        "doc_type_label": label,
        "state": "FL",
        "county": "Miami-Dade",
        "source": "miamidade-or-standardsearch",
        "source_page": SEARCH_PAGE,
        "association": association,
        "filers": filers,
        "respondents": respondents,
        "n_parties": len(people),
        "amount": m.get("consideratioN_1") or 0,
        "case_number": (m.get("casE_NUM") or "") or "",
        "parcel_id": str(folio) if folio else "",
        "legal_description": (m.get("legaL_DESCRIPTION") or m.get("subdiV_NAME") or "").strip(),
        "subdivision": (m.get("subdiV_NAME") or "").strip(),
        "book_page": (m.get("reC_BOOKPAGE") or "").strip(),
        "address": (m.get("address") or "").strip(),
    }


# --------------------------------------------------------------------------
# Primary mode: name-driven. HOA Spy is association-keyed and already holds
# the Florida association list, so we ask the index the question we actually
# want answered — "what has THIS association filed?" — one name at a time.
#
# This is the mode that is COMPLETE. An exact-name party search returns every
# recording naming that party across all years in a single response, and a lone
# association's lien history is far under the 500-row cap (measured: the busiest
# condo associations run a few hundred lifetime lien-family filings). It sits
# under the cap where blind date-enumeration does not (see enumerate_window).
# --------------------------------------------------------------------------

def search_party(s: requests.Session, name: str,
                 doctypes: list[str] | None = None, frm: str = "1978-01-01",
                 to: str | None = None, pace: float = 0.4) -> list[dict]:
    """Every lien-family record naming `name`, as Broward-shaped records.

    Queries each document type separately (each result set is tiny and complete)
    and collapses to one record per clerk file number. If any single type comes
    back at the row cap for one name, that is logged — it would mean an
    association with 250+ filings of one type, which should be inspected, not
    silently truncated."""
    doctypes = doctypes or DEFAULT_TYPES
    to = to or date.today().isoformat()
    by_cfn: dict[int, tuple[dict, str]] = {}
    for dt in doctypes:
        rows, capped = _query(s, dt, frm, to, party=name)
        time.sleep(pace)
        if capped:
            log.warning("Miami-Dade name %r type %s hit the %d-row cap — "
                        "results for this association may be incomplete",
                        name, dt, ROW_CAP)
        for cfn, m in _collapse(rows).items():
            by_cfn.setdefault(cfn, (m, dt))
    return [to_record(m, dt) for m, dt in by_cfn.values()]


def fetch_for_names(s: requests.Session, names: list[str],
                    doctypes: list[str] | None = None, pace: float = 0.4,
                    require_association: bool = False, progress=None,
                    skip: set[str] | None = None, seen_ids: set[str] | None = None,
                    on_name_done=None) -> list[dict]:
    """Run search_party over many association names, deduped across names.

    `require_association` re-applies the association-name filter to the returned
    parties; off by default because the query name is itself the association, so
    a match is expected even when the clerk's spelling dodges the regex.

    For long runs: `skip` is a set of names already collected (resume), and
    `on_name_done(name, new_records)` fires after each name so the caller can
    checkpoint to disk. `seen_ids` seeds the cross-name dedupe from a resume."""
    skip = skip or set()
    seen: set[str] = set(seen_ids or ())
    out: list[dict] = []
    for i, name in enumerate(names, 1):
        if name in skip:
            if progress:
                progress(i, len(names), name, len(out))
            continue
        try:
            recs = search_party(s, name, doctypes=doctypes, pace=pace)
        except MiamiDadeError:
            raise
        except Exception as exc:  # one bad name must not sink the whole run
            log.warning("Miami-Dade name %r failed: %s", name, exc)
            continue
        fresh: list[dict] = []
        for r in recs:
            if require_association and not r["association"]:
                continue
            r = {**r, "query_name": name}
            if r["doc_id"] in seen:
                continue
            seen.add(r["doc_id"])
            fresh.append(r)
        out.extend(fresh)
        if on_name_done:
            on_name_done(name, fresh)
        if progress:
            progress(i, len(names), name, len(out))
    return out


# --------------------------------------------------------------------------
# Secondary mode: date-window discovery. Walks a date range to surface liens
# filed by associations NOT yet in our list. Honest about its limit: the index
# caps result sets at 500 rows with no enumeration paging, so a day with more
# than ~250 lien-family recordings is TRUNCATED. We detect and report that
# rather than pretend to page past it — use the FTP folder for a complete
# historical index. Good for a recent rolling window, where daily volume is low.
# --------------------------------------------------------------------------

def enumerate_window(s: requests.Session, start: date, end: date,
                     doctypes: list[str] | None = None, pace: float = 0.5,
                     associations_only: bool = True) -> tuple[list[dict], list[str]]:
    """Association lien-family records in [start, end] by date. Returns
    (records, truncated_days) where truncated_days lists any (type, day) whose
    result set hit the cap and is therefore incomplete."""
    doctypes = doctypes or DEFAULT_TYPES
    seen: set[int] = set()
    out: list[dict] = []
    truncated: list[str] = []
    for dt in doctypes:
        d = start
        while d <= end:
            rows, capped = _query(s, dt, d.isoformat(), d.isoformat())
            time.sleep(pace)
            if capped:
                truncated.append(f"{dt} {d.isoformat()}")
                log.warning("Miami-Dade %s %s: %d-row cap hit — day is "
                            "truncated, not complete", dt, d.isoformat(), ROW_CAP)
            for cfn, m in _collapse(rows).items():
                if cfn in seen:
                    continue
                seen.add(cfn)
                rec = to_record(m, dt)
                if associations_only and not rec["association"]:
                    continue
                out.append(rec)
            d += timedelta(days=1)
    return out, truncated
