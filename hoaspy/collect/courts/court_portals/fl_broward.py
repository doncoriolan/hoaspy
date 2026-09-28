"""Broward County (FL) Clerk of Courts — eCaseView business-name search,
driven through the local BrowserOS instance.

    https://www.browardclerk.org/Web2/CaseSearchECA/

17th Judicial Circuit, all divisions; we search the Civil division by
business name. The search POST carries a Cloudflare Turnstile token that the
server validates and that only a real browser can produce (it auto-passes,
no click), and result/detail URLs are session-bound blobs — so this adapter
runs each search inside a throw-away BrowserOS browser context over CDP
(127.0.0.1:9100) and reads the results array the page embeds for its grid.
~8–12 s per name. Hard cap 200 rows per search (newest first, no paging).
Recon 2026-09-02 (courts_recon/FL_broward_ecaseview.md).

Best-effort by construction: the portal matches on leading words, so we
query the distinctive core of the DBPR name ("STIRLING VILLAS" for
"STIRLING VILLAS TOWNHOUSE CONDOMINIUM") and keep only captions whose party
text carries that core. Foreclosures naming the association as a
co-defendant are kept (role recorded). No stable public deep link: `url` is
the search page and `docket_number` (CACE…) re-enters the case there.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

import websocket

from ._common import normalize, record, clean

STATE = "FL"
KEY = "fl_broward"
NEEDS_COOKIE = False
COUNTIES = {"BROWARD"}
CDP_HTTP = "http://127.0.0.1:9100"
SEARCH_URL = "https://www.browardclerk.org/Web2/CaseSearchECA/"
INFO = {
    "name": "Broward County Clerk of Courts — eCaseView (business-name search, Civil)",
    "url": SEARCH_URL,
    "access": "anonymous; Cloudflare Turnstile passed by the local BrowserOS browser; "
              "per-association business-name search",
    "coverage": "Broward County / 17th Circuit civil division",
    "caveat": "best-effort — leading-word name match on the DBPR core name; "
              "200-row cap per search; no per-case public deep link",
}
_STOPPERS = re.compile(
    r"\b(?:CONDOMINIUM|CONDO|CONDOMINIUMS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|"
    r"OWNERS|ASSOCIATION|ASSOC|ASSN|HOA|POA|COA|COMMUNITY ASSOCIATION|MASTER ASSOCIATION|"
    r"TOWNHOMES?|TOWNHOUSES?|VILLAS? ASSOCIATION|A CONDO)\b.*$", re.I)
_SUFFIX = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
_JS_SUBMIT = r'''(async()=>{
  const f=document.getElementById('businessSearchForm'); if(!f) return 'NOFORM';
  document.getElementById('BusiName').value=NAME;
  const sel=document.getElementById('CaseCategoryKeys2');
  if(![...sel.options].some(o=>o.value==='CV')) sel.add(new Option('Civil','CV'));
  sel.value='CV';
  let tok='';
  for(let i=0;i<60;i++){tok=f.querySelector('input[name="cf-turnstile-response"]').value; if(tok.length>50) break; await new Promise(r=>setTimeout(r,500));}
  if(tok.length<50) return 'NOTOKEN';
  f.querySelector('button[type=submit]').click(); return 'SUBMITTED';
})()'''
_JS_STATE = r'''location.href+' | '+(document.body.innerText.match(/[0-9]+ - [0-9]+ of [0-9]+ items|No items to display|could not be completed[^.]*|Oops![^.]*\.|HTTP ERROR [0-9]+/)||['?'])[0]'''
_JS_ARRAY = r'''(document.documentElement.outerHTML.match(/var array = "(.*?)";\s*\n/)||[])[1]||''' + "''"


def query_name(name: str) -> str:
    """Distinctive leading core of a DBPR/registry name for a leading-word search."""
    n = _SUFFIX.sub("", clean(name)).strip(" ,.")
    core = _STOPPERS.sub("", n).strip(" ,.-&")
    core = re.sub(r"^(THE|A)\s+", "", core, flags=re.I).strip()
    if len(core.split()) < 2 or len(core) < 6:
        core = n
    return core


class _CDP:
    def __init__(self):
        try:
            ver = json.load(urllib.request.urlopen(CDP_HTTP + "/json/version", timeout=5))
        except Exception as exc:
            raise PermissionError(f"BrowserOS CDP not reachable at {CDP_HTTP} ({exc}); "
                                  "start BrowserOS (see docs/NEEDS.md 3c) and rerun") from exc
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
        return (r or {}).get("result", {}).get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _decode_array(raw: str):
    s = json.loads('"' + raw + '"')
    while isinstance(s, str):
        s = json.loads(s)
    return s


def raw_search(name: str, court: str = "CV", timeout: int = 45) -> list[dict]:
    cdp = _CDP()
    ctx = cdp.send("Target.createBrowserContext")["result"]["browserContextId"]
    tid = cdp.send("Target.createTarget", {"url": "about:blank", "browserContextId": ctx,
                                           "background": True})["result"]["targetId"]
    try:
        sid = cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["result"]["sessionId"]
        cdp.send("Page.enable", sid=sid)
        cdp.send("Page.navigate", {"url": SEARCH_URL}, sid=sid)
        cdp.wait(-1, 5)
        st = cdp.eval(sid, _JS_SUBMIT.replace("NAME", json.dumps(name)).replace("'CV'", json.dumps(court)),
                      timeout=timeout, await_promise=True)
        if st != "SUBMITTED":
            raise RuntimeError(f"submit failed: {st}")
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            time.sleep(2)
            state = cdp.eval(sid, _JS_STATE, timeout=8) or ""
            if any(k in state for k in ("/Results", "items", "No items", "Oops",
                                        "could not be completed", "HTTP ERROR")):
                break
        if "No items" in state:
            return []
        raw = cdp.eval(sid, _JS_ARRAY, timeout=8) or ""
        if not raw:
            raise RuntimeError(f"no results array; page state: {state}")
        return _decode_array(raw)
    finally:
        cdp.send("Target.closeTarget", {"targetId": tid})
        cdp.send("Target.disposeBrowserContext", {"browserContextId": ctx})
        cdp.close()


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace

    def search(self, name: str) -> list[dict]:
        core = query_name(name)
        ncore = normalize(core)
        last = None
        for attempt in range(3):
            try:
                rows = raw_search(core)
                break
            except PermissionError:
                raise
            except Exception as exc:               # backend hiccups / Oops pages
                last = exc
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"broward search failed after retries: {last}")
        out = []
        for h in rows:
            style = clean(h.get("Style") or "")
            sides = [re.sub(r"\s+(Plaintiff|Defendant|Petitioner|Respondent)s?$", "", s.strip(), flags=re.I)
                     for s in re.split(r"\s+vs?\.?\s+", style, maxsplit=1, flags=re.I)]
            style = " v. ".join(sides)
            matched = [s for s in sides if ncore and ncore in normalize(s)]
            if not matched:
                # The party index matches on leading words ("HARBOR VILLA" also
                # hits "Harbor Village"), and the caption shows lead parties
                # only — a row whose caption doesn't carry our core name can't
                # be verified, so it is dropped (co-defendant foreclosures are
                # the main loss).
                continue
            role = "plaintiff" if ncore in normalize(sides[0]) else "defendant"
            assoc = [re.sub(r",?\s*et al\.?$", "", m, flags=re.I) for m in matched]
            rec = record(
                key=KEY, state=STATE, case_name=style,
                court=" ".join(x for x in ("Broward County", h.get("CourtType") or "",
                                            h.get("CourtLocation") or "") if x),
                docket_number=h.get("CaseNumber") or "", date_filed=(h.get("CaseFiledDate") or "")[:10],
                date_terminated=(h.get("CaseStatusDate") or "")[:10] if (h.get("CaseStatusDesc") or "").lower().startswith("closed") else "",
                nature_of_suit=h.get("CaseUTypeDesc") or "", status=h.get("CaseStatusDesc") or "",
                associations=assoc, url=SEARCH_URL,
                case_id=h.get("UCN") or h.get("CaseNumber") or "", queried=name)
            rec["association_role"] = [role]
            rec["ucn"] = h.get("UCN") or ""
            out.append(rec)
        return out
