"""Cook County (IL) recorder lien index, via the Clerk's Recordings System (CRS).

The Cook County Recorder of Deeds merged into the County Clerk's office in
December 2020; recorded documents are searched at
<https://crs.cookcountyclerkil.gov/Search>. Everything below was verified
2026-10-01:

1. **Anonymous.** No login, no captcha. A session cookie plus a per-form
   ``__RequestVerificationToken`` are the only auth, both minted by a GET of
   the search page. Long runs re-warm the session when a response redirects
   to ``/Account/Login``.

2. **PIN search needs a full 14-digit unit PIN.** Building-level (10-digit
   pin10) prefixes return zero rows, and assessment liens are recorded
   against unit PINs — Cook holds ~450k condominium unit PINs — so a
   building-by-building sweep is impossible. The working shape is the
   **Advanced Search party-name form** (``POST /Search/Additional?Index=
   %23collapse2``): ``GTName`` (all tokens must appear in one party name),
   ``GTGECode`` (``""`` either side / ``D`` grantor / ``I`` grantee),
   ``DocumentTypes`` (a multi-select over 178 type codes) and
   ``RecordedFromDate``/``RecordedToDate``. The side matters: searched on
   either side, ``CONDO`` with the lis pendens types returns over 1,000
   documents a year in the 2008-2012 foreclosure wave, all of them lenders
   joining the association as a defendant; on the grantor side it returns
   the handful the associations filed (14 for 2010).

3. **A 1,000-row cap** on the Advanced Search (10 pages x 100 rows, newest
   first; the page says ``Total Documents : 1,000`` and page 11 is empty).
   The plain search box caps at 10,000 instead but takes no date range, so
   it can never reach past its newest 10,000. ``CONDO`` alone records more
   than 1,000 lien-family documents a year, so a term is swept by **walking
   back**: search ``[earliest, to]``, read every page, and if the result
   was capped search again with ``to`` set to the oldest recording date
   seen (``next_cursor``). Each search yields up to 1,000 new documents;
   the boundary day is fetched twice and de-duplicated by document number.

4. **List pages** (``ResultAddt``) carry doc number, recorded/executed
   dates, the type label, consideration, 1st grantor, 1st grantee, an
   associated-doc column and the first PIN — enough to shape a record when
   one of the two first parties is the association (``row_to_parsed``).
   Paging is ``/Search/SortResultAddt?...&page=N`` against the session's
   LAST search — a resumed window re-POSTs its criteria before fetching
   page N.

5. **Detail pages** (``/Document/Detail?dId&hId`` — opaque ids scraped from
   the list row; they stay valid in a later session) carry the full
   grantor/grantee party lists (one ``<tr>`` per party — the list page's
   "1st" columns lose co-parties), the consideration amount, the address
   and the legal table's PINs. A document needs its detail page when
   neither first party is an association (``needs_detail``): the
   association the search matched is then a co-party.

Document-type vocabulary. Cook records a condominium association's lien
against a unit owner under the plain type ``LIEN`` (``CORRECTED LIEN`` for a
re-recording); foreclosure escalation shows as ``LIS PENDENS FORECLOSURE``
(plus AMENDED/CORRECTED variants) and is kept only when the association
filed it: a lender's foreclosure that joins the association as a defendant
is dropped. A contractor's ``MECHANICS LIEN`` naming
the association as the property's debtor is kept apart under ``LXA`` — the
slot California's ``JLX`` uses — so it is never counted as a lien the
association filed. Federal/state/tax lien codes (FDLN, TAXL, STLN, CSLP,
NPTD, LEVY, the release/subordination family) are excluded at the query and
dropped defensively if one appears anyway.

Which party is the association is decided with the shared court-portals gate
(``looks_like_association`` / ``has_business_form``) plus a financial-name
guard mirroring ``build_site.is_financial``: national banks are chartered as
"... National Association" and party names like ``US BK NATL ASSN`` carry an
ASSN token without being a community, so a bank on either side must never be
picked as the association (that mislabelled a contractor's mechanics lien as
filed by an association in the first one-off run).
"""

from __future__ import annotations

import html as _html
import logging
import re
import time
from datetime import date

import requests

from hoaspy.collect.courts.court_portals._common import (
    has_business_form,
    iso_date,
    looks_like_association,
)

log = logging.getLogger("records.cook")

BASE = "https://crs.cookcountyclerkil.gov"
SEARCH_PAGE = f"{BASE}/Search"
ADVANCED_PAGE = f"{BASE}/Search/Additional"
ADVANCED_FORM = "/Search/Additional?Index=%23collapse2"
RESULT_PAGER = "/Search/SortResultAddt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

PAGE_SIZE = 100
ROW_CAP = 1000          # the Advanced Search never returns more than this
PAGE_CAP = ROW_CAP // PAGE_SIZE
MAX_DETAIL_PARTIES = 50  # sanity bound; Cook lien docs run 1-6 parties

# The lien-family document-type codes sent to the Advanced Search multi-select.
# Federal/state/tax lien codes and their releases are deliberately absent.
LIEN_TYPE_CODES = ["LIEN", "CORL", "MECL", "LISF", "AMLF", "COLF"]

# Defensive only: these must never reach a record even if a search returns one.
DROP_TYPE_RE = re.compile(
    r"FEDERAL|TAX|STATE LIEN|CHILD SUPPORT|LEVY|WITHDRAWAL|SUBORDIN|RELEASE", re.I)

# Cook type label -> shared vocabulary code, when the ASSOCIATION is the filer.
# MECHANICS LIEN maps here too: an association CAN be a mechanics-lien filer,
# and a lien the association recorded counts as a claim of lien.
FILER_TYPE_MAP = {
    "LIEN": ("LIE", "claim_of_lien"),
    "CORRECTED LIEN": ("LIE", "claim_of_lien"),
    "MECHANICS LIEN": ("LIE", "claim_of_lien"),
    "LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
    "AMENDED LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
    "CORRECTED LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
}

# A lien where the association is the DEBTOR (a contractor's mechanics lien
# or other lien naming it): counted under "other" by the build, like
# California's JLX — never as a lien the association filed.
DEBTOR_TYPE = ("LXA", "lien_against_association")

EARLIEST = date(1980, 1, 1)   # Cook's online index starts in the mid-1980s;
                              # empty early windows cost one request each.


# --------------------------------------------------------------------------
# Party classification
# --------------------------------------------------------------------------

# Mirrors hoaspy/build/build_site.is_financial (the build cannot be imported
# from a collector): a bank/GSE name-form in financial context, unless a
# community token rescues it ("Bankston Meadows Homeowners Association").
# Covers the clerk spellings a national bank's charter name produces:
# "US BK NATL ASSN", "FEDERAL NATIONAL MTGE ASSN", "... TRUST ...".
_BANKISH_RE = re.compile(r"\b\w{0,8}(?:BANK|BANC)\w{0,12}\b|\bBN?K\b")
_FIN_CTX_RE = re.compile(
    r"NATIONAL|NATIONS?\b|NATIO\w+|TRUST|MORTGAGE|SAVINGS|LOAN|FEDERAL|"
    r"SERIES|\bN[A-Z]{0,7}L\b|\bNAT?\b|\bTR\b|\bSAV\b|\bCUST\b")
_FIN_ONLY_RE = re.compile(
    r"MORTGAGE|\bMTGE?\b|\bMRTGE?\b|\bMORTG\b|\bSAV(?:INGS?)?\b|\bLOANS?\b|\bLN\b|"
    r"\bS ?& ?L\b|PASS ?THROUGH|CREDIT UNION|\bCU\b|\bINS(?:URANCE)?\b|ANNUITY|"
    # a national bank's charter name: "... NATIONAL ASSOCIATION" / "NATL ASSN"
    r"\bNAT(?:IONA)?L\s+ASS(?:OCIATIO)?N\b")
_COMMUNITY_TOKEN_RE = re.compile(
    r"HOMEOWNERS?|CONDOMINIUM|\bCONDO\b|PROPERTY OWNERS|OWNERS ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|RESIDENTS?\b|TOWNHOME|TOWNHOUSE|"
    r"\bHOA\b|APARTMENT|COOPERATIVE|\bCO ?OP\b|VILLAS?\b|\bESTATES\b")


# "ALL UNIT OWNERS", "401 INDIVIDUAL UNIT OWNERS", "UNKNOWN OWNERS": the people
# a mechanics lien or a foreclosure is aimed at, not an association.
_OWNERS_AT_LARGE_RE = re.compile(
    r"\b(?:ALL|INDIVIDUALS?|UNKNOWN|VARIOUS|NON[- ]?RECORD|OTHER)\s+(?:UNIT\s+|PROPERTY\s+)?OWNERS\b")


_CREDIT_UNION_RE = re.compile(r"CREDIT UNION|\bF?CU$")


def is_financial_name(name: str) -> bool:
    norm = " ".join((name or "").upper().split())
    if not norm:
        return False
    if _CREDIT_UNION_RE.search(norm):       # "CONSUMERS COOPERATIVE CU": no community word rescues it
        return True
    if _COMMUNITY_TOKEN_RE.search(norm):
        return False
    if _BANKISH_RE.search(norm) and _FIN_CTX_RE.search(norm):
        return True
    return bool(_FIN_ONLY_RE.search(norm))


# "TENG & ASSOC INC", "SEARL & ASSOC ARCHITECTS PC", "MIDWEST CONST ASSOC INC":
# ASSOC is "Associates" there. Without a word that says community, a bare
# ASSOC / "& ASSN" is a firm; ASSOCIATION and ASSN on their own still pass.
_ASSOCIATES_RE = re.compile(r"(?:&|\bAND)\s*ASS(?:OC|N)\w*|\bASSOCS?\b|\bASSOCIATES\b")
_COMMUNITY_WORD_RE = re.compile(
    r"CONDO|HOME ?OWNER|TOWN ?HOME|TOWN ?HOUSE|\bOWNERS\b|\bMASTER\b|COMMUNITY|IMPROVEMENT|"
    r"\bCO ?OP\b|COOPERATIVE|\bBOARD\b")
_BARE_BOARD_RE = re.compile(r"^(?:THE )?BOARD (?:OF )?(?:MANAGERS|DIRECTORS)$")


def is_association_party(name: str) -> bool:
    """The shared party gate plus the financial-name guard: a party is the
    association when it reads as one (marker, no business form) and is not a
    bank/GSE name-form — 'US BK NATL ASSN' fails here."""
    name = " ".join((name or "").split())
    if not name:
        return False
    upper = name.upper()
    if has_business_form(name) or _OWNERS_AT_LARGE_RE.search(upper) or _BARE_BOARD_RE.match(upper):
        return False
    if _ASSOCIATES_RE.search(upper) and not _COMMUNITY_WORD_RE.search(upper):
        return False
    return looks_like_association(name) and not is_financial_name(name)


def pick_association(filers: list[str], respondents: list[str]) -> tuple[str, str]:
    """(association, side) — the filer side wins, then the respondent side,
    exactly like Broward/Miami-Dade. side is 'filer', 'respondent' or ''."""
    for p in filers:
        if is_association_party(p):
            return p, "filer"
    for p in respondents:
        if is_association_party(p):
            return p, "respondent"
    return "", ""


# --------------------------------------------------------------------------
# Record shaping
# --------------------------------------------------------------------------

def drop_reason(doc_type_label: str, filers: list[str], respondents: list[str]) -> str:
    """Why a document is not a record, or "" when it is one:

    - ``excluded_type``: a federal / state / tax lien or a release;
    - ``no_association_party``: nobody on either side reads as an association;
    - ``unmapped_type``: the association filed something outside the lien
      family this collector maps;
    - ``foreclosure_by_another_party``: a lis pendens somebody else filed —
      a lender foreclosing on a unit joins the association as a defendant
      for its junior lien, which says nothing about the association."""
    raw = " ".join((doc_type_label or "").upper().split())
    if not raw or DROP_TYPE_RE.search(raw):
        return "excluded_type"
    association, side = pick_association(filers, respondents)
    if not association:
        return "no_association_party"
    if side == "filer":
        return "" if raw in FILER_TYPE_MAP else "unmapped_type"
    return "foreclosure_by_another_party" if "LIS PENDENS" in raw else ""


def classify(doc_type_label: str, filers: list[str],
             respondents: list[str]) -> tuple[str, str, str] | None:
    """(doc_type, doc_type_label, association) for a parsed document, or
    None when drop_reason() names a reason to leave it out."""
    if drop_reason(doc_type_label, filers, respondents):
        return None
    raw = " ".join((doc_type_label or "").upper().split())
    association, side = pick_association(filers, respondents)
    if side == "filer":
        return (*FILER_TYPE_MAP[raw], association)
    return DEBTOR_TYPE[0], DEBTOR_TYPE[1], association


def to_record(parsed: dict, retrieved_at: str) -> dict | None:
    """A parsed detail page (see parse_detail) -> a shared-shape lien record,
    or None when classify() says drop."""
    got = classify(parsed.get("doc_type", ""), parsed.get("filers", []),
                   parsed.get("respondents", []))
    if got is None:
        return None
    code, label, association = got
    recorded = iso_date(parsed.get("recorded") or "")
    return {
        "doc_id": str(parsed.get("doc_number") or "").strip(),
        "doc_type": code,
        "doc_type_label": label,
        "recorded_date": recorded,
        "recorded_ymd": recorded,   # ISO sorts correctly as a plain string
        "year": int(recorded[:4]) if recorded[:4].isdigit() else 0,
        "state": "IL",
        "county": "Cook",
        "association": association,
        "filers": parsed.get("filers", []),
        "respondents": parsed.get("respondents", []),
        "n_parties": len(parsed.get("filers", [])) + len(parsed.get("respondents", [])),
        "amount": parsed.get("amount"),
        "case_number": "",
        "parcel_id": parsed.get("pin", ""),
        "legal_description": "",
        "property_address": parsed.get("address", ""),
        "source": "cook-crs",
        "source_page": SEARCH_PAGE,
        "retrieved_at": retrieved_at,
    }


# --------------------------------------------------------------------------
# HTML parsing (fixtures exercise these against captured pages)
# --------------------------------------------------------------------------

_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)

# Results-page column headers -> row keys. The plain FullSearch results page
# has no consideration column and the Advanced Search page heads it
# "Consi. Amt."; mapping by header label handles both layouts.
_HEADER_KEYS = {
    "doc number": "doc_number",
    "doc recorded": "recorded",
    "doc executed": "executed",
    "doc type": "doc_type_label",
    "consideration amount": "consideration",
    "consi. amt.": "consideration",
    "1st grantor": "grantor1",
    "1st grantee": "grantee1",
    "1st pin": "pin",
}


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c)).strip()
            for c in _CELL_RE.findall(row_html, re.S)]


def _unescape(s: str) -> str:
    return _html.unescape(s or "")


def parse_result_page(page_html: str) -> tuple[list[dict], int]:
    """A results page -> (rows, npages). Rows carry the list-page fields and
    the opaque detail href; parties beyond the first come from the detail
    page, not here."""
    rows: list[dict] = []
    npages = 1
    header: dict[int, str] = {}
    for m in _ROW_RE.finditer(page_html):
        cells = _cells(m.group(1))
        if not cells:
            continue
        if any("Doc Number" in c for c in cells):
            header = {i: _HEADER_KEYS[c.lower()] for i, c in enumerate(cells)
                      if c.lower() in _HEADER_KEYS}
            continue
        if len(cells) < 6 or cells[1].upper() != "VIEW":
            continue
        link = re.search(r'href="([^"]*Document/Detail[^"]*)"', m.group(1))
        row: dict = {"consideration": "", "grantor1": "", "grantee1": "", "pin": ""}
        for i, key in header.items():
            if i < len(cells):
                row[key] = _unescape(cells[i])
        pin = (row.get("pin") or "").split()
        row["pin"] = pin[0] if pin else ""
        row["detail_href"] = _unescape(link.group(1)) if link else None
        if row.get("doc_number"):
            rows.append(row)
    m = re.search(r"Page\s+\d+\s+of\s+(\d+)", page_html)
    if m:
        npages = int(m.group(1))
    return rows, npages


NO_DOCUMENTS = "No Document(s) found"      # what a search with no matches answers


def result_total(page_html: str) -> int | None:
    """The "Total Documents" count a results page shows (1,000 when the
    search was capped), 0 for the no-matches page, or None when the page
    carries neither."""
    if NO_DOCUMENTS in page_html:
        return 0
    m = re.search(r"Total Documents\s*:\s*(?:</span>\s*<span[^>]*>)?\s*([\d,]+)", page_html)
    return int(m.group(1).replace(",", "")) if m else None


def row_to_parsed(row: dict) -> dict:
    """A results-page row in parse_detail()'s shape: the two first parties
    stand in for the party lists and there is no address. Enough for
    classify()/to_record() when one of them is the association."""
    amt = None
    m = re.search(r"\$([\d,]+(?:\.\d{1,2})?)", row.get("consideration") or "")
    if m:
        amt = float(m.group(1).replace(",", ""))
    one = lambda v: [" ".join(v.split())] if (v or "").strip() else []   # noqa: E731
    return {
        "doc_number": row.get("doc_number", ""),
        "doc_type": row.get("doc_type_label", ""),
        "recorded": row.get("recorded", ""),
        "executed": row.get("executed", ""),
        "address": "",
        "amount": amt,
        "pin": row.get("pin", ""),
        "filers": one(row.get("grantor1")),
        "respondents": one(row.get("grantee1")),
    }


NAME_CAP = 50           # the results page cuts a party name at this many characters


def truncated(row: dict) -> bool:
    """True when a first-party name fills the results page's column: the
    rest of it ("... CONDOMINIUM ASSOCIA") is only on the detail page."""
    return any(len(row.get(k) or "") >= NAME_CAP for k in ("grantor1", "grantee1"))


def needs_detail(row: dict) -> bool:
    """True when the row alone cannot say which party is the association:
    the search matched a co-party the list page does not show."""
    p = row_to_parsed(row)
    return not pick_association(p["filers"], p["respondents"])[0]


def _label_value(page_html: str, label: str) -> str:
    """The <td> text of the header-table row whose <label> is `label`."""
    m = re.search(rf"<th><label>{re.escape(label)}:</label></th>\s*<td>(.*?)</td>",
                  page_html, re.S)
    if not m:
        return ""
    return _unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip())


def parse_parties(page_html: str, heading: str) -> list[str]:
    """The grantor or grantee table, one party per <tr> — never joined.
    HTML entities are decoded ("A &amp; B CONDO ASSN" -> "A & B CONDO ASSN")."""
    m = re.search(rf'<span class="fs-5">{heading}</span>.*?<tbody>(.*?)</tbody>',
                  page_html, re.S)
    if not m:
        return []
    out = []
    for row in _ROW_RE.findall(m.group(1)):
        cells = _cells(row)
        if not cells:
            continue
        name = _unescape(cells[0]).strip()
        if name:
            out.append(" ".join(name.split()))
    return out[:MAX_DETAIL_PARTIES]


def parse_detail(page_html: str) -> dict:
    """A Document/Detail page -> the raw fields classify()/to_record() shape.
    `pin` is the legal table's first PIN (lien docs target one unit; a multi-
    parcel lis pendens keeps its first)."""
    amount = _label_value(page_html, "Consideration Amount")
    amt = None
    m = re.search(r"\$([\d,]+(?:\.\d{1,2})?)", amount)
    if m:
        try:
            amt = float(m.group(1).replace(",", ""))
        except ValueError:
            amt = None
    pin = ""
    m = re.search(r"Property Index # \(PIN\)</a>.*?<tbody>\s*<tr[^>]*>(.*?)</tr>",
                  page_html, re.S)
    if m:
        cells = _cells(m.group(1))
        pin = _unescape(cells[0]).strip() if cells else ""
    return {
        "doc_number": _label_value(page_html, "Document Number"),
        "doc_type": _label_value(page_html, "Document Type"),
        "recorded": _label_value(page_html, "Date Recorded"),
        "executed": _label_value(page_html, "Date Executed"),
        "address": _label_value(page_html, "Address"),
        "amount": amt,
        "pin": pin,
        "filers": parse_parties(page_html, "Grantors"),
        "respondents": parse_parties(page_html, "Grantees"),
    }


# --------------------------------------------------------------------------
# Window planning (pure — unit-tested without network)
# --------------------------------------------------------------------------

def parse_us_date(text: str) -> date | None:
    """'9/23/2026' as the results page prints it -> a date."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return date(int(m.group(3)), int(m.group(1)), int(m.group(2))) if m else None


def us_date(d: date) -> str:
    """A date as the form's mask wants it: MM/DD/YYYY."""
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def next_cursor(rows: list[dict], total: int | None, to: date) -> date | None:
    """Where the next search for this term should end, or None when the
    search just read was complete.

    `rows` are every row of the window `[earliest, to]`, `total` the page's
    "Total Documents". Under the cap the term is done. At the cap the result
    holds only the newest ROW_CAP documents, so the next window ends on the
    oldest recording date seen — inclusive, because that day may be cut in
    half; the overlap is de-duplicated by document number. A single day that
    fills the cap by itself cannot be walked past that way: the cursor then
    steps to the day before, and the caller records the day as truncated."""
    capped = (total is not None and total >= ROW_CAP) or len(rows) >= ROW_CAP
    if not capped:
        return None
    days = [d for d in (parse_us_date(r.get("recorded", "")) for r in rows) if d]
    if not days:
        return None
    oldest = min(days)
    return oldest if oldest < to else to - date.resolution


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class CookCRSError(RuntimeError):
    pass


class CookCRS:
    """A warm, self-refreshing CRS session. Paging reads the session's last
    search, so post_window() precedes the page() calls of every window, and
    a session loss mid-window means re-posting the window before paging on."""

    def __init__(self, pace: float = 1.5):
        self.s = requests.Session()
        self.s.headers.update({"user-agent": UA, "accept": "*/*"})
        self.pace = pace
        self.token: str | None = None
        self._last_window: tuple | None = None
        self.warm()

    def _sleep(self):
        time.sleep(self.pace)

    def _lost(self, r: requests.Response) -> bool:
        if "Account/Login" in r.url:
            return True
        if NO_DOCUMENTS in r.text:          # an answer, not a lost session
            return False
        return ("__RequestVerificationToken" in r.text
                and "View Doc" not in r.text)

    def _get(self, path: str, **kw) -> requests.Response:
        r = self.s.get(BASE + path, timeout=60, allow_redirects=True, **kw)
        if self._lost(r):
            raise CookCRSError(f"session lost on GET {path} -> {r.url}")
        self._sleep()
        return r

    def warm(self):
        r = self.s.get(ADVANCED_PAGE, timeout=60)
        r.raise_for_status()
        m = re.search(r'<form[^>]*action="(/Search/Additional\?Index=%23collapse2)".*?</form>',
                      r.text, re.S)
        if not m:
            raise CookCRSError("advanced-search form not found on " + ADVANCED_PAGE)
        token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', m.group(0))
        if not token:
            raise CookCRSError("no request-verification token on the advanced form")
        self.token = token.group(1)
        self._last_window = None
        self._sleep()

    def post_window(self, term: str, frm: str, to: str,
                    types: list[str] | None = None, side: str = "") -> tuple[str, int]:
        """Run one window search; returns (first-page HTML, npages) — read the
        row count with result_total(). Dates are MM/DD/YYYY as the form's
        mask requires. `side` is the form's party side: "" either, "D" the
        term must be in a grantor's name, "I" in a grantee's."""
        self._last_window = (term, frm, to, types, side)
        data = ([("__RequestVerificationToken", self.token), ("GTName", term),
                 ("GTGECode", side), ("RecordedFromDate", frm), ("RecordedToDate", to)]
                + [("DocumentTypes", t) for t in (types or LIEN_TYPE_CODES)])
        r = self.s.post(BASE + ADVANCED_FORM, data=data,
                        headers={"Referer": ADVANCED_PAGE}, timeout=60,
                        allow_redirects=True)
        if self._lost(r):
            raise CookCRSError(f"window search {term!r} {frm}..{to} was not accepted")
        self._sleep()
        _, npages = parse_result_page(r.text)
        return r.text, npages

    def page(self, n: int) -> str:
        """Page n of the current window's result set."""
        return self._get(f"{RESULT_PAGER}?id1=%23collapse2&column=DateRecorded"
                         f"&direction=desc&page={n}").text

    def detail(self, href: str) -> str:
        return self._get(href).text

    def retry_window(self) -> tuple[str, int]:
        """Re-post the last window (token or session rotated mid-paging)."""
        saved = self._last_window
        self.warm()
        if saved is not None:
            return self.post_window(*saved)
        raise CookCRSError("no window to retry")
