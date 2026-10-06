"""Cook County, IL recorder lien index, over the Clerk's Recordings System (CRS).

The Cook County Recorder of Deeds merged into the County Clerk's office in
December 2020; recorded documents are searched at
https://crs.cookcountyclerkil.gov/Search.

1. **Anonymous.** No login, no captcha. A session cookie plus a per-form
   `__RequestVerificationToken` are the only state, both minted by a GET
   of the search page. Long runs re-warm the session when a response
   redirects to /Account/Login.

2. **A PIN search needs a full 14-digit unit PIN.** Building-level
   (10-digit) prefixes return nothing, and assessment liens are recorded
   against unit PINs — Cook holds ~450k condominium unit PINs — so a
   building-by-building sweep is impossible. The working shape is the
   **Advanced Search party-name form** (POST /Search/Additional?Index=
   %23collapse2): `GTName` (every token must appear in one party name),
   `GTGECode` ("" either side, "D" grantor, "I" grantee), `DocumentTypes`
   (a multi-select over 178 type codes) and `RecordedFromDate` /
   `RecordedToDate`. The side matters: searched on either side, CONDO with
   the lis pendens types returns over 1,000 documents a year in the
   2008-2012 foreclosure wave, all of them lenders joining the association
   as a defendant; on the grantor side it returns the handful the
   associations filed.

3. **A 1,000-row cap** on the Advanced Search (10 pages of 100 rows,
   newest first; the page says "Total Documents : 1,000" and page 11 is
   empty). CONDO alone records more than 1,000 lien-family documents a
   year, so a term is swept by **walking back**: search [earliest, to],
   read every page, and if the result was capped search again with `to`
   set to the oldest recording date seen (`next_cursor`). The boundary
   day is fetched twice and de-duplicated by document number.

4. **List pages** carry the document number, recorded and executed dates,
   the type label, the consideration, the first grantor, the first grantee
   and the first PIN — enough to shape a record when one of the two first
   parties is the association (`row_to_parsed`). Paging is
   /Search/SortResultAddt?...&page=N against the session's LAST search, so
   a resumed window re-posts its criteria before fetching page N.

5. **Detail pages** (/Document/Detail?dId&hId, opaque ids from the list
   row that stay valid later) carry the full grantor and grantee lists,
   the consideration, the address and the legal table's PINs. A document
   needs its detail page when neither first party is an association
   (`needs_detail`): the association the search matched is then a co-party.

Document types. Cook records a condominium association's lien against a
unit owner under the plain type LIEN (CORRECTED LIEN for a re-recording);
foreclosure escalation shows as LIS PENDENS FORECLOSURE (plus AMENDED and
CORRECTED variants) and is kept only when the association filed it. A
contractor's MECHANICS LIEN naming the association as the debtor is kept
apart under LXA — the slot California's JLX uses — so it is never counted
as a lien the association filed. Federal, state and tax lien codes are
excluded at the query and dropped if one appears anyway.

Which party is the association is decided with the court-portal party
gate (`looks_like_association` / `has_business_form`) plus a financial-
name guard: national banks are chartered as "... National Association"
and party names like "US BK NATL ASSN" carry an ASSN token without being a
community, so a bank on either side is never picked as the association.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import date

import requests

from hoaspy.collect.courts.court_portals._common import has_business_form, iso_date, looks_like_association
from hoaspy.lib.http import BROWSER_USER_AGENT
from hoaspy.lib.records import Lien

log = logging.getLogger(__name__)

RECORDER_URL = "https://crs.cookcountyclerkil.gov"
SEARCH_PAGE = f"{RECORDER_URL}/Search"
ADVANCED_PAGE = f"{RECORDER_URL}/Search/Additional"
ADVANCED_FORM = "/Search/Additional?Index=%23collapse2"
RESULT_PAGER = "/Search/SortResultAddt"
SOURCE = "cook-crs"

PAGE_SIZE = 100
ROW_CAP = 1000          # the Advanced Search never returns more than this
PAGE_CAP = ROW_CAP // PAGE_SIZE
MAX_DETAIL_PARTIES = 50     # a sanity bound; Cook lien documents run 1-6 parties
NAME_CAP = 50               # the results page cuts a party name at this many characters
EARLIEST = date(1980, 1, 1)     # Cook's online index starts in the mid-1980s; empty early windows cost one request each
NO_DOCUMENTS = "No Document(s) found"      # what a search with no matches answers

# The lien-family document-type codes sent to the Advanced Search
# multi-select. Federal, state and tax lien codes and their releases are
# deliberately absent.
LIEN_TYPE_CODES = ["LIEN", "CORL", "MECL", "LISF", "AMLF", "COLF"]
# Defensive only: these must never reach a record even if a search returns one.
DROP_TYPE_RE = re.compile(r"FEDERAL|TAX|STATE LIEN|CHILD SUPPORT|LEVY|WITHDRAWAL|SUBORDIN|RELEASE", re.I)
# Cook type label -> shared code, when the ASSOCIATION is the filer. A
# MECHANICS LIEN maps here too: an association can file one, and a lien it
# recorded counts as a claim of lien.
FILER_TYPES = {
    "LIEN": ("LIE", "claim_of_lien"),
    "CORRECTED LIEN": ("LIE", "claim_of_lien"),
    "MECHANICS LIEN": ("LIE", "claim_of_lien"),
    "LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
    "AMENDED LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
    "CORRECTED LIS PENDENS FORECLOSURE": ("LP", "lis_pendens"),
}
# A lien where the association is the DEBTOR (a contractor's mechanics lien
# or another lien naming it): counted apart, like California's JLX.
DEBTOR_TYPE = ("LXA", "lien_against_association")


# ---- party classification --------------------------------------------------------

# A bank or GSE name-form in financial context, unless a community token
# rescues it ("Bankston Meadows Homeowners Association"). Covers the clerk
# spellings a national bank's charter name produces: "US BK NATL ASSN",
# "FEDERAL NATIONAL MTGE ASSN", "... TRUST ...".
BANKISH_RE = re.compile(r"\b\w{0,8}(?:BANK|BANC)\w{0,12}\b|\bBN?K\b")
FINANCIAL_CONTEXT_RE = re.compile(
    r"NATIONAL|NATIONS?\b|NATIO\w+|TRUST|MORTGAGE|SAVINGS|LOAN|FEDERAL|"
    r"SERIES|\bN[A-Z]{0,7}L\b|\bNAT?\b|\bTR\b|\bSAV\b|\bCUST\b")
FINANCIAL_ONLY_RE = re.compile(
    r"MORTGAGE|\bMTGE?\b|\bMRTGE?\b|\bMORTG\b|\bSAV(?:INGS?)?\b|\bLOANS?\b|\bLN\b|"
    r"\bS ?& ?L\b|PASS ?THROUGH|CREDIT UNION|\bCU\b|\bINS(?:URANCE)?\b|ANNUITY|"
    # a national bank's charter name: "... NATIONAL ASSOCIATION" / "NATL ASSN"
    r"\bNAT(?:IONA)?L\s+ASS(?:OCIATIO)?N\b")
COMMUNITY_TOKEN_RE = re.compile(
    r"HOMEOWNERS?|CONDOMINIUM|\bCONDO\b|PROPERTY OWNERS|OWNERS ASSOCIATION|"
    r"COMMUNITY ASSOCIATION|MASTER ASSOCIATION|RESIDENTS?\b|TOWNHOME|TOWNHOUSE|"
    r"\bHOA\b|APARTMENT|COOPERATIVE|\bCO ?OP\b|VILLAS?\b|\bESTATES\b")
# "ALL UNIT OWNERS", "401 INDIVIDUAL UNIT OWNERS", "UNKNOWN OWNERS": the
# people a mechanics lien or a foreclosure is aimed at, not an association.
OWNERS_AT_LARGE_RE = re.compile(
    r"\b(?:ALL|INDIVIDUALS?|UNKNOWN|VARIOUS|NON[- ]?RECORD|OTHER)\s+(?:UNIT\s+|PROPERTY\s+)?OWNERS\b")
CREDIT_UNION_RE = re.compile(r"CREDIT UNION|\bF?CU$")
ASSN_RE = re.compile(r"\bASS(?:OCIATIO)?N\b")
# "TENG & ASSOC INC", "SEARL & ASSOC ARCHITECTS PC", "MIDWEST CONST ASSOC INC":
# ASSOC is "Associates" there. Without a word that says community, a bare
# ASSOC / "& ASSN" is a firm; ASSOCIATION and ASSN on their own still pass.
ASSOCIATES_RE = re.compile(r"(?:&|\bAND)\s*ASS(?:OC|N)\w*|\bASSOCS?\b|\bASSOCIATES\b")
COMMUNITY_WORD_RE = re.compile(
    r"CONDO|HOME ?OWNER|TOWN ?HOME|TOWN ?HOUSE|\bOWNERS\b|\bMASTER\b|COMMUNITY|IMPROVEMENT|"
    r"\bCO ?OP\b|COOPERATIVE|\bBOARD\b")
# "BOARD OF MANAGERS", "CONDIMINIUM ASSOCIATION": the tail of a name the
# clerk split over two party entries. With these words taken out nothing is left.
GENERIC_WORD_RE = re.compile(
    r"\b(?:THE|OF|AND|BOARD|MANAGERS|DIRECTORS|COND\w*|HOME ?OWNERS?|TOWN ?HOMES?|TOWN ?HOUSES?|UNIT|"
    r"PROPERTY|OWNERS|MASTER|COMMUNITY|ASSOCIATION|ASSN|ASSOC|ASN|INC|NFP|NO)\b|[^A-Z0-9]")


def is_financial_name(name: str) -> bool:
    squashed = " ".join((name or "").upper().split())
    if not squashed:
        return False
    if CREDIT_UNION_RE.search(squashed):       # "CONSUMERS COOPERATIVE CU": no community word rescues it
        return True
    if COMMUNITY_TOKEN_RE.search(squashed):
        return False
    if BANKISH_RE.search(squashed) and (FINANCIAL_CONTEXT_RE.search(squashed) or ASSN_RE.search(squashed)):
        return True                            # "LASALLE BK NALT ASSN": a bank however NATL is typed
    return bool(FINANCIAL_ONLY_RE.search(squashed))


def is_generic_name(name: str) -> bool:
    return not GENERIC_WORD_RE.sub("", (name or "").upper())


def is_association_party(name: str) -> bool:
    """The shared party gate plus the financial-name guard: a party is the
    association when it reads as one (a marker, no business form) and is
    not a bank or GSE name-form — "US BK NATL ASSN" fails here."""
    name = " ".join((name or "").split())
    if not name:
        return False
    upper = name.upper()
    if has_business_form(name) or OWNERS_AT_LARGE_RE.search(upper) or is_generic_name(upper):
        return False
    if ASSOCIATES_RE.search(upper) and not COMMUNITY_WORD_RE.search(upper):
        return False
    return looks_like_association(name) and not is_financial_name(name)


def pick_association(filers: list[str], respondents: list[str]) -> tuple[str, str]:
    """(association, side): the filer side wins, then the respondent side,
    as for Broward and Miami-Dade; side is "filer", "respondent" or "".
    On a side with several candidates (a mechanics lien can name fifteen
    respondents) the one whose name says community comes first."""
    for side, parties in (("filer", filers), ("respondent", respondents)):
        found = [party for party in parties if is_association_party(party)]
        if found:
            return next((party for party in found if COMMUNITY_WORD_RE.search(party.upper())), found[0]), side
    return "", ""


# ---- record shaping --------------------------------------------------------------

def drop_reason(doc_type_label: str, filers: list[str], respondents: list[str]) -> str:
    """Why a document is not a record, or "" when it is one:

    - `excluded_type`: a federal, state or tax lien, or a release;
    - `no_association_party`: nobody on either side reads as an association;
    - `unmapped_type`: the association filed something outside the lien
      family this collector maps;
    - `foreclosure_by_another_party`: a lis pendens somebody else filed — a
      lender foreclosing on a unit joins the association as a defendant for
      its junior lien, which says nothing about the association."""
    label = " ".join((doc_type_label or "").upper().split())
    if not label or DROP_TYPE_RE.search(label):
        return "excluded_type"
    association, side = pick_association(filers, respondents)
    if not association:
        return "no_association_party"
    if side == "filer":
        return "" if label in FILER_TYPES else "unmapped_type"
    return "foreclosure_by_another_party" if "LIS PENDENS" in label else ""


def classify(doc_type_label: str, filers: list[str], respondents: list[str]) -> tuple[str, str, str] | None:
    """(doc_type, doc_type_label, association) for a parsed document, or
    None when drop_reason() names a reason to leave it out."""
    if drop_reason(doc_type_label, filers, respondents):
        return None
    label = " ".join((doc_type_label or "").upper().split())
    association, side = pick_association(filers, respondents)
    if side == "filer":
        return (*FILER_TYPES[label], association)
    return DEBTOR_TYPE[0], DEBTOR_TYPE[1], association


def to_record(parsed: dict, retrieved_at: str) -> Lien | None:
    """A parsed document (see parse_detail / row_to_parsed) as a lien
    record, or None when classify() says drop."""
    kind = classify(parsed.get("doc_type", ""), parsed.get("filers", []), parsed.get("respondents", []))
    if kind is None:
        return None
    code, label, association = kind
    recorded = iso_date(parsed.get("recorded") or "")
    filers, respondents = parsed.get("filers", []), parsed.get("respondents", [])
    return Lien(
        doc_id=str(parsed.get("doc_number") or "").strip(), doc_type=code, doc_type_label=label,
        recorded_date=recorded, recorded_ymd=recorded,      # ISO sorts correctly as text
        year=int(recorded[:4]) if recorded[:4].isdigit() else 0, state="IL", county="Cook",
        association=association, filers=filers, respondents=respondents, n_parties=len(filers) + len(respondents),
        amount=parsed.get("amount"), parcel_id=parsed.get("pin", ""), property_address=parsed.get("address", ""),
        source=SOURCE, source_page=SEARCH_PAGE, retrieved_at=retrieved_at)


# ---- HTML parsing (the fixtures exercise these against captured pages) ---------

ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
# Results-page column headers -> row keys. The plain FullSearch results
# page has no consideration column and the Advanced Search page heads it
# "Consi. Amt."; mapping by header label handles both layouts.
HEADER_KEYS = {
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
AMOUNT_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)")


def cell_texts(row_html: str) -> list[str]:
    return [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip() for cell in CELL_RE.findall(row_html, re.S)]


def parse_result_page(page_html: str) -> tuple[list[dict], int]:
    """A results page -> (rows, number of pages). A row carries the list
    columns and the opaque detail href; parties beyond the first come from
    the detail page, not from here."""
    rows: list[dict] = []
    pages = 1
    header: dict[int, str] = {}
    for match in ROW_RE.finditer(page_html):
        cells = cell_texts(match.group(1))
        if not cells:
            continue
        if any("Doc Number" in cell for cell in cells):
            header = {i: HEADER_KEYS[cell.lower()] for i, cell in enumerate(cells) if cell.lower() in HEADER_KEYS}
            continue
        if len(cells) < 6 or cells[1].upper() != "VIEW":
            continue
        link = re.search(r'href="([^"]*Document/Detail[^"]*)"', match.group(1))
        row: dict = {"consideration": "", "grantor1": "", "grantee1": "", "pin": ""}
        for i, key in header.items():
            if i < len(cells):
                row[key] = html.unescape(cells[i])
        pins = (row.get("pin") or "").split()
        row["pin"] = pins[0] if pins else ""
        row["detail_href"] = html.unescape(link.group(1)) if link else None
        if row.get("doc_number"):
            rows.append(row)
    match = re.search(r"Page\s+\d+\s+of\s+(\d+)", page_html)
    if match:
        pages = int(match.group(1))
    return rows, pages


def result_total(page_html: str) -> int | None:
    """The "Total Documents" count a results page shows (1,000 when the
    search was capped), 0 for the no-matches page, or None when the page
    carries neither."""
    if NO_DOCUMENTS in page_html:
        return 0
    match = re.search(r"Total Documents\s*:\s*(?:</span>\s*<span[^>]*>)?\s*([\d,]+)", page_html)
    return int(match.group(1).replace(",", "")) if match else None


def dollar_amount(text: str) -> float | None:
    match = AMOUNT_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def row_to_parsed(row: dict) -> dict:
    """A results-page row in parse_detail()'s shape: the two first parties
    stand in for the party lists and there is no address. Enough for
    classify() and to_record() when one of them is the association."""
    def party(value: str | None) -> list[str]:
        return [" ".join(value.split())] if (value or "").strip() else []

    return {
        "doc_number": row.get("doc_number", ""), "doc_type": row.get("doc_type_label", ""),
        "recorded": row.get("recorded", ""), "executed": row.get("executed", ""), "address": "",
        "amount": dollar_amount(row.get("consideration")), "pin": row.get("pin", ""),
        "filers": party(row.get("grantor1")), "respondents": party(row.get("grantee1")),
    }


def truncated(row: dict) -> bool:
    """True when a first-party name fills the results page's column: the
    rest of it ("... CONDOMINIUM ASSOCIA") is only on the detail page."""
    return any(len(row.get(key) or "") >= NAME_CAP for key in ("grantor1", "grantee1"))


def needs_detail(row: dict) -> bool:
    """True when the row alone cannot say which party is the association:
    the search matched a co-party the list page does not show."""
    parsed = row_to_parsed(row)
    return not pick_association(parsed["filers"], parsed["respondents"])[0]


def label_value(page_html: str, label: str) -> str:
    """The <td> text of the header-table row whose <label> is `label`."""
    match = re.search(rf"<th><label>{re.escape(label)}:</label></th>\s*<td>(.*?)</td>", page_html, re.S)
    if not match:
        return ""
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", match.group(1))).strip())


def parse_parties(page_html: str, heading: str) -> list[str]:
    """The grantor or grantee table, one party per <tr>, never joined;
    HTML entities decoded ("A &amp; B CONDO ASSN" -> "A & B CONDO ASSN")."""
    match = re.search(rf'<span class="fs-5">{heading}</span>.*?<tbody>(.*?)</tbody>', page_html, re.S)
    if not match:
        return []
    out = []
    for row_html in ROW_RE.findall(match.group(1)):
        cells = cell_texts(row_html)
        if not cells:
            continue
        name = html.unescape(cells[0]).strip()
        if name:
            out.append(" ".join(name.split()))
    return out[:MAX_DETAIL_PARTIES]


def parse_detail(page_html: str) -> dict:
    """A Document/Detail page -> the raw fields classify() and to_record()
    shape. `pin` is the legal table's first PIN (a lien document targets one
    unit; a multi-parcel lis pendens keeps its first)."""
    pin = ""
    match = re.search(r"Property Index # \(PIN\)</a>.*?<tbody>\s*<tr[^>]*>(.*?)</tr>", page_html, re.S)
    if match:
        cells = cell_texts(match.group(1))
        pin = html.unescape(cells[0]).strip() if cells else ""
    return {
        "doc_number": label_value(page_html, "Document Number"),
        "doc_type": label_value(page_html, "Document Type"),
        "recorded": label_value(page_html, "Date Recorded"),
        "executed": label_value(page_html, "Date Executed"),
        "address": label_value(page_html, "Address"),
        "amount": dollar_amount(label_value(page_html, "Consideration Amount")),
        "pin": pin,
        "filers": parse_parties(page_html, "Grantors"),
        "respondents": parse_parties(page_html, "Grantees"),
    }


# ---- window planning (pure; unit-tested without network) ---------------------

def parse_us_date(text: str) -> date | None:
    """'9/23/2026' as the results page prints it -> a date."""
    match = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return date(int(match.group(3)), int(match.group(1)), int(match.group(2))) if match else None


def us_date(day: date) -> str:
    """A date as the form's mask wants it: MM/DD/YYYY."""
    return f"{day.month:02d}/{day.day:02d}/{day.year}"


def next_cursor(rows: list[dict], total: int | None, to: date) -> date | None:
    """Where the next search for this term should end, or None when the
    search just read was complete.

    `rows` are every row of the window [earliest, to], `total` the page's
    "Total Documents". Under the cap the term is done. At the cap the
    result holds only the newest ROW_CAP documents, so the next window
    ends on the oldest recording date seen — inclusive, because that day
    may be cut in half; the overlap is de-duplicated by document number. A
    single day that fills the cap by itself cannot be walked past that
    way: the cursor then steps to the day before, and the caller records
    the day as truncated."""
    capped = (total is not None and total >= ROW_CAP) or len(rows) >= ROW_CAP
    if not capped:
        return None
    days = [day for day in (parse_us_date(row.get("recorded", "")) for row in rows) if day]
    if not days:
        return None
    oldest = min(days)
    return oldest if oldest < to else to - date.resolution


# ---- the HTTP client ----------------------------------------------------------

class CookCRSError(RuntimeError):
    """The site answered with something other than a search result."""


class CookCRS:
    """A warm, self-refreshing CRS session. Paging reads the session's last
    search, so post_window() precedes the page() calls of every window, and
    a session lost mid-window means re-posting the window before paging on."""

    def __init__(self, pace: float = 1.5):
        self.session = requests.Session()
        self.session.headers.update({"user-agent": BROWSER_USER_AGENT, "accept": "*/*"})
        self.pace = pace
        self.token: str | None = None
        self.window: tuple | None = None        # the window being swept, for retry_window()
        self.warm()

    def _sleep(self) -> None:
        time.sleep(self.pace)

    def session_lost(self, response: requests.Response) -> bool:
        """True when the answer is the login page or the bare search form
        (the "no documents" page is an answer, not a lost session)."""
        if "Account/Login" in response.url:
            return True
        if NO_DOCUMENTS in response.text:
            return False
        return "__RequestVerificationToken" in response.text and "View Doc" not in response.text

    def _get(self, path: str, **kwargs) -> requests.Response:
        response = self.session.get(RECORDER_URL + path, timeout=60, allow_redirects=True, **kwargs)
        if self.session_lost(response):
            raise CookCRSError(f"session lost on GET {path} -> {response.url}")
        self._sleep()
        return response

    def warm(self) -> None:
        """Mint a session cookie and the advanced form's verification token."""
        response = self.session.get(ADVANCED_PAGE, timeout=60)
        response.raise_for_status()
        form = re.search(r'<form[^>]*action="(/Search/Additional\?Index=%23collapse2)".*?</form>', response.text, re.S)
        if not form:
            raise CookCRSError("advanced-search form not found on " + ADVANCED_PAGE)
        token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', form.group(0))
        if not token:
            raise CookCRSError("no request-verification token on the advanced form")
        self.token = token.group(1)
        self._sleep()

    def post_window(self, term: str, frm: str, to: str, types: list[str] | None = None,
                    side: str = "") -> tuple[str, int]:
        """Run one window search; (first-page HTML, number of pages) — read
        the row count with result_total(). Dates are MM/DD/YYYY as the form's
        mask requires. `side` is the form's party side: "" either, "D" the
        term must be in a grantor's name, "I" in a grantee's."""
        self.window = (term, frm, to, types, side)
        data = ([("__RequestVerificationToken", self.token), ("GTName", term),
                 ("GTGECode", side), ("RecordedFromDate", frm), ("RecordedToDate", to)]
                + [("DocumentTypes", code) for code in (types or LIEN_TYPE_CODES)])
        response = self.session.post(RECORDER_URL + ADVANCED_FORM, data=data,
                                     headers={"Referer": ADVANCED_PAGE}, timeout=60, allow_redirects=True)
        if self.session_lost(response):
            raise CookCRSError(f"window search {term!r} {frm}..{to} was not accepted")
        self._sleep()
        _, pages = parse_result_page(response.text)
        return response.text, pages

    def page(self, number: int) -> str:
        """Page `number` of the current window's result set."""
        return self._get(f"{RESULT_PAGER}?id1=%23collapse2&column=DateRecorded&direction=desc&page={number}").text

    def detail(self, href: str) -> str:
        return self._get(href).text

    def retry_window(self) -> None:
        """A fresh session, with the window being swept posted again (a
        token or session rotated mid-paging); nothing to re-post during the
        detail phase."""
        self.warm()
        if self.window is not None:
            self.post_window(*self.window)
