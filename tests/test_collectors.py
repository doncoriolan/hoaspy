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
from unittest import mock
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

    def test_town_repairs(self):
        """The "Town/City of X" capture is repaired from the report itself: a
        line wrap that cut the name short (City of Casa / Grande) is extended
        from the same name elsewhere in the text, words that are not the name
        (Payson Gila County, Tucson Standard Detail, Queen Creek Boundary) are
        trimmed, St./Ft. abbreviations survive, "…, Tucson, Pima County,
        Arizona" names the town when no "City of" does (but never a street),
        and the ZIP comes from the Location section itself before the
        local-services addresses, which may wrap too."""
        parse = self.az.parse_report
        wrapped = ("SUBDIVISION LOCATION: Northwest of the corner of East Kortsen Road, City of Casa\n"
                   "Grande, Pinal County, Arizona.\n\nSewage Disposal: City of Casa Grande, (520) 421-8600.\n"
                   "Water: City of Casa Grande, 510 E. Florence Blvd., Casa\nGrande, Arizona 85122.\n")
        p = parse(wrapped)
        self.assertEqual((p["city"], p["zip"]), ("Casa Grande", "85122"))
        county = ("SUBDIVISION LOCATION: E. Frontier Street and S. Ridgeway Lane Town of Payson Gila County,\n"
                  "Arizona.\n\nWater: Provider The Town of Payson 928-474-5242.\n"
                  "Sales office: 1000 N. Beeline Hwy, Payson, Arizona 85541.\n")
        p = parse(county)
        self.assertEqual((p["city"], p["zip"]), ("Payson", "85541"))
        junk = ("SUBDIVISION LOCATION: Sunrise Drive and Hacienda Del Sol Road., Tucson, Pima County, Arizona.\n\n"
                "Landscaping shall follow the planting guidelines of Pima County, City of Tucson Standard Detail\n"
                "WWM A-4.\nTucson Water, 310 W. Alameda St., Tucson, AZ 85701.\n")
        p = parse(junk)
        self.assertEqual((p["city"], p["zip"]), ("Tucson", "85701"))
        boundary = ("SUBDIVISION LOCATION: Between Kenworthy Road and Chandler Heights Road, south of Ocotillo\n"
                    "Road, within Pinal County, Arizona.\n\n\uf0b7 Town of Queen Creek Boundary, approximately 1 mile\n"
                    "\uf0b7 City of Mesa Boundary, approximately 3 miles\n")
        self.assertEqual(parse(boundary)["city"], "Queen Creek", "a boundary distance is a town-level placement, not a name")
        street = "SUBDIVISION LOCATION: Chandler Heights Road, Pinal County, Arizona.\n\n"
        self.assertEqual(parse(street)["city"], "", "a street before ', Pinal County, Arizona' is not a town")
        abbrev = "The well is 18 miles northeast of the Town of St. Johns. The principle aquifer is the Coconino\nSandstone.\n"
        self.assertEqual(parse(abbrev)["city"], "St. Johns")
        loczip = ("SUBDIVISION LOCATION: Northeast corner of Rancho Vistoso Blvd., Town of Oro\nValley, Pima County, "
                  "Arizona 85737. Major cross streets are Tangerine Road.\n\n"
                  "Sewer: Pima County Wastewater, Oro Valley, Arizona 85755.\nFire: Golder Ranch, Oro Valley, Arizona 85755.\n")
        p = parse(loczip)
        self.assertEqual((p["city"], p["zip"]), ("Oro Valley", "85737"),
                         "the Location section's own ZIP outranks the local-services majority")
        plain = "SUBDIVISION LOCATION: West of 91st Avenue, City of Mesa, Maricopa County, Arizona.\n\nWater: City of Mesa Gas Utility.\n"
        self.assertEqual(parse(plain)["city"], "Mesa", "an equally rare longer candidate must not extend a good name")
        cases = {   # location text -> town, for the "…, X, <County> County, Arizona" rule
            "2831 Tonto Dr, .Lake Havasu City, Mohave County, State of Arizona .": "Lake Havasu City",
            "with entrances at Desperado Way, in the City Phoenix, Maricopa County, State of Arizona.": "Phoenix",
            "Master Planned Community of San Tan 320, Unincorporated Pinal\nCounty, Pinal County, State of Arizona.":
                "unincorporated Pinal County",
            "East of Lovers Lane, Unincorporated, Apache County, State of Arizona.": "unincorporated Apache County",
            "Northwest corner of Avenida Compadres, Pima County, Arizona.": "",
            "Entering into the City of St. Johns from Hwy 60. Turn South on 24th West.": "St. Johns",
            "South of Hayward Avenue Phoenix, Maricopa County, Arizona.": "Phoenix",
            "North of the intersection of Grand and North Dysart Roads, Maricopa County, Arizona.": "",
            "Latimore Drive, In Bullhead City, Mohave County, Arizona.": "Bullhead City",
            "Northwest corner of 1st St. Tucson, Pima County, Arizona.": "Tucson",
            "Section 12, Gila and Salt River Meridian, Maricopa County, Arizona.": "",
            "East of Circle City, Maricopa County, Arizona.": "Circle City",
            "Tucson, Pima County Arizona – Rosalynn Place\n\nWater: City of Tucson- Water Dept.": "Tucson",
            "18 miles northeast of town, St. Johns, Apache County, Arizona.": "St. Johns",
            "Highway 95 and Joy Lane, Ft. Mohave, Mohave County, Arizona.\n\nWater: 1234 Hwy 95, Ft. Mohave, AZ 86426.": "Ft. Mohave",
        }
        for text, want in cases.items():
            self.assertEqual(parse(f"SUBDIVISION LOCATION: {text}\n\n")["city"], want, text)
        spaced = ("SUBDIVISION LOCATION: Latimore Drive and Clark Farms Boulevard, Town of Ma r a n a , Pima County, Arizona.\n\n"
                  "Water: Town of Marana Municipal Water (520) 382-2570.\nSewage Disposal: Town of Marana Municipal Water.\n")
        self.assertEqual(parse(spaced)["city"], "Marana", "a letter-spaced print falls back to the rest of the report")
        informal = ("SUBDIVISION LOCATION: McCulloch Blvd and Malibu Drive- Lake Havasu City – Mohave County - Arizona\n\n"
                    "Water: City of Lake Havasu City. (928) 855-2618\nSewage Disposal: City of Lake Havasu City. (928) 855-2618\n"
                    "Streets: City of Lake Havasu with costs for maintenance.\nFire: City of Lake Havasu with costs.\n"
                    "Garbage: City of Lake Havasu with costs.\nOffice: 2330 McCulloch Blvd, Lake Havasu City, AZ 86403.\n")
        p = parse(informal)
        self.assertEqual((p["city"], p["zip"]), ("Lake Havasu City", "86403"),
                         "the report's own addresses pick the official name over the more frequent informal one")

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
            5: {"id": 5, "pdf": True, "registration_no": "DM26-000005", "legal_name": "TAMARACK RESORT",
                "date_issued": "1/9/2026", "county": "Valley County Idaho", "associations": ["Tamarack Owners Association"]},
            6: {"id": 6, "pdf": True, "registration_no": "DM26-000006", "legal_name": "MAUI CONDOS",
                "date_issued": "1/9/2026", "county": "Out Of State", "associations": ["Maui Condos Association"]},
            7: {"id": 7, "pdf": True, "registration_no": "DM26-000007", "legal_name": "GOODYEAR PHASE 1",
                "date_issued": "1/9/2026", "county": "City Of Goodyear, Maricopa County,",
                "associations": ["Goodyear Homeowners Association"]},
        }
        az.TEXT_DIR = ROOT / "tests" / "fixtures" / "nope"        # no cached text -> keep given parse
        rows = az.to_records(details)
        self.assertEqual(sorted(r["name"] for r in rows), ["Arlington Estates at South Mountain Homeowners Association",
                                                           "Goodyear Homeowners Association"],
                         "out-of-state land registered for sale in Arizona (Idaho, 'Out Of State') is not an AZ row")
        goodyear = next(r for r in rows if r["name"].startswith("Goodyear"))
        self.assertEqual(goodyear["county"], "Maricopa", "the county is read out of the card's free text")
        r = next(r for r in rows if r["name"].startswith("Arlington"))
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
        # Maryland's statutory condominium form, with and without normalize()'s stop words
        for name in ("Council of Unit Owners of Fairmont 1001", "COUNCIL OF CO-OWNERS OF PRINCE PLACE AT",
                     "The Council of Unit Owners of Treover, a Condominium, Inc.",
                     "Board of Directors of Woodview Village"):
            self.assertTrue(la(name), name)


class TestMdCaseSearchParsing(unittest.TestCase):
    """Maryland Judiciary Case Search adapter — no network. The fixture is
    real party rows from the portal's /api-caselist/v1/cases (individuals in
    captions replaced by Doe/Roe): what a name query and each statewide
    sweep keep, drop and merge; the query prefix and match core derived
    from a registry name; the date-window split at the 600-row cap; and
    the 403 / no-results / 429 answers."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.courts.court_portals import md_casesearch as md
        except Exception as exc:
            raise unittest.SkipTest(f"md_casesearch import failed: {exc}")
        cls.md = md
        cls.rows = json.loads((ROOT / "tests" / "fixtures" / "md_casesearch_rows.json").read_text())

    def _shape(self, queried, known=None):
        md = self.md
        sweep = queried.lower() in md.SWEEPS
        return md.shape(self.rows, queried, "" if sweep else md.match_core(queried), sweep, known)

    def test_query_prefix_and_match_core(self):
        md = self.md
        # distinctive core as the prefix; a one-word core takes the next word along
        self.assertEqual(md.query_for("Waters Edge At North Lake Condominium, Inc."), "Waters Edge At North Lake")
        self.assertEqual(md.query_for("Grosvenor Homeowners Association, Inc."), "Grosvenor Homeowners")
        self.assertEqual(md.query_for("FAIRHAVEN RESIDENTS ASSOCIATION INC"), "FAIRHAVEN RESIDENTS")
        self.assertEqual(md.query_for("The Villages of Urbana HOA"), "Villages of Urbana")
        # a Maryland form is searched whole, and matched on the community inside it
        self.assertEqual(md.query_for("HOMEOWNERS ASSOCIATION OF WEXFORD INC"), "HOMEOWNERS ASSOCIATION OF WEXFORD")
        self.assertEqual(md.match_core("HOMEOWNERS ASSOCIATION OF WEXFORD INC"), "WEXFORD")
        self.assertEqual(md.match_core("Council of Unit Owners of Fairmont 1001"), "FAIRMONT 1001")
        self.assertEqual(md.match_core("Grosvenor Homeowners Association, Inc."), "GROSVENOR")
        self.assertEqual(md.match_core("Waters Edge At North Lake Condominium"), "WATERS EDGE AT NORTH LAKE")
        self.assertEqual(md.query_for("   "), "")

    def test_name_query_keeps_the_association_and_drops_lookalikes(self):
        recs = self._shape("Wexford Homeowners Association, Inc.")
        by = {r["docket_number"]: r for r in recs}
        self.assertIn("06-01-0014065-1993", by)
        self.assertIn("06-01-0006161-1999", by)                 # "WEXFORD HOMEOWNERS ASSOC."
        self.assertIn("08-04-0002140-2001", by)                 # another Wexford community, kept under its own name
        self.assertEqual(by["08-04-0002140-2001"]["associations"], ["WEXFORD GARDEN CONDOMINIUM"])
        self.assertNotIn("02-02-0000956-2004", by)              # Wexford Health Sources
        self.assertNotIn("236720V", by)                         # Wexford Bancgroup LLC
        r = by["06-01-0014065-1993"]
        self.assertEqual(r["state"], "MD")
        self.assertEqual(r["court"], "Rockville District Court")
        self.assertEqual(r["county"], "Montgomery")
        self.assertEqual(r["date_filed"], "1993-06-16")
        self.assertEqual(r["association_role"], ["plaintiff"])
        self.assertEqual(r["source"], "md_casesearch")
        self.assertEqual(r["queries"], ["Wexford Homeowners Association, Inc."])
        self.assertEqual(r["url"], "https://casesearch.courts.state.md.us/casesearch/case-detail-page?caseId=060100140651993")
        for k in ("case_name", "court", "docket_number", "date_filed", "date_terminated",
                  "nature_of_suit", "cause", "url", "state", "associations", "case_data_id"):
            self.assertIn(k, r)

    def test_homeowners_sweep_keeps_the_form_drops_the_trade(self):
        recs = self._shape("homeowners")
        by = {r["docket_number"]: r for r in recs}
        self.assertEqual(by["10-01-0000118-2014"]["associations"], ["HOMEOWNERS' ASSN. OF WOODLAND VILLAGE"])
        self.assertIn("07-02-0005879-2014", by)                 # Homeowners of Dorchester Assoc
        # the same case under two party rows -> one record carrying both roles
        self.assertEqual(by["10-01-0002660-2006"]["association_role"], ["defendant", "plaintiff"])
        # c/o manager tail dropped from the association name
        self.assertEqual(by["D-09-CV-18-001661"]["associations"], ["HOMEOWNERS ASSOCIATION OF WEXFORD, INC"])
        for cn in ("10121V",            # Homeowners Loan Corp
                   "08-C-11-001431",    # Homeowners Association Dispute Review Board
                   "11-01-0001445-1992",  # bare "HOMEOWNERS ASSOCIATION INC" names no community
                   "79785V",            # title & escrow company
                   "03-C-97-000347"):   # "… Ltd" is a business form
            self.assertNotIn(cn, by, cn)
        self.assertEqual(by["10-01-0002660-2006"]["county"], "Howard")

    def test_council_sweep_drops_the_bare_form(self):
        recs = self._shape("council of unit owners")
        by = {r["docket_number"]: r for r in recs}
        self.assertNotIn("D-08-CV-26-027427", by)               # "COUNCIL OF UNIT OWNERS" alone
        self.assertNotIn("24-L-86-002287", by)
        self.assertIn("D-05-CV-25-007501", by)
        self.assertEqual(by["C-15-JG-25-013454"]["associations"],
                         ["COUNCIL OF UNIT OWNERS OF MILESTONE II TOWNHOUSE CONDOMINIUM"])
        self.assertIn("D-06-CV-26-021560", by)                  # Mill's Choice: CHOICE is a name here
        self.assertEqual(by["D-05-CV-26-008780"]["association_role"], ["other"])
        self.assertEqual(by["D-05-CV-25-007501"]["county"], "Prince George's")

    def test_board_sweep_needs_a_marker_or_a_held_community(self):
        by = {r["docket_number"]: r for r in self._shape("board of directors of")}
        self.assertIn("06-02-0027850-2001", by)                 # … of Avonshire HOA Inc
        self.assertNotIn("05-02-0016405-2001", by)              # … of Woodview Village: no marker, not held
        self.assertNotIn("C-24-CV-25-003764", by)               # … of Rebound, Inc.
        self.assertNotIn("C-24-CV-25-004842", by)               # … of a church
        by = {r["docket_number"]: r for r in self._shape("board of directors of", known={"WOODVIEW VILLAGE"})}
        self.assertIn("05-02-0016405-2001", by)
        self.assertEqual(by["06-02-0027850-2001"]["county"], "Montgomery")   # "Silver Spring District Court 02"

    def test_titles_and_counties(self):
        md = self.md
        self.assertEqual(md.county_of("Howard County District Court"), "Howard")
        self.assertEqual(md.county_of("Prince George's County Circuit Court"), "Prince George's")
        self.assertEqual(md.county_of("Saint Mary's County District Court"), "St. Mary's")
        self.assertEqual(md.county_of("Civil District Court"), "Baltimore City")
        self.assertEqual(md.county_of("Baltimore City Circuit Court"), "Baltimore City")
        self.assertEqual(md.county_of("Upper Marlboro District Court"), "Prince George's")
        self.assertEqual(md.county_of("Bel-Air District Court"), "Harford")
        self.assertEqual(md.county_of("Somewhere Else"), "")
        self.assertEqual(md.case_url("D-101-CV-26-013077"),
                         "https://casesearch.courts.state.md.us/casesearch/case-detail-page?caseId=D101CV26013077")
        # a null caption falls back to the association; newlines in captions collapse
        rows = [{"caseNumber": "1V", "fullName": "COUNCIL OF UNIT OWNERS OF ZED CONDOMINIUM", "partyTypeDisplay": "Plaintiff",
                 "locationName": "Rockville District Court", "caseType": "Contract", "caseStatus": "Open",
                 "filingDate": "01/02/2020", "title": None},
                {"caseNumber": "2V", "fullName": "COUNCIL OF UNIT OWNERS OF ZED CONDOMINIUM", "partyTypeDisplay": "AKA",
                 "locationName": "Rockville District Court", "caseType": "Contract", "caseStatus": "Open",
                 "filingDate": "01/02/2020", "title": "COUNCIL OF UNIT OWNERS OF ZED\nvs\nSOMEONE"}]
        recs = {r["docket_number"]: r for r in md.shape(rows, "council of unit owners", "", True)}
        self.assertEqual(recs["1V"]["case_name"], "COUNCIL OF UNIT OWNERS OF ZED CONDOMINIUM")
        self.assertEqual(recs["2V"]["case_name"], "COUNCIL OF UNIT OWNERS OF ZED vs SOMEONE")
        self.assertEqual(recs["2V"]["association_role"], ["other"])

    def test_capped_windows_are_split_by_filing_date(self):
        """A 600-row answer means the portal truncated: the window is halved
        until every half is under the cap, and a single capped day is kept
        (and noted) rather than split forever."""
        import datetime as dt
        md = self.md
        c = md.Client.__new__(md.Client)
        c.pace = 0
        c.capped = []
        calls = []

        def fake_query(prefix, d0=None, d1=None):
            calls.append((d0, d1))
            days = (d1 - d0).days + 1
            n = md.CAP if days > 10 or d0 == dt.date(2020, 1, 1) else 3
            return [{"caseNumber": f"{d0}-{i}", "fullName": "COUNCIL OF UNIT OWNERS OF Y CONDOMINIUM",
                     "partyTypeDisplay": "Plaintiff", "locationName": "Towson District Court", "caseType": "Contract",
                     "caseStatus": "Open", "filingDate": "01/01/2020", "title": "x vs y"} for i in range(n)]
        c._query = fake_query
        rows = c._rows("council of unit owners", dt.date(2020, 1, 1), dt.date(2020, 2, 9))
        self.assertGreater(len(calls), 4)
        self.assertEqual(calls[0], (dt.date(2020, 1, 1), dt.date(2020, 2, 9)))     # whole window first
        self.assertTrue(all(d0 <= d1 for d0, d1 in calls))
        leaves = [(d0, d1) for d0, d1 in calls if (d1 - d0).days + 1 <= 10]
        self.assertTrue(leaves and all(d1.toordinal() - d0.toordinal() < 10 for d0, d1 in leaves))
        self.assertEqual(c.capped, ["council of unit owners 2020-01-01"])
        self.assertTrue(any(r["caseNumber"].startswith("2020-01-01") for r in rows))

    def test_portal_answers(self):
        md = self.md
        from hoaspy.collect.courts.court_portals import RateLimited

        class Resp:
            def __init__(self, code, body=None, headers=None):
                self.status_code, self._body, self.headers = code, body, headers or {}
                self.text = json.dumps(body) if body is not None else ""
            def json(self):
                if self._body is None:
                    raise ValueError("no json")
                return self._body
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise AssertionError("unexpected")

        c = md.Client("msal=x; datadome=abc123")
        self.assertEqual(c.s.headers["Cookie"], "datadome=abc123")
        self.assertEqual(md.Client("abc123").s.headers["Cookie"], "datadome=abc123")
        with self.assertRaises(ValueError):
            md.Client("")
        # the cookie file may name the browser the cookie came from; the client hints follow it
        linux = md.Client("datadome=abc123\nUser-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36\n")
        self.assertEqual(linux.s.headers["Cookie"], "datadome=abc123")
        self.assertIn("X11; Linux", linux.s.headers["User-Agent"])
        self.assertEqual(linux.s.headers["sec-ch-ua-platform"], '"Linux"')
        self.assertIn('v="152"', linux.s.headers["sec-ch-ua"])
        self.assertEqual(c.s.headers["sec-ch-ua-platform"], '"macOS"')

        class S:
            def __init__(self, resp):
                self.resp = resp
            def post(self, *a, **k):
                return self.resp
        c.s = S(Resp(403, {"url": "https://geo.captcha-delivery.com/captcha/?x"}))
        with self.assertRaises(PermissionError):
            c._query("wexford")
        c.s = S(Resp(400, {"code": 404, "error": "CaseSearch will only display results for cases that exist"}))
        self.assertEqual(c._query("zzzz"), [])
        c.s = S(Resp(429, headers={"Retry-After": "120"}))
        with self.assertRaises(RateLimited) as cm:
            c._query("wexford")
        self.assertEqual(cm.exception.retry_after, 120)
        c.s = S(Resp(200, [{"caseNumber": "1"}]))
        self.assertEqual(c._query("wexford"), [{"caseNumber": "1"}])

    def test_browser_transport_reloads_once_on_a_refusal(self):
        """A `cdp:` line in the cookie file sends every search through a real
        Chrome tab as the page's own fetch(); a 403 reloads the portal page
        once (the device check passes again) and retries, a second 403 stops
        the run resumably, and the usual answers are parsed the same way."""
        md = self.md

        class FakeTab:
            def __init__(self, endpoint):
                self.endpoint, self.answers, self.reloads, self.posted = endpoint, [], 0, []
            def ready(self, reload=False):
                self.reloads += reload
            def post(self, body):
                self.posted.append(body)
                return self.answers.pop(0)

        real = md._Tab
        md._Tab = FakeTab
        try:
            c = md.Client("cdp: http://127.0.0.1:9612\n", pace=0)
            self.assertEqual(c.tab.endpoint, "http://127.0.0.1:9612")
            self.assertEqual(c.s.headers["Cookie"], "datadome=")
            c.tab.answers = [(403, '{"url": "https://geo.captcha-delivery.com/captcha/?x"}', ""),
                             (200, '[{"caseNumber": "1"}]', "")]
            self.assertEqual(c._query("wexford"), [{"caseNumber": "1"}])
            self.assertEqual(c.tab.reloads, 1)
            self.assertEqual(c.tab.posted[0]["businessName"], "wexford")
            self.assertEqual(c.tab.posted[0]["caseType"], "CIVIL")
            c.tab.answers = [(403, "{}", ""), (403, "{}", "")]
            with self.assertRaises(PermissionError):
                c._query("wexford")
            c.tab.answers = [(400, '{"code": 404, "error": "no cases"}', "")]
            self.assertEqual(c._query("zzz"), [])
            c.tab.answers = [(429, "", "90")]
            from hoaspy.collect.courts.court_portals import RateLimited
            with self.assertRaises(RateLimited) as cm:
                c._query("wexford")
            self.assertEqual(cm.exception.retry_after, 90)
            c.tab.answers = [(500, "boom", "")]
            with self.assertRaises(RuntimeError):
                c._query("wexford")
        finally:
            md._Tab = real
        # the fetch the tab runs is same-origin, credentialed and JSON
        js = md._FETCH_JS % ('"/api"', '"{}"')
        self.assertIn('credentials: "include"', js)
        self.assertIn('method: "POST"', js)

    def test_driver_names_include_irs_roster_and_sweeps(self):
        """get_state_courts queries the IRS exempt-organization roster too, and
        appends an adapter's SWEEPS after the names unless --no-sweeps or
        --names FILE is given."""
        from hoaspy.collect.courts import get_state_courts as gsc
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "records").mkdir()
            (root / "records" / "irs_exempt_orgs.jsonl").write_text(json.dumps(
                {"state": "MD", "name": "FAIRHAVEN RESIDENTS ASSOCIATION INC"}) + "\n")
            (root / "records" / "state_registries.jsonl").write_text(json.dumps(
                {"state": "MD", "name": "Grosvenor Homeowners Association, Inc."}) + "\n"
                + json.dumps({"state": "VA", "name": "Not Mine Homeowners Association"}) + "\n")
            old = gsc.ROOT
            gsc.ROOT = root
            try:
                names = gsc.association_names("MD")
                self.assertEqual(names, ["Grosvenor Homeowners Association, Inc.",
                                         "FAIRHAVEN RESIDENTS ASSOCIATION INC"])

                class Args:
                    names = None; limit = None; no_sweeps = False
                class Mod:
                    STATE = "MD"; SWEEPS = ("council of unit owners", "homeowners")
                self.assertEqual(gsc.portal_names(Mod, Args), names + ["council of unit owners", "homeowners"])
                Args.no_sweeps = True
                self.assertEqual(gsc.portal_names(Mod, Args), names)
                Args.names = root / "names.txt"
                Args.names.write_text("Only This One\n")
                self.assertEqual(gsc.portal_names(Mod, Args), ["Only This One"])
            finally:
                gsc.ROOT = old


class TestVaGdcParsing(unittest.TestCase):
    """Virginia General District Court civil name search (va_gdc), against a
    captured result page and case-detail page (`tests/fixtures/va_gdc_*.html`,
    real markup; the individuals named in them are replaced with placeholders).
    No network, no browser: the CDP session is stubbed."""

    NAME = "MONTCLAIR PROPERTY OWNERS ASSOCIATION INC"

    @classmethod
    def setUpClass(cls):
        from hoaspy.collect.courts.court_portals import va_gdc
        cls.va = va_gdc
        cls.results = (ROOT / "tests" / "fixtures" / "va_gdc_results.html").read_text()
        cls.detail = (ROOT / "tests" / "fixtures" / "va_gdc_detail.html").read_text()

    def test_query_levels(self):
        q = self.va.query_levels
        # one-word core: never searched bare — first letter of the association word, then narrower
        self.assertEqual(q(self.NAME), [["MONTCLAIR P"], ["MONTCLAIR PROP", "MONTCLAIR POA"]])
        self.assertEqual(q("SALISBURY HOMEOWNERS ASSOCIATION"), [["SALISBURY H"], ["SALISBURY HOME", "SALISBURY HOA"]])
        # two-word core goes in bare first
        self.assertEqual(q("LAKE MONTICELLO OWNERS ASSOCIATION"),
                         [["LAKE MONTICELLO"], ["LAKE MONTICELLO O"], ["LAKE MONTICELLO OWNE"]])
        self.assertEqual(q("THE GINTER PARK RESIDENTS ASSOCIATION")[0], ["GINTER PARK"])
        # no usable core: the whole name, cut to the field's 30 characters
        self.assertEqual(q("PROPERTY OWNERS OF SHENANDOAH FARMS INC"), [["PROPERTY OWNERS OF SHENANDOAH"]])
        self.assertEqual(q("COMMUNITY ASSOCIATIONS INSTITUTE"), [["COMMUNITY ASSOCIATIONS INSTITU"]])
        self.assertEqual(q("CAMERON MEWS LTD"), [["CAMERON MEWS"]])
        for name in (self.NAME, "BROYHILLS ADDITION TO LAKEVALE ESTATES COMMUNITY ASSOCIATION"):
            for level in q(name):
                for prefix in level:
                    self.assertLessEqual(len(prefix), self.va.MAX_QUERY)
        self.assertEqual(self.va.query_name(self.NAME), "MONTCLAIR P")

    def test_party_matches(self):
        ok = self.va.party_matches
        for party in ("MONTCLAIR PROERTY OWNERS ASSOCIATION INC", "MONTCLAIR PROPERT OWNERS ASSOCIATION INC",
                      "MONTCLAIR PROPERTY OWNERS ASOOCIATION, INC.", "MONTCLAIR PROPERTY OWNERS ASSCIATION INC",
                      "MONTCLAIR PROPERTY OWNERS ASSOCIAITON INC", "MONTCLAIR PROPERTY OWNERS ASSOC",
                      "MONTCLAIR POA", "MONTCLAIR PROPERTY OWNERS ASSOC (GARNISHEE)"):
            self.assertTrue(ok(party, self.NAME), party)
        for party in ("MONTCLAIR PLAZA LLC", "MONTCLAIR PARK HOA", "MONTCLAIR", "MONTCLAIR PROPERTIES LLC",
                      "MONTCLAIR CONDOMINIUM UNIT OWNERS ASSOC", "OWNER01, SAMPLE"):
            self.assertFalse(ok(party, self.NAME), party)
        lm = "LAKE MONTICELLO OWNERS ASSOCIATION"
        for party in ("LAKE MONTICELLO OWNER'S ASN", "LAKE MONTICELLO OWNERS' ASSN.", "LAKE MONTICELLO OWNERS'S ASSN"):
            self.assertTrue(ok(party, lm), party)
        for party in ("LAKE MONTICELLO VOLUNTEER FIRE DEPT", "LAKE MONTICELLO"):
            self.assertFalse(ok(party, lm), party)
        # an association of another kind that shares the core is someone else
        self.assertFalse(ok("FOREST HILLS CONDOMINIUM ASSOC", "FOREST HILLS COMMUNITY ASSOCIATION"))
        self.assertTrue(ok("FOREST HILLS COMM ASSN", "FOREST HILLS COMMUNITY ASSOCIATION"))
        self.assertFalse(ok("VERONA CIVIC ASSOC", "VERONA COMMUNITY ASSOCIATION"))
        self.assertFalse(ok("SALISBURY HEIGHTS HOA", "SALISBURY HOMEOWNERS ASSOCIATION"))
        self.assertTrue(ok("SALISBURY HOME OWNERS ASSOC", "SALISBURY HOMEOWNERS ASSOCIATION"))
        self.assertFalse(ok("VILLAGE GREEN APARTMENTS LLC", "VILLAGE GREEN COMMUNITY ASSOCIATION"))
        # an extra word after the core names a neighbouring association, not ours
        cg = "COURTHOUSE GREEN PROPERTY OWNERS ASSOCIATION"
        self.assertFalse(ok("COURTHOUSE GREEN FIRST HOMES ASSOCIATION INC", cg))
        for party in ("COURTHOUSE GREEN PROPERTY OWNERS ASSOCIATION", "COURTHOUSE GREEN POA",
                      "COURTHOUSE GREEN PEOPERTY OWNERS ASSOC"):
            self.assertTrue(ok(party, cg), party)
        self.assertFalse(ok("COURTHOUSE GREEN OWNERS ASSOCIATION", cg))
        # words after the association words are part of who it is
        rs = "WINTERGREEN PROPERTY OWNERS VOLUNTEER RESCUE SQUAD INC"
        self.assertFalse(ok("WINTERGREEN PROPERTY OWNERS ASSOCIATION", rs))
        self.assertFalse(ok("WINTERGREEN PROPERTY OWNERS", rs))
        self.assertTrue(ok("WINTERGREEN PROPERTY OWNERS VOL RESCUE SQUAD", rs))
        self.assertFalse(ok("FAIR HARBOR PROPERTY OWNERS ASSOCIATION", "FAIR HARBOR PROPERTY OWNERS SWIM ASSOCIATION"))
        self.assertTrue(ok("AQUIA HARBOUR PROPERTY OWNERS", "AQUIA HARBOUR PROPERTY OWNERS ASSOCIATION INC"))
        # the IRS file runs a care-of name on after INC
        self.assertTrue(ok("BELLAIR OWNERS ASSOCIATION", "BELLAIR OWNERS ASSOCIATION INC RALPH L FEIL"))
        self.assertEqual(self.va.query_levels("BELLAIR OWNERS ASSOCIATION INC RALPH L FEIL")[0], ["BELLAIR O"])
        # the kind is recognised through a clerk's misspelling too
        for party, name in (("LAKE LAND OR POPERTY OWNERS ASSOCIATION", "LAKE LAND OR PROPERTY OWNERS ASSOC"),
                            ("LITTLE ROCKY RUN HOWEOWNERS ASSOCIATION", "LITTLE ROCKY RUN HOMEOWNERS ASSOCIATION")):
            self.assertTrue(ok(party, name), party)
        self.assertFalse(ok("STONE RIDGE TOWNES HOMEOWNERS ASSOCIATION INC", "STONE RIDGE ASSOCIATION INC"))
        self.assertFalse(ok("TIMBERLAKE COMMONS HOMEOWNERS ASSOCIATION", "TIMBERLAKE COMMUNITY ASSOCIATION"))
        self.assertTrue(ok("TIMBERLAKE COMMINITY ASSOC", "TIMBERLAKE COMMUNITY ASSOCIATION"))
        self.assertFalse(ok("HARTSHORN COMMUNITY ASSOCIATION", "HARTSHORN COMMUNITY COUNCIL"))
        # a party that does not say what kind of association it is cannot stand in for one that does
        self.assertFalse(ok("VILLAGE GREEN OWNERS ASSOCIATION", "VILLAGE GREEN COMMUNITY ASSOCIATION"))
        self.assertTrue(ok("LAKE MONTICELLO ASSOCIATION", "LAKE MONTICELLO OWNERS ASSOCIATION"))
        # sister associations in one development differ only by kind
        sr = "SUGARLAND RUN HOMEOWNERS ASSOCIATIO INC"
        self.assertTrue(ok("SUGARLAND RUN HOMEOWNERS ASSOCIATION INC", sr))
        self.assertTrue(ok("SUGARLAND RUN HOA", sr))
        for party in ("SUGARLAND RUN TOWNHOUSE OWNERS ASSOCIATION", "SUGARLAND RUN TOWNHOUSE OWNERS"):
            self.assertFalse(ok(party, sr), party)
        self.assertFalse(ok("SUDLEY PLACE HOA", "SUDLEY PLACE TOWNHOUSE ASSOCIATION"))
        self.assertFalse(ok("CAMPUS EAST TOWNHOMES", "CAMPUS EAST COMMUNITY ASSOCIATION"))
        self.assertTrue(ok("BURKE TOWNHOUISE HOMEOWNERS ASSOCIATION", "BURKE TOWNHOUSE HOMEOWNERS ASSOCIATION"))
        self.assertTrue(ok("DANBURY FOREST COMMUNNITY ASSOCIATION", "DANBURY FOREST COMMUNITY ASSOCIATION"))
        # names with no association word match letter for letter (or clerk-truncated) only
        self.assertTrue(ok("KINGSTOWNE RESIDENTIAL OWNERS CORP", "KINGSTOWNE RESIDENTIAL OWNERS CORPORATION"))
        self.assertTrue(ok("CAMERON MEWS LTD", "CAMERON MEWS LTD"))
        self.assertFalse(ok("CAMERON MEWS LLC", "CAMERON MEWS LTD"))
        # "ASSOCIATES" is a firm, not a misspelt association
        self.assertFalse(ok("STONE RIDGE ASSOCIATES", "STONE RIDGE ASSOCIATION INC"))
        self.assertTrue(ok("STONE RIDGE ASSOC", "STONE RIDGE ASSOCIATION INC"))

    def test_parse_courts_keeps_civil_dockets_only(self):
        courts = self.va.parse_courts(self.results)
        self.assertEqual(courts, [("001", "Accomack General District Court"),
                                  ("059", "Fairfax County General District Court"),
                                  ("703", "Newport News-Civil General District Court"),
                                  ("710", "Norfolk General District Court"),
                                  ("153", "Prince William General District Court")])

    def test_parse_results(self):
        page = self.va.parse_results(self.results)
        self.assertEqual(len(page["rows"]), self.va.PAGE_ROWS)
        self.assertTrue(page["next"])
        self.assertEqual(page["counter"], "2")
        self.assertEqual(page["cursor"], {
            "firstRowName": "MONTCLAIR PLAZA LLC", "firstRowCaseNumber": "GV25026738-00",
            "lastRowName": "MONTCLAIR PROPERTY OWNERS ASSOCIAITON INC", "lastRowCaseNumber": "GV21001050-03"})
        self.assertEqual(page["rows"][2], {
            "number": "GV19013223-00", "plaintiff": "MONTCLAIR PROERTY OWNERS ASSOCIATION INC",
            "defendant": "OWNER02, SAMPLE", "hearing_date": "2019-09-18", "result": "Plaintiff",
            "type": "Warrant In Debt"})
        empty = self.va.parse_results("<html><script>var searchCounter=7</script><table></table></html>")
        self.assertEqual((empty["rows"], empty["next"], empty["counter"]), ([], False, "7"))

    def test_fixture_names_no_private_individual(self):
        """The fixtures ship in a public repo: every party that is not an
        association or a company must be a placeholder."""
        for row in self.va.parse_results(self.results)["rows"]:
            for party in (row["plaintiff"], row["defendant"]):
                self.assertRegex(party, r"MONTCLAIR|LLC$|^OWNER\d\d, SAMPLE$")
        text = re.sub(r"<[^>]+>", "\n", re.search(r"<main>(.*?)</main>", self.detail, re.S).group(1))
        people = {l.strip() for l in text.splitlines() if re.fullmatch(r"\s*[A-Z][A-Z0-9' -]+, [A-Z][A-Z .;]+\s*", l)}
        self.assertEqual(people, {"OWNER02, SAMPLE"})

    def test_parse_detail(self):
        det = self.va.parse_detail(self.detail)
        self.assertEqual(det["number"], "GV19013223-00")
        self.assertEqual(det["date_filed"], "2019-07-29")
        self.assertEqual(det["type"], "Warrant In Debt")
        self.assertEqual(det["judgment"], {
            "Judgment": "Plaintiff", "Principal Amount": "$605.00", "Costs": "$56.00", "Attorney Fees": "500.00",
            "Interest Award": "10% FROM DOJ", "Is Judgment Satisfied": "Yes", "Date Satisfaction Filed": "04/18/2023"})

    def test_build_records_one_per_case(self):
        rows = self.va.parse_results(self.results)["rows"]
        det = self.va.parse_detail(self.detail)
        recs = self.va.build_records(self.NAME, "153", "Prince William General District Court", rows,
                                     {det["number"]: det})
        by = {r["docket_number"]: r for r in recs}
        # 20 rows -> 12 cases: the LLC's case is dropped, later actions fold into their case
        self.assertEqual(len(recs), 12)
        self.assertNotIn("GV25026738-00", by)
        r = by["GV19013223-00"]
        self.assertEqual(r["case_name"], "MONTCLAIR PROERTY OWNERS ASSOCIATION INC v. OWNER02, SAMPLE")
        self.assertEqual((r["date_filed"], r["cause"], r["nature_of_suit"]),
                         ("2019-07-29", "Judgment for plaintiff", "Warrant In Debt"))
        self.assertEqual(r["judgment"]["Principal Amount"], "$605.00")
        self.assertEqual((r["source"], r["state"], r["court"], r["court_fips"], r["case_data_id"]),
                         ("va_gdc", "VA", "Prince William General District Court", "153", "153-GV19013223"))
        self.assertEqual((r["queries"], r["associations"], r["association_role"], r["url"]),
                         ([self.NAME], ["MONTCLAIR PROERTY OWNERS ASSOCIATION INC"], ["plaintiff"], self.va.BASE))
        # a warrant in debt and its three garnishments are one case with four actions
        g = by["GV21001050-00"]
        self.assertEqual([a["number"][-2:] for a in g["actions"]], ["00", "01", "02", "03"])
        self.assertEqual([a["type"] for a in g["actions"]], ["Warrant In Debt"] + ["Garnishment"] * 3)
        self.assertEqual((g["hearing_date"], g["filed_year"], g["date_filed"]), ("2022-10-19", 2021, ""))
        # only later actions still online: the earliest one leads, no filing date is invented
        late = by["GV11004738-04"]
        self.assertEqual((late["nature_of_suit"], late["date_filed"], late["filed_year"], len(late["actions"])),
                         ("Garnishment", "", 2011, 3))
        # the association as defendant
        d = by["GV16014977-00"]
        self.assertEqual((d["association_role"], d["associations"], d["cause"]),
                         (["defendant"], ["MONTCLAIR PROPERTY OWNERS ASSOC"], "Non-suit"))
        # same docket shape as every other trial-court adapter
        from hoaspy.collect.courts.court_portals._common import record
        base = set(record(key="k", state="VA", case_name="a", court="c", docket_number="d",
                          associations=["a"], url="u", queried="q"))
        self.assertTrue(base <= set(r))
        self.assertEqual(self.va.filed_year("GV99000001-00"), 1999)

    def _client(self, pages):
        """A Client whose _post serves canned pages: {prefix: [rows-per-page, …]}."""
        c = self.va.Client(pace=0)
        c.details = False
        c.asked = []

        def row(n, plaintiff):
            return {"number": f"GV24{n:06d}-00", "plaintiff": plaintiff, "defendant": "OWNER01, SAMPLE",
                    "hearing_date": "2024-05-01", "result": "Plaintiff", "type": "Warrant In Debt"}

        def post(fips, query, action, cursor=None):
            i = 0 if action == "newSearch" else cursor["page"] + 1
            c.asked.append((query, i))
            plan = pages.get(query, [])
            party, count = plan[i] if i < len(plan) else ("", 0)
            return {"rows": [row(i * 100 + k, party) for k in range(count)], "next": i + 1 < len(plan),
                    "counter": "0", "cursor": {"page": i}}
        c._post = post
        return c

    def test_flooded_prefix_is_narrowed(self):
        name = "VILLAGE GREEN COMMUNITY ASSOCIATION"
        noise, ours = ("VILLAGE GREEN APARTMENTS LLC", 20), ("VILLAGE GREEN COMMUNITY ASSOC", 20)
        c = self._client({"VILLAGE GREEN": [noise] * 40, "VILLAGE GREEN C": [ours, ours]})
        rows = c._court_rows("059", name)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(r["plaintiff"] == ours[0] for r in rows))
        # gave up on the bare core after FLOOD_PAGES[0] pages, did not page through the apartments
        self.assertEqual([q for q, _ in c.asked].count("VILLAGE GREEN"), self.va.FLOOD_PAGES[0])

    def test_busy_association_is_paged_out_not_narrowed(self):
        name = "LAKE MONTICELLO OWNERS ASSOCIATION"
        ours = ("LAKE MONTICELLO OWNERS ASSN", 20)
        c = self._client({"LAKE MONTICELLO": [ours] * 9})
        rows = c._court_rows("065", name)
        self.assertEqual(len(rows), 180)
        self.assertEqual({q for q, _ in c.asked}, {"LAKE MONTICELLO"})

    def test_search_reopens_a_timed_out_session(self):
        c = self._client({"MONTCLAIR P": [("MONTCLAIR POA", 2)]})
        opened = []

        class Portal:
            cdp = object()
            courts = [("153", "Prince William General District Court"), ("059", "Fairfax County General District Court")]
            def open(self):
                opened.append(1)
        c.portal = Portal()
        real_post, calls = c._post, []

        def flaky(fips, query, action, cursor=None):
            calls.append(fips)
            if len(calls) == 1:
                raise self.va._SessionLost("status 200")
            return real_post(fips, query, action, cursor)
        c._post = flaky
        with mock.patch.object(self.va.time, "sleep"):
            recs = c.search(self.NAME)
        self.assertEqual(opened, [1])
        self.assertEqual(calls, ["153", "153", "059"])           # the interrupted court was redone
        self.assertEqual(sorted({r["court_fips"] for r in recs}), ["059", "153"])
        self.assertEqual(len(recs), 4)

    def test_registered_with_the_driver(self):
        from hoaspy.collect.courts import court_portals
        mod = court_portals.registry()["va_gdc"]
        self.assertEqual((mod.STATE, mod.NEEDS_COOKIE), ("VA", False))
        self.assertTrue({"name", "url", "access", "coverage", "caveat"} <= set(mod.INFO))


class TestPaUjsCaptionMatching(unittest.TestCase):
    """Pennsylvania MDJ captions abbreviate the association ("Hemlock Farms
    Community Assoc." in 331 of 411 cases, spelled out in 9), so pa_ujs
    searches the name's leading words plus a stem and folds abbreviations
    before comparing parties."""

    @classmethod
    def setUpClass(cls):
        from hoaspy.collect.courts.court_portals import pa_ujs
        cls.m = pa_ujs

    def test_query_is_leading_words_plus_stem(self):
        q = self.m.query_name
        for name, want in (
                ("HEMLOCK FARMS COMMUNITY ASSOCIATION", "HEMLOCK FARMS Comm%"),
                ("Lake Meade Property Owners Association, Inc.", "Lake Meade Prop%"),
                ("LAKEVIEW CONDOMINIUM ASSOCIATION", "LAKEVIEW Condo%"),
                ("WALTON WOODS HOME OWNERS ASSOCIATION", "WALTON WOODS HOME%"),
                ("The Estates of Warwick Community Association, a Planned Community",
                 "The Estates of Warwick Comm%"),
                ("ELK MANOR ESTATES HOA INC", "ELK MANOR ESTATES H%"),
                ("GREEN HOA", "GREEN HOA%"),
                # distinguishing words after the generic ones, a generic word
                # first, or none at all: searched whole
                ("Rental Property Owners' Association of Lebanon County",
                 "Rental Property Owners' Association of Lebanon County%"),
                ("RESIDENTS ASSOCIATION LIMA ESTATES", "RESIDENTS ASSOCIATION LIMA ESTATES%"),
                ("Townhome, Inc.", "Townhome%"),
                ("HOMES FOR MANHEIM", "HOMES FOR MANHEIM%")):
            self.assertEqual(q(name), want, name)

    def test_party_matches_folds_caption_abbreviations(self):
        ok = self.m.party_matches
        name = "HEMLOCK FARMS COMMUNITY ASSOCIATION"
        for party in ("Hemlock Farms Community Assoc.", "Hemlock Farms Comm Assoc",
                      "Hemlock Farms Comm.Assoc.", "HEMLOCK FARMS COMMUNITY ASSOCI",
                      "Hemlock Farms Community Assn.", "Hemlock Farms Community"):
            self.assertTrue(ok(party, name), party)
        for party in ("Hemlock Farms", "Hemlock Farms Realty", "Hemlock Farms Condo Assoc",
                      "Hemlock Farms Community Services LLC", ""):
            self.assertFalse(ok(party, name), party)
        self.assertTrue(ok("Lake Meade Propertyowners", "LAKE MEADE PROPERTY OWNERS ASSOCIATION INC"))
        self.assertTrue(ok("Elk Manor Estates Home Owner's Assn", "ELK MANOR ESTATES HOA INC"))
        self.assertTrue(ok("Green H.O.A.", "GREEN HOA"))
        self.assertTrue(ok("Hazelwood Green Property Owners Association",
                           "HAZELWOOD GREEN PROPERTY OWNERS ASS OCIATION"), "IRS field break")
        self.assertFalse(ok("Southpointe II Property Owners Assoc",
                            "SOUTHPOINTE PROPERTY OWNERS ASSOCIATION INC"), "a different body")
        self.assertFalse(ok("Townhome Associates", "Townhome, Inc."))
        self.assertTrue(ok("Mh Townhomes LLC", "Mh Townhomes Llc"), "the roster's own spelling")

    def test_search_keeps_matching_parties_under_the_queried_name(self):
        row = dict.fromkeys(self.m.COLS, "")
        hits = [dict(row, docket_number="MJ-60302-CV-0000123-2019", court_type="Magisterial District",
                     caption="Hemlock Farms Community Assoc. v. Doe, Jane", case_status="Closed",
                     filing_date="04/02/2019", county="Pike", court_office="MDJ-60-3-02"),
                dict(row, docket_number="MJ-43202-CV-0000099-1997", court_type="Magisterial District",
                     caption="Weseloh & Co. v. Hemlock Farms Comm Assoc, et al", filing_date="07/09/1997",
                     county="Monroe", court_office="MDJ-43-2-02"),
                dict(row, docket_number="MJ-60302-CV-0000007-2020", court_type="Magisterial District",
                     caption="Hemlock Farms Realty v. Roe, Sam", filing_date="01/09/2020")]
        client = self.m.Client()
        asked = []
        client._post = lambda q: asked.append(q) or hits
        recs = client.search("HEMLOCK FARMS COMMUNITY ASSOCIATION")
        self.assertEqual(asked, ["HEMLOCK FARMS Comm%"])
        self.assertEqual([r["docket_number"] for r in recs],
                         ["MJ-60302-CV-0000123-2019", "MJ-43202-CV-0000099-1997"])
        self.assertEqual([r["association_role"] for r in recs], [["plaintiff"], ["defendant"]])
        # the queried name, not the caption's abbreviation, is what build_site joins on
        self.assertEqual(recs[1]["associations"], ["HEMLOCK FARMS COMMUNITY ASSOCIATION"])
        self.assertIn("Hemlock Farms Comm Assoc", recs[1]["case_name"])
        self.assertEqual(recs[0]["date_filed"], "2019-04-02")
        self.assertEqual(recs[0]["nature_of_suit"], "Magisterial District — Civil")


class TestOhSupremeParsing(unittest.TestCase):
    """Supreme Court of Ohio docket adapter (`oh_supreme`) against six real
    cases captured from `Ajax.ashx` (`tests/fixtures/oh_supreme_cases.json`:
    search rows plus GetCaseDetails answers, dockets and attorneys trimmed,
    individuals replaced by Doe/Roe placeholders) — no network. Guards the phrase built from a roster name, the gate that
    tells a community association from an insurer / developer / person
    wearing the words, the record shape (level, county, lower court, roles,
    disposition, deep link) and the 1,000-row cap split."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.courts.court_portals import oh_supreme
        except Exception as exc:
            raise unittest.SkipTest(f"oh_supreme import failed: {exc}")
        cls.m = oh_supreme
        cls.fx = json.loads((ROOT / "tests" / "fixtures" / "oh_supreme_cases.json").read_text())

    def test_adapter_is_registered_and_anonymous(self):
        from hoaspy.collect.courts import court_portals
        self.assertIs(court_portals.registry()["oh_supreme"], self.m)
        self.assertEqual((self.m.STATE, self.m.NEEDS_COOKIE, self.m.LEVEL), ("OH", False, "supreme"))
        for k in ("name", "url", "access", "coverage", "caveat"):
            self.assertTrue(self.m.INFO[k], k)

    def test_query_phrase_from_a_roster_name(self):
        q = self.m.query_for
        self.assertEqual(q("WELLINGTON HILLS HOMEOWNERS ASSOCIATION"), "WELLINGTON HILLS")
        self.assertEqual(q("APPLE VALLEY PROP OWNERS ASSN INC"), "APPLE VALLEY")
        self.assertEqual(q("THE MILLS CREEK-EAST ASSOCIATION"), "MILLS CREEK-EAST")
        self.assertEqual(q("ZOAR COMMUNITY ASSOCIATION"), "ZOAR COMMUNITY")     # one-word core
        self.assertEqual(q("COMMUNITY ASSOCIATIONS INSTITUTE"), "COMMUNITY ASSOCIATIONS INSTITUTE")
        # the clerk writes "and" where the roster writes "&": both are searched
        self.assertEqual(self.m.phrases_for("HILLS & DALES HOME OWNERS ASSOCIATION"),
                         ["HILLS & DALES", "HILLS and DALES"])
        self.assertEqual(self.m.phrases_for("Hills and Dales Owners Association"),
                         ["Hills and Dales", "Hills & Dales"])
        self.assertEqual(self.m.phrases_for("WELLINGTON HILLS HOMEOWNERS ASSOCIATION"), ["WELLINGTON HILLS"])
        self.assertEqual(self.m.case_url("2026-0797"),
                         "https://www.supremecourt.ohio.gov/clerk/ecms/#/caseinfo/2026/0797")

    def test_named_query_keeps_only_the_same_community(self):
        ok = self.m.same_community
        self.assertTrue(ok("Hills and Dales Owners Association", "HILLS & DALES HOME OWNERS ASSOCIATION"))
        self.assertTrue(ok("Apple Valley Property Owners Association, Inc.", "APPLE VALLEY PROP OWNERS ASSN INC"))
        self.assertTrue(ok("Candlewood Lake Association, Inc., et al.", "CANDLEWOOD LAKE ASSOCIATION INC"))
        self.assertTrue(ok("Edgewater Home Owners' Assn", "EDGEWATER HOMEOWNERS ASSOCIATION"))
        # one-word core: the next word must agree too
        self.assertFalse(ok("Edgewater Condominium Association", "EDGEWATER HOMEOWNERS ASSOCIATION"))
        self.assertFalse(ok("Smith Overlook Condominium Association", "OVERLOOK ASSOCIATION"))
        self.assertFalse(ok("Wellington Hills LLC", "WELLINGTON HILLS HOMEOWNERS ASSOCIATION"))
        self.assertFalse(ok("Wellington Hills", "WELLINGTON HILLS HOMEOWNERS ASSOCIATION"))

    def test_sweep_gate_keeps_communities_only(self):
        cn = self.m.community_name
        for party, want in (
                ("Cobblestone Lane Condominium Association, Inc.", "Cobblestone Lane Condominium Association, Inc."),
                ("Green Cove Resort I Owners' Association", "Green Cove Resort I Owners' Association"),
                ("Marine Towers East Condominum Owners' Association, Inc.",
                 "Marine Towers East Condominum Owners' Association, Inc."),
                ("East Bank at Hayden Falls Condominium Association", "East Bank at Hayden Falls Condominium Association"),
                ("Lake Mohawk Property Owners Associated, Incorporated", "Lake Mohawk Property Owners Associated, Incorporated"),
                ("Eagle Ridge Subdivision Property Owners Association, Incorporated, et al.",
                 "Eagle Ridge Subdivision Property Owners Association, Incorporated"),
                ("Board of Directors of The Chelsea Condominium", "Chelsea Condominium"),
                ("Ottawa Street Condominium Association Board of Trustees", "Ottawa Street Condominium Association"),
                ("Officers of Homeowners' Association of Tweed Lakes, Inc.", "Homeowners' Association of Tweed Lakes, Inc."),
                ("Mary Doe-Roe, President, Lost Hollow Property Owners Association Board of Directors",
                 "Lost Hollow Property Owners Association")):
            self.assertEqual(cn(party), want, party)
        for party in ("Auto-Owners Insurance Company", "Home Owners Insurance Company", "Owners Ins Co",
                      "East Bank Condominiums II, LLC", "The Condominiums at Stonebridge, Ltd.",
                      "US Bank Trust, National Association, as Trustee of American Homeowner Preservation Trust Series 2014A",
                      "Omni Community Association Managers, LLC", "Athens Cty Property Owners Assn Inc",
                      "Affected Property Owners", "Property Owners", "Lot Owners", "Highland Park Owners Inc",
                      "Independence Homeowners-Citizens", "Woodside Terrace Mobile Home Owners",
                      "Pat Condo", "Stratford Chase Townhouses", "Heather Lake Association",
                      "Blanchard Valley Farmers Cooperative, Inc.", "John R. Doe, c/o A. Richard Doe, POA"):
            self.assertEqual(cn(party), "", party)

    def test_shape_builds_the_docket_record(self):
        d = self.fx["details"]
        r = self.m.shape(d["2026-0797"], "condo", True)
        self.assertEqual(r["case_name"], "John A. Doe v. Cobblestone Lane Condominium Association, Inc.")
        self.assertEqual((r["court"], r["docket_number"], r["date_filed"], r["date_terminated"]),
                         ("Supreme Court of Ohio", "2026-0797", "2026-06-24", "2026-09-15"))
        self.assertEqual((r["nature_of_suit"], r["cause"], r["state"], r["source"], r["level"]),
                         ("Jurisdictional Appeal", "Disposed", "OH", "oh_supreme", "supreme"))
        self.assertEqual(r["associations"], ["Cobblestone Lane Condominium Association, Inc."])
        self.assertEqual(r["association_role"], ["appellant"])
        self.assertEqual((r["county"], r["lower_court"], r["lower_court_case"]),
                         ("Summit", "9th District Court of Appeals", ["31501"]))
        self.assertEqual(r["url"], "https://www.supremecourt.ohio.gov/clerk/ecms/#/caseinfo/2026/0797")
        self.assertTrue(r["disposition"].startswith("Jurisdiction declined."))
        self.assertNotIn("<", r["disposition"])
        self.assertEqual(r["queries"], ["condo"])
        for k in ("case_name", "court", "docket_number", "date_filed", "date_terminated",
                  "nature_of_suit", "cause", "state", "associations", "url"):
            self.assertIn(k, r, k)                       # what build_site.add_courts reads

    def test_shape_original_action_has_no_county_or_lower_court(self):
        r = self.m.shape(self.fx["details"]["2017-0797"], "property owners", True)
        self.assertEqual(r["associations"], ["Lost Hollow Property Owners Association"])
        self.assertEqual(r["association_role"], ["respondent"])
        self.assertEqual(r["nature_of_suit"], "Original Action in Mandamus")
        for k in ("county", "lower_court", "lower_court_case"):
            self.assertNotIn(k, r)

    def test_shape_drops_people_insurers_developers_and_bystanders(self):
        d = self.fx["details"]
        self.assertIsNone(self.m.shape(d["1990-2459"], "condo", True))          # Pat Condo
        self.assertIsNone(self.m.shape(d["1995-0135"], "owners", True))         # Auto-Owners Insurance
        r = self.m.shape(d["2010-2092"], "condo", True)                         # the LTD is the developer
        self.assertEqual(r["associations"], ["The Condominiums at Stonebridge Owners' Association"])
        r = self.m.shape(d["2002-0152"], "homeowner", True)                     # the Alias row is not a party
        self.assertEqual(r["associations"], ["Hickory Creek Homeowners Association"])
        # an association that only files as a friend of the court is not in the dispute
        amicus = json.loads(json.dumps(d["1995-0135"]))
        amicus["Parties"].append({"Name": "Rubicon Mill Condominium Association", "ProSe": False,
                                  "Type": "Amicus Curiae on behalf of Appellant", "Attorneys": []})
        self.assertIsNone(self.m.shape(amicus, "condo", True))

    def test_shape_named_query_matches_the_roster_name(self):
        d = self.fx["details"]["2026-0797"]
        r = self.m.shape(d, "COBBLESTONE LANE CONDOMINIUM ASSOCIATION", False)
        self.assertEqual(r["associations"], ["Cobblestone Lane Condominium Association, Inc."])
        self.assertIsNone(self.m.shape(d, "COBBLESTONE CHASE HOMEOWNERS ASSOCIATION", False))

    def test_client_splits_a_capped_search_and_caches_details(self):
        import datetime as dt
        m, fx = self.m, self.fx
        calls = []
        full = [dict(fx["search"][0], CaseNumber=f"1990-{i:04d}") for i in range(m.CAP)]

        def fake_call(**form):
            calls.append(form)
            if form["action"] == "GetCaseDetails":
                return fx["details"].get(f'{form["paramCaseYear"]}-{form["paramCaseNumber"]}', "Too many results")
            if form["paramPartyEntityName"] == "big" and form["paramCaseFiledFrom"] == "01-01-1980":
                # the whole range hits the cap; the first half-window holds one row
                whole = form["paramCaseFiledTo"] == dt.date.today().strftime("%m-%d-%Y")
                return full if whole else fx["search"][:1]
            if form["paramPartyEntityName"] == "big":
                return fx["search"][1:2]
            return fx["search"]

        c = m.Client(pace=0)
        c._call = fake_call
        rows = c._search("big", m.FIRST_DAY, dt.date.today())
        self.assertEqual([r["CaseNumber"] for r in rows], ["2026-0797", "2017-0797"])
        self.assertEqual(len([f for f in calls if f["action"] == "CaseSearch"]), 3)
        self.assertEqual(c.capped, [])
        recs = c.search("condo")
        self.assertEqual(sorted(r["docket_number"] for r in recs),
                         ["2002-0152", "2010-2092", "2017-0797", "2026-0797"])
        n = len([f for f in calls if f["action"] == "GetCaseDetails"])
        self.assertEqual(n, len(fx["search"]))
        c.search("COBBLESTONE LANE CONDOMINIUM ASSOCIATION")       # same cases: details come from the cache
        self.assertEqual(len([f for f in calls if f["action"] == "GetCaseDetails"]), n)
        self.assertEqual(c.search(""), [])



class TestCookLiensParsing(unittest.TestCase):
    """Cook County (IL) recorder liens (`get_cook_liens` + `records_cook`)
    against three pages captured from the Clerk's Recordings System
    (`tests/fixtures/cook_crs_*.html`: an Advanced Search results page and
    two document pages; document numbers, PINs, addresses and every
    individual's or contractor's name replaced by placeholders) — no
    network. Guards the results-page and detail-page parsing, the choice of
    the association among the parties, the document-type vocabulary, the
    walk back through the 1,000-row cap, and a whole run against a stand-in
    for the site."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.liens import get_cook_liens, records_cook
        except Exception as exc:
            raise unittest.SkipTest(f"cook liens import failed: {exc}")
        cls.rc, cls.driver = records_cook, get_cook_liens
        fx = ROOT / "tests" / "fixtures"
        cls.results = (fx / "cook_crs_results.html").read_text()
        cls.lien = (fx / "cook_crs_detail_lien.html").read_text()
        cls.mechanics = (fx / "cook_crs_detail_mechanics.html").read_text()

    def test_results_page_rows_pages_and_total(self):
        rows, npages = self.rc.parse_result_page(self.results)
        self.assertEqual((len(rows), npages, self.rc.result_total(self.results)), (6, 4, 340))
        lien = next(r for r in rows if r["doc_number"] == "2600000006")
        self.assertEqual((lien["recorded"], lien["executed"], lien["doc_type_label"], lien["consideration"]),
                         ("1/29/2026", "1/28/2026", "LIEN", "$1,520.20"))
        self.assertEqual((lien["grantor1"], lien["grantee1"], lien["pin"]),
                         ("ENCLAVE AT GALEWOOD CROSSINGS MASTER ASSN", "DOE RICHARD", "00-00-000-000-1004"))
        self.assertEqual(lien["detail_href"], "/Document/Detail?dId=SAMPLEDID&hId=SAMPLEHID")   # &amp; decoded
        self.assertEqual([r["doc_type_label"] for r in rows],
                         ["LIS PENDENS FORECLOSURE"] * 3 + ["LIEN", "LIEN", "MECHANICS LIEN"])
        self.assertEqual(self.rc.parse_result_page("<html><body>no table</body></html>"), ([], 1))
        self.assertIsNone(self.rc.result_total("<html></html>"))
        # a search with no matches is an answer (zero documents, term finished), not a lost session
        from datetime import date
        from types import SimpleNamespace
        empty = ('<form><input name="__RequestVerificationToken" value="t"></form>'
                 '<div class="alert">No Document(s) found</div>')
        self.assertEqual(self.rc.result_total(empty), 0)
        self.assertEqual(self.rc.parse_result_page(empty)[0], [])
        self.assertIsNone(self.rc.next_cursor([], 0, date(2026, 10, 1)))
        crs = self.rc.CookCRS(pace=0)
        url = self.rc.BASE + "/Search/ResultAddt?id1=%23collapse2"
        self.assertFalse(crs._lost(SimpleNamespace(url=url, text=empty)))
        self.assertTrue(crs._lost(SimpleNamespace(url=url, text=empty.replace("No Document(s) found", ""))),
                        "the search form coming back with neither rows nor that message is a lost session")

    def test_detail_page_keeps_parties_apart_and_decodes_entities(self):
        p = self.rc.parse_detail(self.lien)
        self.assertEqual((p["doc_number"], p["doc_type"], p["recorded"], p["executed"]),
                         ("2600000001", "LIEN", "4/14/2025", "4/14/2025"))
        self.assertEqual(p["filers"], ["ARTHUR & CALIFORNIA CONDO ASSN INC"])      # "&amp;" in the page
        self.assertIn("ARTHUR &amp; CALIFORNIA", self.lien)
        self.assertEqual(len(p["respondents"]), 6, "one entry per party, never a joined string")
        self.assertEqual(p["respondents"][:2], ["DOE SUSAN TR", "DOE JOHN TRUST"])
        self.assertEqual((p["amount"], p["pin"], p["address"]),
                         (10005.91, "00-00-000-000-1001", "100 SAMPLE ST UNIT 1, CHICAGO"))

    def test_association_gate_rejects_banks_llcs_and_owners_at_large(self):
        ok = self.rc.is_association_party
        for name in ("WESTGATE TERR CONDO ASSN", "ARTHUR & CALIFORNIA CONDO ASSN INC",
                     "1216 ASTOR CONDOMINIUM UNIT OWNERS ASSOCIATION", "BROOKSIDE PROPERTY OWNERS ASSN",
                     "1030 NATL HONORE CONDO ASSN", "LAKE SHORE BK CONDO ASSN", "BANKSTON MEADOWS HOMEOWNERS ASSN",
                     "SAUGANASH VLG ASSOCIATION", "1317 N LARRABEE ASSN", "RIVERSIDE CONDO ASSOC",
                     "BOARD OF MANAGERS MALIBU"):
            self.assertTrue(ok(name), name)
        for name in ("US BK NATL ASSN", "JPMORGAN CHASE BK NATL ASSN", "WELLS FARGO BANK NATIONAL ASSOCIATION",
                     "FEDERAL NATIONAL MTGE ASSN", "TALMAN HOME FED SAV & LOAN ASSN", "TEACHERS INS & ANNUITY ASSN",
                     "CONSUMERS COOPERATIVE CU",
                     # ASSOC = "Associates": architects and contractors, not communities
                     "TENG & ASSOC INC", "SEARL & ASSOC ARCHITECTS PC", "MIDWEST CONST ASSOC INC",
                     "ALEXANDER GAMMIE ASSOC PLUMBING & HEATING CO", "JOHN BELMONT & ASSN INC", "SMITH ASSOC",
                     "BOARD OF MANAGERS",
                     "VILLAGE GREENE CONDO ASSN ALSIP LLC", "ROCKET MTG LLC", "DOE JOHN", "",
                     "401 INDIVIDUAL UNIT OWNERS", "ALL UNIT OWNERS AND NEWPORT RLTY MGMT", "UNKNOWN OWNERS"):
            self.assertFalse(ok(name), name)
        # the filer side wins; a bank beside the association is never picked
        self.assertEqual(self.rc.pick_association(["US BK NATL ASSN", "A CONDO ASSN"], ["B CONDO ASSN"]),
                         ("A CONDO ASSN", "filer"))
        self.assertEqual(self.rc.pick_association(["US BK NATL ASSN"], ["DOE JOHN", "B CONDO ASSN"]),
                         ("B CONDO ASSN", "respondent"))
        self.assertEqual(self.rc.pick_association(["ROCKET MTG LLC"], ["DOE JOHN"]), ("", ""))

    def test_lien_record_has_the_shared_shape(self):
        rec = self.rc.to_record(self.rc.parse_detail(self.lien), "2026-10-01T00:00:00+00:00")
        self.assertEqual((rec["doc_id"], rec["doc_type"], rec["doc_type_label"]), ("2600000001", "LIE", "claim_of_lien"))
        self.assertEqual((rec["recorded_date"], rec["recorded_ymd"], rec["year"]), ("2025-04-14", "2025-04-14", 2025))
        self.assertEqual((rec["state"], rec["county"], rec["association"]),
                         ("IL", "Cook", "ARTHUR & CALIFORNIA CONDO ASSN INC"))
        self.assertEqual((rec["filers"], len(rec["respondents"]), rec["n_parties"]),
                         (["ARTHUR & CALIFORNIA CONDO ASSN INC"], 6, 7))
        self.assertEqual((rec["amount"], rec["parcel_id"], rec["property_address"]),
                         (10005.91, "00-00-000-000-1001", "100 SAMPLE ST UNIT 1, CHICAGO"))
        self.assertEqual((rec["case_number"], rec["legal_description"], rec["source"], rec["source_page"]),
                         ("", "", "cook-crs", "https://crs.cookcountyclerkil.gov/Search"))
        shared = {"doc_id", "doc_type", "doc_type_label", "recorded_date", "recorded_ymd", "year", "state", "county",
                  "association", "filers", "respondents", "n_parties", "amount", "case_number", "parcel_id",
                  "legal_description", "property_address", "source", "source_page", "retrieved_at"}
        self.assertEqual(set(rec), shared)
        self.assertNotIn("&amp;", json.dumps(rec))

    def test_mechanics_lien_naming_a_bank_is_a_lien_against_the_association(self):
        p = self.rc.parse_detail(self.mechanics)
        self.assertEqual(p["doc_type"], "MECHANICS LIEN")
        self.assertEqual(p["filers"], ["SAMPLE CONST CORP", "ARGENT MTG CO LLC", "MERS INC", "US BK NATL ASSN"])
        self.assertIn("4068 S LAKE PK AVE CONDO ASSN", p["respondents"])
        rec = self.rc.to_record(p, "t")
        self.assertEqual((rec["doc_type"], rec["doc_type_label"]), ("LXA", "lien_against_association"),
                         "the bank's ASSN must not make this a lien the association filed")
        self.assertEqual(rec["association"], "4068 S LAKE PK AVE CONDO ASSN")
        self.assertEqual((rec["year"], rec["amount"], rec["n_parties"]), (2021, 14732.0, 11))

    def test_document_types_and_what_is_dropped(self):
        assn, owner, bank = ["A CONDO ASSN"], ["DOE JOHN"], ["US BK NATL ASSN"]
        kind = lambda t, f, r: (self.rc.classify(t, f, r) or (None,))[0]        # noqa: E731
        for t in ("LIEN", "CORRECTED LIEN", "MECHANICS LIEN"):
            self.assertEqual(kind(t, assn, owner), "LIE", t)
        for t in ("LIS PENDENS FORECLOSURE", "AMENDED LIS PENDENS FORECLOSURE", "CORRECTED LIS PENDENS FORECLOSURE"):
            self.assertEqual(self.rc.classify(t, assn, owner), ("LP", "lis_pendens", "A CONDO ASSN"), t)
        self.assertEqual(kind("LIEN", ["SAMPLE CONST CORP"], assn), "LXA")
        self.assertEqual(kind(" mechanics   lien ", ["SAMPLE CONST CORP"], assn), "LXA")
        why = self.rc.drop_reason
        self.assertEqual(why("LIEN", assn, owner), "")
        self.assertEqual(why("LIS PENDENS FORECLOSURE", bank, owner + assn), "foreclosure_by_another_party")
        self.assertIsNone(self.rc.classify("LIS PENDENS FORECLOSURE", bank, owner + assn))
        self.assertEqual(why("FEDERAL LIEN", ["INTERNAL REVENUE SERVICE"], assn), "excluded_type")
        self.assertEqual(why("RELEASE", assn, owner), "excluded_type")
        self.assertEqual(why("LIEN", bank, owner), "no_association_party")
        self.assertEqual(why("NOTICE", assn, owner), "unmapped_type")
        self.assertEqual(why("", assn, owner), "excluded_type")

    def test_results_row_is_enough_when_a_first_party_is_the_association(self):
        rows, _ = self.rc.parse_result_page(self.results)
        need = {r["doc_number"]: self.rc.needs_detail(r) for r in rows}
        self.assertEqual(need, {"2600000001": True, "2600000003": True, "2600000004": True,
                                "2600000006": False, "2600000007": False, "2600000008": False})
        lien = next(r for r in rows if r["doc_number"] == "2600000006")
        rec = self.rc.to_record(self.rc.row_to_parsed(lien), "t")
        self.assertEqual((rec["doc_type"], rec["association"], rec["respondents"], rec["amount"]),
                         ("LIE", "ENCLAVE AT GALEWOOD CROSSINGS MASTER ASSN", ["DOE RICHARD"], 1520.2))
        self.assertEqual((rec["recorded_ymd"], rec["parcel_id"], rec["property_address"], rec["n_parties"]),
                         ("2026-01-29", "00-00-000-000-1004", "", 2))
        mech = next(r for r in rows if r["doc_number"] == "2600000008")
        rec = self.rc.to_record(self.rc.row_to_parsed(mech), "t")
        self.assertEqual((rec["doc_type"], rec["association"]), ("LXA", "LAKE ARLINGTON TOWNE MASTER ASSN"))
        # the results page cuts names at 50 characters: such a row is recorded, and its page is wanted
        cut = dict(lien, doc_number="2600000099", grantor1="LEXINGTON COMMONS COACH HOUSES CONDOMINIUM ASSOCIA")
        self.assertEqual(len(cut["grantor1"]), self.rc.NAME_CAP)
        self.assertTrue(self.rc.truncated(cut))
        self.assertFalse(self.rc.truncated(lien))
        self.assertFalse(self.rc.needs_detail(cut))
        index = {r["doc_number"]: r for r in rows + [cut]}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(self.driver, "DETAILS", Path(tmp)):
            decided = {k: r for k, r in index.items() if not self.rc.needs_detail(r)}
            self.assertEqual([r["doc_number"] for r in self.driver.detail_queue(decided, "needed")], ["2600000099"])
            self.assertEqual([r["doc_number"] for r in self.driver.detail_queue(decided, "all")],
                             ["2600000099", "2600000006", "2600000007", "2600000008"])

    def test_walk_back_through_the_row_cap(self):
        from datetime import date
        rc, to = self.rc, date(2026, 10, 1)
        self.assertEqual((rc.ROW_CAP, rc.PAGE_CAP), (1000, 10))
        self.assertEqual(rc.us_date(date(2026, 1, 2)), "01/02/2026")
        self.assertEqual(rc.parse_us_date("9/23/2026"), date(2026, 9, 23))
        self.assertIsNone(rc.parse_us_date(""))
        rows = [{"recorded": "9/23/2026"}] * 400 + [{"recorded": "1/2/2026"}] * 600
        self.assertIsNone(rc.next_cursor(rows[:340], 340, to), "under the cap the term is finished")
        self.assertEqual(rc.next_cursor(rows, 1000, to), date(2026, 1, 2),
                         "capped: the next window ends on the oldest day seen, inclusive")
        self.assertEqual(rc.next_cursor(rows, None, to), date(2026, 1, 2), "1,000 rows read is capped too")
        one_day = [{"recorded": "1/2/2026"}] * 1000
        self.assertEqual(rc.next_cursor(one_day, 1000, date(2026, 1, 2)), date(2026, 1, 1),
                         "a day that fills the cap by itself is stepped past")
        self.assertIsNone(rc.next_cursor([], 1000, to))

    def test_run_sweeps_shapes_and_resumes_against_a_stand_in_site(self):
        """get_cook_liens end to end with the three fixtures served by a fake
        client: one window finishes the term, rows are indexed once however
        often they are read, lender foreclosures are neither fetched nor
        recorded, the output and its sources file are written, coverage.json
        gains the Cook lien index and loses the 'search-only' entry, a
        second run posts nothing, and a cached detail page replaces the
        results row."""
        run = self.driver

        class FakeCRS:
            posts, pages, details = [], [], []

            def post_window(self, term, frm, to, types=None, side=""):
                self.posts.append((term, frm, to, tuple(types or ()), side))
                return TestCookLiensParsing.results, 4

            def page(self, n):
                self.pages.append(n)
                return TestCookLiensParsing.results

            def detail(self, href):
                self.details.append(href)
                return TestCookLiensParsing.lien

            def warm(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "coverage.json").write_text(json.dumps({"states": {"IL": {"name": "Illinois", "collected": {}, "unavailable": [
                {"source": "Cook County Clerk current recordings", "why": "search-only"},
                {"source": "SOS corporate bulk", "why": "paid"}]}}}))
            paths = {"OUT": root / "liens" / "liens_cook.jsonl", "SOURCES": root / "liens" / "sources_cook.json",
                     "COVERAGE": root / "coverage.json", "CACHE": root / "cache", "DETAILS": root / "cache" / "details",
                     "INDEX": root / "cache" / "index.jsonl", "SWEEP": root / "cache" / "sweep.json"}
            with contextlib.ExitStack() as stack:
                for name, value in paths.items():
                    stack.enter_context(mock.patch.object(run, name, value))
                from datetime import date
                crs, index, sweep, fails = FakeCRS(), run.load_index(), run.load_sweep(), [0]
                run.sweep_term(crs, "MASTER ASSN", sweep, index, date(1980, 1, 1), None, fails)
                # two passes: the lien types on either side, the lis pendens types on the grantor side
                self.assertEqual([(p[0], p[1], p[3], p[4]) for p in crs.posts],
                                 [("MASTER ASSN", "01/01/1980", ("LIEN", "CORL", "MECL"), ""),
                                  ("MASTER ASSN", "01/01/1980", ("LISF", "AMLF", "COLF"), "D")])
                self.assertEqual(crs.pages, [2, 3, 4, 2, 3, 4])
                self.assertEqual(len(index), 6, "the same rows on every page of both passes are indexed once")
                self.assertEqual({r["pass"] for r in index.values()}, {"liens"})
                st = sweep["terms"]["MASTER ASSN / liens"]
                self.assertEqual((st["done"], st["windows"], st["rows"], st["new"], st["oldest"], st["newest"]),
                                 (True, 1, 24, 6, "2023-07-18", "2026-09-10"))
                self.assertEqual(sweep["terms"]["MASTER ASSN / foreclosures"]["new"], 0)
                self.assertEqual([r["doc_number"] for r in run.detail_queue(index, "needed")],
                                 ["2600000001", "2600000003", "2600000004"],
                                 "rows whose first parties are not an association need their page, newest first")
                self.assertEqual([r["doc_number"] for r in run.detail_queue(index, "all")],
                                 ["2600000001", "2600000003", "2600000004", "2600000006", "2600000007", "2600000008"])
                self.assertEqual(run.detail_queue(index, "none"), [])

                records = run.write_outputs(index, sweep, 0.0)
                self.assertEqual([(r["doc_id"], r["doc_type"], r["detail_page"]) for r in records],
                                 [("2600000006", "LIE", False), ("2600000007", "LIE", False), ("2600000008", "LXA", False)])
                self.assertEqual([json.loads(line)["doc_id"] for line in paths["OUT"].read_text().splitlines()],
                                 ["2600000006", "2600000007", "2600000008"], "newest first")
                src = json.loads(paths["SOURCES"].read_text())
                self.assertEqual((src["records"], src["associations"], src["years"], src["documents_indexed"]),
                                 (3, 2, "2023-2026", 6))
                self.assertEqual(src["counts"], {"from_results_row": 3, "awaiting_detail_page": 3})
                self.assertEqual(src["document_types"], {"claim_of_lien": 2, "lien_against_association": 1})
                self.assertFalse(src["sweep_complete"], "one term of the list is not the whole sweep")
                self.assertEqual(sorted(src["terms"]), ["MASTER ASSN / foreclosures", "MASTER ASSN / liens"])
                il = json.loads(paths["COVERAGE"].read_text())["states"]["IL"]
                cook = il["counties"]["Cook"]["liens"]
                self.assertEqual((cook["collector"], cook["records"], cook["associations"], cook["years"]),
                                 ("get_cook_liens.py", 3, 2, "2023-2026"))
                self.assertIn("sweep unfinished", cook["coverage"])
                self.assertEqual([u["source"] for u in il["unavailable"]], ["SOS corporate bulk"])

                # a second run finds the term finished and asks the site for nothing
                run.sweep_term(crs, "MASTER ASSN", run.load_sweep(), run.load_index(), date(1980, 1, 1), None, fails)
                self.assertEqual(len(crs.posts), 2)
                # a cached detail page wins over the results row: full parties and the address
                run.DETAILS.mkdir(parents=True)
                run.detail_path("2600000006").write_text(self.lien)
                records = run.write_outputs(run.load_index(), run.load_sweep(), 0.0)
                full = next(r for r in records if r["detail_page"])
                self.assertEqual((full["association"], full["n_parties"], full["property_address"]),
                                 ("ARTHUR & CALIFORNIA CONDO ASSN INC", 7, "100 SAMPLE ST UNIT 1, CHICAGO"))
                self.assertEqual(len(records), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestCourtListenerBulk(unittest.TestCase):
    """CourtListener bulk-data collector (`get_courts_bulk`) against three
    small files in the bulk CSV dialect (`tests/fixtures/courtlistener_bulk/`:
    rows captured from the 2026-09-30 `courts`, `dockets` and
    `opinion-clusters` files, columns the collector does not read emptied,
    individuals replaced by Doe/Roe placeholders) — no network. Guards the
    bucket listing, the PostgreSQL CSV dialect, the gate that tells an
    association from a person / lender / club wearing the words, the caption
    splitter, the record shapes and the blocked-row rule."""

    @classmethod
    def setUpClass(cls):
        try:
            from hoaspy.collect.courts import get_courts_bulk
        except Exception as exc:
            raise unittest.SkipTest(f"get_courts_bulk import failed: {exc}")
        cls.m = get_courts_bulk
        cls.fx = ROOT / "tests" / "fixtures" / "courtlistener_bulk"
        cls.courts = cls.m.court_table(cls.fx / "courts.csv")

    def _refined(self):
        m = self.m
        clusters = list(m.scan(self.fx / "opinion-clusters.csv", m.CLUSTER_COLS))
        ids = frozenset(r["docket_id"] for r in clusters)
        dockets = list(m.scan(self.fx / "dockets.csv", m.DOCKET_COLS, ids))
        return m.refine(clusters, dockets, self.courts, "2026-09-30", "2026-10-01T00:00:00+00:00")

    def test_listing_finds_the_latest_complete_snapshot(self):
        m = self.m
        def obj(key, size):
            return f"<Contents><Key>{key}</Key><LastModified>x</LastModified><ETag>e</ETag><Size>{size}</Size></Contents>"
        xml = ("<ListBucketResult>"
               + obj("bulk-data/courts-2026-06-30.csv.bz2", 81000)
               + obj("bulk-data/dockets-2026-06-30.csv.bz2", 5000000000)
               + obj("bulk-data/opinion-clusters-2026-06-30.csv.bz2", 2400000000)
               + obj("bulk-data/courts-2026-09-30.csv.bz2", 81226)
               + obj("bulk-data/dockets-2026-09-30.csv.bz2", 5144011100)   # clusters not uploaded yet
               + obj("bulk-data/opinions-2026-09-30.csv.bz2", 55252600000)
               + "<NextContinuationToken>abc&amp;def</NextContinuationToken></ListBucketResult>")
        items, token = m.parse_listing(xml)
        self.assertEqual(token, "abc&def")
        self.assertIn(("bulk-data/dockets-2026-09-30.csv.bz2", 5144011100), items)
        snaps = m.snapshots(items)
        self.assertEqual(list(snaps), ["2026-06-30"])           # the incomplete one is not offered
        self.assertEqual(snaps["2026-06-30"]["dockets"], ("bulk-data/dockets-2026-06-30.csv.bz2", 5000000000))
        self.assertEqual(m.parse_listing("<ListBucketResult></ListBucketResult>"), ([], ""))

    def test_download_ranges_cover_the_file_exactly(self):
        plan = self.m.plan_parts
        parts = plan(5144011100, 8)
        self.assertEqual(len(parts), 8)
        self.assertEqual(parts[0][0], 0)
        self.assertEqual(parts[-1][1], 5144011100 - 1)
        for (_, end), (start, _) in zip(parts, parts[1:]):
            self.assertEqual(start, end + 1)
        self.assertEqual(plan(81226, 8), [(0, 81225)])           # a small file is one request
        self.assertEqual(sum(e - s + 1 for s, e in plan(200 << 20, 8)), 200 << 20)

    def test_csv_dialect_escapes_nulls_and_multiline_fields(self):
        m = self.m
        with m.open_rows(self.fx / "dockets.csv") as rows:
            head = next(rows)
            body = list(rows)
        self.assertEqual(len(head), 54)
        self.assertEqual({len(r) for r in body}, {54})
        col = {c: i for i, c in enumerate(head)}
        quoted = next(r for r in body if r[0] == "74274066")
        self.assertEqual(quoted[col["case_name"]], 'JANE "JD" DOE v. GLOBE COMMUNICATIONS')   # \" in the file
        self.assertEqual(quoted[col["date_terminated"]], "")                                  # NULL
        with m.open_rows(self.fx / "opinion-clusters.csv") as rows:
            head = next(rows)
            body = list(rows)
        self.assertEqual(len(body), 5)                           # the headmatter spans six lines
        col = {c: i for i, c in enumerate(head)}
        northlake = next(r for r in body if r[0] == "4488611")
        self.assertIn('<parties id="p-1">\n', northlake[col["headmatter"]])
        # a .bz2 file reads the same through bzip2 / the bz2 module
        import bz2
        with tempfile.TemporaryDirectory() as tmp:
            packed = Path(tmp) / "courts-2026-09-30.csv.bz2"
            packed.write_bytes(bz2.compress((self.fx / "courts.csv").read_bytes()))
            self.assertEqual(m.court_table(packed), self.courts)

    def test_court_table_gives_state_and_jurisdiction(self):
        c = self.courts
        self.assertEqual(len(c), 11)                             # one court's notes span two lines
        self.assertEqual(c["mdctspecapp"], {"state": "MD", "jurisdiction": "SA",
                                            "name": "Court of Special Appeals of Maryland"})
        self.assertEqual((c["mied"]["state"], c["mied"]["jurisdiction"]), ("MI", "FD"))
        self.assertEqual((c["ca11"]["state"], c["ca11"]["jurisdiction"]), ("", "F"))

    def test_association_gate(self):
        ok = self.m.is_association
        for name in ("Twelve Hills Community Association", "FALLS GARDEN CONDOMINIUM ASSOCIATION, INC",
                     "Council of Unit Owners of Annen Woods Condominium No. 4", "Sunset Lakes HOA",
                     "SAWGRASS LAKES HOMEOWNERS", "Board of Managers of the 432 Park Condominium",
                     "Relay Improvement Association", "Bradford Village Condo Trust"):
            self.assertTrue(ok(name), name)
        for name in ("Hoa Van Doe", "Nguyen Hoa", "HOA VAN DOE",        # a given name, not an HOA
                     "Home Owners Loan Corporation", "Homeowners Choice Property & Casualty Insurance Company",
                     "Bank of America, National Association", "Signature Point Condominiums LLC",
                     "Standardbred Owners Association, Inc", "Sportsman's Park and Club Association",
                     "Homeowners", "Property Owners", "Condos",           # truncated captions
                     "on Behalf of Themselves and All Other Property Owners in the Subdivision",
                     "Community Associations Institute", "Empire Indemnity Insurance Company"):
            self.assertFalse(ok(name), name)

    def test_captions_are_split_into_parties_with_roles(self):
        f = self.m.find_associations
        # reporter abbreviations; the full caption wins and "The" is dropped
        self.assertEqual(f("Falls Garden Condominium Ass'n v. Falls Homeowners Ass'n",
                           "FALLS GARDEN CONDOMINIUM ASSOCIATION, INC. v. The FALLS HOMEOWNERS ASSOCIATION, INC."),
                         {"FALLS GARDEN CONDOMINIUM ASSOCIATION, INC": "plaintiff",
                          "FALLS Homeowners ASSOCIATION, INC": "defendant"})
        self.assertEqual(f("Lake Point Tower Condo. Ass'n v. Roe"),
                         {"Lake Point Tower Condominium Association": "plaintiff"})
        self.assertEqual(f("Board of Mgrs. of the 432 Park Condominium v. 56th & Park (NY) Owner, LLC"),
                         {"Board of Managers of the 432 Park Condominium": "plaintiff"})
        # Florida's shortened case_name, the association only in the full caption
        self.assertEqual(f("JANE DOE and JOHN v. SAWGRASS LAKES HOMEOWNERS",
                           "JANE DOE and JOHN DOE v. SAWGRASS LAKES HOMEOWNERS ASSOC."),
                         {"SAWGRASS LAKES Homeowners Association": "defendant"})
        # one of several defendants; ", Inc." stays on its name; role words go
        self.assertEqual(f("Roe v. Wells Fargo Bank, N.A., Arbor Ridge Community Association, Inc., et al."),
                         {"Arbor Ridge Community Association, Inc": "defendant"})
        self.assertEqual(f("Sand and Sea Homeowners Association and John Doe v. Roe"),
                         {"Sand and Sea Homeowners Association": "plaintiff"})
        self.assertEqual(f("In re: Port Louis Owners Association, Inc."), {"Port Louis Owners Association, Inc": ""})
        self.assertEqual(f("Malcolm Roe v. Lakeshore Estates Homeowner&39;s Association, Inc"),
                         {"Lakeshore Estates Homeowners Association, Inc": "defendant"})
        for caption in ("State v. Hoa Van Doe", "Doe v. Home Owners Loan Corporation",
                        "Roe v. Bank of America, National Association", "Doe v. Roe"):
            self.assertEqual(f(caption), {}, caption)
        self.assertEqual(self.m.clean_caption('Roe Dental, LLC <b><font color="red">Jointly Administered</font></b>'),
                         "Roe Dental, LLC Jointly Administered")

    def test_scan_keeps_association_captions_and_skips_blocked_rows(self):
        m = self.m
        stats = __import__("collections").Counter()
        clusters = list(m.scan(self.fx / "opinion-clusters.csv", m.CLUSTER_COLS, stats=stats))
        self.assertEqual((stats["rows"], stats["kept"], stats["blocked"]), (5, 3, 1))
        self.assertNotIn("10933293", {r["id"] for r in clusters})     # the blocked opinion
        self.assertNotIn("6732967", {r["id"] for r in clusters})      # "Park & Club Ass'n": not in the net
        self.assertEqual(set(clusters[0]), set(m.CLUSTER_COLS))
        stats = __import__("collections").Counter()
        dockets = {r["id"]: r for r in m.scan(self.fx / "dockets.csv", m.DOCKET_COLS,
                                              frozenset({"74274066"}), stats)}
        self.assertEqual((stats["rows"], stats["blocked"], stats["malformed"]), (12, 1, 0))
        self.assertNotIn("1055750", dockets)                           # blocked docket, association or not
        self.assertTrue(dockets["74274066"]["for_opinion"])            # no association word: kept only by id
        self.assertNotIn("for_opinion", dockets["67847660"])
        self.assertIn("53328483", dockets)                             # "Hoa" passes the wide net …

    def test_refine_writes_docket_and_opinion_records(self):
        dockets, opinions, stats = self._refined()
        by_id = {d["docket_id"]: d for d in dockets}
        # … and the name gate drops it, with the lender
        self.assertEqual(sorted(by_id), [6368320, 65062906, 65070136, 67400605, 67847660, 73645368, 74274459])
        d = by_id[67847660]
        self.assertEqual(d, {
            "docket_id": 67847660, "case_name": "Doe v. Poinsettia Homeowners Association, Inc.",
            "court": "District Court, E.D. Michigan", "court_id": "mied", "jurisdiction": "FD",
            "docket_number": "2:23-cv-12481", "date_filed": "2023-10-02", "date_terminated": "2024-06-24",
            "nature_of_suit": "Civil Rights: Other", "cause": "42:1981 Civil Rights", "state": "MI",
            "associations": ["Poinsettia Homeowners Association, Inc"],
            "association_role": {"Poinsettia Homeowners Association, Inc": "defendant"},
            "url": "https://www.courtlistener.com/docket/67847660/doe-v-poinsettia-homeowners-association-inc/",
            "source": "courtlistener-bulk", "queries": ["bulk-data 2026-09-30"],
            "retrieved_at": "2026-10-01T00:00:00+00:00"})
        # the fields build_site's add_courts reads are all there
        for key in ("case_name", "court", "docket_number", "date_filed", "date_terminated",
                    "nature_of_suit", "cause", "url", "state", "associations"):
            for rec in dockets:
                self.assertIn(key, rec)
        # a federal court of appeals has no state of its own: the district appealed from gives it
        self.assertEqual((by_id[67400605]["state"], by_id[67400605]["jurisdiction"]), ("FL", "F"))
        self.assertEqual(by_id[74274459]["associations"], ["SAWGRASS LAKES Homeowners Association"])
        self.assertEqual(by_id[73645368]["association_role"], {"CASH ENERGY CONDOMINIUM ASSOCIATION": "plaintiff"})

        by_cluster = {o["cluster_id"]: o for o in opinions}
        self.assertEqual(sorted(by_cluster), [4488611, 7967752, 7974835])
        o = by_cluster[7967752]
        self.assertEqual(o, {
            "cluster_id": 7967752, "docket_id": 65062906, "case_name": "Doe v. Twelve Hills Community Ass'n",
            "court": "Court of Appeals of Maryland", "court_id": "md", "jurisdiction": "S",
            "docket_number": "No. 13", "state": "MD", "date_filed": "2005-10-12", "status": "Published",
            "associations": ["TWELVE HILLS COMMUNITY ASSOCIATION"],
            "association_role": {"TWELVE HILLS COMMUNITY ASSOCIATION": "defendant"},
            "url": "https://www.courtlistener.com/opinion/7967752/doe-v-twelve-hills-community-assn/",
            "source": "courtlistener-bulk-opinions", "queries": ["bulk-data 2026-09-30"],
            "retrieved_at": "2026-10-01T00:00:00+00:00"})
        self.assertEqual(by_cluster[7974835]["associations"],
                         ["FALLS GARDEN CONDOMINIUM ASSOCIATION, INC", "FALLS Homeowners ASSOCIATION, INC"])
        self.assertEqual(stats["dockets kept"], 7)
        self.assertEqual(stats["opinions kept"], 3)
        self.assertEqual(stats["docket captions without an association party"], 2)

    def test_an_opinion_without_its_docket_is_dropped(self):
        m = self.m
        row = {"id": "1", "docket_id": "999", "date_filed": "2020-01-01", "slug": "x", "precedential_status": "Published",
               "case_name": "Roe v. Elm Court Condominium Association", "case_name_full": "", "source": "C",
               "citation_count": "0"}
        self.assertIsNone(m.opinion_record(row, None, self.courts, "2026-09-30", "now"))
        dockets, opinions, stats = m.refine([row], [], self.courts, "2026-09-30", "now")
        self.assertEqual((opinions, stats["opinions without a docket row"]), ([], 1))

    def test_sources_entry_is_merged_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sources.json"
            path.write_text(json.dumps({"dockets": 7527, "tx_research": {"records": 42814}}))
            self.m.merge_sources(path, {"snapshot": "2026-09-30", "dockets": 3})
            data = json.loads(path.read_text())
            self.assertEqual(data["dockets"], 7527)
            self.assertEqual(data["tx_research"], {"records": 42814})
            self.assertEqual(data["courtlistener_bulk"], {"snapshot": "2026-09-30", "dockets": 3})
            self.m.write_jsonl(Path(tmp) / "bulk_dockets.jsonl", [{"a": 1}, {"a": 2}])
            self.assertEqual((Path(tmp) / "bulk_dockets.jsonl").read_text(), '{"a": 1}\n{"a": 2}\n')
