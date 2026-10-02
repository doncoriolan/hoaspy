/* CA Secretary of State bizfile sweep — runs inside YOUR logged-in tab.
 *
 * Why: the bizfile API needs the site's Imperva cookies plus its hourly
 * Okta token; a script outside the browser loses both within the hour.
 * Inside the tab the site keeps them fresh itself, so this is the same
 * keyword x entity-type x filing-date-window sweep as get_ca_sos.py, run by
 * the page's own fetch(). Nothing here bypasses anything: it is the search
 * the page makes, paced slower than a person clicking.
 *
 * How:
 *   1. Open https://bizfileonline.sos.ca.gov/search/business, sign in.
 *   2. DevTools (Cmd+Opt+J) -> Console -> paste this whole file -> Enter.
 *      Chrome asks once to allow multiple downloads: allow.
 *   3. Leave the tab open. Parts land in ~/Downloads as
 *      ca_sos_<stamp>_partN.jsonl (one raw API row per line) every 2000 new
 *      rows and at the end; `window.__caSos.stop = true` stops early and
 *      flushes what it has.
 *   4. Ingest: ./venv/bin/python get_ca_sos.py --ingest ~/Downloads/ca_sos_*.jsonl
 */
(async () => {
  const CAP = 500, PACE_MS = 1200, FLUSH_EVERY = 2000, DATE_MIN = "1850-01-01";
  const CID_TYPES = ["62", "69", "72"];   // HOAs by definition (see get_ca_sos.py)
  const CID_WORDS = ["ASSOCIATION","OWNERS","HOMEOWNERS","HOA","CONDOMINIUM","CONDO",
    "COMMUNITY","MAINTENANCE","COUNCIL","CORPORATION","PROPERTY","RESIDENTS","TENANTS",
    "COOPERATIVE","MUTUAL","CLUB","VILLAGE","ESTATES","PARK","HILLS","GARDENS","TERRACE",
    "VILLAS","VILLA","TOWNHOMES","TOWNHOUSE","TOWNHOUSES","MANOR","RANCH","RANCHO","VISTA",
    "VIEW","LAKE","OAKS","PALM","PALMS","HEIGHTS","SQUARE","PLACE","COURT","STREET","AVENUE",
    "DRIVE","ROAD","LANE","WAY","COMMONS","PLAZA","CREEK","CANYON","BAY","BEACH","OCEAN","SEA",
    "HARBOR","MARINA","RIDGE","VALLEY","MESA","MOUNTAIN","SPRINGS","WOODS","GROVE","MEADOWS",
    "POINT","POINTE","COVE","SHORES","ISLAND","NORTH","SOUTH","EAST","WEST","GREEN","GLEN",
    "TRAILS","CROSSING","LANDING","COTTAGES","LOFTS","TOWERS","TOWER","APARTMENTS","UNITS",
    "BUILDING","HOMES","HOME","HOUSE","CASA","CASAS","SAN","SANTA","LOS","LAS","DEL","MISSION",
    "CAMINO","PASEO","PACIFIC","CALIFORNIA","SUNSET","SIERRA","MAR","VERDE","ALTA","MONTE",
    "LOMA","TIC","GATE","GARDEN","WOOD","HILL","0","1","2","3","4","5","6","7","8","9"];
  const NAMED_WORDS = ["HOA","HOMEOWNERS","HOMEOWNER","HOME OWNERS","CONDOMINIUM","CONDO",
    "PROPERTY OWNERS","COMMUNITY ASSOCIATION","MASTER ASSOCIATION","RESIDENTS ASSOCIATION",
    "TOWNHOMES","TOWNHOME","TOWNHOUSE","VILLAS"];
  const STOP = new Set(["THE","OF","AND","INC","A","AN","AT","IN","ON","FOR","BY","TO","DE",
    "LA","EL","NO","OR","LLC","LTD","CO","CORP"]);
  const EXPAND = 150;

  const S = (window.__caSos = window.__caSos || { rows: new Map(), done: new Set(),
    pending: [], part: 0, calls: 0, stop: false, stamp: new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "") });
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const mdy = (iso) => { const [y, m, d] = iso.split("-"); return `${m}/${d}/${y}`; };
  const addDays = (iso, n) => { const t = new Date(iso + "T00:00:00Z"); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
  const today = new Date().toISOString().slice(0, 10);

  function token() {
    try {
      const t = JSON.parse(localStorage.getItem("okta-token-storage") || "{}");
      return t.accessToken && t.accessToken.accessToken;
    } catch (e) { return null; }
  }
  function body(term, ftype, start, end) {
    return { SEARCH_VALUE: term, SEARCH_FILTER_TYPE_ID: "0", SEARCH_TYPE_ID: "1",
      FILING_TYPE_ID: ftype, STATUS_ID: "", FILING_DATE: { start: start ? mdy(start) : null, end: end ? mdy(end) : null },
      CORPORATION_BANKRUPTCY_YN: false, CORPORATION_LEGAL_PROCEEDINGS_YN: false,
      OFFICER_OBJECT: { FIRST_NAME: "", MIDDLE_NAME: "", LAST_NAME: "" },
      NUMBER_OF_FEMALE_DIRECTORS: "99", NUMBER_OF_UNDERREPRESENTED_DIRECTORS: "99",
      COMPENSATION_FROM: "", COMPENSATION_TO: "", SHARES_YN: false, OPTIONS_YN: false,
      BANKRUPTCY_YN: false, FRAUD_YN: false, LOANS_YN: false, AUDITOR_NAME: "" };
  }
  async function search(term, ftype, start, end) {
    for (let attempt = 0; ; attempt++) {
      let tok = token();
      if (!tok) tok = window.__caSosToken || (window.__caSosToken = prompt("bizfile authorization header value (DevTools > Network > businesssearch):"));
      S.calls++;
      const r = await fetch("/api/Records/businesssearch", { method: "POST",
        headers: { "content-type": "application/json", authorization: tok, accept: "*/*" },
        body: JSON.stringify(body(term, ftype, start, end)) });
      if (r.ok) { const p = await r.json(); return p.rows || {}; }
      if (r.status === 401 || r.status === 403) {
        window.__caSosToken = null;                        // force a re-read / re-prompt
        console.warn(`HTTP ${r.status} on ${term}/${ftype} — token or session refresh needed; retrying in 30s`);
        await sleep(30000); continue;
      }
      console.warn(`HTTP ${r.status} — backing off ${20 * (attempt + 1)}s`);
      await sleep(20000 * (attempt + 1));
    }
  }
  function flush(final) {
    if (!S.pending.length) return;
    const blob = new Blob([S.pending.map((r) => JSON.stringify(r)).join("\n") + "\n"], { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `ca_sos_${S.stamp}_part${++S.part}.jsonl`;
    document.body.appendChild(a); a.click(); a.remove();
    console.log(`saved ${a.download} (${S.pending.length} rows${final ? ", final" : ""})`);
    S.pending = [];
  }
  async function window_(term, ftype, start, end) {
    if (S.stop) return;
    const key = `${term}|${ftype}|${start}|${end}`;
    if (S.done.has(key)) return;
    const rows = await search(term, ftype, start, end);
    let n = 0, fresh = 0;
    for (const [id, row] of Object.entries(rows)) { n++; if (!S.rows.has(id)) { S.rows.set(id, row); S.pending.push(row); fresh++; } }
    const capped = n >= CAP && start !== end;
    console.log(`${term} type=${ftype || "*"} ${start}..${end} -> ${n} rows, ${fresh} new${capped ? "  CAPPED->split" : ""}  [total ${S.rows.size}, ${S.calls} calls]`);
    if (S.pending.length >= FLUSH_EVERY) flush(false);
    await sleep(PACE_MS * (0.7 + Math.random() * 0.7));
    if (capped) {
      const a = new Date(start + "T00:00:00Z"), b = new Date(end + "T00:00:00Z");
      const mid = new Date(a.getTime() + Math.floor((b - a) / 2 / 86400000) * 86400000).toISOString().slice(0, 10);
      await window_(term, ftype, start, mid);
      await window_(term, ftype, addDays(mid, 1), end);
    }
    S.done.add(key);
  }
  async function sweep(term, ftype) {
    const before = S.rows.size;
    await window_(term, ftype, DATE_MIN, today);
    return S.rows.size - before;
  }
  function mine(used) {
    const df = new Map();
    for (const row of S.rows.values()) {
      if (!/Common Interest Development/.test(row.ENTITY_TYPE || "")) continue;
      const name = ((row.TITLE || [""])[0] || "").toUpperCase();
      for (const w of new Set(name.match(/[A-Z][A-Z']{2,}/g) || [])) df.set(w, (df.get(w) || 0) + 1);
    }
    return [...df.entries()].sort((x, y) => y[1] - x[1] || (x[0] < y[0] ? -1 : 1))
      .map((e) => e[0]).filter((w) => !used.has(w) && !STOP.has(w)).slice(0, EXPAND);
  }

  const used = new Set();
  for (const t of CID_TYPES) for (const w of CID_WORDS) { used.add(w); const n = await sweep(w, t); console.log(`== cid ${w} type=${t}: +${n}`); if (S.stop) break; }
  for (const w of NAMED_WORDS) { if (S.stop) break; used.add(w); const n = await sweep(w, ""); console.log(`== named ${w} all types: +${n}`); }
  for (const t of CID_TYPES) {
    if (S.stop) break;
    const words = mine(used); let zeros = 0;
    console.log(`expansion type=${t}: ${words.length} mined words: ${words.slice(0, 15).join(", ")} ...`);
    for (const w of words) { if (S.stop) break; const n = await sweep(w, t); console.log(`== expand ${w} type=${t}: +${n}`); zeros = n ? 0 : zeros + 1; if (zeros >= 20) { console.log(`type ${t} exhausted`); break; } }
  }
  flush(true);
  console.log(`DONE: ${S.rows.size} rows, ${S.calls} API calls. Now: ./venv/bin/python get_ca_sos.py --ingest ~/Downloads/ca_sos_${S.stamp}_part*.jsonl`);
})();
