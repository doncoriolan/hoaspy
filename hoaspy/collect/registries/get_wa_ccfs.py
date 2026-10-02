"""Washington Secretary of State — Corporations & Charities Filing System
(CCFS) advanced search, association-named entities.

    ./venv/bin/python -m hoaspy.collect.registries.get_wa_ccfs                # full sweep (resumable)
    ./venv/bin/python -m hoaspy.collect.registries.get_wa_ccfs --limit 3      # smoke test: 3 list pages
    ./venv/bin/python -m hoaspy.collect.registries.get_wa_ccfs --fresh        # ignore the checkpoint

Source: https://ccfs.sos.wa.gov (AngularJS SPA over ccfs-api.prod.sos.wa.gov).
Every /api/BusinessSearch/* call needs a Cloudflare Turnstile token that the
SPA's own <cf-turnstile> widget mints non-interactively (managed mode, ~4 s,
no click) and stores in $rootScope.reCaptcha. Tokens outside a browser would
be captcha circumvention, so — like hoaspy/collect/courts/court_portals/fl_broward.py — this
collector drives the real local BrowserOS Chromium over raw CDP
(127.0.0.1:9100) in a throw-away browser context: it types the search term
into the SPA's advanced-search form, clicks its Search button, and pages the
results by calling the results controller's own `search(page)` scope method.
Nothing is minted, replayed, clicked-through or solved from Python.
Recon: scratchpad wa_ccfs/WA_CCFS.md (2026-09-02).

Only the LIST rows are used (25 per page, ~23 KB each on the wire but only
a dozen keys are read off the Angular scope). Per-entity detail calls
(governors, mailing addresses) are a follow-up: ~15k more Turnstile-gated
requests. List rows carry name, UBI, business type, status, formation date,
registered-agent name and the principal-office street address, which is
everything the get_states.py csv_corp row shape needs. Phone numbers, e-mail
addresses and EINs present in the API payload are never read.

Output: WA rows in records/state_corps.jsonl (replacing earlier WA rows),
coverage.json["states"]["WA"]["collected"]["corporate"], and
records/corp_coverage.json["WA"] = "all" (rows carry a status column, so the
site may flag dissolved/inactive corporations). get_states.py is NOT
involved: WA has no state_sources.yml entry, and get_states.py rewrites
corp_coverage.json from that file, so rerun this script after a full
get_states.py run to restore the WA key.

Checkpoint: .cache/wa/ccfs_rows.jsonl (raw slim rows) + ccfs_progress.json
(pages done per term); a rerun resumes at the next page.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import websocket

from hoaspy.collect.registries.get_states import ASSOC_RE, CORPS, OUT_DIR, update_coverage, write_merged

from hoaspy import ROOT
CACHE = ROOT / ".cache" / "wa"
ROWS = CACHE / "ccfs_rows.jsonl"
PROGRESS = CACHE / "ccfs_progress.json"
CORP_COVERAGE = OUT_DIR / "corp_coverage.json"

STATE = "WA"
SOURCE = "Washington Secretary of State — Corporations & Charities Filing System (CCFS)"
SOURCE_URL = "https://ccfs.sos.wa.gov/"
SEARCH_URL = "https://ccfs.sos.wa.gov/#/AdvancedSearch"
CDP_HTTP = "http://127.0.0.1:9100"
PAGE_SIZE = 25

# Name-contains terms; the server search is LIKE %term%, so "CONDO" also
# hits CONDOR/CONDON and "HOA" hits SHOAL — ASSOC_RE (word-bounded) gates
# every row locally, and BusinessID dedupes across overlapping terms.
TERMS = ["HOMEOWNERS", "HOME OWNERS", "CONDOMINIUM", "CONDO", "PROPERTY OWNERS",
         "OWNERS ASSOCIATION", "COMMUNITY ASSOCIATION", "MASTER ASSOCIATION",
         "TOWNHOME", "TOWNHOUSE", "HOA"]

log = logging.getLogger("wa_ccfs")

# ---- page-context JavaScript (runs inside the SPA; awaited via CDP) --------
# Only these keys leave the page: no PhoneNumber/EmailAddress/FEINNo.
_JS_PROJECT = r'''
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

_JS_READY = r'''!!(window.angular && document.getElementById('btnSearch') && document.getElementById('txtOrgname'))'''

# Fill the term and press the SPA's Search button; the results view's own
# Turnstile widget mints a token, its controller then posts page 1 itself.
_JS_SEARCH = _JS_PROJECT + r'''
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const el=document.getElementById('txtOrgname'); if(!el) return JSON.stringify({ok:false,err:'no form'});
  el.value=TERM; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));
  await sleep(300);
  // The advanced-search view has no widget of its own: Search routes to the
  // results view, whose <cf-turnstile> mints the token that triggers page 1.
  document.getElementById('btnSearch').click();
  for(let i=0;i<240;i++){
    await sleep(500);
    const back=document.getElementById('btnReturnToSearch');
    if(back){ const sc=angular.element(back).scope();
      if(sc && sc.businessList && typeof sc.search==='function' && sc.businesssearchType=='AdvancedSearch' && !sc.BusinessListProgressBar)
        return JSON.stringify({ok:true,page:sc.page,total:sc.totalCount,pages:sc.pagesCount,rows:__waProject(sc.businessList)});
    }
    const dlg=document.querySelector('.ngdialog-content');
    if(dlg) return JSON.stringify({ok:false,err:'dialog: '+dlg.innerText.slice(0,200)});
  }
  return JSON.stringify({ok:false,err:'timeout waiting for results'});
})()'''

# Page N (0-based) through the results controller's own search(page).
_JS_PAGE = _JS_PROJECT + r'''
(async()=>{
  const sleep=ms=>new Promise(r=>setTimeout(r,ms));
  const back=document.getElementById('btnReturnToSearch'); if(!back) return JSON.stringify({ok:false,err:'not on results view'});
  const sc=angular.element(back).scope(); const root=sc.$root;
  let tok=null;
  for(let i=0;i<120;i++){ tok=root.reCaptcha; if(tok) break; await sleep(500); }
  if(!tok) return JSON.stringify({ok:false,err:'no token after 60s'});
  const prev=sc.businessList;
  sc.$apply(()=>sc.search(PAGE));
  for(let i=0;i<240;i++){
    await sleep(500);
    if(sc.businessList!==prev && sc.page===PAGE && !sc.BusinessListProgressBar)
      return JSON.stringify({ok:true,page:sc.page,total:sc.totalCount,pages:sc.pagesCount,rows:__waProject(sc.businessList)});
    const dlg=document.querySelector('.ngdialog-content');
    if(dlg) return JSON.stringify({ok:false,err:'dialog: '+dlg.innerText.slice(0,200)});
  }
  return JSON.stringify({ok:false,err:'timeout waiting for page '+PAGE});
})()'''


# ---- CDP plumbing (same shape as hoaspy/collect/courts/court_portals/fl_broward.py) --------------
class CDP:
    def __init__(self):
        try:
            ver = json.load(urllib.request.urlopen(CDP_HTTP + "/json/version", timeout=5))
        except Exception as exc:
            raise PermissionError(f"BrowserOS CDP not reachable at {CDP_HTTP} ({exc}); "
                                  "start the browser with its DevTools port there (README: Browser-driven collectors) and rerun") from exc
        self.ws = websocket.create_connection(ver["webSocketDebuggerUrl"], suppress_origin=True)
        self.n = 0

    def send(self, method, params=None, sid=None, timeout=10):
        self.n += 1
        msg = {"id": self.n, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))
        return self.wait(self.n, timeout)

    def wait(self, id_, timeout):
        self.ws.settimeout(timeout)
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv())
            except Exception:
                break
            if m.get("id") == id_:
                return m
        return None

    def eval(self, sid, js, timeout=30, await_promise=False):
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True,
                                           "awaitPromise": await_promise}, sid=sid, timeout=timeout)
        res = (r or {}).get("result", {})
        if "exceptionDetails" in res:
            raise RuntimeError("page JS threw: " + json.dumps(res["exceptionDetails"])[:300])
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


class Browser:
    """One throw-away browser context; a fresh tab per search term."""

    def __init__(self):
        self.cdp = CDP()
        self.ctx = self.cdp.send("Target.createBrowserContext")["result"]["browserContextId"]
        self.tid = None
        self.sid = None

    def open_tab(self):
        self.close_tab()
        self.tid = self.cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": self.ctx,
                                                          "background": True})["result"]["targetId"]
        self.sid = self.cdp.send("Target.attachToTarget", {"targetId": self.tid, "flatten": True})["result"]["sessionId"]
        self.cdp.send("Page.enable", sid=self.sid)
        self.cdp.send("Page.navigate", {"url": SEARCH_URL}, sid=self.sid)
        for _ in range(60):
            time.sleep(0.5)
            if self.cdp.eval(self.sid, _JS_READY, timeout=5):
                return
        raise RuntimeError("advanced-search form never appeared")

    def close_tab(self):
        if self.tid:
            self.cdp.send("Target.closeTarget", {"targetId": self.tid})
            self.tid = self.sid = None

    def search(self, term: str) -> dict:
        self.open_tab()
        js = _JS_SEARCH.replace("TERM", json.dumps(term))
        out = self.cdp.eval(self.sid, js, timeout=200, await_promise=True)
        return json.loads(out or '{"ok":false,"err":"no eval result"}')

    def page(self, n: int) -> dict:
        js = _JS_PAGE.replace("PAGE", str(n))
        out = self.cdp.eval(self.sid, js, timeout=200, await_promise=True)
        return json.loads(out or '{"ok":false,"err":"no eval result"}')

    def close(self):
        try:
            self.close_tab()
            self.cdp.send("Target.disposeBrowserContext", {"browserContextId": self.ctx})
        finally:
            self.cdp.close()


# ---- row shaping ----------------------------------------------------------
def _iso(v) -> str:
    v = (v or "")[:10]
    return "" if not v or v.startswith("0001-") else v


def to_row(raw: dict) -> dict | None:
    """Slim list row (see _JS_PROJECT) -> get_states.py csv_corp row, or None
    when the name is not association-like."""
    name = " ".join((raw.get("BusinessName") or "").split())
    if not name or not ASSOC_RE.search(name):
        return None
    addr = raw.get("PrincipalStreetAddress") or raw.get("PrincipalMailingAddress") or {}
    status = raw.get("BusinessStatus") or ""
    street = " ".join(x for x in ((addr.get("StreetAddress1") or "").strip(),
                                  (addr.get("StreetAddress2") or "").strip()) if x)
    return {
        "state": STATE,
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "record_id": re.sub(r"\D", "", raw.get("UBINumber") or ""),
        "business_id": str(raw.get("BusinessID") or ""),
        "name": name,
        "corp_status": status,
        "status": status,
        "address": street,
        "city": (addr.get("City") or "").strip().title(),
        "county": "",
        "zip": (addr.get("Zip5") or "")[:5],
        "incorporated": _iso(raw.get("DateOfIncorporation")),
        "registered_agent": " ".join((raw.get("AgentName") or "").split()),
        "entity_type": raw.get("BusinessType") or "",
        "units": None,
        "manager_name": "",
        "officers": [],
    }


# ---- checkpoint -----------------------------------------------------------
def load_progress() -> dict:
    return json.loads(PROGRESS.read_text()) if PROGRESS.exists() else {}


def save_progress(p: dict) -> None:
    tmp = PROGRESS.with_suffix(".tmp")
    tmp.write_text(json.dumps(p, indent=1))
    tmp.replace(PROGRESS)


def load_rows() -> list[dict]:
    if not ROWS.exists():
        return []
    with ROWS.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def append_rows(term: str, page: int, rows: list[dict]) -> None:
    with ROWS.open("a") as fh:
        for r in rows:
            r = dict(r, _term=term, _page=page)
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


# ---- sweep ----------------------------------------------------------------
TOKEN_REFRESH_S = 240          # Turnstile tokens die server-side at 300 s
VERIFY_MSG = "System verification in progress"
BACKOFFS = (60, 120, 300, 600, 900, 1200, 1800)   # after a verification refusal


def sweep(terms: list[str], pace: float, limit: int | None) -> int:
    """Walk every term's list pages; returns pages fetched this run."""
    progress = load_progress()
    fetched = 0
    browser = Browser()

    def open_results(term: str) -> dict:
        res = browser.search(term)
        if not res.get("ok"):
            raise RuntimeError(f"{term}: search failed: {res.get('err')}")
        return res

    try:
        for term in terms:
            st = progress.setdefault(term, {"pages_done": 0, "total": None, "done": False})
            if st["done"]:
                log.info("%-22s done (%s rows)", term, st["total"])
                continue
            if limit is not None and fetched >= limit:
                break
            refusals = 0
            res = None
            while res is None:
                try:
                    res = open_results(term)
                except RuntimeError as exc:
                    if VERIFY_MSG not in str(exc) or refusals >= len(BACKOFFS):
                        raise
                    wait = BACKOFFS[refusals]
                    refusals += 1
                    log.warning("%s: verification refused on search; backing off %ds (%d/%d)",
                                term, wait, refusals, len(BACKOFFS))
                    browser.close_tab()
                    time.sleep(wait)
            opened = time.time()
            total = int(res.get("total") or 0)
            pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
            st["total"] = total
            log.info("%-22s %d rows / %d pages (resume at page %d)", term, total, pages, st["pages_done"] + 1)
            if st["pages_done"] == 0 and res["rows"]:
                append_rows(term, 1, res["rows"])
                st["pages_done"] = 1
                fetched += 1
                save_progress(progress)
            while st["pages_done"] < pages:
                if limit is not None and fetched >= limit:
                    break
                n = st["pages_done"]            # 0-based index of the next page
                if time.time() - opened > TOKEN_REFRESH_S:
                    # fresh tab -> the SPA's widget mints a fresh token before
                    # the old one dies server-side (the SPA reuses one token
                    # across pages and only drops it after 5 min)
                    open_results(term)
                    opened = time.time()
                    time.sleep(pace)
                time.sleep(pace)
                res = browser.page(n)
                if not res.get("ok") or res.get("page") != n:
                    err = str(res.get("err"))
                    if VERIFY_MSG in err and refusals < len(BACKOFFS):
                        wait = BACKOFFS[refusals]
                        refusals += 1
                        log.warning("%s page %d: verification refused; backing off %ds (%d/%d)",
                                    term, n + 1, wait, refusals, len(BACKOFFS))
                        browser.close_tab()
                        time.sleep(wait)
                    elif refusals >= len(BACKOFFS):
                        raise RuntimeError(f"{term}: page {n + 1} failed repeatedly ({err})")
                    else:
                        refusals += 1
                        log.warning("%s page %d failed (%s); retry %d", term, n + 1, err[:120], refusals)
                        browser.close_tab()
                        time.sleep(5 * refusals)
                    open_results(term)
                    opened = time.time()
                    continue
                rows = res["rows"]
                append_rows(term, n + 1, rows)
                st["pages_done"] = n + 1
                fetched += 1
                save_progress(progress)
                if refusals and (n + 1) % 5 == 0:
                    refusals = max(0, refusals - 1)     # earn back headroom while it works
                if (n + 1) % 20 == 0 or not rows:
                    log.info("%-22s page %d/%d (%d rows)", term, n + 1, pages, len(rows))
                if not rows:
                    break
            if st["pages_done"] >= pages:
                st["done"] = True
                save_progress(progress)
                log.info("%-22s complete", term)
    finally:
        browser.close()
    return fetched


def finalize() -> int:
    seen: set[str] = set()
    rows: list[dict] = []
    kept = dropped = 0
    for raw in load_rows():
        key = str(raw.get("BusinessID") or raw.get("UBINumber") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        row = to_row(raw)
        if row is None:
            dropped += 1
            continue
        kept += 1
        rows.append(row)
    total = write_merged(CORPS, rows, {STATE})
    log.info("state_corps.jsonl: %d WA rows (%d unique entities, %d non-association names dropped); %d rows total",
             len(rows), kept + dropped, dropped, total)
    statuses = {}
    for r in rows:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    update_coverage(STATE, "corporate", {
        "source": SOURCE,
        "records": len(rows),
        "url": SOURCE_URL,
        "coverage": "all",
        "note": "advanced-search list rows (name contains HOMEOWNERS/CONDO/…); every status incl. "
                "Administratively Dissolved/Inactive; principal-office address + registered-agent name; "
                "governors need per-entity detail calls (not collected). Turnstile-gated: swept through "
                "BrowserOS (get_wa_ccfs.py), not get_states.py",
        "statuses": dict(sorted(statuses.items(), key=lambda kv: -kv[1])),
        "collected_at": datetime.now(timezone.utc).date().isoformat(),
    })
    cov = json.loads(CORP_COVERAGE.read_text()) if CORP_COVERAGE.exists() else {}
    cov[STATE] = "all"
    CORP_COVERAGE.write_text(json.dumps(cov, indent=2))
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--limit", type=int, help="stop after N list pages (smoke test)")
    ap.add_argument("--pace", type=float, default=1.5, help="seconds between list pages (default 1.5)")
    ap.add_argument("--terms", nargs="+", help="search terms (default: the association vocabulary)")
    ap.add_argument("--fresh", action="store_true", help="discard the checkpoint and start over")
    ap.add_argument("--finalize-only", action="store_true", help="skip the sweep; rebuild outputs from the checkpoint")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    CACHE.mkdir(parents=True, exist_ok=True)
    if args.fresh:
        for p in (ROWS, PROGRESS):
            if p.exists():
                p.unlink()
    if not args.finalize_only:
        started = time.time()
        n = sweep(args.terms or TERMS, max(args.pace, 0.6), args.limit)
        log.info("fetched %d list pages in %.0fs", n, time.time() - started)
    progress = load_progress()
    if args.limit is not None or any(not progress.get(t, {}).get("done") for t in (args.terms or TERMS)):
        log.info("sweep incomplete — outputs not written (rerun without --limit to resume; "
                 "--finalize-only writes what the checkpoint holds)")
        if not args.finalize_only:
            return 0
    finalize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
