"""Collector parsing/shaping tests — no network, no site.

    ./venv/bin/python -m unittest tests.test_collectors -v

Each class exercises one collector against a captured fixture under
tests/fixtures/ or a hand-built sample: the row shape build_site folds in,
the privacy rule (names and roles kept, phone numbers and e-mails dropped),
checkpoint/resume logic, and the per-portal parsers.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TestMiamiDadeParsing(unittest.TestCase):
    """Pure parse/normalise logic in records_miamidade — no network. The
    collector's live fetch is exercised by running it (see 'Collectors are NOT
    covered' in docs/TEST.md); this covers the record-shaping it depends on."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.liens import records_miamidade as md
        except Exception as exc:  # e.g. requests missing in a bare env
            raise unittest.SkipTest(f"records_miamidade import failed: {exc}")
        cls.md = md

    # two rows for one clerk file number: the D (filer) and R (respondent) sides
    ROWS = [
        {"cfN_MASTER_ID": 999, "cfN_YEAR": 2007, "cfN_SEQ": 1074801,
         "clerk_File": "2007 R 1074801", "doC_TYPE": "LIEN - LIE",
         "reC_DATE": "11/7/2007 12:00:00 AM", "reC_BOOKPAGE": "26036/1293",
         "firsT_PARTY": "VILA MARA CONDO ASSN INC", "partY_CODE": "D",
         "consideratioN_1": 0, "foliO_NUMBER": 0, "legaL_DESCRIPTION": "UNIT 5",
         "subdiV_NAME": "VILA MARA CONDO", "address": ""},
        {"cfN_MASTER_ID": 999, "cfN_YEAR": 2007, "cfN_SEQ": 1074801,
         "clerk_File": "2007 R 1074801", "doC_TYPE": "LIEN - LIE",
         "reC_DATE": "11/7/2007 12:00:00 AM", "reC_BOOKPAGE": "26036/1293",
         "firsT_PARTY": "SHINGLES LEE M", "partY_CODE": "R",
         "consideratioN_1": 0, "foliO_NUMBER": 0, "legaL_DESCRIPTION": "UNIT 5",
         "subdiV_NAME": "VILA MARA CONDO", "address": ""},
    ]

    def test_iso_date(self):
        self.assertEqual(self.md._iso("11/7/2007 12:00:00 AM"), "2007-11-07")
        self.assertEqual(self.md._iso(""), "")
        self.assertEqual(self.md._iso("garbage"), "")

    def test_collapse_dedupes_parties_by_cfn(self):
        by_cfn = self.md._collapse(self.ROWS)
        self.assertEqual(list(by_cfn), [999])
        self.assertEqual(len(by_cfn[999]["_parties"]), 2)

    def test_to_record_shapes_a_lien(self):
        by_cfn = self.md._collapse(self.ROWS)
        rec = self.md.to_record(by_cfn[999], "LIEN - LIE")
        self.assertEqual(rec["doc_type"], "LIE")
        self.assertEqual(rec["doc_type_label"], "claim_of_lien")
        self.assertEqual(rec["county"], "Miami-Dade")
        self.assertEqual(rec["recorded_date"], "2007-11-07")
        self.assertEqual(rec["association"], "VILA MARA CONDO ASSN INC")
        self.assertIn("SHINGLES LEE M", rec["respondents"])
        self.assertEqual(rec["book_page"], "26036/1293")

    def test_session_requires_cookie(self):
        with self.assertRaises(self.md.MiamiDadeError):
            self.md.session("")

    def test_fetch_for_names_skips_and_reports_done(self):
        """A resumed run must not re-query names already collected."""
        calls, done = [], []

        def fake_search(sess, name, doctypes=None, pace=0):
            calls.append(name)
            return [{"doc_id": f"{name}-1", "association": name}]

        orig = self.md.search_party
        self.md.search_party = fake_search
        try:
            out = self.md.fetch_for_names(
                None, ["ALPHA", "BETA"], skip={"ALPHA"}, pace=0,
                on_name_done=lambda n, recs: done.append(n))
        finally:
            self.md.search_party = orig
        self.assertEqual(calls, ["BETA"], "skipped name was re-queried")
        self.assertEqual(done, ["BETA"])
        self.assertEqual([r["query_name"] for r in out], ["BETA"])


class TestMiamiDadeCheckpoint(unittest.TestCase):
    """The full-county run is thousands of requests over hours; it must resume."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.liens import get_miamidade_liens as g
        except Exception as exc:
            raise unittest.SkipTest(f"get_miamidade_liens import failed: {exc}")
        cls.g = g

    def test_checkpoint_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            part, done = self.g.checkpoint_paths(out)
            part.write_text(json.dumps({"doc_id": "X1"}) + "\n"
                            + '{"doc_id": "TORN"'  # hard-kill partial line
                            )
            done.write_text("ALPHA\n")
            recs, names = self.g.load_checkpoint(out)
            self.assertEqual([r["doc_id"] for r in recs], ["X1"],
                             "torn trailing line must be skipped, not fatal")
            self.assertEqual(names, {"ALPHA"})
            self.g.clear_checkpoint(out)
            self.assertFalse(part.exists() or done.exists())

    def test_load_checkpoint_empty_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            recs, names = self.g.load_checkpoint(Path(td))
            self.assertEqual((recs, names), ([], set()))


class TestTxResearchParsing(unittest.TestCase):
    """Pure parse/shape logic in get_tx_research — no network. The live fetch
    is exercised by running the collector; this covers the record-shaping and
    the association-party filter that keep full-text cross-references out."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.courts import get_tx_research as tx
        except Exception as exc:  # e.g. requests missing in a bare env
            raise unittest.SkipTest(f"get_tx_research import failed: {exc}")
        cls.tx = tx

    # A re:SearchTX hit for a real HOA suit: association plaintiff + owner
    # defendant, with the highlight tags and HTML entities the API returns.
    HOA_HIT = {
        "caseDataID": "abc123",
        "jurisdiction": "Travis County - District Clerk",
        "caseNumber": "D-1-GN-16-001378",
        "caseCategoryCode": "Civil - Other Civil",
        "caseTypeCode": "Debt&#x2F;Contract",
        "dateFiled": "2016-04-01T12:00:00Z",
        "status": "Open",
        "description": "CIRCLE C <b><mark>HOMEOWNERS</mark></b> V CLEMENS",
        "parties": [
            {"name": "CIRCLE C <b><mark>HOMEOWNERS</mark></b> ASSOCIATION INC"},
            {"name": "CLEMENS, JOHN"},
        ],
    }
    # A cross-reference: a tax suit that merely names an HOA elsewhere, with no
    # association-shaped party of its own -> must be dropped.
    NOISE_HIT = {
        "caseDataID": "def456",
        "jurisdiction": "Harris County - 55th Civil District Court",
        "caseNumber": "202657502",
        "caseTypeCode": "Tax Delinquency",
        "dateFiled": "2026-08-17T12:00:00Z",
        "description": "SCHOOL DISTRICT v SOME PERSON",
        "parties": [{"name": "SOME PERSON"}, {"name": "SCHOOL DISTRICT"}],
    }

    def test_clean_strips_tags_and_entities(self):
        self.assertEqual(self.tx.clean("A <b><mark>B</mark></b>  C"), "A B C")
        self.assertEqual(self.tx.clean("Debt&#x2F;Contract"), "Debt/Contract")

    def test_association_parties_finds_only_association_names(self):
        parts = self.tx.association_parties(self.HOA_HIT)
        self.assertEqual(parts, ["CIRCLE C HOMEOWNERS ASSOCIATION INC"])
        self.assertEqual(self.tx.association_parties(self.NOISE_HIT), [])

    def test_to_record_shapes_a_docket(self):
        rec = self.tx.to_record(self.HOA_HIT, "Circle C Homeowners Association")
        self.assertEqual(rec["state"], "TX")
        self.assertEqual(rec["docket_number"], "D-1-GN-16-001378")
        self.assertEqual(rec["court"], "Travis County - District Clerk")
        self.assertEqual(rec["date_filed"], "2016-04-01")
        self.assertEqual(rec["nature_of_suit"], "Civil - Other Civil — Debt/Contract")
        self.assertEqual(rec["associations"], ["CIRCLE C HOMEOWNERS ASSOCIATION INC"])
        self.assertIn("/ui/case/abc123", rec["url"])
        # shape build_site.add_courts reads
        for k in ("case_name", "court", "docket_number", "date_filed",
                  "date_terminated", "nature_of_suit", "cause", "url",
                  "state", "associations"):
            self.assertIn(k, rec)

    def test_to_record_drops_cross_reference_noise(self):
        self.assertIsNone(self.tx.to_record(self.NOISE_HIT, "Whatever HOA"))

    def test_to_record_requires_name_overlap(self):
        """An association party unrelated to the queried name is not attached."""
        self.assertIsNone(
            self.tx.to_record(self.HOA_HIT, "Sunrise Lakes Condominium"))

    def test_session_requires_cookie(self):
        with self.assertRaises(ValueError):
            self.tx.session("")

    def test_search_raises_quota_error_on_429(self):
        """A 429 (hourly quota) must raise QuotaError carrying Retry-After, not
        a generic HTTPError — so the run pauses/stops resumably instead of
        burning every remaining name as a skip."""
        tx = self.tx

        class FakeResp:
            status_code = 429
            headers = {"Retry-After": "819"}
            def raise_for_status(self):
                raise AssertionError("should not reach raise_for_status on 429")

        class FakeSession:
            def post(self, *a, **k):
                return FakeResp()

        with self.assertRaises(tx.QuotaError) as cm:
            tx.search(FakeSession(), '"x"', 1, 10)
        self.assertEqual(cm.exception.retry_after, 819)

    def test_search_raises_permission_error_on_401(self):
        tx = self.tx

        class FakeResp:
            status_code = 401
            headers = {}
            def raise_for_status(self):  # pragma: no cover - not reached
                pass

        class FakeSession:
            def post(self, *a, **k):
                return FakeResp()

        with self.assertRaises(PermissionError):
            tx.search(FakeSession(), '"x"', 1, 10)

    def test_query_for_strips_querystring_operators(self):
        """Names carry characters (/ : - " etc.) that are operators in the
        OpenSearch query_string grammar and 500 the backend; they must be
        neutralized to a safe quoted phrase."""
        q = self.tx.query_for('4111-4115 Travis Home Owners Assn a/k/a "X"')
        self.assertNotRegex(q[1:-1], r'["\\/:~^?*!(){}\[\]<>|&+-]')
        self.assertTrue(q.startswith('"') and q.endswith('"'))
        self.assertIn("Travis Home Owners Assn", q)
        self.assertEqual(self.tx.query_for("///"), "")

    def test_tx_association_names_filters_state_and_shape(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "assoc.jsonl"
            p.write_text("\n".join(json.dumps(d) for d in [
                {"state": "TX", "name": "Oak Run Homeowners Association"},
                {"state": "TX", "name": "Oak Run Homeowners Association"},  # dup
                {"state": "TX", "name": "Bob's Bait Shop"},                 # not assoc
                {"state": "FL", "name": "Sunset Condominium Association"},  # not TX
            ]) + "\n")
            names = self.tx.tx_association_names(p)
            self.assertEqual(names, ["Oak Run Homeowners Association"])


class TestCaSosParsing(unittest.TestCase):
    """Pure parse/shape logic in get_ca_sos — no network. The live fetch is
    Imperva-walled and exercised by running the collector; this covers title
    splitting, date normalization, the HOA-name gate, and the response parse
    against a captured bizfile sample."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_ca_sos as ca
        except Exception as exc:  # e.g. requests missing in a bare env
            raise unittest.SkipTest(f"get_ca_sos import failed: {exc}")
        cls.ca = ca
        cls.sample = json.loads(
            (ROOT / "tests" / "fixtures" / "ca_sos_sample.json").read_text())

    def test_clean_title_splits_name_and_filing_number(self):
        self.assertEqual(self.ca.clean_title("1 Buena Vista HOA (B20250040101)"),
                         ("1 Buena Vista HOA", "B20250040101"))
        # A name with no parenthetical filing number survives whole.
        self.assertEqual(self.ca.clean_title("PLAIN NAME"), ("PLAIN NAME", ""))
        # Only the trailing parenthetical is the filing number.
        self.assertEqual(
            self.ca.clean_title("1207-1211 NOE ST. HOMEOWNERS ASSOCIATION (HOA) (90001481)"),
            ("1207-1211 NOE ST. HOMEOWNERS ASSOCIATION (HOA)", "90001481"))

    def test_iso_date(self):
        self.assertEqual(self.ca.iso_date("03/21/2025"), "2025-03-21")
        self.assertEqual(self.ca.iso_date("5/17/2012"), "2012-05-17")
        self.assertEqual(self.ca.iso_date(""), "")
        self.assertEqual(self.ca.iso_date("garbage"), "")

    def test_to_record_shapes_a_state_corp_row(self):
        row = self.sample["rows"]["4209256"]  # a CID mutual-benefit corp
        rec = self.ca.to_record(row)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["state"], "CA")
        self.assertEqual(rec["name"], "1004 W. BALBOA HOA")
        self.assertEqual(rec["record_id"], "3477311")
        self.assertEqual(rec["incorporated"], "2012-05-17")
        self.assertEqual(rec["corp_status"], "Active")
        self.assertIn("Common Interest Development", rec["entity_type"])
        # Schema parity with state_corps.jsonl keys build_site.add_corps reads.
        for k in ("state", "source", "record_id", "name", "corp_status",
                  "incorporated", "registered_agent", "entity_type"):
            self.assertIn(k, rec)

    def test_to_record_drops_non_association_names(self):
        # "1058 HOAGIE LLC" and "805 Hoarding Cleanup..." contain "HOA" as a
        # substring but are not associations — the name gate must drop them.
        self.assertIsNone(self.ca.to_record(self.sample["rows"]["5278776"]))
        self.assertIsNone(self.ca.to_record(self.sample["rows"]["9421411"]))

    def test_parse_response_keeps_only_hoa_rows(self):
        records, raw = self.ca.parse_response(self.sample)
        self.assertEqual(raw, len(self.sample["rows"]))  # cap detection input
        names = {r["name"] for r in records}
        self.assertIn("1 Buena Vista HOA", names)
        self.assertIn("1014-1016 Diamond St HOA LLC", names)  # real HOA-as-LLC
        self.assertNotIn("1058 HOAGIE LLC", names)
        self.assertNotIn("805 Hoarding Cleanup & Junk Removal LLC", names)
        self.assertTrue(all(r["state"] == "CA" for r in records))

    def test_write_merged_replaces_ca_and_keeps_other_states(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "state_corps.jsonl"
            out.write_text(
                json.dumps({"state": "CO", "name": "Vista Pointe Townhome Association"}) + "\n"
                + json.dumps({"state": "CA", "name": "OLD STALE HOA"}) + "\n")
            fresh = [{"state": "CA", "name": "1 Buena Vista HOA", "record_id": "x"}]
            self.ca.write_merged(fresh, out)
            rows = [json.loads(x) for x in out.read_text().splitlines()]
            names = {r["name"] for r in rows}
            self.assertIn("Vista Pointe Townhome Association", names)  # CO kept
            self.assertIn("1 Buena Vista HOA", names)                  # CA fresh
            self.assertNotIn("OLD STALE HOA", names)                   # CA replaced

    def test_write_merged_empty_guard_keeps_existing(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "state_corps.jsonl"
            out.write_text(json.dumps({"state": "CA", "name": "KEEP ME HOA"}) + "\n")
            self.ca.write_merged([], out)  # empty result must not clobber
            rows = [json.loads(x) for x in out.read_text().splitlines()]
            self.assertEqual([r["name"] for r in rows], ["KEEP ME HOA"])

    def test_session_requires_cookie(self):
        with self.assertRaises(ValueError):
            self.ca.session("")

    def test_to_record_keeps_cid_rows_regardless_of_name(self):
        """A row filed as a common-interest development is an HOA by entity
        type, so it is kept (flagged is_association) even when the name has
        no HOA keyword; the name gate still applies to other entity types,
        and name reservations are never entities."""
        row = {"TITLE": ["Sea Pines Village (90009999)"], "ID": 4242,
               "ENTITY_TYPE": "Unincorporated Common Interest Development",
               "STATUS": "Active", "STANDING": "Good Standing",
               "FILING_DATE": "07/01/2003", "AGENT": None}
        rec = self.ca.to_record(row)
        self.assertIsNotNone(rec)
        self.assertTrue(rec["is_association"])
        self.assertEqual(rec["standing"], "Good Standing")
        self.assertEqual(rec["bizfile_id"], "4242")
        self.assertIsNone(self.ca.to_record(
            dict(row, ENTITY_TYPE="Limited Liability Company - CA")))
        self.assertIsNone(self.ca.to_record(
            dict(row, TITLE=["Sea Pines HOA (R1)"], ENTITY_TYPE="Name Reservation")))

    def test_split_window_bisects_and_rejects_single_day(self):
        self.assertEqual(self.ca.split_window("2020-01-01", "2020-01-10"),
                         (("2020-01-01", "2020-01-05"), ("2020-01-06", "2020-01-10")))
        with self.assertRaises(ValueError):
            self.ca.split_window("2020-01-01", "2020-01-01")

    def _corpus(self, per_day, days):
        import datetime as dt
        rows = {}
        for i in range(days):
            d = dt.date(2000, 1, 1) + dt.timedelta(days=i)
            for j in range(per_day):
                rid = i * 1000 + j
                rows[str(rid)] = {
                    "TITLE": [f"UNIT {rid} OWNERS ASSOCIATION ({rid})"], "ID": rid,
                    "ENTITY_TYPE": "Nonprofit Corporation - CA - Mutual Benefit - "
                                   "Common Interest Development Corporation",
                    "STATUS": "Active", "STANDING": "Good Standing",
                    "FILING_DATE": d.strftime("%m/%d/%Y"), "AGENT": "X"}
        return rows

    def _fake_search(self, rows):
        """A stand-in for the bizfile API: rows inside the date window,
        truncated at the 500 cap exactly like the live endpoint."""
        calls = []

        def fake(term, ftype, start, end):
            calls.append((term, ftype, start, end))
            sel = {k: v for k, v in rows.items()
                   if start <= self.ca.iso_date(v["FILING_DATE"]) <= end}
            sel = dict(list(sel.items())[:self.ca.CAP])
            recs, raw = self.ca.parse_response({"rows": sel})
            return recs, raw, sel
        return fake, calls

    def test_sweeper_bisects_capped_windows_until_complete(self):
        rows = self._corpus(per_day=4, days=300)  # 1200 rows: any 125+ day window caps
        fake, calls = self._fake_search(rows)
        ckpt = self.ca.Checkpoint(None, None)
        sw = self.ca.Sweeper(fake, ckpt)
        self.assertEqual(sw.run_term("ASSOCIATION", "69"), 1200)
        self.assertEqual(len(ckpt.by_id), 1200)
        self.assertEqual(sw.capped_days, [])
        self.assertGreater(len(calls), 3)  # the cap forced real splitting
        # Resume is free: a second sweep over the same checkpoint makes no calls.
        n = len(calls)
        self.assertEqual(self.ca.Sweeper(fake, ckpt).run_term("ASSOCIATION", "69"), 0)
        self.assertEqual(len(calls), n)

    def test_sweeper_reports_capped_single_day(self):
        rows = self._corpus(per_day=600, days=1)  # 100 rows are unreachable
        fake, calls = self._fake_search(rows)
        ckpt = self.ca.Checkpoint(None, None)
        sw = self.ca.Sweeper(fake, ckpt)
        sw.run_term("ASSOCIATION", "69")
        self.assertEqual(len(ckpt.by_id), 500)
        self.assertEqual(len(sw.capped_days), 1)
        self.assertIn("2000-01-01|2000-01-01", sw.capped_days[0])

    def test_words_used_is_tracked_per_type_and_persists(self):
        """A word swept against type 62 is still fair game for type 69, and
        the history outlives the run so a later expansion pass skips only
        the (word, type) pairs already tried."""
        rows = self._corpus(per_day=1, days=3)
        fake, _ = self._fake_search(rows)
        with tempfile.TemporaryDirectory() as d:
            words = Path(d) / "words.json"
            sw = self.ca.Sweeper(fake, self.ca.Checkpoint(None, None), words_path=words)
            sw.run_term("OAK", "62")
            self.assertIn("OAK", sw.words_used["62"])
            self.assertNotIn("OAK", sw.words_used["69"])
            again = self.ca.Sweeper(fake, self.ca.Checkpoint(None, None), words_path=words)
            self.assertEqual(again.words_used["62"], {"OAK"})
            names = ["OAK CREEK OWNERS ASSOCIATION"]
            self.assertNotIn("OAK", self.ca.mine_words(names, again.words_used["62"], 5))
            self.assertIn("OAK", self.ca.mine_words(names, again.words_used["69"], 5))

    def test_checkpoint_seed_loads_prior_rows_without_duplicates(self):
        ckpt = self.ca.Checkpoint(None, None)
        prior = [{"bizfile_id": "1", "name": "A", "is_association": True},
                 {"bizfile_id": "1", "name": "A", "is_association": True},
                 {"name": "no id"}]
        self.assertEqual(ckpt.seed(prior), 1)
        self.assertEqual(list(ckpt.by_id), ["1"])

    def test_mine_words_ranks_unseen_name_words(self):
        names = ["SEA PINES VILLAGE OWNERS ASSOCIATION", "PINES AT THE LAKE ASSOCIATION",
                 "THE LAKE INC", "LAKE SHORE CLUB"]
        words = self.ca.mine_words(names, used={"ASSOCIATION"}, limit=4)
        self.assertEqual(words[:2], ["LAKE", "PINES"])
        for w in ("THE", "INC", "ASSOCIATION", "AT"):
            self.assertNotIn(w, words)

    def test_load_dropped_reads_jsonl_and_captured_response(self):
        with tempfile.TemporaryDirectory() as d:
            part = Path(d) / "ca_sos_part1.jsonl"
            part.write_text("\n".join(json.dumps(r) for r in
                                      self.sample["rows"].values()) + "\n")
            capture = Path(d) / "capture.json"
            capture.write_text(json.dumps(self.sample))
            rows = self.ca.load_dropped([part, capture])
            self.assertEqual(set(rows), set(self.sample["rows"]))

    def test_build_site_keeps_cid_rows_without_hoa_keyword(self):
        try:
            from hoaspy.build import build_site as b
        except Exception as exc:
            raise unittest.SkipTest(f"build_site import failed: {exc}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state_corps.jsonl"
            path.write_text(
                json.dumps({"state": "CA", "name": "SEA PINES VILLAGE",
                            "record_id": "1", "is_association": True}) + "\n"
                + json.dumps({"state": "CA", "name": "SEA PINES VILLAGE LLC",
                              "record_id": "2"}) + "\n")
            builder = b.Builder()
            builder.add_corps(None, path)
            names = {e["name"] for (st, _), e in builder.entities.items() if st == "CA"}
            self.assertIn("SEA PINES VILLAGE", names)
            self.assertNotIn("SEA PINES VILLAGE LLC", names)


class TestCaUccParsing(unittest.TestCase):
    """Pure parse/shape logic in get_ca_ucc (CA SOS judgment liens) — no
    network: party-line splitting, descriptor stripping, the person-vs-HOA
    guards, record shaping into the liens schema, and the paged/bisected
    sweep against a fake endpoint."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.liens import get_ca_ucc as ucc
            from hoaspy.collect.registries import get_ca_sos as sos
        except Exception as exc:
            raise unittest.SkipTest(f"get_ca_ucc import failed: {exc}")
        cls.ucc, cls.sos = ucc, sos
        # stand-in for records/state_corps.jsonl: one keyword name, one
        # CID-typed corporation whose name carries no HOA keyword at all
        ucc._KNOWN = {"RIO VISTA WALK HOA", "VILLA LA PAZ MAINTENANCE CORPORATION"}

    def test_parse_party_splits_name_city_state_and_strips_descriptors(self):
        pp = self.ucc.parse_party
        self.assertEqual(pp("RIO VISTA WALK HOA, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT "
                            "CORPORATION - OCEANSIDE, CA"),
                         {"name": "RIO VISTA WALK HOA", "city": "Oceanside", "state": "CA"})
        self.assertEqual(pp("PARK PLACE MANAGEMENT, INC. A CALIFORNIA CORPORATION DBA "
                            "PARK PLACE MANAGEMENT & HOA PROPERTY MANAGEMENT - GARDENA, CA")["name"],
                         "PARK PLACE MANAGEMENT, INC.")
        self.assertEqual(pp("BANK OF HOPE FKA WILSHIRE STATE BANK, A CALIFORNIA BANKING "
                            "CORPORATION - LOS ANGELES, CA")["name"], "BANK OF HOPE")
        self.assertEqual(pp("A & B HOMEOWNERS ASSOCIATION - FRESNO, CA")["name"],
                         "A & B HOMEOWNERS ASSOCIATION")        # leading 'A' is the name
        self.assertEqual(pp("NO SUFFIX HERE"), {"name": "NO SUFFIX HERE", "city": "", "state": ""})

    def test_is_association_guards_people_named_hoa(self):
        ia = self.ucc.is_association
        self.assertTrue(ia("LAUREL POINTE COMMUNITY ASSOCIATION"))
        self.assertTrue(ia("SEA PINES HOA"))                       # HOA as last token is normal
        self.assertTrue(ia("HOA OF ROCKEFELLER LANE"))
        self.assertTrue(ia("RIO VISTA WALK HOA"))                  # known CA corporation
        self.assertTrue(ia("VILLA LA PAZ MAINTENANCE CORPORATION")) # known CID, no keyword
        self.assertFalse(ia("ACME MAINTENANCE CORPORATION"))        # unknown, no keyword
        self.assertFalse(ia("HOMEOWNERS MARKETING SERVICES, INC."))  # vendor borrowing the word
        self.assertTrue(ia("LAKESIDE HOMEOWNERS ASSOCIATION MANAGEMENT COMMITTEE"))
        self.assertFalse(ia("HOA NGUYEN"))                         # given name, no other signal
        self.assertFalse(ia("HOA T. T. PHAM", raw="HOA T. T. PHAM, AN INDIVIDUAL DBA DAN'S LIQUOR"))
        self.assertFalse(ia("DAVID HOANG TRAN"))

    def _row(self, debtor, creditor, **kw):
        row = {"ID": 739724, "RECORD_NUM": "177595183753", "TITLE": [debtor],
               "SEC_PARTY": [creditor], "FILING_DATE": "07/11/2017",
               "LAPSE_DATE": "07/11/2022", "RECORD_TYPE": "Judgment Lien", "STATUS": "Lapsed"}
        row.update(kw)
        return row

    def test_to_record_creditor_and_debtor_roles(self):
        r = self.ucc.to_record(self._row(
            "ALICIA CORNEJO - ANAHEIM, CA",
            "RIO VISTA WALK HOA, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION - OCEANSIDE, CA"))
        self.assertEqual((r["hoa_role"], r["doc_type"], r["doc_type_label"]),
                         ("creditor", "JL", "judgment_lien"))
        self.assertEqual(r["association"], "RIO VISTA WALK HOA")
        self.assertEqual(r["respondents"], ["ALICIA CORNEJO"])
        self.assertEqual((r["recorded_ymd"], r["year"], r["state"], r["county"], r["city"]),
                         ("20170711", 2017, "CA", "", "Oceanside"))
        for k in ("doc_id", "doc_type", "recorded_date", "association", "filers",
                  "respondents", "source", "source_page", "lapse_date", "status"):
            self.assertIn(k, r)
        d = self.ucc.to_record(self._row(
            "SUNSET RIDGE HOMEOWNERS ASSOCIATION - IRVINE, CA",
            "ACME ROOFING, INC., A CALIFORNIA CORPORATION - IRVINE, CA"))
        self.assertEqual((d["hoa_role"], d["doc_type"]), ("debtor", "JLX"))
        self.assertEqual(d["association"], "SUNSET RIDGE HOMEOWNERS ASSOCIATION")
        self.assertIsNone(self.ucc.to_record(self._row(
            "DAVID HOANG TRAN, AN INDIVIDUAL - GARDENA, CA",
            "BANK OF HOPE, A CALIFORNIA BANKING CORPORATION - LOS ANGELES, CA")))

    def test_ucc_body_carries_offset_only_when_paging(self):
        b = self.ucc.ucc_body("HOA", "2154", "2020-01-01", "2020-12-31")
        self.assertEqual(b["RECORD_TYPE_ID"], "2154")
        self.assertEqual(b["FILING_DATE"], {"start": "01/01/2020", "end": "12/31/2020"})
        self.assertNotIn("edge", b)
        self.assertEqual(self.ucc.ucc_body("HOA", offset=100)["edge"], {"offset": 100, "limit": 100})

    def _fake_endpoint(self, rows_by_date, page=100, honours_offset=True):
        """Mimics uccsearch: rows in the date window, `page` per call, an
        edge with the true total; ignores the offset when told to."""
        calls = []

        def fake(term, rtype, start, end, offset=0, limit=100):
            calls.append(offset)
            sel = [r for d, rs in sorted(rows_by_date.items())
                   if (not start or d >= start) and (not end or d <= end) for r in rs]
            total = len(sel)
            eff = offset if honours_offset else 0
            chunk = sel[eff:eff + page]
            raw = {str(r["ID"]): r for r in chunk}
            recs, n = self.ucc.parse_response({"rows": raw})
            raw["__edge__"] = {"offset": eff, "limit": page, "total": total}
            return recs, n, raw
        return fake, calls

    def _corpus(self, per_day, days):
        import datetime as dt
        out = {}
        for i in range(days):
            d = (dt.date(2015, 1, 1) + dt.timedelta(days=i))
            out[d.isoformat()] = [self._row(
                f"OWNER {i * 1000 + j} - ANAHEIM, CA",
                "RIO VISTA WALK HOA, A CALIFORNIA NON-PROFIT MUTUAL BENEFIT CORPORATION - OCEANSIDE, CA",
                ID=i * 1000 + j, RECORD_NUM=f"U{i * 1000 + j}",
                FILING_DATE=d.strftime("%m/%d/%Y")) for j in range(per_day)]
        return out

    def test_sweep_pages_when_offset_is_honoured(self):
        fake, calls = self._fake_endpoint(self._corpus(per_day=5, days=50))  # 250 rows
        ckpt = self.sos.Checkpoint(None, None)
        new, how = self.ucc.sweep_term(fake, ckpt, "HOA", "2154")
        self.assertEqual((new, how), (250, "paged"))
        self.assertEqual(calls, [0, 100, 200])

    def test_sweep_bisects_when_offset_is_ignored(self):
        fake, calls = self._fake_endpoint(self._corpus(per_day=5, days=50), honours_offset=False)
        ckpt = self.sos.Checkpoint(None, None)
        new, how = self.ucc.sweep_term(fake, ckpt, "HOA", "2154")
        self.assertEqual((new, how), (250, "bisected"))
        self.assertEqual(len(ckpt.by_id), 250)
        self.assertGreater(len(calls), 3)
        # a rerun is free: the term is checkpointed as done
        self.assertEqual(self.ucc.sweep_term(fake, ckpt, "HOA", "2154"), (0, "done"))


class TestUtahRegistryParsing(unittest.TestCase):
    """get_ut_hoa detail-card parsing against a captured registry page — no
    network. Guards the privacy rule (names and roles kept, phone numbers and
    e-mail addresses dropped) and the registry row shape."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_ut_hoa as ut
        except Exception as exc:
            raise unittest.SkipTest(f"get_ut_hoa import failed: {exc}")
        cls.ut = ut
        cls.html = (ROOT / "tests" / "fixtures" / "ut_hoa_detail.html").read_text()

    def _detail(self):
        client = self.ut.Client(pace=0)
        client.call = lambda f, v: self.html          # offline
        return client.detail("14255465")

    def test_detail_card_fields(self):
        d = self._detail()
        self.assertTrue(d["name"])
        self.assertRegex(d["registration_no"], r"^\d{8}-(HOA1|MHOA)$")
        self.assertIn(d["registration_type"], ("HOA Registration", "Master HOA Registration"))
        self.assertEqual(d["status"], "Active")
        self.assertRegex(d["expires"], r"^\d{1,2}/\d{1,2}/\d{4}$")
        self.assertTrue(d["county"] and not d["county"].endswith("County"))

    def test_record_keeps_names_drops_contacts(self):
        rec = self.ut.to_record(self._detail(), {})
        self.assertEqual(rec["state"], "UT")
        self.assertTrue(rec["source_url"].startswith("https://services.commerce.utah.gov/hoa/?p="))
        self.assertRegex(rec["status_detail"], r"^expires \d{4}-\d{2}-\d{2}$")
        self.assertTrue(rec["officers"], "president/board names expected")
        for o in rec["officers"]:
            self.assertIn(o["title"], ("President", "Board member"))
            self.assertNotIn("@", o["name"])
            self.assertNotRegex(o["name"], r"\d{3}-\d{4}")
        blob = json.dumps(rec)
        self.assertNotIn("@", blob, "an e-mail address leaked into the registry row")


class TestAzAdreParsing(unittest.TestCase):
    """get_az_adre against a captured ADRE detail card and an excerpt of a
    subdivision Public Report — no network. Guards the detail-card fields,
    the two-column "Name of the HOA" form parser (name wraps under the
    assessment column), the town/ZIP/lot-count extraction, the generic-name
    filter and the one-row-per-association grouping."""

    @classmethod
    def setUpClass(cls):
        from hoaspy.collect.registries import get_az_adre as az
        cls.az = az
        cls.html = (ROOT / "tests" / "fixtures" / "az_adre_detail.html").read_text()
        cls.text = (ROOT / "tests" / "fixtures" / "az_adre_report.txt").read_text()

    def test_detail_card_fields(self):
        d = self.az.parse_detail(self.html)
        self.assertEqual(d["registration_no"], "DM02-030803")
        self.assertEqual(d["legal_name"], "SIERRA VISTA")
        self.assertEqual(d["marketing_name"], "", "NONE must read as empty")
        self.assertEqual(d["date_issued"], "1/22/2003")
        self.assertEqual(d["county"], "Maricopa")
        self.assertEqual(self.az.parse_detail(self.html.replace(">Maricopa<", ">MARICOPA<"))["county"], "Maricopa",
                         "county spelling must be normalised")
        self.assertEqual(d["developer"], "BROWN FAMILY COMMUNITIES")
        self.assertEqual(d["application_status"], "Issued")

    def test_report_form_block_and_location(self):
        p = self.az.parse_report(self.text, "Maricopa")
        self.assertEqual(p["associations"], ["Arlington Estates at South Mountain Homeowners Association"])
        self.assertEqual(p["assessment"], "$84.00/month")
        self.assertEqual(p["city"], "Phoenix")
        self.assertEqual(p["zip"], "85007")
        self.assertEqual(p["units"], 395)

    def test_generic_names_and_sentences(self):
        az = self.az
        for g in ("the Association", "Homeowners Association", "Residential Association",
                  "Neighborhood Association", "Master Association"):
            self.assertTrue(az._generic(g), g)
        self.assertFalse(az._generic("Jorde Farms Community Association"))
        names = az.association_names(
            "NAME AND ASSESSMENTS: Purchaser will belong to Jorde Farms Community Association "
            "(HOA), an Arizona nonprofit corporation, and are subject to assessments.")
        self.assertEqual(names, ["Jorde Farms Community Association"])
        self.assertEqual(az.form_block("Name of the HOA: None          Current Assessments: N/A\n"), ("", " N/A"))

    def test_rows_group_phases_into_one_association(self):
        az = self.az
        details = {
            2: {"id": 2, "pdf": True, "registration_no": "DM26-000002", "legal_name": "ARLINGTON ESTATES PHASE 2",
                "date_issued": "9/14/2026", "county": "Maricopa", "developer": "DEV LLC",
                "associations": ["Arlington Estates at South Mountain Homeowners Association"],
                "assessment": "$84.00/month", "city": "Phoenix", "zip": "85007", "units": 236},
            1: {"id": 1, "pdf": True, "registration_no": "DM26-000001", "legal_name": "ARLINGTON ESTATES PHASE 1",
                "date_issued": "9/14/2025", "county": "MARICOPA", "developer": "DEV LLC",
                "associations": ["Arlington Estates At South Mountain Homeowners Association"],
                "assessment": "", "city": "Phoenix", "zip": "85007", "units": 395},
            3: {"id": 3, "pdf": True, "registration_no": "DM26-000003", "legal_name": "NO HOA",
                "date_issued": "1/1/2026", "county": "Pima", "associations": []},
            4: {"id": 4, "missing": True},
        }
        az.TEXT_DIR = ROOT / "tests" / "fixtures" / "nope"        # no cached text -> keep given parse
        rows = az.to_records(details)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["state"], "AZ")
        self.assertEqual(r["county"], "Maricopa")
        self.assertEqual(r["name"], "Arlington Estates at South Mountain Homeowners Association")
        self.assertEqual(len(r["subdivisions"]), 2)
        self.assertEqual(r["recorded_date"], "2025-09-14")
        self.assertEqual(r["units"], 631)
        self.assertEqual(r["assessment"], "$84.00/month")
        self.assertIn("2 subdivision Public Reports", r["status"])
        self.assertEqual(r["status_detail"], "latest issued 2026-09-14; regular assessment $84.00/month")
        self.assertTrue(r["source_url"].startswith("https://services.azre.gov/PdbWeb/Development/ViewDevelopment/"))
        self.assertNotIn("@", json.dumps(r))


class TestCookCondosShaping(unittest.TestCase):
    """get_cook_condos: unit PINs group into buildings, the unit designator is
    stripped off the address, and the registry row is shaped — no network."""

    @classmethod
    def setUpClass(cls):
        from hoaspy.collect.registries import get_cook_condos as cc
        cls.cc = cc

    def test_street_of_strips_units(self):
        f = self.cc.street_of
        self.assertEqual(f("850 N LAKE SHORE DR 412"), "850 N Lake Shore Dr")
        self.assertEqual(f("600 W RUSSELL ST 301"), "600 W Russell St")
        self.assertEqual(f("1030 N STATE ST UNIT 5A"), "1030 N State St")
        self.assertEqual(f("5445 N SHERIDAN RD APT 3107"), "5445 N Sheridan Rd")
        self.assertEqual(f("100 E BELLEVUE PL"), "100 E Bellevue Pl")
        self.assertEqual(f("10 CEDAR CREEK CT A-10"), "10 Cedar Creek Ct")
        self.assertEqual(f("100 E WALTON ST 10AB"), "100 E Walton St")
        self.assertEqual(f("1001 W WASHINGTON BLVD P2-65"), "1001 W Washington Blvd")
        self.assertEqual(f("1034 N WOLCOTT AVE 1FRON"), "1034 N Wolcott Ave")
        self.assertEqual(f("1038 ALTGELD"), "1038 Altgeld")
        self.assertEqual(f("1203 46TH ST 2"), "1203 46th St")
        self.assertEqual(f("2 E OAK ST"), "2 E Oak St")
        self.assertEqual(f(""), "")

    def test_group_and_row(self):
        cc = self.cc
        units = [
            {"pin": "17032280391001", "pin10": "1703228039", "zip_code": "60611", "lat": "41.9", "lon": "-87.6",
             "cook_municipality_name": "CITY OF CHICAGO", "township_name": "North Chicago"},
            {"pin": "17032280391002", "pin10": "1703228039", "zip_code": "60611", "lat": "41.9", "lon": "-87.6",
             "cook_municipality_name": "CITY OF CHICAGO", "township_name": "North Chicago"},
            {"pin": "01022020501094", "pin10": "0102202050", "zip_code": "60010", "lat": "42.15", "lon": "-88.14",
             "cook_municipality_name": "VILLAGE OF BARRINGTON", "township_name": "Barrington"},
        ]
        b = cc.group_units(units)
        self.assertEqual(set(b), {"1703228039", "0102202050"})
        self.assertEqual(len(b["1703228039"]["pins"]), 2)
        row = cc.to_row(b["1703228039"], {"max_char_yrblt": "1968.0", "max_char_building_non_units": "1.0"},
                        {"prop_address_full": "850 N LAKE SHORE DR 412", "prop_address_city_name": "CHICAGO",
                         "prop_address_zipcode_1": "60611"})
        self.assertEqual(row["name"], "850 N Lake Shore Dr Condominium")
        self.assertEqual((row["state"], row["county"], row["city"], row["zip"]), ("IL", "Cook", "Chicago", "60611"))
        self.assertEqual(row["units"], 1)
        self.assertEqual(row["year_built"], "1968")
        self.assertIn("built 1968", row["status_detail"])
        self.assertAlmostEqual(row["lat"], 41.9)
        self.assertEqual(row["record_id"], "1703228039")
        # no address row -> municipality gives the city, ZIP from the parcels; no street -> no row
        self.assertIsNone(cc.to_row(b["0102202050"], None, None))
        self.assertEqual(cc.muni_city("VILLAGE OF BARRINGTON", "Barrington"), "Barrington")
        blob = json.dumps(row)
        for forbidden in ("mail_address", "owner_address", "@"):
            self.assertNotIn(forbidden, blob)


class TestWiDfiParsing(unittest.TestCase):
    """get_wi_dfi listing-page parsing against a captured DFI search page
    (`tests/fixtures/wi_dfi_sample.html`) — no network. Guards the registry
    row shape, the municipality/county split and the privacy rule (management
    companies kept, personal names in that column dropped)."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_wi_dfi as wi
        except Exception as exc:
            raise unittest.SkipTest(f"get_wi_dfi import failed: {exc}")
        cls.wi = wi
        cls.html = (ROOT / "tests" / "fixtures" / "wi_dfi_sample.html").read_text()

    def test_listing_rows_parse(self):
        rows = self.wi.parse_rows(self.html)
        self.assertEqual(len(rows), 7)
        for r in rows:
            self.assertRegex(r["number"], r"^HOA\d{5}$")
            self.assertTrue(r["name"] and " - " in r["place"])
        self.assertEqual(rows[0]["name"], "Aldon Station Neighborhood Association, Inc")
        self.assertEqual(rows[0]["place"], "Village of Ashwaubenon - Brown")
        self.assertEqual(self.wi.parse_rows("<p>Sorry your search returned no records</p>"), [])

    def test_record_shape_and_place_split(self):
        recs = {r["record_id"]: r for r in map(self.wi.to_record, self.wi.parse_rows(self.html))}
        r = recs["HOA12041"]
        self.assertEqual((r["state"], r["city"], r["county"]), ("WI", "Ashwaubenon", "Brown"))
        self.assertTrue(r["source"].startswith("Wisconsin DFI"))
        self.assertTrue(r["source_url"].startswith(self.wi.BASE + "?Query=HOA12041"))
        self.assertIn("Village of Ashwaubenon", r["status_detail"])
        self.assertEqual(set(r), {"state", "source", "source_url", "record_id", "name", "status",
                                  "status_detail", "recorded_date", "address", "city", "county",
                                  "zip", "units", "manager_name"})
        self.assertEqual((recs["HOA12405"]["city"], recs["HOA12405"]["county"]), ("Howard", "Brown"))
        self.assertEqual((recs["HOA12449"]["city"], recs["HOA12449"]["county"]), ("Ixonia", "Jefferson"))
        self.assertEqual((recs["HOA12203"]["city"], recs["HOA12203"]["county"]), ("Brown Deer", "Milwaukee"))
        self.assertEqual(self.wi.split_place("City of Madison - County of Dane"), ("Madison", "Dane"))

    def test_manager_is_company_only(self):
        recs = {r["record_id"]: r for r in map(self.wi.to_record, self.wi.parse_rows(self.html))}
        self.assertEqual(recs["HOA12449"]["manager_name"], "SENTRY MANAGEMENT INC")
        self.assertTrue(recs["HOA12029"]["manager_name"].endswith("Association, Inc."))
        self.assertEqual(recs["HOA12075"]["manager_name"], "", "a named employee is not a company")
        self.assertEqual(recs["HOA12041"]["manager_name"], "")
        blob = json.dumps(list(recs.values()))
        self.assertNotIn("@", blob)
        self.assertNotRegex(blob, r"\d{3}[-.]\d{3}[-.]\d{4}")
        self.assertNotIn("Garrett", blob)

    def test_county_checkpoint_resumes(self):
        """A killed sweep resumes: finished counties are skipped and their rows
        come back from `.cache/wi/dfi_counties.jsonl` (stub client, no network)."""
        rows = self.wi.parse_rows(self.html)
        with tempfile.TemporaryDirectory() as d:
            ckpt = Path(d) / "ck.jsonl"
            self.wi.save_ckpt("Brown", rows[:3], ckpt)
            done, found = self.wi.load_ckpt(ckpt)
            self.assertEqual((done, sorted(found)), ({"Brown"}, ["HOA12041", "HOA12203", "HOA12543"]))
            client = type("C", (), {"requests": 0,
                                    "search": lambda self, q, c: rows[3:] if c == "Dane" else []})()
            out = self.wi.sweep(client, ["Brown", "Dane"], ckpt=ckpt)
            self.assertEqual(len(out), 7, "Brown from the checkpoint + Dane from the client")
            self.assertEqual(self.wi.load_ckpt(ckpt)[0], {"Brown", "Dane"})


class TestWaCcfsParsing(unittest.TestCase):
    """get_wa_ccfs list-row shaping against a captured CCFS advanced-search
    page (`tests/fixtures/wa_ccfs_page.json`, the slim projection the page
    script reads off the Angular scope) — no network, no browser. Guards the
    csv_corp row shape, the ASSOC_RE gate the LIKE-search needs, and the
    privacy rule (phone / e-mail / EIN never leave the page)."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_wa_ccfs as wa
        except Exception as exc:
            raise unittest.SkipTest(f"get_wa_ccfs import failed: {exc}")
        cls.wa = wa
        cls.raw = json.loads((ROOT / "tests" / "fixtures" / "wa_ccfs_page.json").read_text())

    def test_list_rows_become_corp_rows(self):
        from hoaspy.collect.registries.get_states import ASSOC_RE
        rows = [self.wa.to_row(r) for r in self.raw]
        self.assertTrue(rows and all(rows), "every fixture row is association-named")
        want = {"state", "source", "source_url", "record_id", "name", "corp_status", "status",
                "address", "city", "county", "zip", "incorporated", "registered_agent",
                "entity_type", "units", "manager_name", "officers", "business_id"}
        for raw, r in zip(self.raw, rows):
            self.assertEqual(set(r), want)
            self.assertEqual(r["state"], "WA")
            self.assertEqual(r["source_url"], "https://ccfs.sos.wa.gov/")
            self.assertRegex(r["record_id"], r"^\d{9}$", "UBI is digits only")
            self.assertEqual(r["record_id"], re.sub(r"\D", "", raw["UBINumber"]))
            self.assertEqual(r["business_id"], str(raw["BusinessID"]))
            self.assertRegex(r["incorporated"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertEqual(r["status"], r["corp_status"])
            self.assertIn(r["status"], ("Active", "Inactive", "Administratively Dissolved"))
            self.assertTrue(ASSOC_RE.search(r["name"]) and r["name"] == r["name"].strip())
            self.assertTrue(r["entity_type"].startswith("WA "))
            self.assertEqual(r["city"], r["city"].title())
            self.assertIsNone(r["units"]); self.assertEqual(r["officers"], [])
        self.assertEqual({r["status"] for r in rows},
                         {"Active", "Inactive", "Administratively Dissolved"},
                         "fixture spans every status so the site can flag dissolved corps")

    def test_assoc_gate_and_no_contacts(self):
        base = dict(self.raw[0])
        for name in ("CONDOR TRUCKING INC", "SHOAL BAY LLC", "HOANG FAMILY DENTAL", ""):
            self.assertIsNone(self.wa.to_row(dict(base, BusinessName=name)), name)
        # the API payload carries phone/e-mail/EIN; the row must not, even if
        # they were projected by mistake
        leaky = dict(base, PhoneNumber="360-555-0100", EmailAddress="board@example.org",
                     FEINNo="994453805",
                     PrincipalStreetAddress=dict(base["PrincipalStreetAddress"],
                                                 PhoneNumber="360-555-0100", EmailAddress="x@example.org"))
        blob = json.dumps(self.wa.to_row(leaky))
        for bad in ("@", "555-0100", "994453805", "PhoneNumber", "EmailAddress", "FEIN"):
            self.assertNotIn(bad, blob)
        for bad in ("PhoneNumber", "EmailAddress", "FEINNo"):
            self.assertNotIn(bad, self.wa._JS_PROJECT, "page projection must not read contact fields")
        self.assertRegex(json.dumps(self.raw), r'"UBINumber": "\d{3} \d{3} \d{3}"')


class TestNevadaLayersParsing(unittest.TestCase):
    """get_nv_hoa: Clark County / Henderson / Las Vegas ArcGIS attribute rows
    (captured in tests/fixtures/nv_hoa_layers.json) become registry rows
    without phone numbers, and the cross-layer merge keys on the SoS file
    number."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_nv_hoa as nv
        except Exception as exc:
            raise unittest.SkipTest(f"get_nv_hoa import failed: {exc}")
        cls.nv = nv
        cls.fx = json.loads((ROOT / "tests" / "fixtures" / "nv_hoa_layers.json").read_text())

    def _rows(self):
        nv = self.nv
        rows = [nv.clark_record(a, nv.LAYERS["clark"]) for a in self.fx["clark"]]
        rows += [nv.henderson_record(a, nv.LAYERS["henderson"], False) for a in self.fx["henderson"]]
        rows += [nv.henderson_record(a, nv.LAYERS["henderson_master"], True) for a in self.fx["henderson_master"]]
        rows += [nv.las_vegas_record(a, nv.LAYERS["las_vegas"]) for a in self.fx["las_vegas"]]
        return [r for r in rows if r]

    def test_rows_shape_and_privacy(self):
        rows = self._rows()
        self.assertGreaterEqual(len(rows), 10)
        for r in rows:
            self.assertEqual(r["state"], "NV")
            self.assertEqual(r["county"], "Clark")
            self.assertTrue(r["name"] and r["name"] == r["name"].upper())
            self.assertTrue(r["record_id"])
            self.assertTrue(r["source_url"].startswith("https://"))
            self.assertNotRegex(json.dumps(r), r"\(\d{3}\) \d{3}-\d{4}", "phone number leaked")
        clark = [r for r in rows if "Clark County" in r["source"]][0]
        self.assertEqual(clark["status"], "registered association")
        self.assertIsInstance(clark["units"], int)
        self.assertTrue(clark["manager_name"], "C/O line should become the manager name")

    def test_merge_on_sos_number(self):
        nv = self.nv
        a = nv.clark_record({"OBJECTID": 1, "Name": "ALPHA HOMEOWNERS ASSOCIATION", "Assn_Type": "REG",
                             "F__of_Units": 10, "SOS_": "123-2001", "City": "LAS VEGAS", "Zip_Code": "89113"},
                            nv.LAYERS["clark"])
        b = nv.henderson_record({"OBJECTID": 2, "NAME": "ALPHA HOA", "FILE_": "123-2001", "INACTIVE": "N",
                                 "NUM_UNITS": 10, "MANAGEMENT_COMPANY": "ACME MGMT"},
                                nv.LAYERS["henderson"], False)
        c = nv.las_vegas_record({"OBJECTID": 3, "ASSOC_NAME": "Beta Estates", "NTYPE": "HOA",
                                 "REGISTERED": "Y", "CORP_NO": "NV1", "CITY1": "Las Vegas", "ZIP1": "89129"},
                                nv.LAYERS["las_vegas"])
        merged = nv.merge([a, b, c])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["manager_name"], "Acme Mgmt")
        self.assertIn(nv.LAYERS["henderson"]["source"], merged[0]["also_listed_by"])


class TestGovLayersMapping(unittest.TestCase):
    """get_gov_layers.to_rows(): YAML field mapping of raw layer attributes
    into registry rows — name upper-casing, parcel collapse by normalized
    name, constants, the association-name gate, and the refusal to map
    phone/e-mail columns."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.registries import get_gov_layers as gl
        except Exception as exc:
            raise unittest.SkipTest(f"get_gov_layers import failed: {exc}")
        cls.gl = gl

    def test_mapping_and_parcel_collapse(self):
        cfg = {"kind": "arcgis", "source": "Test County — condo projects", "page": "https://x/y",
               "url": "https://x/FeatureServer/0", "name_filter": True,
               "constants": {"county": "Test", "status": "condominium project"},
               "fields": {"name": "OWNER", "address": "ADDR", "city": "TOWN", "zip": "ZIP",
                          "units": "UNITS", "record_id": "PID"}}
        raw = [
            {"OWNER": "Harbor View Condominium Trust", "ADDR": "1 Main St", "TOWN": "SALEM",
             "ZIP": "01970-1234", "UNITS": 12.0, "PID": "A1"},
            {"OWNER": "HARBOR VIEW CONDOMINIUM TRUST", "ADDR": "3 Main St", "TOWN": "SALEM",
             "ZIP": "01970", "UNITS": None, "PID": "A2"},
            {"OWNER": "JOHN SMITH", "ADDR": "9 Elm", "TOWN": "SALEM", "ZIP": "01970", "PID": "B1"},
            {"OWNER": "", "PID": "C1"},
        ]
        rows = self.gl.to_rows("MA", cfg, raw)
        self.assertEqual(len(rows), 1, "two parcels of one trust collapse; non-association dropped")
        r = rows[0]
        self.assertEqual(r["name"], "HARBOR VIEW CONDOMINIUM TRUST")
        self.assertEqual(r["parcels"], 2)
        self.assertEqual((r["state"], r["county"], r["status"]), ("MA", "Test", "condominium project"))
        self.assertEqual(r["city"], "Salem")
        self.assertEqual(r["zip"], "01970")
        self.assertEqual(r["units"], 12)
        self.assertEqual(r["source_url"], "https://x/y")

    def test_refuses_contact_columns(self):
        cfg = {"kind": "arcgis", "source": "t", "url": "u",
               "fields": {"name": "NAME", "manager": "MANAGER_PHONE"}}
        with self.assertRaises(ValueError):
            self.gl.to_rows("NV", cfg, [{"NAME": "X HOA", "MANAGER_PHONE": "555"}])

    def test_where_list_fetches_each_filter(self):
        """A list-valued `where` (needed when a service's query timeout forces
        partitioning, e.g. WI's 3.6M-row parcel layer) runs the filters one
        after another and concatenates the pages — no network, stub session."""
        calls = []

        class Resp:
            def __init__(self, where):
                self.where = where

            def raise_for_status(self):
                pass

            def json(self):
                return {"features": [{"attributes": {"OWNER": f"HOA {self.where}"}}]}

        class Session:
            def get(self, url, params=None, timeout=None):
                calls.append((url, params["where"], params["resultOffset"]))
                return Resp(params["where"])

        cfg = {"kind": "arcgis", "source": "t", "url": "https://x/FeatureServer/0",
               "where": ["OBJECTID <= 5", "OBJECTID > 5"], "page_size": 10}
        raw = self.gl.fetch_arcgis(Session(), cfg, pace=0)
        self.assertEqual([c[1] for c in calls], ["OBJECTID <= 5", "OBJECTID > 5"])
        self.assertTrue(all(c[0].endswith("/query") for c in calls))
        self.assertEqual([r["OWNER"] for r in raw], ["HOA OBJECTID <= 5", "HOA OBJECTID > 5"])
        one = self.gl.fetch_arcgis(Session(), {**cfg, "where": "1=1"}, pace=0)
        self.assertEqual(len(one), 1, "a plain string where still works")

    def test_zip_dbf_rows(self):
        """A shapefile zip (Teton County WY assessor download) is read from its
        .dbf member: one dict per record, deleted records skipped, values stripped."""
        import io
        import struct
        import zipfile
        fields = [("owner", 30), ("pidn", 10)]
        hdr = bytearray(struct.pack("<BBBBIHH20x", 3, 26, 9, 2, 3, 32 + 32 * len(fields) + 1,
                                    1 + sum(w for _, w in fields)))
        for name, w in fields:
            hdr += name.encode().ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([w]) + b"\0" * 15
        hdr += b"\r"
        recs = [(b" ", "ASPEN HOMEOWNERS ASSOCIATION", "22-41-1"), (b"*", "DELETED ROW", "x"),
                (b" ", "SMITH, JOHN", "22-41-2")]
        body = b"".join(flag + o.encode().ljust(30) + p.encode().ljust(10) for flag, o, p in recs)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("ownership/ownership.prj", "PROJCS[]")
            zf.writestr("ownership/ownership.dbf", bytes(hdr) + body + b"\x1a")
        rows = self.gl._read_zip_dbf(buf.getvalue(), None)
        self.assertEqual(rows, [{"owner": "ASPEN HOMEOWNERS ASSOCIATION", "pidn": "22-41-1"},
                                {"owner": "SMITH, JOHN", "pidn": "22-41-2"}])
        self.assertEqual(self.gl._read_zip_dbf(buf.getvalue(), "ownership/ownership.dbf"), rows)


class TestStateSourceConstants(unittest.TestCase):
    """get_states.fetch_csv `constants:` fill columns a file lacks (the
    Montgomery County CCOC export has no county or status column) before
    the status filter and the row builder read them."""

    def test_constants_fill_missing_columns(self):
        from hoaspy.collect.registries import get_states
        with tempfile.TemporaryDirectory() as td:
            csv_path = Path(td) / "list.csv"
            csv_path.write_text("Registration Number,Community,Street,City,Zip Code,Unit Count\n"
                                "1,Example Homeowners Association,MAIN ST,ROCKVILLE,20850,12\n")
            cfg = {"local_cache": str(csv_path), "source": "test",
                   "constants": {"County": "Montgomery", "Status": "registered COC"},
                   "fields": {"record_id": "Registration Number", "name": "Community",
                              "status": "Status", "address": "Street", "city": "City",
                              "county": "County", "zip": "Zip Code", "units": "Unit Count"}}
            rows = get_states.fetch_csv(None, cfg, "MD", registry=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["county"], "Montgomery")
        self.assertEqual(rows[0]["status"], "registered COC")
        self.assertEqual(rows[0]["units"], "12")


class TestS3SyncCli(unittest.TestCase):
    """s3_sync.py's push/pull CLI refuses the member database and cookie
    files before touching the network."""

    def test_refuses_member_db_and_cookies(self):
        from hoaspy.lib import s3_sync
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = s3_sync.main(["push", "--prefix", "hoa-records/", "--dir", "records",
                                 "state_corps.jsonl", "reviews.db", "tx_cookie.txt"])
        self.assertEqual(code, 2)
        self.assertIn("reviews.db", err.getvalue())
        self.assertIn("tx_cookie.txt", err.getvalue())


class TestMiamiDadeCivilFeed(unittest.TestCase):
    """records_miamidade_civil.py turns the Clerk's Civil FTP tables into
    trial-court docket records: association parties are picked out of the
    caption (insurers, banks, LLCs and managers captioned with "homeowners"
    or "condo" are not), each association's side is recorded, the sued
    property's ZIP is kept but no owner name or street address is, and every
    case gets the public Online Case Search link built from its encrypted
    token. Network-free: the encryptor is never called here."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.courts import records_miamidade_civil as m
        except Exception as exc:
            raise unittest.SkipTest(f"records_miamidade_civil import failed: {exc}")
        cls.m = m

    @staticmethod
    def _daily_zip(path):
        import zipfile
        cases = (
            "10097173^2026-002269-CA-01^02/03/2026^COSTA BRAVA CONDOMINIUM OF BELLE ISLE, INC  VS  ELYCIA F SOLKOFF ET AL^CA023^00303^CA30^132026CA00226901GE01^^^02/03/2026\r\n"
            "10253619^2026-126623-CC-25^09/22/2026^CALIFORNIA CLUB HOMES HOMEOWNER'S ASSOCIATION, INC. VS NOAH L. WALLER ET AL^CC006^00303^CC03^132026CC12662301GE25^^^09/22/2026\r\n"
            "10240257^2026-017800-CA-01^09/01/2026^ROSARIO LEYVA VS HOMEOWNERS CHOICE PROPERTY & CASUALTY INS CO^CA003^00303^CA23^132026CA01780001GE01^^09/20/2026^09/20/2026\r\n"
            "10240258^2026-017801-CA-01^09/01/2026^EPIC WEST CONDO LLC VS SMITH, JOHN^CA003^00303^CA23^132026CA01780101GE01^^^09/20/2026\r\n"
        )
        parties = (
            "1^10097173^2026-002269-CA-01^COSTA BRAVA CONDOMINIUM OF BELLE ISLE, INC^PN^17391^MATTINGLY, KARENA^^^1 East Broward Blvd.^Suite 1800^^^Fort Lauderdale^FL^33301\r\n"
            "2^10097173^2026-002269-CA-01^SOLKOFF, ELYCIA F^DN^^^^^11 Island Avenue, No. 512^^^^Miami Beach^FL^33139\r\n"
            "3^10097173^2026-002269-CA-01^WILMINGTON SAVINGS FUND SOCIETY^DN^92547^SILVER, JASON^^^1201 HAYS STREET^^^^Tallahassee^FL^32301\r\n"
            "4^10097173^2026-002269-CA-01^Rental/Eviction Property Address^LT^^^^^11 Island Avenue^^^^Miami Beach^FL^33139\r\n"
            "5^10253619^2026-126623-CC-25^HOMEOWNER'S ASSOCIATION, INC., CALIFORNIA CLUB HOMES^PN^^^^^606 Sw 114 Ave^^^^Miami^FL^33174\r\n"
            "6^10253619^2026-126623-CC-25^WALLER, NOAH L.^DN^^^^^20105 NW 37 Ave^^^^Miami Gardens^FL^33056\r\n"
            "7^10253619^2026-126623-CC-25^WALLER, SARA^DN^^^^^20105 NW 37 Ave^^^^Miami Gardens^FL^33056\r\n"
            "8^10240257^2026-017800-CA-01^LEYVA, ROSARIO^PN^^^^^^^^^Miami^FL^33125\r\n"
            "9^10240257^2026-017800-CA-01^HOMEOWNERS CHOICE PROPERTY & CASUALTY INS CO^DN^^^^^^^^^Tampa^FL^33607\r\n"
            "10^10240258^2026-017801-CA-01^EPIC WEST CONDO LLC^PN^^^^^^^^^Miami^FL^33131\r\n"
            "11^10240258^2026-017801-CA-01^SMITH, JOHN^DN^^^^^^^^^Miami^FL^33131\r\n"
        )
        casetype = ("25349^CA023^Condominium^2013-08-23 00:00:00^\r\n"
                    "27304^CC006^Mortgage/Real Property Foreclosure (County $8,001- $15K)^2020-09-30 00:00:00^\r\n"
                    "1^CA003^Contract & Indebtedness^2013-08-23 00:00:00^\r\n")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("CASES.EXP", cases)
            zf.writestr("PARTIES.EXP", parties)
            zf.writestr("CASETYPE.EXP", casetype)
            zf.writestr("DOCKETS.EXP", "")

    @staticmethod
    def _indebtedness_zip(path):
        import zipfile
        hdr = ("CASE_NUMBER^CASE_TYPE^JUDGE_SECT^FILE_DATE^PLAINTIFF_NAME^DEFENDANT_NAME^"
               "CASE_STATUS^DISPO_DATE^DISPO_CODE^DISPO_DESCRIPTION^NEW_SP_DT^SPPT_DT^SPPN_DT^"
               "SVRT_CD^SVRT_DT^SMRN_DT^DFLT_DT^DJUD_DT^MDFT_DT^MDDT_DT^ANWSER_CD^ANWSER_DT^"
               "MCAR_DT^FWNG_DT^FWND_DT^NSCA_DT^VOLD_DT^FJUD_DT^FJDF_DT^PN_ATTY_NAME^CLAIM_AMT^"
               "DN_STREET^DN_CITY^DN_STATE^DN_ZIP^NEXT_HRG_DT^\r\n")
        rows = (
            "2025-000603-CC-05^CC006A^03^2025-01-02^CENTURY PARK CONDOMINIUM NO. 2 ASSOCIATION, INC.^KOESSLER, ROBERT^PJREPACT^2025-05-13^CCDBH^^^^^^^^^2025-12-09^^^^^^^^^^^^ESTEVEZ, MATTHEW^^7463 SW 188TH TER^CUTLER BAY^FL^33157^\r\n"
            "2012-007835-SP-25^SP003^03^2012-04-09^PORTFOLIO RECOVERY ASSOC (LLC)^CARDOSO, CLEVER^RECLOSED^2014-10-14^DIDFLT^DJUD - Default Final Judgment^^^^^^^^^^^^^^^^^^^^DOMINGUEZ, JOSEPH C^1175.31^^^^^^\r\n"
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Indebtedness_20260921.txt", hdr + rows)

    def test_is_association_tiers(self):
        m = self.m
        known = {m.normalize("Miami Shores Property Owners Association Inc"),
                 m.normalize("Fountainview Association, Inc. #5")}
        for good in ("COSTA BRAVA CONDOMINIUM OF BELLE ISLE, INC",
                     "CALIFORNIA CLUB HOMES HOMEOWNER'S ASSOCIATION, INC.",
                     "THE MOORS MASTER MAINTENANCE ASSOCIATION, INC.",
                     "REGAL PALM ESTATES NEIGHBORHOOD ASSOCIATION INC",
                     "BRICKELL KEY MASTER ASSOCIATION, INC.",
                     "719 APARTMENT ASSOCIATION, INC.",
                     "Fountainview Association Inc #5"):          # tier 2: a name we hold
            self.assertTrue(m.is_association(good, known), good)
        for bad in ("HOMEOWNERS CHOICE PROPERTY & CASUALTY INS CO",
                    "HOMEOWNERS CHOICE PROPERTY & CASUALTY IN",   # truncated caption
                    "HOME OWNERS CLAIM EXPERT INC",
                    "EPIC WEST CONDO LLC", "FIRST CONDO MGMT (LLC)",
                    "CONDO OWNERS MANAGEMENT SERVICES (LLC)",
                    "PORTFOLIO RECOVERY ASSOC (LLC)",
                    "U.S. BANK NATIONAL ASSOCIATION",
                    "UNITED STATES POLO ASSOCIATION, INC",        # generic, not held
                    ""):
            self.assertFalse(m.is_association(bad, known), bad)

    def test_daily_zip_to_records(self):
        """Parsing a daily_civil zip keeps the two association cases with
        their side and the sued unit's ZIP (defendant address, only when the
        association is plaintiff), drops the insurer and the developer LLC,
        labels the court by division, and links via the encrypted token."""
        m = self.m
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "daily_civil_09232026.zip"
            self._daily_zip(z)
            casetypes = {}
            cases = m.parse_daily_zip(z, casetypes)
        self.assertEqual(len(cases), 4)
        self.assertEqual(casetypes["CC006"], "Mortgage/Real Property Foreclosure (County $8,001- $15K)")
        # LT rows (property address) and counsel are not parties
        self.assertEqual([p["type"] for p in cases["2026-002269-CA-01"]["parties"]], ["PN", "DN", "DN"])
        links = {"2026-002269-CA-01": "j7Qp%2Bg9iUYLZb05FebX1ewm%2FCPlrkS7wZ7gGGR9XxghXeOKuZY%2BP%2F6%2FR4qxADr7e"}
        recs = m.build_records(cases, casetypes, set(), links)
        self.assertEqual([r["docket_number"] for r in recs],
                         ["2026-002269-CA-01", "2026-126623-CC-25"])
        a, b = recs
        self.assertEqual(a["associations"], ["COSTA BRAVA CONDOMINIUM OF BELLE ISLE, INC"])
        self.assertEqual(a["association_role"], ["plaintiff"])
        self.assertEqual(a["zip"], "33139")                       # the unit, not the bank
        self.assertEqual(a["court"], "Miami-Dade County Circuit Court, Civil Division")
        self.assertEqual(a["nature_of_suit"], "Condominium")
        self.assertEqual((a["date_filed"], a["date_terminated"], a["cause"]),
                         ("2026-02-03", "", "Open"))
        self.assertEqual(a["url"], m.ocs_url(links["2026-002269-CA-01"]))
        self.assertTrue(a["has_public_link"])
        self.assertEqual((a["state"], a["county"], a["source"], a["case_data_id"]),
                         ("FL", "Miami-Dade", "fl_miamidade", "132026CA00226901GE01"))
        self.assertEqual(b["court"], "Miami-Dade County Court, Civil Division")
        self.assertEqual(b["zip"], "33056")
        self.assertEqual(b["url"], m.SEARCH_URL)                  # no token yet → search page
        self.assertFalse(b["has_public_link"])
        # The caption is the public case name (as in every court source);
        # what must never leave the feed is the party table: street
        # addresses, party rows, counsel.
        blob = json.dumps(recs)
        for private in ("Island Avenue", "NW 37 Ave", "Miami Gardens", "MATTINGLY", "parties"):
            self.assertNotIn(private, blob, f"party-table detail leaked: {private}")

    def test_indebtedness_zip_to_records(self):
        """The weekly backfile parses through the same shape: status from
        the dump, defendant ZIP as the property ZIP, debt buyers dropped."""
        m = self.m
        with tempfile.TemporaryDirectory() as d:
            z = Path(d) / "Indebtedness_20260921.zip"
            self._indebtedness_zip(z)
            cases = m.parse_indebtedness_zip(z)
        self.assertEqual(set(cases), {"2025-000603-CC-05", "2012-007835-SP-25"})
        recs = m.build_records(cases, {"CC006A": "Mortgage/Real Property Foreclosure (County $15,001 - $30K)"}, set(), {})
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["case_name"], "CENTURY PARK CONDOMINIUM NO. 2 ASSOCIATION, INC. vs KOESSLER, ROBERT")
        self.assertEqual((r["date_filed"], r["date_terminated"], r["cause"], r["zip"]),
                         ("2025-01-02", "2025-05-13", "Pjrepact", "33157"))
        self.assertEqual(r["nature_of_suit"], "Mortgage/Real Property Foreclosure (County $15,001 - $30K)")
        self.assertEqual(r["association_role"], ["plaintiff"])

    def test_ocs_url_is_the_public_case_page(self):
        u = self.m.ocs_url("abc%2Fdef")
        self.assertEqual(u, "https://www2.miamidadeclerk.gov/ocs/searchResults?qs=abc%2Fdef")


class TestCourtPartyGate(unittest.TestCase):
    """The Broward and Hillsborough portals match parties on leading words,
    so a query for "GOLDEN LAKES, A CONDO" also returns "Golden Lakes Medical
    Center Inc". party_is_association keeps the association and drops the
    business that shares its first words (or carries an association word
    next to a business word)."""

    def test_party_gate(self):
        from hoaspy.collect.courts.court_portals._common import party_is_association as ok
        core = "GOLDEN LAKES"
        for party in ("Golden Lakes Condominium Association Inc", "GOLDEN LAKES, A CONDO",
                      "Golden Lakes", "The Golden Lakes Inc", "Golden Lakes Phase II Assn",
                      "GOLDEN LAKES HOMEOWNERS ASSOCIATION, INC. et al"):
            self.assertTrue(ok(party, core), party)
        for party in ("Golden Lakes Medical Center Inc", "Golden Lakes Apartments LLC",
                      "Golden Lakes Fee Owner LP", "Golden Lakes Realty Corp",
                      "Third Avenue Chiropractic Ctr", "Homeowners Finance Co.",
                      "Sunrise Lakes Inc"):
            self.assertFalse(ok(party, core), party)
        self.assertTrue(ok("Third Avenue Condo of Hallandale, a Condo", "THIRD AVENUE"))
        self.assertFalse(ok("Third Avenue Chiropractic Ctr", "THIRD AVENUE"))
        self.assertTrue(ok("Gates of Westshore", "GATES OF WESTSHORE"))
        self.assertFalse(ok("Gates of Westshore Owner LLC", "GATES OF WESTSHORE"))
        # an association marker wins over trade words, not over a business form
        for party, core in (("Capital Ridge Homeowners Association", "CAPITAL RIDGE"),
                            ("Roberto Clemente Homes Condominium Assoc", "ROBERTO CLEMENTE HOMES"),
                            ("Alaska Medical Building Condominium Assoc", "ALASKA MEDICAL BUILDING"),
                            ("Church Ridge Estates Homeowners Association", "CHURCH RIDGE ESTATES")):
            self.assertTrue(ok(party, core), party)
        for party, core in (("Park Plaza Assoc Ltd", "PARK PLAZA"), ("Sherman Townhomes LLC", "SHERMAN"),
                            ("Townhouse Associates D/B/A Spring Garden Townhouses", "SPRING GARDEN")):
            self.assertFalse(ok(party, core), party)
        from hoaspy.collect.courts.court_portals._common import looks_like_association as la
        self.assertTrue(la("Harvest Queen Creek Community Association"))
        self.assertTrue(la("315 SEVENTH AVENUE CONDOMINIUM"))
        self.assertFalse(la("Homeowners Finance Co."))
        self.assertFalse(la("Ilikai Property Owner LLC"))
        self.assertFalse(la("Golden Lakes Medical Center Inc"))
        self.assertFalse(la("Park Plaza Assoc Ltd"), "LTD must be seen before stop words strip it")
        self.assertFalse(la("Community Association Underwriters of America"))
        self.assertTrue(la("Orangewood East Master Condominium Association, Inc. c/o Elite Property Management"))
        self.assertTrue(la("Jockey Club Condominium Apartments Inc"), "condo apartments are associations")
        self.assertTrue(ok("Sunrise Lakes Condominium Apts Phase I", "SUNRISE LAKES"))
        from hoaspy.collect.registries.get_gov_layers import clean_city
        self.assertEqual(clean_city("11010 Raven Ridge Rd"), "")
        self.assertEqual(clean_city("Po Box 97243"), "")
        self.assertEqual(clean_city("  Charlotte "), "Charlotte")
        # "Community Services Association" is a Texas HOA form, not a service company
        self.assertTrue(la("Walden on Lake Houston Community Services Association Inc"))
        self.assertFalse(la("Homeowner Association Services, Inc."))
        from hoaspy.collect.courts.court_portals._common import has_business_form
        self.assertTrue(has_business_form("Homeowners Finance Co."))
        self.assertTrue(has_business_form("Park Plaza Assoc Ltd"))
        self.assertFalse(has_business_form("First Colony Community Services Association"))
        # an activity word BEFORE the association word is part of the name
        for name in ("The Left Bank Condominium Association, Inc.", "Financial Center Condominium Office Association",
                     "Park National Bank Condominium Association, Inc.", "Purgatory Condominium Rental Association",
                     "Monument Meadows Property Owners Association, Ltd.", "Portview Condominium Association, PLLC",
                     "Board of Managers of the Riverview Condominium", "Jockey Club Condominium Apartments Inc"):
            self.assertFalse(has_business_form(name), name)
            self.assertTrue(la(name), name)
        for name in ("Homeowners Mutual Insurance Company", "Alliance of Community Association Managers",
                     "Community Association Insurance Solutions, LLC", "Redstone Condo, LLC",
                     "Jay Street Condominiums, Building 1, LP", "Park Plaza Assoc Ltd",
                     "Marys Lake Estates Homeowners Associates, Inc.", "Sumner Townhomes Fee Owner LLC"):
            self.assertTrue(has_business_form(name), name)
            self.assertFalse(la(name), name)



if __name__ == "__main__":
    unittest.main(verbosity=2)
