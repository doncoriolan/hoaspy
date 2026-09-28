# Association registries — who the HOAs are

The collectors under `hoaspy/collect/registries/` answer the site's first
question: *which community associations exist, where, and in what standing?*
They pull state HOA/condo registries, corporate rosters, county/city HOA
inventories, the IRS tax-exempt roster and South Carolina's complaint reports
into `records/` (one row per association or complaint); most also record what
they collected in `coverage.json` ([DATA.md](DATA.md#master-tracking)).
`hoaspy/build/build_site.py` folds every file below into the entity index
([DATA.md](DATA.md#the-entity-model)). Lien and court collectors:
[LIENS.md](LIENS.md), [COURTS.md](COURTS.md).

## At a glance

| Collector (module) | States | Writes | Access | `coverage.json` key it updates |
| --- | --- | --- | --- | --- |
| `get_records` (+ `records_fl`, `records_tx`) | FL, TX | `records/associations.jsonl`, `.csv`, `sources.json` | anonymous HTTP | none (FL/TX `hoa_registry` entries are hand-maintained) |
| `records_sunbiz` (parsed by `build_site`) | FL | nothing — read at build time from `.cache/sunbiz/npcordata*.txt` | Sunbiz quarterly extract, downloaded by hand | none (FL `corporate` hand-maintained) |
| `get_states` (+ `records_socrata`, `records_tecuity`, `state_sources.yml`) | AK CO CT DC IA ID MS ND NY OH OR PA (corporate); VA MD (registries) | `records/state_corps.jsonl`, `records/state_registries.jsonl`, `records/corp_coverage.json` | anonymous APIs/CSVs; AK, MS, OH need a browser-downloaded file in `.cache/` | `corporate`, `hoa_registry` |
| `get_wa_ccfs` | WA | `state_corps.jsonl` (WA rows), `corp_coverage.json["WA"]` | BrowserOS Chromium over CDP (Turnstile-gated SPA) | `corporate` |
| `get_hi_registry` | HI | `state_registries.jsonl` | anonymous PDF download | `hoa_registry` |
| `get_ut_hoa` | UT | `state_registries.jsonl` | anonymous (one ajax endpoint) | `hoa_registry` |
| `get_wi_dfi` | WI | `state_registries.jsonl` | anonymous search page | `hoa_registry` |
| `get_nv_hoa` | NV (Clark County area only) | `state_registries.jsonl` | anonymous ArcGIS REST | `hoa_registry` (partial) |
| `get_az_adre` | AZ | `state_registries.jsonl` (AZ rows) | anonymous HTTP + `pdftotext` | `hoa_registry` (partial — developer disclosures) |
| `get_cook_condos` | IL (Cook County) | `records/cook_condos.jsonl` | anonymous Socrata API | `condo_buildings` (partial) |
| `get_gov_layers` (+ `gov_layers/<ST>.yml`) | 25 states | `records/gov_layers.jsonl` | anonymous ArcGIS / Socrata / CSV / shapefile | `local_inventory` (partial) |
| `get_irs_bmf` | all 50 + DC | `records/irs_exempt_orgs.jsonl` | anonymous CSV (cached) | `irs_exempt` |
| `get_ca_sos` (+ `ca_sos_browser.js`) | CA | `state_corps.jsonl` (CA rows), `records/ca_sos_raw.jsonl` | browser cookie + hourly Okta token, or the in-tab script | none (CA `corporate` hand-maintained) |
| `get_sc_complaints` | SC | `records/sc_complaints.jsonl` | anonymous XLSX | none (SC `complaints` hand-maintained) |

## Where the code lives

| File | Role |
| --- | --- |
| `hoaspy/collect/registries/get_records.py`, `records_fl.py`, `records_tx.py` | FL DBPR condo extracts + TX TREC certificates → `associations.jsonl` |
| `hoaspy/collect/registries/records_sunbiz.py` | fixed-width parser for the Sunbiz non-profit extract; `ASSOCIATION_NAME_RE` (the name gate CA reuses) |
| `hoaspy/collect/registries/get_states.py`, `records_socrata.py`, `records_tecuity.py` | config-driven state collector; owns `ASSOC_RE`, `write_merged()`, `update_coverage()` that the single-state collectors import |
| `hoaspy/collect/registries/get_wa_ccfs.py` | WA CCFS advanced search driven through BrowserOS |
| `hoaspy/collect/registries/get_hi_registry.py`, `get_ut_hoa.py`, `get_wi_dfi.py`, `get_nv_hoa.py` | single-state HOA registries → `state_registries.jsonl` |
| `hoaspy/collect/registries/get_gov_layers.py` | county/city inventories mapped through `gov_layers/<ST>.yml` |
| `hoaspy/collect/registries/get_irs_bmf.py` | IRS Exempt Organizations Business Master File |
| `hoaspy/collect/registries/get_ca_sos.py`, `ca_sos_browser.js` | CA bizfile business-search sweep (Python, checkpointed) and the same sweep inside the browser tab |
| `hoaspy/collect/registries/get_sc_complaints.py` | SC Dept of Consumer Affairs annual complaint workbooks |
| `state_sources.yml`, `gov_layers/<ST>.yml` (repo root) | per-state plans for `get_states` and `get_gov_layers` |

## Shared mechanics

- **Merged per-state output.** `get_states.write_merged(path, rows, states)`
  keeps every row whose state is *not* being replaced, appends the fresh rows
  and swaps the file in atomically. A re-run of one state never touches
  another's rows. `get_ca_sos.write_merged()` does the same for
  `state_corps.jsonl` and refuses to write an empty CA result.
- **Coverage bookkeeping.** `get_states.update_coverage(state, key, info)`
  writes `coverage.json["states"][ST]["collected"][key]`, bumps a
  `pending`/`researching` state to `collected` and stamps `updated_at`. The
  collectors marked "hand-maintained" above never call it — edit
  `coverage.json` yourself after running them.
- **Name gate.** `get_states.ASSOC_RE` (HOMEOWNER, CONDOMINIUM, CONDO,
  PROPERTY OWNERS, COMMUNITY/MASTER/OWNERS/RESIDENTS ASSOCIATION, TOWNHOME,
  TOWNHOUSE, HOA) filters *corporate* rows; registry rows are kept whole.
- **No contact details.** UT, WI, NV, WA and the gov-layer collectors keep
  names and roles but never phone numbers or e-mail addresses;
  `get_gov_layers.to_rows()` raises if a YAML maps a column matching
  `phone|tel|email|e_mail|mail_addr|cell|fax`.
- **`--no-upload`.** Only `get_records` can push to S3 (`hoaspy/lib/s3_sync.py`,
  [S3.md](S3.md)); the others accept the flag as a no-op. Project rule:
  collectors run with `--no-upload`; the mirror is pushed explicitly.

## Collectors

### `get_records` — Florida DBPR condos + Texas TREC certificates

`records_fl` downloads the five regional condo extracts (`Condo_NF.csv`,
`condo_CE.csv`, `Condo_CW.csv`, `Condo_MD.csv`, `condo_PB.csv`) from
`https://www2.myfloridalicense.com/sto/file_download/extracts/`; each row is
one condominium *project* with address, county, units, `Primary Status` and
the compliance-bearing `Secondary Status` (e.g. `Delinquent`) plus the
managing entity. `records_tx` pages `https://data.texas.gov/resource/8auc-hzdi.json`
(5,000 rows per page): name, county, city, zip and the recorded certificate
PDF; associations under 60 lots are exempt from filing. `record_id()` hashes
`state|source|record_id-or-NAME|zip` to a 16-hex id (idempotent re-runs,
region-boundary duplicates collapse). Flags: `--state FL|TX` (repeatable),
`--county`, `--name-contains`, `--status`, `--with-address`, `--out`,
`--prefix`, `--no-upload`, `--force`, `--timeout`, `-v`.

### `records_sunbiz` — Florida non-profit corporations

Not a collector: a parser `build_site.add_corps()` runs over
`.cache/sunbiz/npcordata*.txt`, the fixed-width (1,440-char) quarterly
non-profit extract from the Division of Corporations
(`https://dos.fl.gov/sunbiz/other-services/data-downloads/quarterly-data/`),
downloaded by hand. Every row yields `corp_number`, `name`, `corp_status`,
`filing_type`, address, `incorporated`, `last_activity`, `registered_agent`
and up to six `officers`. The extract holds **active** corporations only,
which is why FL corporate coverage is `active_only`: a registered condo with
no active corporation is flagged "no active Florida corporation found"
([DATA.md](DATA.md#flags-things-to-watch-out-for-and-score)).

### `get_states` — every state in `state_sources.yml`

One YAML list per state; each entry has a `kind`:

| kind | Fetcher | Output | Notes |
| --- | --- | --- | --- |
| `socrata_corp` | `records_socrata.fetch()` | `state_corps.jsonl` | SoQL `$where upper(name) like '%HOMEOWNER%' OR …` keeps the filter server-side; optional `where`, `app_token`, `dedupe` |
| `socrata_registry` | `records_socrata.fetch(filter_names=…)` | `state_registries.jsonl` | rows kept whole unless `filter_names: true` |
| `tecuity_corp` | `records_tecuity.fetch()` | `state_corps.jsonl` | ID SOSBiz / ND FirstStop: union of keyword queries, each capped at 500 rows → always `partial` |
| `arcgis_corp` | `get_states.fetch_arcgis()` | `state_corps.jsonl` | DC: FeatureServer with a server-side `LIKE` filter, 2,000-row paging |
| `csv_corp` | `get_states.fetch_csv()` | `state_corps.jsonl` | direct download or a `local_cache` / `local_glob` file; `zip`, `xlsx`, `encoding`, `delimiter`, `exclude_status`, `dedupe`, `constants`, `officials` |
| `csv_registry` | `get_states.fetch_csv(registry=True)` | `state_registries.jsonl` | same options, no name filter |

Entry keys: `source` (label), `page` (human URL → `source_url`), `url` or
`domain`+`dataset`, `fields` (which column is `name`, `record_id`, `status`,
`address`, `city`, `county`, `zip`, `incorporated`, `agent`, `type`, `units`,
`manager`; a list joins columns), `coverage` (`all` | `active_only` |
`partial`), `local_cache` (downloaded by the script when a `url` is given —
IA, MD — otherwise placed there after a browser download: AK's
`CorporationsDownload.csv`, `Mississippi.xlsx`, monthly OH reports under
`.cache/oh/`), and `officials` (a companion officers CSV keyed by parent
record id — Alaska — merged into `officers: [{name, title}]`).

`--state CO NV` limits the run. Every run rewrites `records/corp_coverage.json`
from the YAML: `{ST: "all" | "active_only" | "partial"}`.
`build_site.add_corps()` reads it as: `active_only` → a missing corporation
implies dissolution (absence flag allowed); `all` → a status column exists, so
a bad status is direct evidence; `partial` → never infer from absence. **WA is
not in the YAML**, so a full run drops the WA key — rerun `get_wa_ccfs` after.

### `get_wa_ccfs` — Washington CCFS through BrowserOS

Every CCFS API call needs a Cloudflare Turnstile token minted by the SPA
itself, so the collector drives the real local BrowserOS Chromium over CDP at
`http://127.0.0.1:9100` in a throw-away context: it types each term of the
association vocabulary into the advanced-search form, clicks Search and pages
the results (25 per page) through the page's own scope method. Only list rows
are read: name, UBI (`record_id`), CCFS `business_id`, type, status, formation
date, agent, principal-office street/city/zip; phone, e-mail and EIN fields
are never read. Flags: `--limit N` (list pages), `--pace` (1.5 s), `--terms`,
`--fresh`, `--finalize-only` (rebuild outputs from the checkpoint), `--no-upload`.
Checkpoint `.cache/wa/ccfs_rows.jsonl` + `ccfs_progress.json` (resumes at the
next page); tokens refresh every 240 s; a "System verification in progress"
refusal backs off 60 s → 30 min. README quotes about an hour.

### `get_hi_registry` — Hawaii AOUO contact list (PDF)

Downloads the DCCA Real Estate Branch AOUO contact-list PDF once into
`.cache/hi_aouo.pdf` (URL pinned in the module — update it when DCCA posts a
new list) and reads each page's table with `pdfplumber`. Rows carry
`record_id`, `name`, `status: "registered AOUO"`, `manager_name` and the
listed officer as `registered_agent` ("Title: Name"); the list's address is
the officer's, not the property's, so `address`/`city`/`zip` stay empty.

### `get_ut_hoa` — Utah HOA Registry

Registration is mandatory (Utah Code 57-8a-105 / 57-8-13.1). One endpoint,
`assets/js/hoa-ajax.php`, backs the public search: `f=s&v=%` lists the whole
registry (`v` is a SQL LIKE pattern), then `f=d&v=<pid>` fetches each detail
card. Rows: `record_id` (registration # or pid), `name`, `dba`, `status`
("<status> — <type>"), `status_detail` ("expires …"), `address`, `city`,
`county`, `zip` (84xxx parsed out of the address), `manager_name`, `officers`
(president + board members), `registration_type`. The state's "DTS Smoke
Test" row is skipped. Flags: `--pace` (0.7 s per worker), `--workers` (3),
`--limit`, `--fresh`, `--no-upload`. Checkpoint `.cache/ut/hoa_details.jsonl`;
README quotes about two hours.

### `get_wi_dfi` — Wisconsin DFI HOA public notices

Since 2022 every Wisconsin HOA must file a public notice (Wis. Stat. 710.085);
condominiums under ch. 703 are not covered. DFI publishes only a search page,
so the sweep asks each of the 72 counties for every word prefix `a..z`/`0..9`,
dedupes on the HOA number, splits any page that hits the 500-row cap by
appending one more character, and paces one request per second (never under
0.7 s; bursts return empty pages, retried). Rows: `record_id`
(`HOA#####`), `name`, `status: "Registered — HOA public notice"`,
`status_detail` ("planned community in …"), `city`, `county`, `manager_name`
(kept only when it reads as a company). No addresses exist in the source.
Flags: `--county Dane Brown`, `--limit`, `--fresh`, `--pace`, `--no-upload`.
Checkpoint `.cache/wi/dfi_counties.jsonl`; the docstring quotes ~2.5 h.

### `get_nv_hoa` — Nevada from local GIS layers

NRED registers every association but publishes no roster, so the collector
reads three public ArcGIS layers: Clark County `Clark_County_HOA/FeatureServer/0`
(NRED's own schema: type, units, Secretary of State file number, mailing
address), Henderson `HOAs/MapServer/1` and `/0` (masters), Las Vegas
`CLV_NeighAreas/MapServer/0`. `merge()` collapses rows across layers on the
SoS file number, else the normalized name, keeping the richer row and listing
the other source in `also_listed_by`. Washoe/Reno/Carson City publish nothing,
so coverage is partial. Flags: `--pace`, `--no-upload`.

### `get_az_adre` — Arizona from ADRE subdivision Public Reports

Arizona has no HOA registry and its corporate search is captcha-gated, but
the Department of Real Estate publishes every subdivision *Public Report*
(A.R.S. 32-2183) — the buyer disclosure whose "Property Owners'
Associations" section names the HOA the purchaser will belong to and its
regular assessment. The collector reads the bulk registrations CSV
(`List/DownloadList/4`), opens each `DMyy-0<id>` registration's detail card
(`Development/ViewDevelopment/<id>`: legal/marketing name, dates, type,
status, county, developer), POSTs the card's anti-forgery token to
`DownloadPublicReport` for the PDF, runs `pdftotext -layout`, and parses:

- the association name — from the two-column `Name of the HOA: …
  Current Assessments: …` form in newer reports (the name wraps under the
  assessment column, so the columns are split at the form's x-position), or
  the older "Purchaser will belong to X" sentence, or, failing both, a
  proper-cased "… Association" name repeated in the text; generic phrases
  ("the Association", "Homeowners Association") are dropped;
- the regular assessment (`$84.00/month`), the lot count ("divided into 395
  Lots"), the subdivision's town (from "Town/City of X" in the location
  section) and the town's ZIP — the most common "X, Arizona 85xxx" in the
  report, i.e. the local-services addresses: a town-level placement.

Reports issued before `--since` (default 2005) and pre-2002 numeric
registration numbers (no id mapping, no structured section) are skipped.
One row per association; phased subdivisions naming the same HOA merge,
with every registration listed under `subdivisions`, plus `developer` and
`assessment` (shown on the report). Broker and developer contact details are
never stored. Resumable: `.cache/az_adre/details.jsonl` is the checkpoint
and `.cache/az_adre/text/<id>.txt.gz` the text cache; `--finalize-only`
re-parses the cache after a parser change without refetching.

### `get_cook_condos` — Cook County, IL condominium buildings

The Cook County Assessor classifies every residential condominium unit
(class 299) and publishes the parcels with coordinates. Grouping the unit
PINs by their 10-digit building PIN gives every condo building in the
county: the Parcel Universe (`pabr-t5kh`, class 299 → pin, pin10, ZIP,
lat/lon, municipality), the unit-characteristics table (`3r7i-mrz4` → year
built, parking/common PIN count per building) and the parcel-address table
(`3723-97qp` → the street address of one unit per building, unit designator
stripped). The data carries no association names, so each building is
listed as "<street address> Condominium" with `units` = unit PINs minus
parking/common PINs — an inventory of where condominium associations exist,
not who runs them. Owner and mailing names on the address table are never
read. Output `records/cook_condos.jsonl`, folded by `build_site` as a county
inventory (the state is marked `local_inventory`, not registry-covered).

### `get_gov_layers` — county/city inventories in 25 states

For states with no statewide registry, `gov_layers/<ST>.yml` lists ArcGIS
layers, Socrata datasets and CSV/XLSX/shapefile downloads that carry
association names: statewide parcel layers with owner names (MA, NC, WI, WV,
VT, MT), county condo-plat indexes (Oakland MI, Dane/Milwaukee WI, DuPage IL…),
city HOA/neighborhood registries (Irvine, Wichita, OKC, Lexington…). YAML keys
(documented in the module docstring): `kind`, `source`, `page`, `url`, `dbf`,
`where` (a string, or a list of filters fetched one after another when a
service times out on big queries), `fields`, `constants`, `name_filter`,
`name_regex`, `exclude_name`, `group_by_name`. Names are upper-cased; parcel
rows sharing a normalized name collapse to one association with a `parcels`
count; the same name in two different cities stays two associations. Flags:
`--state MA MI`, `--list`, `--pace`, `--no-upload`. Cache `.cache/gov_layers/`.
`coverage.json` gets `local_inventory` with `coverage: partial` and a note
that absence means nothing.

### `get_irs_bmf` — IRS Exempt Organizations Business Master File

Downloads `eo1.csv`, `eo2.csv`, `eo3.csv` from `https://www.irs.gov/pub/irs-soi/`
into `.cache/irs/` when missing and keeps rows whose NTEE code starts with
`L50` ("Homeowners Associations") or whose `NAME`/`SORT_NAME` passes
`ASSOC_RE` (when only `SORT_NAME` matches it becomes the name — `NAME` is
often the management agent). One row per EIN: `record_id`/`ein`, `status`
("501(c)(4) exempt" …), `status_detail` ("IRS ruling YYYY-MM"),
`recorded_date`, address, `in_care_of`, `ntee`, `subsection`, `tax_period`,
`income_amt`, `asset_amt`. Most HOAs file Form 1120-H and never appear, so
this is a floor, not a census. Flags: `--state OK WY`, `--no-upload`.

### `get_ca_sos` — California from the SoS bizfile search

California has no HOA registry, but every common-interest development is on
file with the Secretary of State under three filing types (69 mutual-benefit
CID corporation, 62 unincorporated CID, 72 public-benefit CID). Rows under
those types are HOAs by definition and carry `is_association: true`, so
`build_site.add_corps` keeps them without an HOA keyword; every other entity
type must pass `records_sunbiz.ASSOCIATION_NAME_RE`. The API
(`POST https://bizfileonline.sos.ca.gov/api/Records/businesssearch`) caps every
query at 500 rows with no paging, so capped (term, type, filing-date) windows
are bisected by date; search is word-based, so three phases sweep the CID
types × a broad word list, all types × HOA keywords, then mined name words.

- **Auth.** The browser's Imperva cookies *and* the site's hourly Okta bearer
  token, read from `ca_cookie.txt` / `ca_token.txt` at the repo root (both
  git-ignored). On 401/403 the run pauses, polls those files for a newer mtime
  (up to `--wait-minutes`, 90) and resumes from its checkpoint. Cookies are
  bound to the machine/network that produced them — NEEDS.md §3b.
- **Alternative.** Paste `ca_sos_browser.js` into the DevTools console of the
  logged-in search tab; it runs the identical sweep with the page's own
  `fetch()`, saves `~/Downloads/ca_sos_<stamp>_partN.jsonl`, and
  `--ingest ~/Downloads/ca_sos_*.jsonl` shapes those rows with no network.
- **Flags.** `--cookie-file`, `--token-file`, `--phase cid|named|expand|all`,
  `--keywords`, `--filing-types`, `--expand N` (150), `--pace` (1.0 s,
  jittered), `--wait-minutes`, `--max-requests`, `--date-min`, `--out`,
  `--raw`, `--ingest FILE…`, `--fresh`, `--no-upload`, `-v`.
- **Outputs.** CA rows in `state_corps.jsonl` (`record_id` = filing number,
  `bizfile_id`, `corp_status`, `standing`, `incorporated`, `registered_agent`,
  `entity_type`, `is_association`; no address, county or zip) and the raw API
  rows in `records/ca_sos_raw.jsonl`. Checkpoints are dot-files in `records/`:
  `.ca_sos_done.txt`, `.ca_sos_partial.jsonl`, `.ca_sos_words.json`.

### `get_sc_complaints` — South Carolina HOA complaints

S.C. Code 37-6-117 makes the Department of Consumer Affairs report HOA
complaints annually. The collector scrapes every `.xlsx` link off
`https://consumer.sc.gov/HOA-reports` (new years appear automatically), matches
headers loosely, dedupes on complaint number + name across adjacent reports,
and writes `records/sc_complaints.jsonl`: `state`,
`source: "sc-dca-hoa-complaints"`, `source_url`, `complaint_number`, `date`,
`year`, `name`, `city`, `county`, `manager_name`, `category`, `status`,
`retrieved_at`. No flags.

## Run it

From the repo root; every collector caches under `.cache/` and is safe to re-run.
```bash
./venv/bin/python -m hoaspy.collect.registries.get_records --no-upload   # FL + TX
./venv/bin/python -m hoaspy.collect.registries.get_states                # every state in state_sources.yml
./venv/bin/python -m hoaspy.collect.registries.get_wa_ccfs               # WA via BrowserOS (resumable)
./venv/bin/python -m hoaspy.collect.registries.get_hi_registry
./venv/bin/python -m hoaspy.collect.registries.get_ut_hoa                # resumable
./venv/bin/python -m hoaspy.collect.registries.get_wi_dfi                # resumable
./venv/bin/python -m hoaspy.collect.registries.get_nv_hoa
./venv/bin/python -m hoaspy.collect.registries.get_az_adre              # resumable; --since 2005, needs pdftotext
./venv/bin/python -m hoaspy.collect.registries.get_cook_condos
./venv/bin/python -m hoaspy.collect.registries.get_gov_layers            # all 25 states
./venv/bin/python -m hoaspy.collect.registries.get_irs_bmf               # all states
./venv/bin/python -m hoaspy.collect.registries.get_ca_sos --no-upload    # needs ca_cookie.txt + ca_token.txt
./venv/bin/python -m hoaspy.collect.registries.get_sc_complaints
./venv/bin/python -m hoaspy.build.build_site                             # then rebuild the site
```

Order matters once: `get_states` rewrites `records/corp_coverage.json`
without WA, so run `get_wa_ccfs` (or `get_wa_ccfs --finalize-only`) after it.

## Inputs & outputs

| File | Written by | One row per | Row shape |
| --- | --- | --- | --- |
| `records/associations.jsonl` / `.csv` | `get_records` | FL condo project or TX certificate | `id`, `state`, `source`, `source_region`, `source_url`, `record_id`, `file_number`, `name`, `type`, `county`, `address`, `city`, `zip`, `address_raw`, `units`, `recorded_date`, `status`, `status_detail`, `manager_*`, `document_url`, `retrieved_at` (the CSV keeps the `FIELDS` subset) |
| `records/sources.json` | `get_records` | run | per-state source, page, files/endpoint, rows; totals, filters, duration |
| `records/state_corps.jsonl` | `get_states`, `get_wa_ccfs`, `get_ca_sos` | association-named corporation | `state`, `source`, `source_url`, `record_id`, `name`, `corp_status`, `address`, `city`, `county`, `zip`, `incorporated`, `registered_agent`, `entity_type`, `officers`; WA adds `business_id`, CA adds `bizfile_id`, `standing`, `is_association` |
| `records/state_registries.jsonl` | `get_states` (VA, MD), `get_hi_registry`, `get_ut_hoa`, `get_wi_dfi`, `get_nv_hoa`, `get_az_adre` | registered association | `state`, `source`, `source_url`, `record_id`, `name`, `status`, `status_detail`, `recorded_date`, `address`, `city`, `county`, `zip`, `units`, `manager_name`, plus per-source extras (`officers`, `dba`, `registration_type`, `sos_file_number`, `also_listed_by`) |
| `records/corp_coverage.json` | `get_states` (+ `get_wa_ccfs` for WA) | state | `"all"` / `"active_only"` / `"partial"` |
| `records/gov_layers.jsonl` | `get_gov_layers` | association from a local layer | registry shape + `parcels`, `layer_kind`, `also_listed_by` |
| `records/cook_condos.jsonl` | `get_cook_condos` | Cook County condo building | registry shape + `pin10`, `year_built`, `lat`, `lon` |
| `records/irs_exempt_orgs.jsonl` | `get_irs_bmf` | EIN | registry shape + `ein`, `in_care_of`, `ntee`, `subsection`, `tax_period`, `income_amt`, `asset_amt` |
| `records/ca_sos_raw.jsonl` | `get_ca_sos` | raw bizfile API row | as returned, keyed by bizfile `ID` |
| `records/sc_complaints.jsonl` | `get_sc_complaints` | complaint | see above |
| `.cache/sunbiz/npcordata*.txt` | you (manual download) | FL non-profit corporation | fixed-width, parsed by `records_sunbiz.parse_all()` |
| `coverage.json` (repo root) | `update_coverage()` callers + hand edits | state | `collected`, `available`, `unavailable`, `counties`, `notes`, `status` |

`records/`, `.cache/` and the cookie/token files are git-ignored; `records/`
is mirrored to S3 by `scripts/s3_push_all.sh` ([S3.md](S3.md)).

## Configuration & secrets

- `state_sources.yml` — the `get_states` plan. Adding a state with a Socrata,
  ArcGIS, Tecuity or CSV/XLSX source is usually just a new entry; sources that
  block scripts are downloaded in a browser and pointed to with `local_cache`.
- `gov_layers/<ST>.yml` — the `get_gov_layers` plan; skipped layers and why
  are commented inside each file.
- `ca_cookie.txt`, `ca_token.txt` — CA bizfile credentials (git-ignored via
  `*_cookie.txt` / `*_token.txt`; machine-bound; token expires hourly).
- BrowserOS with CDP on `127.0.0.1:9100` — `get_wa_ccfs` (NEEDS.md §3c).
- `.env` (`ACCESSKEYID` / `SECRETACCESSKEY`) + `config.yml` `s3:` — only
  `get_records` without `--no-upload`.

## Limits & gotchas

- **Registries are not censuses.** DBPR registers condos, barely HOAs; TREC
  exempts associations under 60 lots; WI covers post-2022 HOA filings and no
  condos; NV covers the Clark County area only; the IRS file lists only
  associations with a 501(c) ruling; local inventories cover only the
  governments that publish one. `coverage.json` carries `partial` /
  `active_only` markers and [DATA.md](DATA.md#coverage-honesty-rules) says how
  the site phrases absence.
- **Absence flags need `active_only`.** Only FL (Sunbiz) and the
  `active_only` states in `corp_coverage.json` may say "no active corporation
  found"; `partial` extracts (ID, ND, OH) never support that inference.
- **CA rows have no location** (name, status, standing, date, agent only), so
  CA communities have no county filter and are absent from the map
  (NEEDS.md §0a).
- **Two VA registry sources share one coverage key.** Both VA `csv_registry`
  entries call `update_coverage(…, "hoa_registry", …)`, so `coverage.json`
  records only the last one's count.
- **Rate limits.** WI needs ≥ 0.7 s between requests, Tecuity sleeps 2 s per
  keyword, CA paces 1.0 s with jitter, WA backs off on verification refusals.

## Tests

Collectors hit live services and are not run by the suite
(TEST.md); parsing and row shaping are
covered on fixtures under `tests/fixtures/`: `TestCaSosParsing`,
`TestUtahRegistryParsing`, `TestWiDfiParsing`, `TestWaCcfsParsing`,
`TestNevadaLayersParsing`, `TestGovLayersMapping`, `TestStateSourceConstants`,
`TestDataIntegrity` (IRS rows, AK officers reaching the built site).

## Related docs

PIPELINE.md (order, adding a source) · [DATA.md](DATA.md)
(entity model, flags, coverage rules) · [STATES.md](STATES.md) and
`states/<ST>.md` (per-state counts) · [LIENS.md](LIENS.md), [COURTS.md](COURTS.md)
· NEEDS.md §3b/§3c/§3e · [S3.md](S3.md) · README
