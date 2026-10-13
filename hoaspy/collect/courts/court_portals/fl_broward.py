"""Broward County, FL Clerk of Courts — eCaseView business-name search,
through a local browser.

    https://www.browardclerk.org/Web2/CaseSearchECA/

17th Judicial Circuit, all divisions; the Civil division is searched by
business name. The search POST carries a Cloudflare Turnstile token that
the server validates and that only a real browser can produce (it passes
on its own, no click), and result and detail URLs are session-bound blobs,
so each search runs inside a throw-away browser context over the DevTools
port (hoaspy/lib/browser.py) and reads the results array the page embeds
for its grid. About 8-12 s per name; 200 rows at most per search (newest
first, no paging).

Best-effort by construction: the portal matches on leading words, so the
query is the distinctive core of the registered name ("STIRLING VILLAS"
for "STIRLING VILLAS TOWNHOUSE CONDOMINIUM") and only captions whose party
text carries that core are kept. Foreclosures naming the association as a
co-defendant are kept, with the role recorded. No stable public deep link:
`url` is the search page and `docket_number` (CACE…) re-enters the case.
"""

from __future__ import annotations

import json
import re
import time

from hoaspy.lib.browser import DevTools

from ._common import clean, normalize, party_is_association, record

STATE = "FL"
KEY = "fl_broward"
NEEDS_COOKIE = False
COUNTIES = {"BROWARD"}
SEARCH_URL = "https://www.browardclerk.org/Web2/CaseSearchECA/"
INFO = {
    "name": "Broward County Clerk of Courts — eCaseView (business-name search, Civil)",
    "url": SEARCH_URL,
    "access": "anonymous; Cloudflare Turnstile passed by a local browser; per-association business-name search",
    "coverage": "Broward County / 17th Circuit civil division",
    "caveat": "best-effort — leading-word name match on the DBPR core name; "
              "200-row cap per search; no per-case public deep link",
}
CIVIL_DIVISION = "CV"
STOPPER_RE = re.compile(
    r"\b(?:CONDOMINIUM|CONDO|CONDOMINIUMS|HOMEOWNERS?|HOME OWNERS?|PROPERTY OWNERS?|"
    r"OWNERS|ASSOCIATION|ASSOC|ASSN|HOA|POA|COA|COMMUNITY ASSOCIATION|MASTER ASSOCIATION|"
    r"TOWNHOMES?|TOWNHOUSES?|VILLAS? ASSOCIATION|A CONDO)\b.*$", re.I)
ENTITY_SUFFIX_RE = re.compile(r"[\s,.]*(?:\b(?:INC|INCORPORATED|LLC|CORP|CORPORATION|LTD)\b[.]?[\s,.]*)+$", re.I)
ROLE_SUFFIX_RE = re.compile(r"\s+(Plaintiff|Defendant|Petitioner|Respondent)s?$", re.I)
ET_AL_RE = re.compile(r",?\s*et al\.?$", re.I)
# Fill the form, wait for the Turnstile token, press Search.
SUBMIT_JS = r'''(async()=>{
  const f=document.getElementById('businessSearchForm'); if(!f) return 'NOFORM';
  document.getElementById('BusiName').value=NAME;
  const sel=document.getElementById('CaseCategoryKeys2');
  if(![...sel.options].some(o=>o.value==='CV')) sel.add(new Option('Civil','CV'));
  sel.value='CV';
  let tok='';
  for(let i=0;i<60;i++){tok=f.querySelector('input[name="cf-turnstile-response"]').value;
    if(tok.length>50) break; await new Promise(r=>setTimeout(r,500));}
  if(tok.length<50) return 'NOTOKEN';
  f.querySelector('button[type=submit]').click(); return 'SUBMITTED';
})()'''
# Where the page is and what it says: the results count, "No items", or an error.
STATE_JS = (r'''location.href+' | '+(document.body.innerText.match(/[0-9]+ - [0-9]+ of [0-9]+ items|'''
            r'''No items to display|could not be completed[^.]*|Oops![^.]*\.|HTTP ERROR [0-9]+/)||['?'])[0]''')
# The results array the page embeds for its grid.
ARRAY_JS = r'''(document.documentElement.outerHTML.match(/var array = "(.*?)";\s*\n/)||[])[1]||''' + "''"
SETTLED = ("/Results", "items", "No items", "Oops", "could not be completed", "HTTP ERROR")


def query_name(name: str) -> str:
    """The distinctive leading core of a DBPR or registry name, for a leading-word search."""
    bare = ENTITY_SUFFIX_RE.sub("", clean(name)).strip(" ,.")
    core = STOPPER_RE.sub("", bare).strip(" ,.-&")
    core = re.sub(r"^(THE|A)\s+", "", core, flags=re.I).strip()
    if len(core.split()) < 2 or len(core) < 6:
        core = bare
    return core


def decode_array(raw: str):
    """The grid's results array, JSON-encoded one or more times over."""
    value = json.loads('"' + raw + '"')
    while isinstance(value, str):
        value = json.loads(value)
    return value


def raw_search(name: str, court: str = CIVIL_DIVISION, timeout: int = 45) -> list[dict]:
    """One business-name search in a fresh browser context: the grid's rows."""
    devtools = DevTools()
    try:
        with devtools.context() as context:
            tab = context.open_tab(SEARCH_URL)
            devtools.drain(5)
            status = tab.evaluate(SUBMIT_JS.replace("NAME", json.dumps(name)).replace("'CV'", json.dumps(court)),
                                  timeout=timeout, await_promise=True)
            if status != "SUBMITTED":
                raise RuntimeError(f"submit failed: {status}")
            deadline = time.time() + timeout
            state = ""
            while time.time() < deadline:
                time.sleep(2)
                state = tab.evaluate(STATE_JS, timeout=8) or ""
                if any(marker in state for marker in SETTLED):
                    break
            if "No items" in state:
                return []
            raw = tab.evaluate(ARRAY_JS, timeout=8) or ""
            if not raw:
                raise RuntimeError(f"no results array; page state: {state}")
            return decode_array(raw)
    finally:
        devtools.close()


class Client:
    def __init__(self, cookie: str | None = None, pace: float = 1.0):
        self.pace = pace

    def search(self, name: str) -> list[dict]:
        core = query_name(name)
        core_key = normalize(core)
        last = None
        for attempt in range(3):
            try:
                rows = raw_search(core)
                break
            except PermissionError:
                raise
            except Exception as exc:               # backend hiccups, "Oops" pages
                last = exc
                time.sleep(5 * (attempt + 1))
        else:
            raise RuntimeError(f"broward search failed after retries: {last}")
        out = []
        for hit in rows:
            style = clean(hit.get("Style") or "")
            sides = [ROLE_SUFFIX_RE.sub("", side.strip())
                     for side in re.split(r"\s+vs?\.?\s+", style, maxsplit=1, flags=re.I)]
            style = " v. ".join(sides)
            matched = [side for side in sides if core_key and party_is_association(side, core)]
            if not matched:
                # The party index matches on leading words ("HARBOR VILLA" also
                # hits "Harbor Village") and the caption shows lead parties only:
                # a row whose caption does not carry the core name cannot be
                # verified, so it is dropped (co-defendant foreclosures are the main loss).
                continue
            role = "plaintiff" if core_key in normalize(sides[0]) else "defendant"
            closed = (hit.get("CaseStatusDesc") or "").lower().startswith("closed")
            out.append(record(
                key=KEY, state=STATE, case_name=style,
                court=" ".join(part for part in ("Broward County", hit.get("CourtType") or "",
                                                 hit.get("CourtLocation") or "") if part),
                docket_number=hit.get("CaseNumber") or "", date_filed=(hit.get("CaseFiledDate") or "")[:10],
                date_terminated=(hit.get("CaseStatusDate") or "")[:10] if closed else "",
                nature_of_suit=hit.get("CaseUTypeDesc") or "", status=hit.get("CaseStatusDesc") or "",
                associations=[ET_AL_RE.sub("", party) for party in matched], url=SEARCH_URL,
                case_id=hit.get("UCN") or hit.get("CaseNumber") or "", queried=name,
                association_role=[role], ucn=hit.get("UCN") or "").as_row())
        return out
