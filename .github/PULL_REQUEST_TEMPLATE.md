<!-- CONTRIBUTING.md has the full guide. Delete the sections that do not apply. -->

## Source

<!-- Who publishes it, the URL a person would open, and what it lists. -->

## What this changes

<!-- New collector / new layer in gov_layers / fix to an existing parser / docs. -->

## Sourcing rules

- [ ] Government or court origin, publicly accessible, and its terms do not forbid automated access
- [ ] Names and roles kept as filed; no phone numbers or e-mail addresses in the output
- [ ] Every record links to the issuing source; no documents are re-hosted
- [ ] No collected data, cookie or token file is in this pull request

## Evidence it works

<!-- Paste the last lines of a short live run: `--limit 5`, or the collector's equivalent. -->

```
```

## Tests

- [ ] A fixture under `tests/fixtures/` and a test class in `tests/test_collectors.py`
- [ ] Private individuals' names and street addresses in the fixture are replaced with placeholders (Doe / Roe)
- [ ] `python -m unittest discover -s tests -t . -v` passes

## For the state page

<!-- docs/states/ and coverage.json are generated elsewhere: say what the state page should list. -->
