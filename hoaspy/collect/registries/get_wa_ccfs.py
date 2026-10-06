"""Washington Secretary of State — Corporations & Charities Filing System
(CCFS) advanced search: the association-named entities.

    python -m hoaspy wa-ccfs                 # full sweep (resumable)
    python -m hoaspy wa-ccfs --limit 3       # smoke test: 3 list pages
    python -m hoaspy wa-ccfs --fresh         # ignore the checkpoint

Source: https://ccfs.sos.wa.gov, an AngularJS app over ccfs-api.prod.sos.wa.gov.
Every /api/BusinessSearch/* call needs a Cloudflare Turnstile token that
the app's own <cf-turnstile> widget mints without a click. A token made
outside a browser would be captcha circumvention, so this collector drives
a local Chromium over its DevTools port (hoaspy/lib/browser.py) in a
throw-away browser context: it types each search term into the app's
advanced-search form, clicks Search, and pages the results by calling the
results controller's own `search(page)`. Nothing is minted, replayed or
solved from Python.

Only the list rows are read (25 per page): name, UBI, business type,
status, formation date, registered-agent name and the principal-office
street address — the corporate-roster columns. Per-entity detail calls
(governors, mailing addresses) are not made. Phone numbers, e-mail
addresses and EINs in the API payload are never read off the page.

Output: Washington's rows in records/state_corps.jsonl (replaced on each
complete run) plus `business_id`; coverage.json collected["corporate"] for
WA; records/corp_coverage.json["WA"] = "all" (rows carry a status, so a
dissolved or inactive corporation can be flagged). The `states` command
rewrites corp_coverage.json from state_sources.yml, where WA has no entry:
run this again after a full `states` run to restore the WA key.

Checkpoint: .cache/wa/ccfs_rows.jsonl (the raw list rows) and
ccfs_progress.json (pages done per term); a rerun resumes at the next page.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time

from hoaspy import ROOT
from hoaspy.collect.registries import CORPORATIONS_PATH
from hoaspy.collect.registries.get_states import CORP_COVERAGE_PATH
from hoaspy.lib.browser import DevTools, Tab
from hoaspy.lib.coverage import update_coverage
from hoaspy.lib.jsonl import append_row, now_iso, read_json, read_rows, replace_rows, write_json
from hoaspy.lib.names import REGISTRY_NAME_RE
from hoaspy.lib.records import Association

CACHE_DIR = ROOT / ".cache" / "wa"
ROWS_PATH = CACHE_DIR / "ccfs_rows.jsonl"
PROGRESS_PATH = CACHE_DIR / "ccfs_progress.json"

STATE = "WA"
SOURCE = "Washington Secretary of State — Corporations & Charities Filing System (CCFS)"
SOURCE_PAGE = "https://ccfs.sos.wa.gov/"
SEARCH_URL = "https://ccfs.sos.wa.gov/#/AdvancedSearch"
PAGE_SIZE = 25
MIN_PACE = 0.6
TOKEN_REFRESH_SECONDS = 240     # Turnstile tokens die server-side at 300 s
VERIFY_MESSAGE = "System verification in progress"
BACKOFFS = (60, 120, 300, 600, 900, 1200, 1800)   # after a verification refusal

# Name-contains terms. The server search is LIKE %term%, so "CONDO" also
# hits CONDOR/CONDON and "HOA" hits SHOAL: REGISTRY_NAME_RE (word-bounded)
# gates every row locally, and BusinessID de-duplicates across terms.
TERMS = ["HOMEOWNERS", "HOME OWNERS", "CONDOMINIUM", "CONDO", "PROPERTY OWNERS",
         "OWNERS ASSOCIATION", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
         "TOWNHOME", "TOWNHOUSE", "HOA"]

log = logging.getLogger(__name__)

# ---- page-context JavaScript (runs inside the app; awaited over DevTools) ----
# Only these keys leave the page: no PhoneNumber, EmailAddress or FEINNo.
PROJECT_JS = r'''
function __waProject(list){
  const addr=a=>a?{StreetAddress1:a.StreetAddress1,StreetAddress2:a.StreetAddress2,City:a.City,
                   State:a.State,Zip5:a.Zip5,County:a.County||a.CountyName||null}:null;
  return (list||[]).map(b=>({BusinessID:b.BusinessID,BusinessName:b.BusinessName,UBINumber:b.UBINumber,
    BusinessType:b.BusinessType,BusinessStatus:b.BusinessStatus,AgentName:b.AgentName,
    DateOfIncorporation:b.DateOfIncorporation,JurisdictionState:b.JurisdictionState,
    TotalRowCount:(b.Criteria||{}).TotalRowCount,
    PrincipalStreetAddress:addr((b.PrincipalOffice||{}).PrincipalStreetAddress),
    PrincipalMailingAddress:addr((b.PrincipalOffice||{}).PrincipalMailingAddress)}));
}'''

READY_JS = r'''!!(window.angular && document.getElementById('btnSearch')
     && document.getElementById('txtOrgname'))'''

# Fill the term and press the app's Search button; the results view's own
# Turnstile widget mints a token, its controller then posts page 1 itself.
SEARCH_JS = PROJECT_JS + r'''
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const el=document.getElementById('txtOrgname'); if(!el) return JSON.stringify({ok:false,err:'no form'});
  el.value=TERM; el.dispatchEvent(new Event('input',{bubbles:true}));
  el.dispatchEvent(new Event('change',{bubbles:true}));
  await sleep(300);
  // The advanced-search view has no widget of its own: Search routes to the
  // results view, whose <cf-turnstile> mints the token that triggers page 1.
  document.getElementById('btnSearch').click();
  for(let i=0;i<240;i++){
    await sleep(500);
    const back=document.getElementById('btnReturnToSearch');
    if(back){ const sc=angular.element(back).scope();
      if(sc && sc.businessList && typeof sc.search==='function' && sc.businesssearchType=='AdvancedSearch'
         && !sc.BusinessListProgressBar)
        return JSON.stringify({ok:true,page:sc.page,total:sc.totalCount,pages:sc.pagesCount,
                               rows:__waProject(sc.businessList)});
    }
    const dlg=document.querySelector('.ngdialog-content');
    if(dlg) return JSON.stringify({ok:false,err:'dialog: '+dlg.innerText.slice(0,200)});
  }
  return JSON.stringify({ok:false,err:'timeout waiting for results'});
})()'''

# Page N (0-based) through the results controller's own search(page).
PAGE_JS = PROJECT_JS + r'''
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const back=document.getElementById('btnReturnToSearch');
  if(!back) return JSON.stringify({ok:false,err:'not on results view'});
  const sc=angular.element(back).scope(); const root=sc.$root;
  let tok=null;
  for(let i=0;i<120;i++){ tok=root.reCaptcha; if(tok) break; await sleep(500); }
  if(!tok) return JSON.stringify({ok:false,err:'no token after 60s'});
  const prev=sc.businessList;
  sc.$apply(()=>sc.search(PAGE));
  for(let i=0;i<240;i++){
    await sleep(500);
    if(sc.businessList!==prev && sc.page===PAGE && !sc.BusinessListProgressBar)
      return JSON.stringify({ok:true,page:sc.page,total:sc.totalCount,pages:sc.pagesCount,
                             rows:__waProject(sc.businessList)});
    const dlg=document.querySelector('.ngdialog-content');
    if(dlg) return JSON.stringify({ok:false,err:'dialog: '+dlg.innerText.slice(0,200)});
  }
  return JSON.stringify({ok:false,err:'timeout waiting for page '+PAGE});
})()'''

NO_ANSWER = '{"ok":false,"err":"no eval result"}'


class Portal:
    """The app in one throw-away browser context, a fresh tab per search term."""

    def __init__(self):
        self.devtools = DevTools()
        self.context = self.devtools.context()
        self.tab: Tab | None = None

    def open_tab(self) -> None:
        self.close_tab()
        self.tab = self.context.open_tab(SEARCH_URL)
        for _ in range(60):
            time.sleep(0.5)
            if self.tab.evaluate(READY_JS, timeout=5):
                return
        raise RuntimeError("advanced-search form never appeared")

    def close_tab(self) -> None:
        if self.tab is not None:
            self.tab.close()
            self.tab = None

    def search(self, term: str) -> dict:
        """Open a fresh tab, search `term`; the first page's answer."""
        self.open_tab()
        answer = self.tab.evaluate(SEARCH_JS.replace("TERM", json.dumps(term)), timeout=200, await_promise=True)
        return json.loads(answer or NO_ANSWER)

    def page(self, number: int) -> dict:
        """Page `number` (0-based) of the open results view."""
        answer = self.tab.evaluate(PAGE_JS.replace("PAGE", str(number)), timeout=200, await_promise=True)
        return json.loads(answer or NO_ANSWER)

    def close(self) -> None:
        try:
            self.context.dispose()
        finally:
            self.devtools.close()


# ---- row shaping ---------------------------------------------------------------

def iso_date(value) -> str:
    day = (value or "")[:10]
    return "" if not day or day.startswith("0001-") else day


def to_record(raw: dict, retrieved_at: str = "") -> Association | None:
    """A slim list row (see PROJECT_JS) as a corporate-roster record, or
    None when the name does not read as an association."""
    name = " ".join((raw.get("BusinessName") or "").split())
    if not name or not REGISTRY_NAME_RE.search(name):
        return None
    address = raw.get("PrincipalStreetAddress") or raw.get("PrincipalMailingAddress") or {}
    status = raw.get("BusinessStatus") or ""
    street = " ".join(part for part in ((address.get("StreetAddress1") or "").strip(),
                                        (address.get("StreetAddress2") or "").strip()) if part)
    return Association(
        state=STATE, source=SOURCE, source_url=SOURCE_PAGE, record_id=re.sub(r"\D", "", raw.get("UBINumber") or ""),
        name=name, corp_status=status, status=status, address=street,
        city=(address.get("City") or "").strip().title(), zip=(address.get("Zip5") or "")[:5],
        incorporated=iso_date(raw.get("DateOfIncorporation")),
        registered_agent=" ".join((raw.get("AgentName") or "").split()), entity_type=raw.get("BusinessType") or "",
        retrieved_at=retrieved_at, extras={"business_id": str(raw.get("BusinessID") or "")})


# ---- checkpoint ----------------------------------------------------------------

def load_progress() -> dict:
    return read_json(PROGRESS_PATH, {})


def save_progress(progress: dict) -> None:
    write_json(PROGRESS_PATH, progress)


def append_rows(term: str, page: int, rows: list[dict]) -> None:
    with ROWS_PATH.open("a", encoding="utf-8") as fh:
        for row in rows:
            append_row(fh, dict(row, _term=term, _page=page))


# ---- sweep ---------------------------------------------------------------------

def sweep(terms: list[str], pace: float, limit: int | None) -> int:
    """Walk every term's list pages; returns the pages fetched this run."""
    progress = load_progress()
    fetched = 0
    portal = Portal()

    def open_results(term: str) -> dict:
        answer = portal.search(term)
        if not answer.get("ok"):
            raise RuntimeError(f"{term}: search failed: {answer.get('err')}")
        return answer

    try:
        for term in terms:
            state = progress.setdefault(term, {"pages_done": 0, "total": None, "done": False})
            if state["done"]:
                log.info("%-22s done (%s rows)", term, state["total"])
                continue
            if limit is not None and fetched >= limit:
                break
            refusals = 0
            answer = None
            while answer is None:
                try:
                    answer = open_results(term)
                except RuntimeError as exc:
                    if VERIFY_MESSAGE not in str(exc) or refusals >= len(BACKOFFS):
                        raise
                    wait = BACKOFFS[refusals]
                    refusals += 1
                    log.warning("%s: verification refused on search; backing off %ds (%d/%d)",
                                term, wait, refusals, len(BACKOFFS))
                    portal.close_tab()
                    time.sleep(wait)
            opened = time.time()
            total = int(answer.get("total") or 0)
            pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            state["total"] = total
            log.info("%-22s %d rows / %d pages (resume at page %d)", term, total, pages, state["pages_done"] + 1)
            if state["pages_done"] == 0 and answer["rows"]:
                append_rows(term, 1, answer["rows"])
                state["pages_done"] = 1
                fetched += 1
                save_progress(progress)
            while state["pages_done"] < pages:
                if limit is not None and fetched >= limit:
                    break
                number = state["pages_done"]            # 0-based index of the next page
                if time.time() - opened > TOKEN_REFRESH_SECONDS:
                    # A fresh tab: the app's widget mints a new token before the
                    # old one dies server-side (the app reuses one token across
                    # pages and only drops it after 5 minutes).
                    open_results(term)
                    opened = time.time()
                    time.sleep(pace)
                time.sleep(pace)
                answer = portal.page(number)
                if not answer.get("ok") or answer.get("page") != number:
                    error = str(answer.get("err"))
                    if VERIFY_MESSAGE in error and refusals < len(BACKOFFS):
                        wait = BACKOFFS[refusals]
                        refusals += 1
                        log.warning("%s page %d: verification refused; backing off %ds (%d/%d)",
                                    term, number + 1, wait, refusals, len(BACKOFFS))
                        portal.close_tab()
                        time.sleep(wait)
                    elif refusals >= len(BACKOFFS):
                        raise RuntimeError(f"{term}: page {number + 1} failed repeatedly ({error})")
                    else:
                        refusals += 1
                        log.warning("%s page %d failed (%s); retry %d", term, number + 1, error[:120], refusals)
                        portal.close_tab()
                        time.sleep(5 * refusals)
                    open_results(term)
                    opened = time.time()
                    continue
                rows = answer["rows"]
                append_rows(term, number + 1, rows)
                state["pages_done"] = number + 1
                fetched += 1
                save_progress(progress)
                if refusals and (number + 1) % 5 == 0:
                    refusals = max(0, refusals - 1)     # earn back headroom while it works
                if (number + 1) % 20 == 0 or not rows:
                    log.info("%-22s page %d/%d (%d rows)", term, number + 1, pages, len(rows))
                if not rows:
                    break
            if state["pages_done"] >= pages:
                state["done"] = True
                save_progress(progress)
                log.info("%-22s complete", term)
    finally:
        portal.close()
    return fetched


def finalize() -> int:
    """Shape the checkpointed rows into the corporate roster and record the coverage."""
    retrieved_at = now_iso()
    seen: set[str] = set()
    records: list[Association] = []
    dropped = 0
    for raw in read_rows(ROWS_PATH, skip_bad=True):
        key = str(raw.get("BusinessID") or raw.get("UBINumber") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        record = to_record(raw, retrieved_at)
        if record is None:
            dropped += 1
            continue
        records.append(record)
    total = replace_rows(CORPORATIONS_PATH, (record.as_row() for record in records), {STATE})
    log.info("%s: %d WA rows (%d unique entities, %d non-association names dropped); %d rows total",
             CORPORATIONS_PATH.name, len(records), len(records) + dropped, dropped, total)
    statuses: dict[str, int] = {}
    for record in records:
        statuses[record.status] = statuses.get(record.status, 0) + 1
    update_coverage(STATE, "corporate", {
        "source": SOURCE, "records": len(records), "url": SOURCE_PAGE, "coverage": "all",
        "note": "advanced-search list rows (name contains HOMEOWNERS/CONDO/…); every status incl. "
                "Administratively Dissolved/Inactive; principal-office address + registered-agent name; "
                "governors need per-entity detail calls (not collected). Turnstile-gated: swept through "
                "a local browser (wa-ccfs), not the states command",
        "statuses": dict(sorted(statuses.items(), key=lambda item: -item[1])),
        "collected_at": retrieved_at[:10],
    })
    coverage = read_json(CORP_COVERAGE_PATH, {})
    coverage[STATE] = "all"
    write_json(CORP_COVERAGE_PATH, coverage)
    return len(records)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, help="stop after N list pages (smoke test)")
    parser.add_argument("--pace", type=float, default=1.5, help="seconds between list pages (default 1.5)")
    parser.add_argument("--terms", nargs="+", help="search terms (default: the association vocabulary)")
    parser.add_argument("--fresh", action="store_true", help="discard the checkpoint and start over")
    parser.add_argument("--finalize-only", action="store_true",
                        help="skip the sweep; rebuild the output from the checkpoint")


def run(args: argparse.Namespace) -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for path in (ROWS_PATH, PROGRESS_PATH):
            path.unlink(missing_ok=True)
    terms = args.terms or TERMS
    if not args.finalize_only:
        started = time.time()
        pages = sweep(terms, max(args.pace, MIN_PACE), args.limit)
        log.info("fetched %d list pages in %.0fs", pages, time.time() - started)
    progress = load_progress()
    if args.limit is not None or any(not progress.get(term, {}).get("done") for term in terms):
        log.info("sweep incomplete — outputs not written (rerun without --limit to resume; "
                 "--finalize-only writes what the checkpoint holds)")
        if not args.finalize_only:
            return 0
    finalize()
    return 0
