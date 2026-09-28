# County lien indexes (`liens/`)

The lien collectors answer one question per community: *what has this
association recorded against its owners, and has any of it escalated to
foreclosure?* Four sources feed `liens/`: two Florida county recorder indexes
(Broward, Miami-Dade), New York City's ACRIS, and California's statewide
Secretary of State judgment-lien index. All four write the **same record
shape**, so `hoaspy/build/build_site.py` ingests them with one function
(`Builder.add_liens`) and the report's lien block, the per-unit lien-rate
flag and the map's severity ramp work identically everywhere. Nothing is
re-hosted: every record links back to the index it came from.

| Collector | Jurisdiction | Source | Access | Output |
| --- | --- | --- | --- | --- |
| `get_liens` + `records_broward` | Broward County, FL | Official Records yearly index exports, 1978–present | public SFTP (published credentials) | `liens/liens.jsonl`, `liens.csv`, `by_association.csv`, `sources.json` (`broward`) |
| `get_miamidade_liens` + `records_miamidade` | Miami-Dade County, FL | Clerk's Official Records public search API | anonymous `.PremierIDDade` cookie, per-association queries | `liens/liens_miamidade.jsonl`, `liens_miamidade.csv`, `sources.json` (`miami_dade`) |
| `get_nyc_liens` + `records_acris` | New York City (5 boroughs) | ACRIS master/parties/legals on NYC Open Data (Socrata) | anonymous | `liens/liens_nyc.jsonl`, `sources_nyc.json` |
| `get_ca_ucc` | California, statewide | SOS bizfile UCC search, record type 2154 (Judgment Lien) | browser cookie + hourly token (same as `get_ca_sos`) | `liens/liens_ca_ucc.jsonl`, `sources_ca_ucc.json`, `ca_ucc_raw.jsonl` |

Record counts and years per source live in `coverage.json`
(`states.FL.counties`, `states.NY.counties`, `states.CA.collected.judgment_liens`)
and are rendered into [STATES.md](STATES.md) and `states/<ST>.md`.

## Where the code lives

| File | Role |
| --- | --- |
| `hoaspy/collect/liens/get_liens.py` | Broward driver: year parsing, `--min-liens`, outputs, per-association rollup, optional S3 push |
| `hoaspy/collect/liens/records_broward.py` | SFTP download + cache, the three-file join per year, `LIEN_TYPES`, `ASSOCIATION_RE` (shared by `get_courts`) |
| `hoaspy/collect/liens/get_miamidade_liens.py` | Miami-Dade driver: name loading from `records/associations.jsonl`, `query_name()`, per-name checkpoint, merged `sources.json` |
| `hoaspy/collect/liens/records_miamidade.py` | The Clerk's two-call API (`standardsearch` → `qs` blob → `getStandardRecords`), party collapse per CFN, `search_party` (primary) and `enumerate_window` (secondary) |
| `hoaspy/collect/liens/get_nyc_liens.py` | NYC driver: one run, atomic write, `sources_nyc.json` |
| `hoaspy/collect/liens/records_acris.py` | Socrata paging of LOCC/TOLCC documents, batched joins to parties and legals, borough mapping |
| `hoaspy/collect/liens/get_ca_ucc.py` | CA judgment-lien sweep: keyword × record-type, paging-or-bisection, association gate, `JL`/`JLX` shaping; reuses `Client`, `Checkpoint`, `Sweeper` from `hoaspy/collect/registries/get_ca_sos.py` |
| `hoaspy/build/build_site.py` → `Builder.add_liens` | Consumer: groups records by (state, association), matches to entities, builds the per-community lien block |

## The shared record shape

Every `liens/*.jsonl` line is one recorded document. Fields common to all four
sources (set in `records_broward.parse_year`, `records_miamidade.to_record`,
`records_acris.fetch`, `get_ca_ucc.to_record`):

| Field | Meaning |
| --- | --- |
| `doc_id` | the index's own document id (Broward doc id, Miami-Dade clerk file number, ACRIS document id, SOS record number) |
| `doc_type`, `doc_type_label` | short code + label, see the vocabulary below |
| `recorded_date`, `recorded_ymd`, `year` | recording date as displayed, as a sortable string (`YYYYMMDD` for Broward/NYC, ISO for Miami-Dade), and the year |
| `state`, `county` | `FL`/`Broward`, `FL`/`Miami-Dade`, `NY`/borough name, `CA`/`""` (statewide index) |
| `association` | the association-shaped party (filer first, then respondent side) — the join key for the site |
| `filers`, `respondents`, `n_parties` | direct (D) and reverse (R) parties as indexed |
| `amount`, `case_number` | as indexed (often blank) |
| `parcel_id`, `legal_description`, `property_address` | present where the index carries them (see per-source notes) |
| `source`, `source_page` | provenance slug + the public page the record links to |
| `retrieved_at` | ISO timestamp of the run |

Per-source extras: Miami-Dade adds `cfn`, `query_name`, `book_page`,
`subdivision`, `address`; CA adds `bizfile_id`, `city`, `lapse_date`,
`status`, `record_type`, `hoa_role` (`creditor` or `debtor`).

**Document-type vocabulary** (`doc_type`): the collectors normalise every
index onto Broward's short codes so one report logic serves all of them.

| Code | Label | Where | Counted by the site as |
| --- | --- | --- | --- |
| `LIE`, `LIEX` | claim of lien, amended claim | Broward, Miami-Dade | liens |
| `LOCC` | lien of common charges | NYC | liens |
| `JL` | judgment lien held by the association | CA | liens |
| `PALIE`, `SPALIE` | partial lien, its satisfaction | Broward | other |
| `NCL` | notice of contest of lien | Broward, Miami-Dade | contested |
| `LP` | lis pendens (escalation to foreclosure) | Broward, Miami-Dade | lis pendens |
| `TOLCC` | termination of a common-charges lien | NYC | other |
| `JLX` | judgment lien **against** the association | CA | other (shown apart) |
| `RST`, `CFJ`, `FJ` / `CLP`, `FTL`, `NTL`, `SJU` | releases, judgments, tax liens | Broward / Miami-Dade, opt-in via `--types` | other |

The slotting is the `{"LIE": 0, "LIEX": 0, "LOCC": 0, "JL": 0, "LP": 1, "NCL": 2}`
table in `Builder.add_liens`; anything else lands in `other`.

## Broward County — `get_liens` + `records_broward`

Broward publishes its whole Official Records index as pipe-delimited yearly
exports on a public SFTP server (`BCFTP.Broward.org`, credentials published
by the Records, Taxes and Treasury Division). Per year the collector downloads
three files into `.cache/broward/` (sequential reads — the server refuses
paramiko's parallel prefetch) and joins them on the document id:

- `CY<year>doc-rec.txt` — one row per document: id, date, type, amount, case number
- `CY<year>nme-rec.txt` — one row per party with role `D` (filer) or `R` (respondent)
- `CY<year>lgl-rec.txt` — legal description and parcel id, **sparse for liens**

`parse_year` keeps only the wanted document types (`DEFAULT_TYPES`: LIE,
LIEX, PALIE, SPALIE, NCL, LP), then keeps a document only when one of its
parties matches `ASSOCIATION_RE` (filer side first, respondent side second)
unless `--all-parties` is passed. The index has no category field, so this
regex is the whole HOA-vs-construction/tax-lien separation. `--min-liens N`
drops associations with fewer than N filings; `--years` takes `2025`,
`2015-2025` or a comma list. Besides `liens.jsonl` and `liens.csv`, the
driver writes `by_association.csv` (liens / lis pendens / contested / other
per association with first and last year) and `sources.json` with the
years, types and the parcel-id caveat. Re-runs are cheap: cached year files
are reused. `--no-upload` skips the S3 push (`hoa-liens/` prefix), which is
the standing rule — see [S3.md](S3.md).

**Caveat:** measured on CY2025, `lgl-rec` carries a parcel id for 100% of
deeds but 0.5% of liens, so Broward records identify parties and dates, not
property addresses. The daily `Official_Records_Download` folder is named in
the adapter but not used.

## Miami-Dade County — `get_miamidade_liens` + `records_miamidade`

Miami-Dade sells no bulk index (the `$110/mo` Records FTP folder is the
complete source; decision in NEEDS.md §1). What is open is the
JSON API behind the public Official Records search: `POST
/api/home/standardsearch` mints an encrypted `qs` criteria blob, `GET
/api/SearchResults/getStandardRecords?qs=` returns rows. Three facts shape
the adapter, all documented in `records_miamidade.py`:

1. **Auth is one anonymous cookie**, `.PremierIDDade`, copied from a browser
   (DevTools → Application → Cookies). No login; the reCAPTCHA header the site
   sends is not validated. A stale cookie shows up as rows with null search
   criteria and is raised as `MiamiDadeError`, never mistaken for "no data".
2. **Results are hard-capped at 500 rows** with no enumeration paging.
3. **Every document comes back once per party**, so `_collapse` folds rows
   into one record per clerk file number and gathers all parties.

The driver therefore runs the **name-driven mode** (`search_party`): for each
association we already hold in `records/associations.jsonl` (FL rows in
Miami-Dade county, or `--all-florida`, or a `--names` file) it queries each
document type separately over 1978–today. A lone association's lifetime
filings sit far under the cap; if a type still caps for one name the log says
so. Names are shaped by `query_name()` (drop punctuation, `CONDOMINIUM`→`CONDO`,
compass words abbreviated) to match the Clerk's spelling. The date-window
`enumerate_window` mode exists for discovering associations not yet on the
list but reports truncated days rather than pretending to page past the cap.

Runs are hours long, so progress is checkpointed per name
(`liens/.miamidade_partial.jsonl`, `liens/.miamidade_done.txt`); rerun the
same command to resume, `--fresh` to start over. On success the checkpoint is
cleared and `sources.json` gets a `miami_dade` entry *merged* next to
Broward's (`write_outputs` migrates an old single-region file). Unlike
Broward, these rows usually carry the folio, legal description and street
address, so Miami-Dade liens **do** locate the property.

**Caveat:** coverage is best-effort by construction — bounded by name-spelling
agreement (`coverage.json` records a 53% hit rate) and the 500-row cap on a
few mass filers. The site's per-county "not checked" footers stay in force
([DATA.md — coverage honesty](DATA.md#coverage-honesty-rules)).

## New York City — `get_nyc_liens` + `records_acris`

A New York condominium board collecting unpaid common charges records a
*Lien of Common Charges* (`LOCC`) with the City Register, and a `TOLCC` when it
is terminated. ACRIS is on NYC Open Data as three joinable Socrata datasets:
master (`bnx9-e6tj`), parties (`636b-3b5g`), legals (`8h5j-fqxa`). The adapter
pages the master with `$where doc_type in('LOCC','TOLCC')` in 5,000-row
pages, then joins parties and legals in batches of 100 document ids, backing
off on 429. The filer is recognised by `FILER_RE` and the `BOARD OF MANAGERS
OF THE …` prefix is stripped to yield the condominium's name; rows without
an identifiable association are dropped. The property borough comes from the
legals (the master's `recorded_borough` is merely where it was filed), and
`county` is the borough name. The driver takes no arguments, writes
`liens_nyc.jsonl` atomically and `sources_nyc.json`, and never uploads.

Most NYC records carry a parcel (borough-block-lot) **and a street address**,
which is why NYC communities place well on the map.

## California judgment liens — `get_ca_ucc`

California has no HOA registry and no free county-recorder lien index, but
judgment liens against *personal property* are filed with the Secretary of
State (Code Civ. Proc. § 697.510) and indexed by the bizfile UCC search
(`POST /api/Records/uccsearch`, `RECORD_TYPE_ID` 2154). The search matches
debtor and secured-party names, so an HOA keyword finds both judgments the
association holds against owners (`hoa_role: creditor`, `doc_type: JL`,
counted with claims of lien) and judgments held against it (`debtor`, `JLX`,
shown apart). The sweep runs `KEYWORDS` (HOA, HOMEOWNERS, CONDOMINIUM, …)
against the record type; per term it pages by the server's `edge` if the
offset is honoured, else falls back to `get_ca_sos.Sweeper`'s filing-date
bisection (`sweep_term` reports `single`/`paged`/`bisected`).

Party lines arrive as `NAME - CITY, ST` with entity descriptors appended
(`, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION`, `DBA …`);
`parse_party`/`clean_party_name` cut them off. `is_association` tightens the
shared name gate for this index: `AN INDIVIDUAL` parties are people, a bare
leading `HOA <surname>` is a person (a common given name) unless it is a CA
common-interest-development corporation from `records/state_corps.jsonl`,
and trade words (MANAGEMENT, INSURANCE, ROOFING…) without an
ASSOCIATION/ASSN/HOA token mean a vendor. Auth, pacing, the pause-for-fresh-
credentials loop (`--wait-minutes`) and the checkpoint
(`liens/.ca_ucc_done.txt`, `.ca_ucc_partial.jsonl`, `.ca_ucc_words.json`) are
`get_ca_sos`'s — see [REGISTRIES.md](REGISTRIES.md) and
NEEDS.md §3b / §3b-2. `--probe` prints one keyword's paging edge
without writing. Real-property judgment liens (county recorder abstracts of
judgment) remain uncovered, and the records carry a city but no county or
address.

## Run it

```bash
# Broward: a decade of yearly exports, local only
./venv/bin/python -m hoaspy.collect.liens.get_liens --years 2015-2025 --no-upload
./venv/bin/python -m hoaspy.collect.liens.get_liens --years 2025 --all-parties --types LIE LP -v

# Miami-Dade: per-association queries with the anonymous browser cookie (resumable)
./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie '<.PremierIDDade value>' --no-upload
./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie ... --limit 200 --pace 0.4
./venv/bin/python -m hoaspy.collect.liens.get_miamidade_liens --cookie ... --names names.txt --fresh

# NYC ACRIS: no options
./venv/bin/python -m hoaspy.collect.liens.get_nyc_liens

# California judgment liens (cookie + token files from the CA SoS sweep)
./venv/bin/python -m hoaspy.collect.liens.get_ca_ucc --probe
./venv/bin/python -m hoaspy.collect.liens.get_ca_ucc --no-upload

# then rebuild the site and the map
./venv/bin/python -m hoaspy.build.build_site && ./venv/bin/python -m hoaspy.build.build_map
```

Every driver prints a summary (documents by type, most active filers, share
with a parcel id) and exits non-zero on a source failure without touching
the previous output, except `get_liens`, which rewrites its files from the
cached index each run.

## Inputs & outputs

| Path | Written by | Contents |
| --- | --- | --- |
| `.cache/broward/CY<year>{doc,nme,lgl}-rec.txt` | `records_broward.download` | cached SFTP exports (git-ignored, rebuildable) |
| `liens/liens.jsonl`, `liens/liens.csv` | `get_liens` | Broward records; the CSV flattens `respondents` to one column |
| `liens/by_association.csv` | `get_liens` | per-association rollup: liens, lis pendens, contested, other, first/last year |
| `liens/liens_miamidade.jsonl`, `.csv` | `get_miamidade_liens` | Miami-Dade records (`query_name` says which of our names found each) |
| `liens/liens_nyc.jsonl` | `get_nyc_liens` | NYC LOCC/TOLCC records |
| `liens/liens_ca_ucc.jsonl` | `get_ca_ucc` | CA judgment liens, newest first |
| `liens/ca_ucc_raw.jsonl` | `get_ca_ucc` (`Checkpoint`) | raw API rows keyed by bizfile id, for re-shaping without a re-sweep |
| `liens/sources.json` | Broward + Miami-Dade drivers | `{broward: {...}, miami_dade: {...}}` provenance: source, access, page, years/types, counts, `caveat` |
| `liens/sources_nyc.json`, `liens/sources_ca_ucc.json` | NYC and CA drivers | the same provenance shape, one file each |
| `liens/.miamidade_*`, `liens/.ca_ucc_*` | drivers | resume checkpoints; cleared when a run completes |

`liens/` is git-ignored collector output; the S3 mirror
(`scripts/s3_push_all.sh`, prefix `hoa-liens/`) holds the snapshots —
[S3.md](S3.md).

## How the site build uses them

`Builder.add_liens` reads the four files in one pass (missing files are
skipped), groups records by `(state, association)`, and matches each group to
an entity with the county of record as a hint (`_match`, conservative — see
[DATA.md — cross-source matching](DATA.md#cross-source-matching-deliberately-conservative));
an unmatched group creates an entity, so a lien-only community still gets a
report. The community's own ZIP outranks the recording county when deciding
where it is. Per entity it builds `liens`: `by_year` counts in the four slots,
`total_liens` / `lis_pendens` / `contested` / `other`, distinct respondents
and addresses, first/last year, the eight most recent documents (date, type,
amount, case number, address) and a `source_label` (county index, NYC ACRIS
or the CA SOS index). The flags built on this block — lien volume, foreclosure
escalation, contested liens, the ≥8%-of-units lien rate, timeshare context —
and the per-county percentile are catalogued in
[DATA.md — flags](DATA.md#flags-things-to-watch-out-for-and-score); the map's
colour ramp uses the recent-5y count (MAP.md).

## Configuration & secrets

- **Broward:** no secrets; the SFTP credentials are public constants in
  `records_broward.py`. `--config` (default `config.yml`) and `.env` are read
  only for the optional S3 push.
- **Miami-Dade:** `--cookie` is required and is the anonymous
  `.PremierIDDade` value; pass it on the command line or a wrapper, never
  commit it (`.gitignore` covers `*_cookie.txt`, `*.cookie`). `MIAMI_DADE_API`
  in `.env` belongs to the paid Commercial Data Services lookup API probed in
  NEEDS.md §1 — it is not used by this collector.
- **NYC:** none. The Socrata endpoints are anonymous (a `User-Agent` is set).
- **California:** `ca_cookie.txt` and `ca_token.txt` in the repo root (both
  git-ignored), refreshed from the browser while the sweep runs; `--pace`,
  `--wait-minutes`, `--max-requests` as in `get_ca_sos`. This network's IP has
  been blocked by bizfile's Imperva front before — see NEEDS.md §3b.
- **S3:** `--no-upload` is the standing rule for every collector; the mirror is
  pushed explicitly with `scripts/s3_push_all.sh`.

## Limits & gotchas

- **No liens shown ≠ no liens.** Only Broward, Miami-Dade, NYC and the CA SOS
  index are covered; an association elsewhere has simply not been checked.
  The site says so per county ([DATA.md](DATA.md#coverage-honesty-rules)).
- **Broward has no property addresses** for liens (0.5% parcel ids); NYC and
  Miami-Dade do. CA rows carry a city only.
- **Miami-Dade is partial by construction**: name-match recall plus the
  500-row cap; the complete source is the paid FTP folder (NEEDS.md §1).
- **The association regex is the classifier.** `records_broward.ASSOCIATION_RE`
  is copied verbatim into `records_miamidade.py` (to avoid importing paramiko
  there) and reused by `get_courts`; edit them together. Bank/GSE names that
  slip through are removed downstream by the financial-institution exclusion
  in `build_site` ([DATA.md](DATA.md#excluded-entities-financial-institutions)).
- **Timeshares** inflate lien counts (one resort files thousands); the report
  contextualises rather than hides them.
- **CA UCC `JLX`** (a judgment *against* the association) is never counted as
  a lien the association filed.
- `get_liens` rewrites `liens/sources.json` as a Broward-only file; run
  Miami-Dade afterwards so `write_outputs` merges the two entries back.

## Tests

From TEST.md: `TestMiamiDadeParsing` (dates, CFN collapse, record
shaping, cookie check, resume skipping), `TestMiamiDadeCheckpoint` (torn-line
recovery, clear on success), `TestCaUccParsing` (party splitting, the HOA
name gate, creditor/debtor roles, offset paging vs bisection),
`TestCountyNaming` (Dade/Miami-Dade share a key; ZIP beats recording county),
`TestFinancialExclusion` (GSE names never become communities). Broward and
NYC parsing have no unit tests (the suite's "collectors are not covered"
note); `TestDataIntegrity` checks the built output they feed.

## Related docs

- [DATA.md](DATA.md) — entity model, matching, the flag catalog, coverage honesty
- [COURTS.md](COURTS.md) — the escalation side: foreclosure suits in county court
- MAP.md — how lien counts become pin colours
- PIPELINE.md — where liens sit in collect → build → serve, adding a county
- [REGISTRIES.md](REGISTRIES.md) — `get_ca_sos`, whose client `get_ca_ucc` reuses
- [S3.md](S3.md) — the `hoa-liens/` mirror
- NEEDS.md — §0a (per-state gap map), §1 (Miami-Dade purchases), §3b-2 (CA UCC)
- [STATES.md](STATES.md), `states/FL.md`, `states/NY.md`, `states/CA.md` — counts per jurisdiction
