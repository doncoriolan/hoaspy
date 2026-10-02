# News index (GDELT) — retired

`hoaspy/collect/news/get_news.py` queried the GDELT DOC 2.0 API for news naming
Florida and Texas associations. **Dropped on 2026-09-07** (NEEDS.md
§4): the collector stays in the tree, is not part of the pipeline, but
`hoaspy/build/build_site.py` still folds `news/articles.jsonl` in when it exists.

## Where the code lives

| File | Role |
| --- | --- |
| `hoaspy/collect/news/get_news.py` | GDELT sweep + targeted queries → `news/articles.jsonl`, `news/sources.json` |
| `hoaspy/build/build_site.py` (`add_news`, the "In the news" flag) | attaches articles to entities and scores them |

## How it worked

- **Sweep** — FL and TX × `SWEEP_TOPICS` (lawsuit, fraud, embezzlement,
  foreclosure, receivership, "special assessment", investigation, arrested,
  fines, "class action", settlement) × `"homeowners association"` /
  `"condominium association"`, newest first.
- **Targeted** — one query per known problem association: top lien filers in
  `liens/by_association.csv` (`--top-liens`, 120) and the largest delinquent
  DBPR registrants in `records/associations.jsonl` (`--top-delinquent`, 50), by
  the name's distinctive core (`core_phrase()` strips ASSOCIATION, CONDO, PHASE…).
- Endpoint `https://api.gdeltproject.org/api/v2/doc/doc`, 2017 onward;
  `AdaptiveDelay` widens the interval on 429s; English articles only.

## Run it (not part of the pipeline)

```bash
./venv/bin/python -m hoaspy.collect.news.get_news                          # sweep + targeted
./venv/bin/python -m hoaspy.collect.news.get_news --no-sweep --top-liens 40 # quick pass
# also: --out news/  --sweep-records 100  --targeted-records 75  -v
```

## Inputs & outputs

`news/articles.jsonl` (git-ignored) — one row per distinct URL: `url`, `title`,
`domain`, `date` (GDELT `seendate`), `sourcecountry`, `queries[]` of
`{kind: sweep | targeted-liens | targeted-delinquent, query, association, state}`,
merged on re-run. `news/sources.json` records `queries_run`, `articles_total`, `articles_new`.

## How the site still uses it

`build_site.add_news()` runs only if `news/articles.jsonl` exists: targeted
articles attach to the association queried for (state + normalized name); sweep
articles attach when a 2–4-word headline n-gram equals a unique, distinctive
entity core (≥ 10 chars, ≥ 2 words) in the same state, at most three per
article. Attached articles raise the flag "In the news
(n articles)" — `warning` for `targeted-liens`/`sweep` hits, `info` otherwise —
add up to 8 score points and show as the `N{n}` index badge
([DATA.md](DATA.md#flags-things-to-watch-out-for-and-score)). GDELT is listed in
`meta.json` sources when the file is present, so a leftover `news/articles.jsonl`
is still folded in; remove it if news should be absent.

## Tests and related docs

No news-specific tests; `TestDataIntegrity.test_index_files_parse` accepts the `N\d+` badge.
See PIPELINE.md · [REGISTRIES.md](REGISTRIES.md) · [LIENS.md](LIENS.md) · [DATA.md](DATA.md) · NEEDS.md §4.
