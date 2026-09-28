#!/bin/bash
# Mirror every collected dataset to S3 (never reviews.db / cookies / .env).
# Credentials: the standard AWS chain, else ACCESSKEYID/SECRETACCESSKEY in ./.env.
cd "$(dirname "$0")/.."
P=./venv/bin/python
$P -m hoaspy.lib.s3_sync push --prefix hoa-records/ --dir records \
   associations.jsonl associations.csv state_corps.jsonl state_registries.jsonl \
   irs_exempt_orgs.jsonl gov_layers.jsonl sc_complaints.jsonl corp_coverage.json sources.json
$P -m hoaspy.lib.s3_sync push --prefix hoa-records/ --dir . coverage.json
$P -m hoaspy.lib.s3_sync push --prefix hoa-courts/ --dir courts \
   dockets.jsonl opinions.jsonl tx_research.jsonl sources.json $(cd courts && ls trial_*.jsonl 2>/dev/null)
$P -m hoaspy.lib.s3_sync push --prefix hoa-liens/ --dir liens \
   liens.jsonl liens.csv by_association.csv liens_miamidade.jsonl liens_miamidade.csv \
   liens_nyc.jsonl sources.json sources_nyc.json
