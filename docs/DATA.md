# Data model & matching rules

How the collectors' outputs become the per-community reports on the site.
Read this before changing `build_site.py`.

## The entity model

The unit of everything is the **community**: one entity per distinct
normalized association name per state (`(state, normalized_name)` key, id =
first 10 hex of sha1). Entities are assembled from sources in this order,
each able to create new entities or attach to existing ones:

1. registrations (`records/associations.jsonl` — FL DBPR + TX TREC;
   `records/state_registries.jsonl` — VA DPOR, HI AOUO, UT HOA Registry,
   WI DFI Homeowners' Association registry (Wis. Stat. 710.085 public
   notices swept by `get_wi_dfi.py`; HOAs only, no condos, filings since 2022),
   MD Montgomery County CCOC, NV Clark County / Henderson / Las Vegas GIS
   HOA layers; `records/irs_exempt_orgs.jsonl` — IRS EO BMF
   tax-exempt associations in every state, folded as registrations but
   *not* counted as a state registry for coverage purposes;
   `records/gov_layers.jsonl` — county/city HOA inventories from
   `get_gov_layers.py`, folded the same way and flagged `local_inventory`)
2. corporations (`.cache/sunbiz/` FL; `records/state_corps.jsonl` others,
   incl. CA — the CA Secretary of State bizfile business search, keyword-
   aggregated by `get_ca_sos.py` because CA has no HOA registry to download,
   and WA — the SoS CCFS advanced search swept through BrowserOS by
   `get_wa_ccfs.py`, list rows only: status, formation date, agent,
   principal-office address; no governors yet)
3. liens (`liens/liens.jsonl` Broward; `liens/liens_miamidade.jsonl`
   Miami-Dade, best-effort per-association; `liens/liens_nyc.jsonl` ACRIS; `liens/liens_cook.jsonl` Cook County IL
   Clerk recordings, a party-name sweep (LXA = a lien against the association, counted under other);
   `liens/liens_ca_ucc.jsonl` CA Secretary of
   State UCC index — judgment liens, statewide, `hoa_role` creditor/debtor,
   doc_type JL counts with claims of lien, JLX = judgment against the HOA)
4. consumer complaints (`records/sc_complaints.jsonl`)
5. court dockets + opinions (`courts/dockets.jsonl`, `courts/opinions.jsonl`
   CourtListener; `courts/bulk_dockets.jsonl`, `courts/bulk_opinions.jsonl`
   CourtListener's bulk files, collected by `get_courts_bulk.py` and not yet
   read by the build; `courts/tx_research.jsonl` re:SearchTX TX trial courts,
   best-effort per-association — same docket record shape; every
   `courts/trial_*.jsonl` written by `get_state_courts.py` — one file per
   state/county portal adapter in `hoaspy/collect/courts/court_portals/`, same shape plus an
   `association_role` list — folded in the same way; `courts/trial_fl_miamidade.jsonl`
   is written by `records_miamidade_civil.py` from the Miami-Dade Clerk's
   Civil FTP feed instead of a portal search: same shape plus `county`,
   `zip` (the sued property's ZIP, never its street address or owner) and a
   per-case public Online Case Search link — `add_courts` carries `zip` and
   `association_role` onto the case and uses `county` as the match hint)
6. news (`news/articles.jsonl`, when present)

## Where a community is (`hoaspy/build/places.py`)

A community's `city` / `zip` / `address` come from the first record that
carries them, but only when that record places it in its own state:

- `Builder.take_location` skips a record whose ZIP is in another state
  (`zip_state`, a USPS three-digit prefix table) — a registered agent's,
  manager's or developer's mailing address — and drops a "city" that is a
  street line or PO box (`is_address_line`; the NC OneMap mailing-city column
  does this for ~2k parcels). A Fort Lauderdale condo used to read
  "Boston, FL" from its manager's Sunbiz address.
- `Builder.validate_locations` runs last: a location still in another state
  is cleared, a street-line city is cleared, and every remaining city is
  checked against the Census 2023 gazetteer (places + county subdivisions,
  cached under `.cache/geocode/`; `norm_place` expands Ft./N./St. and strips
  "city/town/CDP/township"). A city the gazetteer knows only in *other*
  states is cleared when no in-state ZIP vouches for it; USPS-only names
  (Flushing, Coconut Grove, Hallandale) stay and count as *unverified*. The
  counts land in `meta.json` `stats.locations` and the build log.
- The learned ZIP → county map is keyed by state for the same reason.

## Name normalization

`normalize()`: uppercase, strip punctuation, expand ASSN/ASSOC→ASSOCIATION,
CONDO(S)→CONDOMINIUM, HOMEOWNER→HOMEOWNERS; drop trailing filing noise
(INC, "ET AL", OBO, clerk truncations); drop leading THE.

`core_of()`: the distinctive part — drops generic tokens (ASSOCIATION,
HOMEOWNERS, CONDOMINIUM, PHASE, numbers, roman numerals…). "SUNRISE LAKES
CONDOMINIUM PHASE 4" → core "SUNRISE LAKES".

## Excluded entities (financial institutions)

National banks are chartered as "… National Association" (the N.A. in
"U.S. Bank, N.A."), so their mortgage-foreclosure filings in county indexes
match the association-name filter. `is_financial()` in build_site.py drops a
name at entity creation when it has a **bank word-form** (incl. clerk
misspellings: BANKN, SBANK, BANKNATIONAL, NATIONALK, NAIONTAL…) in
**financial context** (NATIONAL/NATL/NAT, TRUST, FEDERAL, MORTGAGE, series/
certificate language, TR/SAV/CUST suffixes), or a known **brand** (JPMorgan,
HSBC, Citibank, Wachovia…). A **community token** (HOMEOWNERS, CONDOMINIUM,
OWNERS ASSOCIATION, ESTATES, VILLAS…) always overrides the exclusion — so
"Bankston Meadows Homeowners Association", "Marlbank Cove", "The Bank
Building" condo, and multi-party court captions naming a bank *and* a real
HOA all stay in. ~418 records excluded as of 2026-08. Non-community industry
orgs (insurers, realtor boards, utility co-ops) are additionally excluded
from SEO profile pages by `NON_COMMUNITY_RE` in build_seo.py.

## Court parties that never become communities

Trial-court portals match parties on leading words, so a query for
"GOLDEN LAKES, A CONDO" also returned *Golden Lakes Medical Center Inc*,
and *Third Avenue Chiropractic Ctr* came back for a Third Avenue condo.
`Builder.party_ok` (using `party_is_association` / `looks_like_association`
from the collectors' `court_portals/_common.py`) decides whether a party
that matches no existing community may become one: yes if it carries an
association marker (association, assn, HOA/POA/COA, condominium, homeowners,
property owners, townhomes, cooperative, board of managers) and is not a
business (`has_business_form`: an owner form LLC/LP, Ltd/PLLC without the
full word Association, or a business activity — finance, mortgage, bank,
insurance/underwriters, realty, management, services, associates, leasing,
apartments — *after* the last association word, so "Homeowners Finance Co."
and "Community Association Underwriters" are out while "Left Bank
Condominium Association" and "Property Owners Association, Ltd." are in), or
if it is the queried association's core name plus entity/association
suffixes. Everything else is dropped and counted
(`stats.court_parties_dropped`); a party that *does* match an existing
community still attaches. The same test keeps corporate-registry rows with a
business form (developer LLCs, "Homeowners Finance Co.") from becoming
communities, and runs inside the Broward and Hillsborough adapters at
collection time.

An LLM second opinion (`hoaspy/build/llm_audit.py`, LLM_AUDIT.md)
runs over the built index and the next build drops confident business
verdicts that no registry backs, and clears or respells cities.

## Cross-source matching (deliberately conservative)

The failure mode that matters is attaching one association's liens to
another's report, so matching is:

1. **exact** normalized-name match within the state, else
2. **unique core** match — only when exactly one entity and (for corps)
   exactly one corporation share that core, else
3. **zip disambiguation** — several corps share a core (phase communities):
   attach only if exactly one shares the entity's zip, else
4. **leave unlinked**. Ambiguity never guesses.

County hints constrain lien matching (a Broward lien only core-matches
entities in Broward or with no county).

## Flags ("things to watch out for") and score

| Flag | Trigger | Severity |
| --- | --- | --- |
| Registration delinquent | DBPR `status_detail == Delinquent` | serious |
| Corporation not in good standing | attached corp status matches dissolved/forfeited/delinquent/… | serious |
| No active corporation found | **only** in active-only registries (FL Sunbiz), only when the entity's own name is corporate-style, only with recent activity | serious |
| Claims of lien filed | any liens; scaled by last-5-years count | info→serious |
| Foreclosure filings (lis pendens) | LP/foreclosure docs | warning→serious |
| Liens contested by owners | notices of contest | info/warning |
| High lien rate for size | recent liens ≥ 8% of unit count | serious |
| Federal housing/civil-rights, debt-collection suits | nature-of-suit | serious |
| Insurance case | nature-of-suit | info (usually the association suing its insurer) |
| State appellate case | opinion in caption | warning |
| Taken to the state supreme court | docket with `level: supreme` (Supreme Court of Ohio); the detail carries the appeal type and the outcome | warning |
| Consumer complaints (SC) | SCDCA complaint rows | info/warning |
| Timeshare/resort context | name pattern + liens | info (contextualizes inflated counts) |
| No adverse records | nothing above fired | good — with an explicit absence-of-coverage caveat |

Score is a ranking heuristic only (browse order), not shown as a number of
record: recent liens + 4×recent foreclosures + case/complaint weights.

Per-county **percentile** (`liens.percentile`) compares recent-5y lien counts
among that county's filers only — Broward and Manhattan are different worlds.

## Coverage honesty rules

- Corp-registry coverage per state is `active_only` / `all` / `partial`
  (`records/corp_coverage.json`). Absence-of-corporation is only ever
  evidence under `active_only`.
- `meta.coverage[state].irs_exempt` marks states where the IRS roster is
  the only association list: the footer says so, and STATES.md puts them
  in their own "IRS tax-exempt roster only" tier rather than "state-level
  data" — most HOAs file Form 1120-H and never appear in the BMF.
- Every report footer states what was NOT checked for that state
  (from `meta.coverage`), with retrieval dates.
- The index/search layer (`site/data/index-XX.txt`) is pipe-delimited:
  `id|name|city|county|type|score|badges` where badges are
  `D` delinquent, `B` corp not in good standing, `C` no-corp,
  `L{n}` liens, `P{n}` foreclosures, `F{n}` cases, `X{n}` complaints,
  `N{n}` news, `OK` clean.
- Full reports live in `site/data/detail/<id[:2]>.json` shards.

## Map layer (`build_map.py` → `site/data/case_map.json`)

Built from the detail shards (so it inherits the matching and exclusions
above); one file, two feature lists drawn together on one map:

- `features` — liens: one per community with liens and a geocodable
  address. Fields `id name county state lat lon addr units lie lp nct r5 y0
  y1 total rec` — `county` is the community's **own** (ZIP-derived) county,
  `rec` the recording county only when it differs, `lie/lp/nct` the
  lien/lis-pendens/contest counts, `r5` the recent-5y count that drives the
  colour ramp, `y0`/`y1` first/last lien year. Falsy fields are omitted
  (absent-means-zero in `site/map.js`). Geocoding (US Census batch; Nominatim
  opt-in fallback) is cached in `.cache/geocode/lien_map.json`.
- `cases` — court cases: one per community with any court record (dockets
  and appellate opinions, whichever side the association is on) **and a
  ZIP**, placed at the ZIP centroid (Census ZCTA gazetteer, cached). Fields
  `id name county state lat lon n ty top p d kd ko r5 y1 pl` — `n` records,
  `ty` per-type counts and `top` the most common type (`case_type()`:
  Foreclosure/Collections/Insurance/Civil Rights/Property/Eviction/Labor/
  Tax/Appellate/Other), `p`/`d` records where the HOA is plaintiff/defendant
  (`case_side()`, case-insensitive caption split), `kd`/`ko` dockets/opinions.
  `pl` says how it was placed: `zip` from the community's own registry ZIP,
  or `case` when it has no registry address but its county-feed cases carry
  the sued property's ZIP (most common one wins) — the community's own
  ground, not an approximation. Communities with no ZIP in any record are
  counted in `n_cases_unplaced` and left off the map.

Coords are rounded to 5dp. The top level carries `generated`, per-layer
counts and county tallies, and a source note per layer. The frontend hides
the map panel when the data file is absent.

## Search ordering (site/app.js)

The default list with no query ("Highest caution scores", or "Most liens on
file" with a state picked) is filtered to names that read as an association
(`looksLikeHoa`: association word, no business form — the same vocabulary as
the build's party gate) so a landlord or clinic can never headline the front
page; every indexed name stays searchable.


- A query that exactly matches a **city or county name** is a place browse:
  communities located there first, ordered by liens (then foreclosures, then
  score), with communities merely *named* after the place appended after.
- Browsing with a **state/county dropdown filter** and no query is also
  lien-ordered ("Most liens on file").
- Name searches rank by match quality; the sort dropdown (`Most liens`,
  `Highest caution`, `A–Z`) overrides everything.

## Member reviews (serve.py + reviews.db)

Reviews are member-contributed opinions, kept strictly apart from the public
records: they live in `reviews.db` (sqlite, gitignored — contains emails),
are labeled as unverified opinions in the UI, and never affect badges,
flags, or the caution score.

- Auth is passwordless email OTP: `login_codes` holds a salted sha256 of a
  6-digit code (10-min expiry, 5 attempts, ≥60s between sends, 8/day per
  email); `sessions` holds sha256 of bearer tokens (180-day expiry);
  `users` is just id + email.
- `reviews`: UNIQUE(entity_id, user_id) — one per member per community,
  posting again edits. Rating 1–5 required, body 10–4000 chars, entity_id
  must exist in `site/data/index-*.txt`. Authors display as "Member #N".
- The API is same-origin under `/api/` (no CORS needed); a static host
  without serve.py simply shows no reviews section.

## Member-submitted record links (serve.py `submissions` + site/contribute.html)

Records members bring back from their state's own court/lien/agency portals.
Most of those portals give a case no address of its own, so a record is
stored as *where to find it*: the source's address (`url`), the case or
instrument number (`ref`) and what to type into the source's search
(`search_term`). Kept apart from the collected records exactly like reviews
— same database, same sign-in, never fetched by us — but unlike reviews
they *do* feed the verdict once described.

- `submissions`: `entity_id` (nullable — a community we don't index is keyed
  by `state` + `hoa_key`, the app.js-folded name), `kind` ∈ court_case /
  recorded_document / agency_record / news / hoa_document / other,
  `category` ∈ the `SUB_CATEGORIES` vocabulary that `site/verdict.js`
  scores (test-enforced identical), `url` (http/https, host with a dot, no
  userinfo, ≤2000 chars — the record's own page, or the portal it was found
  on), optional `ref` (case / instrument number, ≤80) / `search_term`
  (≤200) / title / `event_date` (YYYY[-MM[-DD]]) / notes; `status` pending
  → approved | rejected. A member's record is its `url` plus its `ref` —
  or, with no `ref`, its `url` plus its `search_term`: re-posting that
  edits the description and requeues, anything else is a new row, so
  several cases found on one portal are several rows. 30 per member per day.
- `site/portals.json` decides where a member is sent: per state `courts`
  (each with `scope`, `access`, `search`, optional `indexed`, and
  `level: "supreme"` on a state supreme court docket — indexed, shown as
  checked, but never counted as the state's trial courts), `liens`
  (optional `scope` — "Broward County" ties the source to one county, none
  means statewide — and `indexed: true` where HOA Spy collects it), `corp`
  and `hoa_registry` (`indexed: true` where HOA Spy holds the registry),
  plus the shared `federal` list. The contribute page recommends, one at a
  time, only the sources for the member's county that are not `indexed`
  (SUBMISSIONS.md); a county
  `scope` also feeds the clerk-domain table in `counties.json`.
- Visibility: `approved` to everyone (as "Member #N"); a member always sees
  their own in any status. `submissions.py export` writes
  `records/member_links.jsonl` (member id only, no email) for a future
  build_site fold-in — not folded in yet.
- Moderation by email: every new or re-queued submission mails
  `HW_ADMIN_EMAIL` (compose passes it from `.env`) a review link,
  `HW_BASE_URL/admin/submissions/<id>?t=<token>`; the token is an HMAC of the
  id under `kv.admin_secret`, generated once per database by `init_db`, so
  `submissions.py link <id>` on the host prints the same URL the container
  mailed. The page shows every field, warns about an impossible date, a name
  that looks like a place rather than an association, and a link with no
  indexed community, and offers Approve / Reject as POST buttons — a bare
  GET can never change status, so mail scanners that prefetch links are
  harmless. A mail failure is logged and never fails the member's save;
  `submissions.py notify <id>` resends. Without `HW_ADMIN_EMAIL` the link
  prints to the server console (dev mode).
- Moderation rule (DATA.md sourcing rules apply): open the link, confirm it
  is a government / court / established-news source and names the
  association, then approve. Reject uploads-by-proxy, social posts, and
  anything naming a private person beyond the record.

## Counties and clerk searches (site/counties.json, site/geo.js, site/places.js)

`make_counties.py` writes `site/counties.json` from the Census Bureau county
gazetteer: every county-equivalent per state, spelled the way the index
spells counties (bare name, no "County"/"Parish"/"Borough" suffix; Virginia
independent cities as "Fairfax City"; Connecticut's eight legacy counties
rather than the 2024 planning regions; NYC borough aliases). `clerks` maps
county → clerk-of-court web domain, merged from county-scoped `portals.json`
entries and the hand-maintained `CLERK_DOMAINS` table in the script.
`geo.js` (`HW_GEO`) serves both pages the full county menus and the clerk
domain of a county. `places.js` (`HW_PLACES`) turns the portal guide and
that domain into the places where a county's records can be searched:
`clerkSite(guide, state, county)` is the clerk's own website
(`https://<domain>/`), offered where a domain is on file and the guide
lists no court search for that county; `list(…)` is every place for the
county — its own court search, the clerk's site, its lien index, then the
state's court, lien, corporate and registry portals, never another
county's — and `top(…, n)` the first few a visitor can use (not `paid`, not
`none`). The search page's report line and empty state show `top(…, 3)`;
the contribute page adds `clerkSite` to its sources. Until 2026-10-01 both
pages built a Google query instead (`site:*<domain> <HOA name>`); clerks
keep dockets and recorded liens behind their own search forms, so it rarely
reached them. `HW_GEO.clerkQuery` / `clerkUrl` / `clerkLabel` are still in
`geo.js`, unused, only so a browser holding an older cached `app.js` keeps
working (DEPLOY.md, the four-hour cache) — delete them in a later release.
Regenerate `counties.json` with `./venv/bin/python -m
hoaspy.build.make_counties`; `--check` reports drift.

## Verdict (site/verdict.js)

A tiered summary shown at the top of every community report and on the
contribute page. Points are itemized and additive: flags (serious/critical
3, warning 1; lien-count flags skipped to avoid double counting the lien
block; court flags — one per docket in the built record — are grouped by
label and capped: serious 3/case to 6, warning 1/case to 3, info 0.25 to
1, so 180 evictions read as one row), recent-5y liens by county
percentile (0.5–4), lis pendens (1 + n/5, cap 4), ≥3 contested liens (1),
state complaints (1 + 0.5n, cap 3), member reviews with ≥3 ratings (avg ≤2
+2, ≤3 +1, ≥4 −1), and member links by category (`w` per record up to
`cap`; `fraud` and `discrimination` are *misconduct* categories that force
the top tier). Tiers: `nodata` (nothing held, no links), `ok` < 1.5,
`concerns` < 5, `troubled` < 10, `redflags` ≥ 10 or any misconduct link.
The panel always prints "Checked" / "Not checked" from `meta.coverage` and
`portals.json` `indexed` flags, and labels pending links "unreviewed".

## Master tracking

`coverage.json` — per state: `collected` (with counts), `available`
(exists, untapped), `unavailable` (paid/restricted + why), `counties`,
`notes`. `hoaspy.build.make_states_md` renders it (plus `state_sources.yml`,
`gov_layers/`, the trial-court adapters and `site/portals.json`) to `STATES.md`
and one page per state under `states/`; `--check` fails the suite when stale.
