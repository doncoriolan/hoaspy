# hoaspy — guide for AI coding agents

This repository holds the collectors behind [HOA Spy](https://hoaspy.com):
the code that fetches community-association records from government
registries, county recorders and court portals. `README.md` is the tour;
`docs/` has a page per collector family and a page per state.

## The one rule that matters most

**If you managed to fetch data from a source, the way you did it belongs in
this repository before you are done.** That covers a new collector, a new
court-portal adapter, a change to how an existing one searches or parses,
and a one-off script that turned out to work. A script left in a temporary
directory, or a data file with no collector behind it, is unfinished work.

For each source that means:

1. **The collector** under `hoaspy/collect/` (`registries/`, `liens/`,
   `courts/`; a trial-court portal is one adapter file in
   `hoaspy/collect/courts/court_portals/`). Copy the nearest existing one:
   paced requests, a checkpoint so a long run resumes, an atomic write of
   the output, `--no-upload` where it could push to S3, and a module
   docstring that states the source, the access path, the output file and
   the caveats.
2. **A fixture and a parsing test**: a captured sample under
   `tests/fixtures/` and a class in `tests/test_collectors.py`. No network in
   tests. This repository is public, so replace individuals' names and
   street addresses in fixtures with placeholders (Doe/Roe, sample
   addresses) before committing.
3. **The docs.** Describe the source in the matching page
   (`docs/COURTS.md`, `docs/LIENS.md`, `docs/REGISTRIES.md`). The per-state
   pages (`docs/STATES.md`, `docs/states/<ST>.md`) and `coverage.json` are
   generated and published from the site repository, a private checkout
   that sits next to this one on the maintainer's machine. When it is there,
   edit the docs and `coverage.json` in that checkout and run its
   `scripts/publish_docs_to_hoaspy.py`; edits made directly here are
   overwritten at the next publish. When it is not, say in the pull request
   what the state page should list.
4. **A pull request.** `main` is protected: commit on a branch, push it and
   open a pull request. Do not merge unless the maintainer has asked for
   changes to be pushed.

A source you tried and found blocked, paid or forbidden by its terms is
worth recording too — in `coverage.json` (`unavailable`, `notes`) through
the site repository, or in the pull request — so the next person does not
repeat the attempt.

## Sourcing rules

- Government or court origin, publicly accessible, no terms that forbid
  automated access. No commercial data vendors.
- No solving image captchas. Where a portal needs a session, a collector
  takes a cookie from the maintainer's own browser through `--cookie-file`
  and nothing more; cookie files are git-ignored and never uploaded.
- Keep names and roles as filed; never store phone numbers or e-mail
  addresses. Link every record to the issuing source; never re-host
  documents.

## Commands

```bash
./venv/bin/python -m unittest tests.test_collectors -v      # the whole suite, no network
./venv/bin/python -m hoaspy.collect.courts.get_state_courts --list
./venv/bin/python -m hoaspy.collect.<family>.<module> --help
```

Run modules from the repository root with `python -m`; every module finds
the root through `hoaspy.ROOT`. Outputs (`records/`, `liens/`, `courts/`,
`news/`, `.cache/`) are git-ignored: never commit them, `.env`, `config.yml`
or a cookie file.

## Working alongside other agents

Several agents may be working in this checkout at once. Run `git status`
before you start, touch only your own files, stage by explicit path, and do
not stash, reset or check out files you did not change. Keep every module
importable at each save: `court_portals.registry()` imports all adapters,
so one broken file stops every court run.
