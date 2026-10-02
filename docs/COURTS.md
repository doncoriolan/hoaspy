# Court records (`courts/`)

Court records are where an association's disputes become public: the
foreclosure it filed against an owner, the owner who sued the board, the
federal housing-discrimination or debt-collection suit. No free national
index of state *trial* courts exists, so `courts/` is built in layers: one
nationwide source for federal dockets and state appellate opinions
(CourtListener — its search API and its bulk files), Texas' statewide portal (re:SearchTX), a per-portal adapter
for other states' and counties' trial-court sites, and one subscriber feed
(Miami-Dade). Every layer writes the **same docket record shape**, and
`hoaspy/build/build_site.py` folds each file in with `Builder.add_courts` /
`add_opinions`. Records are referenced by link, never re-hosted.

| Collector | Jurisdiction | Source | Access | Output |
| --- | --- | --- | --- | --- |
| `get_courts` | all 50 states + DC | CourtListener v4 search API: RECAP federal dockets, state supreme/appellate opinions | anonymous, paced | `courts/dockets.jsonl`, `courts/opinions.jsonl`, `courts/sources.json` (top level) |
| `get_courts_bulk` | every court CourtListener holds, all 50 states + DC | CourtListener quarterly bulk data (public S3 bucket): every docket and opinion **caption** | anonymous download, no rate limit, ~7.7 GB | `courts/bulk_dockets.jsonl`, `courts/bulk_opinions.jsonl`, `sources.json["courtlistener_bulk"]` |
| `get_tx_research` | Texas trial courts (participating county/district courts) | re:SearchTX (Office of Court Administration) | your own logged-in Cookie header; 200 searches/hour | `courts/tx_research.jsonl`, `sources.json["tx_research"]` |
| `get_state_courts` + `court_portals/*` | AK, CT, MD, PA statewide; VA general district courts; Broward and Hillsborough counties FL; the Supreme Court of Ohio | each portal's own search (see the adapter table) | anonymous; Broward and Virginia through the local BrowserOS browser; Maryland with your own browser's DataDome cookie | `courts/trial_<KEY>.jsonl`, `sources.json["trial_courts"][KEY]` |
| `records_miamidade_civil` | Miami-Dade County, FL | Clerk's Commercial Data Services **Civil** FTP feed | subscriber FTP files staged by hand; public per-case links | `courts/trial_fl_miamidade.jsonl`, `sources.json["trial_courts"]["fl_miamidade"]` |

Record counts per source are in `courts/sources.json` (rebuilt on every run)
and, per state, in `coverage.json` → [STATES.md](STATES.md) / `states/<ST>.md`.
The portal survey that decided which states have an adapter and which are
blocked is NEEDS.md §3d.

## Where the code lives

| File | Role |
| --- | --- |
| `hoaspy/collect/courts/get_courts.py` | CourtListener: court maps from the courts API, five party queries + five caption queries, checkpoint after every query, `STATE_NAMES` (also imported by `build_site`) |
| `hoaspy/collect/courts/get_courts_bulk.py` | CourtListener bulk data: bucket listing (`snapshots`), resumable parallel download, the PostgreSQL-CSV reader (`open_rows`), the wide caption net (`BROAD`, `scan`), the caption splitter and association gate (`find_associations`, `is_association`), `docket_record` / `opinion_record`, `merge_sources` |
| `hoaspy/collect/courts/get_tx_research.py` | re:SearchTX: session from a Cookie header, phrase query per association, `to_record`, `QuotaError`/`PermissionError`, per-name checkpoint |
| `hoaspy/collect/courts/get_state_courts.py` | Portal driver: association names per state from `records/` (`association_names`) plus an adapter's `SWEEPS` (`portal_names`), checkpoint per portal, quota/permission handling, `dedupe`, `sources.json` catalog |
| `hoaspy/collect/courts/court_portals/__init__.py` | The adapter contract, `RateLimited`, `registry()` (imports every non-underscore module in the package) |
| `hoaspy/collect/courts/court_portals/_common.py` | `ASSOCIATION_RE`, `normalize`, `name_matches`, `iso_date`, `record()` — the one place the trial record shape is built |
| `hoaspy/collect/courts/court_portals/{ak_courtview,ct_civil,pa_ujs,fl_broward,fl_hillsborough,md_casesearch,va_gdc,oh_supreme}.py` | One adapter per portal |
| `hoaspy/collect/courts/records_miamidade_civil.py` | Feed parser (`daily_civil_*.zip`, `Indebtedness_*.zip`), association tiers, property ZIP, public OCS links |
| `hoaspy/build/build_site.py` → `Builder.add_courts`, `add_opinions`, `_attach_case` | Consumer |

## The docket record shape

`courts/dockets.jsonl` (CourtListener RECAP), `courts/tx_research.jsonl` and
every `courts/trial_*.jsonl` share these fields — the trial files are built
by `court_portals._common.record()` so they cannot drift:

| Field | Meaning |
| --- | --- |
| `case_name` | caption as the source shows it (highlight tags and entities stripped) |
| `court` | court name / jurisdiction string |
| `docket_number` | the durable locator; for portals without a public deep link this is what a reader types into the portal |
| `date_filed`, `date_terminated` | ISO `YYYY-MM-DD` or `""` |
| `nature_of_suit`, `cause` | nature/type as indexed; `cause` carries the case *status* for trial sources |
| `state` | two-letter state |
| `associations` | the association-shaped party names the case is attached to — the join key |
| `url` | public link: CourtListener docket page, re:SearchTX `ui/case/<caseDataID>`, a portal search page, or the Miami-Dade OCS case page |
| `source` | `courtlistener-recap`, `researchtx`, or the adapter `KEY` |
| `queries` | which query / queried name found it |
| `retrieved_at` | ISO timestamp |

Extras: RECAP dockets add `docket_id`, `court_id`, `parties`; re:SearchTX and
the trial files add `case_data_id`; every trial adapter and the Miami-Dade
feed add `association_role` (`plaintiff`/`defendant` per association); the
Miami-Dade feed adds `county`, `zip` (the sued property's ZIP, only when the
association is plaintiff) and `has_public_link`.

`courts/opinions.jsonl` is smaller: `case_name`, `court`, `court_id`,
`state`, `date_filed`, `associations` (caption segments split on " v. "),
`url`, `source: courtlistener-opinions`, `queries`, `retrieved_at`.

## CourtListener — `get_courts`

The Free Law Project's v4 search API is queried twice over: `type=r` (RECAP
dockets) with `PARTY_QUERIES` (`party:"homeowners association"`,
`"condominium association"`, `"property owners association"`, `"community
association"`, `"owners association"`) against every federal district, and
`type=o` (opinions) with the same phrases as `caseName:` queries against every
state supreme and appellate court. The court lists and each court's state
come from CourtListener's courts API (`fetch_court_maps`: jurisdictions `FD`,
`S`, `SA`; criminal-appeals courts and territories dropped), so all 50 states
+ DC are covered without a hand-kept map. Requests are paced by
`hoaspy/lib/pacing.Pacer` (1–2.5 s, exponential 429 backoff); each query
walks the cursor up to `--max-pages` (default 400 × 20 results). Existing
records are loaded first and merged — dockets dedupe on `docket_id`,
opinions on `url` — and the file is rewritten after **every** query, so the
run is safe to kill and re-run. `--only-opinions` / `--only-dockets` skip a
half.

`associations` on a docket is every party matching
`records_broward.ASSOCIATION_RE`; on an opinion it is every caption segment
that matches. `sources.json` records the district/appellate court counts,
the queries and the caveat: RECAP holds what PACER users have shared, and
state coverage is appellate only.

## CourtListener bulk data — `get_courts_bulk`

The search API answers five phrases and, anonymously, is throttled to five
requests a minute with a daily cap behind that (see Limits). The Free Law
Project also publishes its whole database every quarter as CSV files in a
public S3 bucket
(`com-courtlistener-storage.s3-us-west-2.amazonaws.com/bulk-data/`, described
at [courtlistener.com/help/api/bulk-data](https://www.courtlistener.com/help/api/bulk-data/)).
`get_courts_bulk` reads three of them — no account, no rate limit, no query
grammar:

| File (2026-09-30 snapshot) | Size | What is read |
| --- | --- | --- |
| `courts-<date>.csv.bz2` | 81 KB | each court's full name (→ state) and `jurisdiction` code |
| `opinion-clusters-<date>.csv.bz2` | 2.5 GB | one row per decided case: `case_name`, `case_name_full`, `date_filed`, `precedential_status`, `docket_id`, `blocked` |
| `dockets-<date>.csv.bz2` | 5.1 GB | one row per docket in any court: captions, `court_id`, `docket_number`, dates, `nature_of_suit`, `cause`, `appeal_from_id`, `blocked` |

It works in three steps, each skipped when its result is already in the
cache (`.cache/courtlistener_bulk/`, git-ignored):

1. **Download.** `list_bucket` pages the bucket listing, `snapshots` keeps
   the dates that have all three files (a quarter is uploaded over several
   hours), and the latest is taken unless `--snapshot` names one. Each file
   is fetched with `--connections` (default 8) parallel range requests
   (`plan_parts`); every part resumes from the bytes already on disk and the
   joined file must match the listed size.
2. **Scan.** `open_rows` reads the file as PostgreSQL wrote it
   (`COPY … WITH (FORMAT csv, ESCAPE '\')`: `\"` inside a field, fields that
   span lines, an unquoted empty field for NULL), through the `bzip2` binary
   when there is one. `scan` keeps the rows whose `case_name` or
   `case_name_full` matches `BROAD` — a deliberately wide net of association
   words, abbreviations included — cut down to the columns the next step
   needs. Opinions are scanned first; the docket pass then also keeps the
   docket of every kept opinion, because the cluster row does not say which
   court decided it. Rows CourtListener marks `blocked` (kept out of search
   engines, mostly bankruptcies) are skipped here and never reach the cache.
   About an hour for both files, nearly all of it decompression.
3. **Refine.** `find_associations` spells out reporter abbreviations
   (`Ass'n`, `Assn.`, `Assoc.`, `Condo.`, `Bd. of Mgrs.`), splits the caption
   at "v." into sides and each side into parties, trims role words, "In re"
   and a trailing "… and John Doe", and keeps the parties `is_association`
   accepts: an association word, no lender / insurer / builder / public-body
   / trade-group vocabulary ("Home Owners' Loan Corporation", "Bank of
   America, National Association", "Standardbred Owners Association"), no
   LLC or LP form, more than generic words, and "HOA" only as the last word
   of a longer name — never the given name in "Hoa Van Doe". A bare
   "Roseland Townhomes" is refused (as often a rental complex) unless it is
   a corporation; so are a class of owners ("… and All Other Property
   Owners"), an officer sued "Individually and as President of …", two
   parties the caption ran together, and anything over twelve words. A
   person listed ahead of the association ("Jane Doe and Kendall Acres
   Condo Association") is cut off, but not words that pair up inside one
   name ("Golf and Tennis Club", "Sand and Sea"); an insurer suing "a/s/o"
   the association gives the association. The full
   caption is read before the short one, because Florida's appellate dockets
   shorten `case_name` to "X HOMEOWNERS v. DOE". `--refine-only` re-runs
   this step alone after a rule change (seconds, not an hour).

When it writes to the default `courts/` folder the collector also records
per-state counts in `coverage.json` (`courts_bulk_dockets`,
`courts_bulk_opinions`; one read and one write, only its own two keys).

`bulk_dockets.jsonl` has the docket record shape with `source:
courtlistener-bulk`, plus `docket_id`, `court_id`, `jurisdiction`
(CourtListener's code: `FD` federal district, `FB` bankruptcy, `F` federal
appellate, `S` / `SA` state supreme / appellate, `ST` state trial) and
`association_role` (the sides the associations are on, as in the trial
files: `["plaintiff"]`, `["defendant"]`, both, or `[]` when the caption has
no "v."). A federal court of appeals has no state of its
own, so `state` is the state of the district appealed from when the docket
names one. `bulk_opinions.jsonl` has the opinion shape with `source:
courtlistener-bulk-opinions`, plus `cluster_id`, `docket_id`,
`docket_number`, `jurisdiction`, `status` and `association_role`. Both
files overlap what `get_courts` finds — the same case has the same
`docket_id` / opinion `url` — so a consumer of both dedupes on those.

What it adds over the API sweep: state appellate **dockets** (Florida's
district courts of appeal, New York's Appellate Division, the Texas courts
of appeals — cases with no published opinion), bankruptcy and federal
appellate captions, and the opinions whose captions are abbreviated — the
five API phrases spell "association" out, so "Lake Point Tower Condo. Ass'n
v. Roe" was never returned and Illinois had 40 opinions. What it cannot
see: party lists. A bankruptcy captioned "In re John Doe" in which the
association is a creditor is found only by `get_courts`' `party:` search.

## re:SearchTX — `get_tx_research`

Texas' statewide portal exposes every participating county's trial docket
through one JSON search (`POST /CourtRecordsSearch/search`, body
`{queryString, searchIndexType: "Cases", pageSize, pageNumber}`). Three
constraints shape the collector (docstring and NEEDS.md §3a):

1. **Auth is your own account's session.** Copy the whole `Cookie:` header
   from a logged-in tab into a file (`--cookie-file tx_cookie.txt`, or
   `--cookie`, or `HW_TXRESEARCH_COOKIE`). It carries `FedAuth`, a signed
   `RSCH_JWT` that expires in hours, and the AWS WAF token. A 401/403 — or a
   200 login page instead of JSON — raises `PermissionError` and the run stops
   resumably.
2. **Deep paging is capped (~1000 rows) and full text is noisy**, so queries
   are **by association name**: distinct TX association-shaped names from
   `records/associations.jsonl` (TREC certificates), longest first, each
   quoted as a phrase after `query_for` strips query-grammar punctuation.
3. **A hard quota of 200 searches/hour.** A 429 raises `QuotaError` with
   `Retry-After`; by default the run stops and keeps its checkpoint,
   `--wait-on-quota` sleeps it off and continues.

`to_record` keeps a hit only when one of its own parties is
association-shaped **and** shares the queried name's core, so cross-reference
noise (a tax suit merely mentioning an HOA) is dropped. Checkpoints:
`courts/.tx_research_partial.jsonl` and `.tx_research_done.txt`; a name that
5xx'd is left off the done list so a rerun retries it. `write_outputs` never
overwrites a non-empty `tx_research.jsonl` with nothing and adds a
`tx_research` entry to `courts/sources.json`.

## State trial-court portals — `get_state_courts` + `court_portals`

Every other state's trial courts live in that state's (or county's) own
portal. The driver queries each portal **by association name** — the names
we already hold for that state in `records/associations.jsonl`,
`state_corps.jsonl`, `state_registries.jsonl` and `irs_exempt_orgs.jsonl`
(association-shaped only, longest first; county-scoped adapters restrict to
rows in their `COUNTIES`) — through an adapter, and writes
`courts/trial_<KEY>.jsonl`. An adapter may also declare `SWEEPS`: statewide
prefixes queried after the names (Maryland's "council of unit owners"),
which catch communities no roster holds; `--no-sweeps` leaves them out and
`--names FILE` replaces the whole list.

**Adapter contract** (`court_portals/__init__.py`): a module exposes `STATE`,
`KEY` (the output file slug), `INFO` (`name`, `url`, `access`, `coverage`,
`caveat` — copied into `sources.json`), `NEEDS_COOKIE`, optionally
`COUNTIES` and `SWEEPS`, and `class Client(cookie, pace)` with `search(name)
-> list[dict]` built through `_common.record(...)`. Adapters raise
`PermissionError` when a session has expired and `RateLimited(retry_after)`
on a quota. `registry()` discovers adapters by importing every module in the
package that does not start with `_` and has both `KEY` and `Client`, so a
new portal is one new file. `--list` prints them.

| KEY | State / scope | How it works | Cap / caveat |
| --- | --- | --- | --- |
| `ak_courtview` | AK statewide trial courts, reliable from 1990 | CourtView public access; Wicket per-session encrypted `?x=` URLs scraped from each response; company search is starts-with | 500-case cap per search; no public deep link (`url` = portal root, `docket_number` re-enters) |
| `ct_civil` | CT Superior Court civil, family, housing, all 16 districts | Party search (`PartySearch.aspx`), 200 rows/page, no cap; filing date/type/disposition from the stateless `LoadDocket.aspx?DocketNo=` deep link (one extra request per case) | "Starts With" on our registered spelling |
| `pa_ujs` | PA statewide **Magisterial District Judge** dockets | UJS portal organization search with an antiforgery token; starts-with match (a leading `%` is ignored); one 1900-to-today date range returns everything. Captions abbreviate the association ("Hemlock Farms Community Assoc." in 331 of 411 cases, spelled out in 9), so `query_name` sends the words before the first generic association word plus that word's stem ("Hemlock Farms Comm%") and `party_matches` folds Assoc/Assn/Comm/Condo/Propertyowners/H.O.A. before comparing; `associations` carries the queried name, not the caption spelling, so build_site joins the roster's community instead of creating "Hemlock Farms Comm Assoc" | Common Pleas civil dockets are *not* on this portal — small-claims / landlord-tenant tier only. Names that open with a generic word or carry distinguishing words after it are still searched whole; misspelled captions are missed |
| `oh_supreme` | **Supreme Court of Ohio**, every case since 1985 (appeals from the twelve District Courts of Appeals, the Board of Tax Appeals and the PUCO; original actions) — the state's highest court, not a trial court: records carry `level: "supreme"`, `lower_court`, `lower_court_case`, `county` and `disposition` | Clerk's online docket, one handler (`POST Ajax.ashx`): `CaseSearch` by `paramPartyEntityName` — an adjacent-word phrase, each word a prefix — then `GetCaseDetails` per case for the parties and their roles; needs only the `X-CSRF-TOKEN` constant published in the page's own `site.min.js` and a `Referer`; per-name queries on the distinctive core (and its `&`/`and` twin), then statewide `SWEEPS` of the association words ("condo", "homeowner", "owners assoc", …) kept only when the party is a community (`community_name`); deep link `#/caseinfo/<year>/<number>` | 1,000-row cap, newest first → split by filing-date window; county Common Pleas and municipal dockets are not here; amicus-only appearances are left out; lake, civic, village and townhome associations are not swept (too often not HOAs) and are found only by name |
| `fl_broward` | Broward County / 17th Circuit civil division | eCaseView business-name search POSTed with a Cloudflare Turnstile token that only a real browser can mint, so each name runs in a throw-away BrowserOS context over CDP (`127.0.0.1:9100`), ~8–12 s per name; `COUNTIES = {BROWARD}` | 200-row cap, newest first, no paging; leading-word match on the DBPR core name; no deep link |
| `fl_hillsborough` | Hillsborough County / 13th Circuit, filings from 1976 | HOVER JSON API: `LogAnonymous` mints a guid, `Case/Search` by business; `COUNTIES = {HILLSBOROUGH}` | 500-row cap with `start` ignored → split by filing-date window; PerimeterX blocks only the per-case summary call; no deep link |
| `md_casesearch` | MD District and Circuit Court civil cases, all 24 jurisdictions, from the 1980s | Case Search's JSON endpoint (`POST /api-caselist/v1/cases`, business-name **prefix** search, civil), run as same-origin `fetch()` inside your own Chrome window over its DevTools port (`cdp:` line in `--cookie-file md_cookie.txt`) or with that browser's `datadome` cookie (DataDome refuses plain requests and headless browsers, a real browser passes its device check with no click); per-name queries on the distinctive core, then statewide `SWEEPS` of the Maryland forms — "council of unit owners" (Real Prop. § 11-109), "council of co-owners", "homeowners", "board of directors of", "condominium"; public deep link `case-detail-page?caseId=` | 600-row cap, sorted by party name → split by filing-date window; a party recorded only as the bare form ("COUNCIL OF UNIT OWNERS") names no community and is dropped; "Board of Directors of X" kept only when X carries an association word or is a community we hold; a 403 stops the run for a fresh cookie |
| `va_gdc` | VA general district courts — civil claims up to $25,000, where assessments are collected — every court, cases the courts still hold online | Online Case Information System name search, **one court at a time** (128 civil dockets per name). Cloudflare passes in a real browser and the terms page is accepted once, so one tab in its own BrowserOS context (`127.0.0.1:9100`) issues every search as a same-origin `fetch()`; a timed-out session is reopened and the court redone. Starts-with on the name's core, 20 rows a page paged to the end; a flooded prefix is restarted one level narrower (`query_levels`: core → core + first letter of the association word → first four letters + HOA/POA) unless most rows are the association itself. Clerk spellings ("PROERTY OWNERS ASOOCIATION", "OWNER'S ASN") are canonicalised and parties gated by `party_matches`. Rows are folded to one record per base case number (`GV23011788-00` warrant in debt, `-01` garnishment … → `actions`); filing date and judgment amounts come from the detail page of the `-00` action (`HOASPY_VA_GDC_DETAILS=0` skips it; `HOASPY_VA_GDC_COURTS=059,153` limits the courts) | circuit-court civil cases are a different portal; same-named associations in different counties are not told apart (`court` / `court_fips` on every record); no deep link; `date_filed` is empty when only later actions are still online (`filed_year` from the case number stands in); the run stops if the terms page asks for a verification code again |

Portals judged blocked (server-verified captchas, terms of use forbidding
bots, paid indexes) and the verdict per state are in NEEDS.md §3d;
the rule throughout is government sources only, no accounts, no logins, no
solving image captchas — a checkbox or auto-passing challenge in a real
browser is the most we do.

Per portal the run is checkpointed per name (`courts/.trial_<KEY>_partial.jsonl`,
`.trial_<KEY>_done.txt`); `--fresh` clears it. Rate limits (`RateLimited`,
or a bare HTTP 429 message) stop the run resumably unless `--wait-on-quota`;
a `PermissionError` stops it for a fresh cookie; any other exception skips
that name. Output is written on every stop so `build_site` can use partial
results, `dedupe` merges the same case found under several names (union of
`associations` and `queries`), and `sources.json["trial_courts"][KEY]` gets
the adapter's `INFO` plus record/association/court counts. The checkpoint is
cleared only when the run completes.

## Miami-Dade Civil feed — `records_miamidade_civil`

Miami-Dade's own case-search portal validates reCAPTCHA server-side, so no
adapter exists; instead the Clerk's Commercial Data Services **Civil** folder
(a subscriber FTP feed under the Florida Supreme Court electronic-access
standards — the purchase is in NEEDS.md §1) is staged by hand
under `.cache/miamidade_civil/raw/` and parsed in full on every run:

- `daily_civil_MMDDYYYY.zip` — every case with docket activity that day plus
  its full history: `CASES.EXP`, `PARTIES.EXP` (with party addresses),
  `CASETYPE.EXP`. Caret-delimited, CRLF, no header. Later days win per case.
- `Indebtedness_YYYYMMDD.zip` — weekly dump of county contract-and-indebtedness
  and county foreclosure cases back to 1958, the feed's only backfile.

The FTP keeps 30 days, so dailies accumulate locally. `is_association` has
two tiers: the shared `ASSOCIATION_RE` plus master/maintenance/neighborhood/
recreation/apartment forms pass outright; a generic "…ASSOCIATION" caption
passes only when it equals a name already held for FL in `records/`; and
`_NOT_COMMUNITY_RE` (banks, insurers such as Homeowners Choice, adjusters,
debt buyers, LLCs, developers, management companies) never passes. Per case
the record carries which side each association is on, the court named from
the case-number division (`CA` circuit civil, `CC` county civil, `SP` small
claims …), the case type resolved through `CASETYPE.EXP`, a status (`Open` /
`Closed` from the disposition date) and — only when the association is the
plaintiff, i.e. the unit being foreclosed — the defendant's ZIP. Individual
defendants' names and street addresses are deliberately not written out.

Public links come from an open endpoint: `GET /ocs/api/CaseInfo/encrypt/{case
number}` returns the encrypted `qs` that `/ocs/searchResults?qs=` renders as
the Case Information page (no cookie, no login). Tokens are cached in
`.cache/miamidade_civil/ocs_links.json` and fetched with a few threads;
`--no-links` uses the cache only. The `sources.json["trial_courts"]["fl_miamidade"]`
entry adds link/ZIP/plaintiff/defendant counts and the feed statistics.

## Run it

```bash
# nationwide federal dockets + appellate opinions (hours; checkpointed per query)
./venv/bin/python -m hoaspy.collect.courts.get_courts
./venv/bin/python -m hoaspy.collect.courts.get_courts --only-opinions --max-pages 50

# every docket and opinion caption in CourtListener's bulk data (~7.7 GB download, about an hour to scan)
./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk                          # latest complete snapshot
./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk --snapshot 2026-09-30
./venv/bin/python -m hoaspy.collect.courts.get_courts_bulk --refine-only            # re-run the name rules on the cached extract

# Texas trial courts with your re:SearchTX session (resumable; 200 searches/hour)
./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt
./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt --wait-on-quota
./venv/bin/python -m hoaspy.collect.courts.get_tx_research --cookie-file tx_cookie.txt --limit 200 --names names.txt

# other states' trial courts through the portal adapters
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --list
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal ct_civil -v
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --all --wait-on-quota      # every anonymous adapter
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal fl_broward         # needs BrowserOS running (NEEDS.md 3c)
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal md_casesearch --cookie-file md_cookie.txt   # Maryland: your browser's datadome cookie
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal md_casesearch --cookie-file md_cookie.txt --no-sweeps --limit 50 -v
# Virginia general district courts: BrowserOS running; names from the IRS roster (courts/.trial_va_gdc_names.txt), ~5–7 h
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal va_gdc --names courts/.trial_va_gdc_names.txt --pace 0.5 --wait-on-quota
HOASPY_VA_GDC_COURTS=059,153 ./venv/bin/python -m hoaspy.collect.courts.get_state_courts --portal va_gdc --limit 5 -v   # two courts, five names

# Miami-Dade trial courts from the staged Civil feed
./venv/bin/python -m hoaspy.collect.courts.records_miamidade_civil
./venv/bin/python -m hoaspy.collect.courts.records_miamidade_civil --no-links -v

# then fold everything into the site
./venv/bin/python -m hoaspy.build.build_site && ./venv/bin/python -m hoaspy.build.build_map
```

Exit codes from the trial collectors are meaningful: `2` missing cookie,
`3` session expired, `4` quota reached, `130` interrupted — all resumable.

## Inputs & outputs

| Path | Written by | Contents |
| --- | --- | --- |
| `records/associations.jsonl`, `state_corps.jsonl`, `state_registries.jsonl`, `irs_exempt_orgs.jsonl` | the registry collectors ([REGISTRIES.md](REGISTRIES.md)) | the association names the trial collectors query and the Miami-Dade feed whitelists against |
| `courts/dockets.jsonl`, `courts/opinions.jsonl` | `get_courts` | RECAP dockets, appellate opinions (merged across runs) |
| `courts/bulk_dockets.jsonl`, `courts/bulk_opinions.jsonl` | `get_courts_bulk` | every docket / opinion caption in CourtListener's bulk data that names an association (rewritten per run) |
| `.cache/courtlistener_bulk/*.csv.bz2`, `hoa_clusters-<date>.jsonl`, `hoa_dockets-<date>.jsonl` | `get_courts_bulk` | the downloaded bulk files (~7.7 GB) and the step-2 extracts `--refine-only` re-reads |
| `courts/tx_research.jsonl` | `get_tx_research` | TX trial dockets |
| `courts/trial_<KEY>.jsonl` | `get_state_courts` | one file per adapter (`trial_ak_courtview`, `trial_ct_civil`, `trial_pa_ujs`, `trial_fl_broward`, `trial_fl_hillsborough`, `trial_md_casesearch`, `trial_va_gdc`, `trial_oh_supreme`) |
| `courts/trial_fl_miamidade.jsonl` | `records_miamidade_civil` | Miami-Dade trial cases with `association_role`, `county`, `zip`, `has_public_link` |
| `courts/sources.json` | all of the above | CourtListener provenance at the top level; `courtlistener_bulk`, `tx_research` and `trial_courts.<KEY>` entries merged in by the others |
| `courts/.tx_research_*`, `courts/.trial_<KEY>_*` | trial collectors | resume checkpoints (git-ignored) |
| `.cache/miamidade_civil/raw/*.zip`, `ocs_links.json` | you / the feed parser | staged FTP files and the public-link cache |
| `site/portals.json` (`indexed: trial_<KEY>`) | hand-maintained | tells the Find & add records page which portals HOA Spy already indexes; a test checks each named file exists |

`courts/` is git-ignored.

## How the site build consumes them

`build_site.main` folds the files in a fixed order after registries, corps
and liens: `dockets.jsonl` via `add_courts(level="federal")`,
`opinions.jsonl` via `add_opinions`, then `tx_research.jsonl` and every
`trial_*.jsonl` via `add_courts(level="trial")`. Each record becomes a case
dict (`kind: docket|opinion`, `level`, caption, court, docket number, dates,
nature, cause, url — plus `zip` and `association_role` when present) and is
attached to the entity matched from every name in `associations`
(`_attach_case`, county hint from the record's `county`, conservative match;
an unmatched name creates the entity, inheriting the feed's county). The
`level` is what separates "Named in a federal case" from "Sued an owner in
county court" / "Sued in county court"; a record that names its own `level`
keeps it, so the Supreme Court of Ohio docket (`level: "supreme"`, in
`trial_oh_supreme.jsonl`) flags as "Taken to the state supreme court" with
its `disposition` as the outcome and maps as Appellate. The verdict groups same-label
court flags into one row — the catalog is
[DATA.md — flags](DATA.md#flags-things-to-watch-out-for-and-score). The
map's case rings, case-type labels and plaintiff/defendant split are built
from the same case dicts by `hoaspy/build/build_map.py` (MAP.md).

`bulk_dockets.jsonl` and `bulk_opinions.jsonl` are folded in by
`Builder.add_bulk_courts` right after the API files, and each case is shown
once. The two bulk files describe the same cases twice over (a docket and
its opinion) and overlap the API files, so: a docket id already in
`dockets.jsonl`, or an opinion cluster already in `opinions.jsonl`
(`opinion_cluster_ids` reads the ids out of the opinion links), is skipped;
in a federal court (`BULK_LEVEL`: jurisdictions `FD`, `FB`, `FBP`, `F`,
`FS`) the docket stands for the case, as `level: "federal"`, and its
opinions are not added; in a state supreme or appellate court (`S`, `SA`)
the opinion stands for the case when there is one and the docket otherwise,
as `level: "appellate"` — flagged "State appellate case" like an opinion,
sourced to the CourtListener docket, and mapped as Appellate. State trial
and special courts (`ST`, `SS`), territorial, tribal and military courts,
and records with no state are collected but not folded in: no flag wording
fits them. `meta.json` → `stats.bulk_court` carries the counts (`dockets`,
`opinions`, `matched`, `created`, `skipped_seen`, `docket_has_opinion`,
`skipped_court`).

## Configuration & secrets

- **CourtListener:** none; a `User-Agent` identifies the project. The bulk
  files are an anonymous download; a free CourtListener API token would
  raise the search API's limits, and the collectors do not use one.
- **re:SearchTX:** a personal, short-lived Cookie header in `tx_cookie.txt`
  (git-ignored via `tx_cookie.txt` / `*_cookie.txt`) or `HW_TXRESEARCH_COOKIE`.
- **Portals:** the adapters are anonymous (`NEEDS_COOKIE = False`) except
  `md_casesearch`, whose `--cookie-file md_cookie.txt` names how to reach
  the portal, two ways. **(a)** `cdp: http://127.0.0.1:9612` — a real Chrome
  you started with `--remote-debugging-port=9612` (not headless; DataDome
  refuses headless): the adapter takes the tab on the portal, presses
  "I Agree" and runs every search as that page's own same-origin `fetch()`,
  so DataDome sees the browser it already trusts and its tag keeps the
  cookie fresh; a refused fetch reloads the page once. **(b)** the `Cookie:`
  header (or just `datadome=…`) of a `/api-caselist/v1/cases` request from
  your own Chrome session, copied from DevTools → Network, plus a second
  line `User-Agent: …` with that browser's user agent — works outside the
  browser for a few hundred requests, then DataDome refuses it. The file is
  git-ignored (`*_cookie.txt`). A 403 stops the run
  resumably; "Access is temporarily restricted … Automated (bot) activity on
  your network" is an IP-level hold that only time lifts.
  `fl_broward` needs the BrowserOS browser up with CDP on `127.0.0.1:9100`
  (NEEDS.md §3c).
- **Miami-Dade feed:** FTP credentials are the subscriber's and are not in
  the repo; stage the zips by hand. `MIAMI_DADE_API` in `.env` is the separate
  paid lookup API and is not used here.

## Limits & gotchas

- **Coverage is layered and partial.** Federal + appellate everywhere; trial
  courts only for TX, AK, CT, MD, PA (MDJ tier), Broward, Hillsborough and
  Miami-Dade. Absence of a case is absence *in these sources*; the
  per-state "not checked" footers stay in force
  ([DATA.md](DATA.md#coverage-honesty-rules)).
- **The CourtListener search API throttles anonymous use.** Measured
  2026-10-01: 5 requests a minute ("Request was throttled. Rate limit
  exceeded: 5/min"), and after roughly a hundred requests a `Retry-After` of
  40 minutes, then 23 hours. `get_courts` backs off and resumes, but a full
  anonymous sweep now takes days; a per-association-name search of even one
  state's roster does not finish. `get_courts_bulk` is the route that has no
  limit.
- **Bulk captions are not party lists.** `get_courts_bulk` sees only what is
  in the caption, names associations by rule (a truncated caption gives a
  truncated name: "SAILFISH POINT PROPERTY OWNERS'"; a one-word co-party is
  not cut off: "Strong and Whispering Pines Homeowners Association"), and lets through
  neighbourhood advocacy groups that call themselves a community or civic
  association. The snapshot is quarterly, so it is up to three months
  behind the API.
- **Name-match recall is unmeasured.** Every trial collector queries the
  spelling we hold (starts-with or leading-word on most portals); a caption
  spelled differently is missed.
- **Caps:** 500 rows (AK, Hillsborough — the latter splits by date), 600
  rows (Maryland, split by date; a single day still over the cap is logged
  and left short), 200 rows (Broward), ~1000 deep-paging rows and 200
  searches/hour (TX), 8,000 results per CourtListener query at the default
  `--max-pages`.
- **Maryland names communities by their governing body.** Condominiums
  litigate as "Council of Unit Owners of X Condominium", some HOAs as
  "Board of Directors of X"; the records keep the party as the court wrote
  it and `build_site.core_of` drops the body words (`COUNCIL`, `UNIT`,
  `BOARD`, `DIRECTORS`, `MANAGERS`) so the case attaches to the registered
  "X Condominium". A party that is only the bare form, or "Homeowners
  Association Inc" with no community name, is dropped — its caption may
  name the community, but the row does not. Disclaimer recorded 2026-09-30:
  no bar on automated use; only interference and record alteration are
  forbidden.
- **Deep links:** AK, Broward and Hillsborough have none; `url` is the search
  page and `docket_number` is the locator. re:SearchTX links require the
  reader's own login.
- **PA is the small-claims tier only**; Common Pleas civil is in each county
  prothonotary's system.
- **Broward eCaseView hiccups** left ~470 names unmarked in the 2026-09-03
  sweep; because the checkpoint is cleared on completion, retrying them is a
  full re-run (dedupes by case id) — NEEDS.md §3d.
- **Miami-Dade feed** is complete only for cases with docket activity since
  the feed started (2026-08-25) plus the county backfile; idle circuit cases
  stay missing until they move. Pull the dailies within the FTP's 30-day
  window.
- `get_courts.STATE_NAMES` is imported by `build_site`; keep it there.

## Tests

From TEST.md: `TestTxResearchParsing` (tag/entity cleaning, party
selection, record shaping, cross-reference and name-overlap rules, cookie
check, 429 → `QuotaError`, 401 → `PermissionError`, name loader),
`TestMiamiDadeCivilFeed` (association tiers, daily and indebtedness zips to
records, OCS link), `TestDataIntegrity.test_trial_court_files_have_docket_shape`
(every `trial_*.jsonl` has the shape `add_courts` folds in), `TestCourtFlags`
(trial vs federal vs appellate vs supreme labels, level stamping, the bulk files folded in once per case, ZIP/county carried onto cases,
Maryland governing-body forms attaching to the registered community),
`TestMdCaseSearchParsing` (query prefix and match core from a registry
name, what a name query and each sweep keep and drop, role merging, the
600-row date split, 403/404/429 answers, the driver's IRS names and
`SWEEPS`),
`TestVaGdcParsing` (search prefixes per level, the party gate on clerk
misspellings and look-alikes, civil-only court list, result and detail
parsing, one record per base case, narrowing a flooded prefix, redoing a
court after a session timeout, placeholder-only fixtures),
`TestOhSupremeParsing` (search phrase from a roster name and its `&`/`and`
twin, the same-community and sweep gates on real docket parties, the
supreme-court record shape, amicus and alias rows left out, the 1,000-row
date split and the per-case details cache),
`TestCourtListenerBulk` (bucket listing and the latest complete snapshot,
download ranges, the PostgreSQL CSV dialect, the association gate on
people / lenders / trade groups / rental townhomes / truncated captions,
caption splitting with roles and co-parties cut off, blocked rows skipped,
docket and opinion record shapes, the `coverage.json` counts, the
`sources.json` merge),
`TestPortalGuide.test_indexed_portals_name_collected_files` (`portals.json`
never claims an index that does not exist), `TestCaseLayer` (map labels and
per-community case summaries). CourtListener paging and the live portal
adapters are not unit-tested (network); the shape test and the built-data
checks cover their output.

## Related docs

- [DATA.md](DATA.md) — entity model, matching, flags, coverage honesty
- [LIENS.md](LIENS.md) — the lien side of the same foreclosure story
- MAP.md — case rings, case-type labels, ZIP placement
- SUBMISSIONS.md — members bringing back trial-court links from portals we cannot bulk-collect
- PIPELINE.md — adding a portal adapter to the collect → build → serve flow
- [REGISTRIES.md](REGISTRIES.md) — where the queried association names come from
- NEEDS.md — §1 (Miami-Dade folders), §3a (re:SearchTX), §3c (BrowserOS), §3d (portal survey)
- [STATES.md](STATES.md) and `states/<ST>.md` — per-state court counts and adapters
