"""Zero-network tests for pull_copart_web_01.py.

Every Copart-shaped fixture below is VERBATIM from a live archive
(``copartweb_copart_open_audi_s5_2018_2023_20260817T152709.json``, 73 exact
lots), trimmed to the fields under test but never invented. That matters: the
first version of these tests asserted against hand-written ``sellerName`` /
``sellerType`` keys that Copart has never once emitted, so the suite passed
green while the live run classified 73/73 lots as ``unknown``.

Run from the repository root:

    python3 test/test_pull_copart_web_01.py
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import pull_copart_web_01 as pull  # noqa: E402


# --------------------------------------------------------------------------
# Real search rows, copied out of the live archive.
# --------------------------------------------------------------------------
# Canadian lot: locCountry is the ISO-3 "CAN", and it is cross-listed on the US
# site so siteCodes carries BOTH codes. Reading either naively sent this lot to
# UnitedStates in the first live run.
ROW_CANADA = {
    "ln": 46178876, "lotNumberStr": "46178876", "lcy": 2019, "mkn": "AUDI",
    "lm": "S5", "lmg": "S5/RS5", "ltd": "PRESTIGE", "lmtd": "S5 PRESTIGE",
    "ldu": "salvage-2019-audi-s5-prestige-qc-montreal",
    "fv": "WAUC4CF52KA******", "showSeller": False,
    "locCountry": "CAN", "locState": "QC", "locCity": "MONTREAL-EST",
    "siteCodes": ["CPRTCA", "CPRTUS"], "yn": "QC - MONTREAL", "cuc": "CAD",
}
# Seller named AND rendered by Copart (showSeller true) — only 4 of 73 rows.
ROW_SELLER_SHOWN = {
    "ln": 55977946, "lotNumberStr": "55977946", "lcy": 2018, "mkn": "AUDI",
    "lm": "S5", "lmg": "S5/RS5", "ltd": "PREMIUM PLUS",
    "ldu": "salvage-2018-audi-s5-premium-plus-nv-las-vegas-west",
    "fv": "WAUP4AF53JA******", "scn": "CSAA", "scl": "", "showSeller": True,
    "locCountry": "USA", "locState": "NV", "siteCodes": ["CPRTUS"],
    "yn": "NV - LAS VEGAS WEST", "cuc": "USD",
}
# Seller named but NOT rendered — 14 of the 18 named rows look like this. This
# is the row shape that makes showSeller useless as a presence test.
ROW_SELLER_HIDDEN = {
    "ln": 59832836, "lotNumberStr": "59832836", "lcy": 2018, "mkn": "AUDI",
    "lm": "S5", "lmg": "S5/RS5", "ltd": "PRESTIGE",
    "ldu": "2018-audi-s5-prestige-oh-cleveland-west",
    "fv": "WAUC4CF59JA******", "scn": "Farmers Insurance", "scl": "",
    "smd": {"facebookUrl": "https://www.facebook.com/FarmersInsuranceSalvageAndRecycling/"},
    "showSeller": False, "locCountry": "USA", "locState": "OH",
    "siteCodes": ["CPRTUS"], "yn": "OH - CLEVELAND WEST", "cuc": "USD",
}
# No seller published at all — 55 of 73 rows.
ROW_NO_SELLER = {
    "ln": 64794106, "lotNumberStr": "64794106", "lcy": 2018, "mkn": "AUDI",
    "lm": "S5", "lmg": "S5/RS5", "ltd": "PREMIUM PLUS",
    "ldu": "clean-title-2018-audi-s5-premium-plus-ct-hartford-springfield",
    "fv": "WAUP4AF57JA******", "showSeller": False,
    "locCountry": "USA", "locState": "CT", "siteCodes": ["CPRTUS"],
    "yn": "CT - HARTFORD SPRINGFIELD", "cuc": "USD",
}

# A real detail response, trimmed. Note what is NOT here: any seller name or
# seller type. 111 of its keys duplicate the search row.
REAL_DETAIL_PAYLOAD = {
    "returnCode": 1, "returnCodeDesc": "Success",
    "data": {"lotDetails": {
        "lotNumberStr": "64951306", "ln": 64951306, "lcy": 2018, "mkn": "AUDI",
        "lm": "S5", "fv": "WAUC4CF55JA******", "showSeller": False,
        "sellerEligibleVVV": False, "syn": "*NCS - EASTERN REGION",
        "yn": "CT - HARTFORD SPRINGFIELD", "locCountry": "USA",
        "hb": 19700.0, "dd": "MINOR DENT/SCRATCHES", "tgd": "CLEAN TITLE",
    }},
}

IMPERVA_BODY = '<html><head><script src="/_Incapsula_Resource?SWJIYLWA=719d34d31c8e3a6e6fffd425f7e032f3"></script></head></html>'


class IdentityTests(unittest.TestCase):
    def record(self, year=2018, make="AUDI", model="S5", lot="64794106",
               country="USA", sites=None):
        return {
            "ln": int(lot), "lotNumberStr": lot,
            "lcy": year, "mkn": make, "lmg": "S5/RS5", "lm": model,
            "lmtd": f"{model} PREMIUM PLUS",
            "ldu": f"clean-title-{year}-audi-{model.lower()}-ct-hartford",
            "locCountry": country,
            "siteCodes": sites if sites is not None else ["CPRTUS"],
        }

    def payload(self, rows):
        return {
            "returnCode": 1,
            "returnCodeDesc": "Success",
            "data": {
                "query": {"page": 0, "size": 100},
                "results": {
                    "totalElements": len(rows), "content": rows,
                    "facetFields": [], "spellCheckList": [], "suggestions": [],
                },
            },
        }

    def test_default_cohort_is_six_years(self):
        args = pull.build_arg_parser().parse_args([])
        self.assertEqual(args.make, "Audi")
        self.assertEqual(args.model, "S5")
        self.assertEqual(args.year_range, (2018, 2023))

    def test_search_form_uses_exact_model_not_shared_model_group(self):
        form = pull.form_summary(pull.search_form(2018, "Audi", "S5"))
        self.assertEqual(form["query"], "2018 Audi S5")
        self.assertEqual(form["filter[YEAR]"], 'lot_year:"2018"')
        self.assertEqual(form["filter[MAKE]"], 'lot_make_desc:"AUDI"')
        self.assertEqual(form["filter[MODL]"], 'lot_model_desc:"S5"')
        self.assertNotIn("filter[MODLG]", form)

    def test_exact_s5_accepted_and_rs5_rejected(self):
        accepted, reasons, actual = pull.identity_match(
            self.record(), 2018, "Audi", "S5")
        self.assertTrue(accepted)
        self.assertEqual(reasons, [])
        self.assertEqual(actual["model_group"], "S5/RS5")

        accepted, reasons, _ = pull.identity_match(
            self.record(model="RS5"), 2018, "Audi", "S5")
        self.assertFalse(accepted)
        self.assertIn("model='RS5'", reasons)

    def test_wrong_year_rejected(self):
        accepted, reasons, _ = pull.identity_match(
            self.record(year=2019), 2018, "Audi", "S5")
        self.assertFalse(accepted)
        self.assertIn("year=2019", reasons)


class MarketTests(unittest.TestCase):
    """Regression cover for the two lots the first live run mislabelled."""

    def test_iso3_can_is_canada(self):
        # The original code compared locCountry against the word "canada",
        # so "CAN" fell through to the site codes and came back UnitedStates.
        self.assertEqual(pull.market_label(ROW_CANADA), "Canada")

    def test_cross_listed_canadian_lot_is_not_us(self):
        row = dict(ROW_CANADA)
        row.pop("locCountry")
        row.pop("locState")
        # Only the ambiguous pair is left; CPRTCA must win over CPRTUS.
        self.assertEqual(row["siteCodes"], ["CPRTCA", "CPRTUS"])
        self.assertEqual(pull.market_label(row), "Canada")

    def test_us_rows_still_us(self):
        for row in (ROW_SELLER_SHOWN, ROW_SELLER_HIDDEN, ROW_NO_SELLER):
            self.assertEqual(pull.market_label(row), "UnitedStates")

    def test_province_recognised_without_country_or_sites(self):
        self.assertEqual(
            pull.market_label({"locState": "AB"}), "Canada")
        self.assertEqual(
            pull.market_label({"locState": "NV"}), "UnitedStates")

    def test_nothing_published_is_unknown_not_us(self):
        self.assertEqual(pull.market_label({}), "unknown")


class SearchRowSellerTests(unittest.TestCase):
    """Seller comes from the search row. No HTTP request is involved."""

    def test_named_carrier_is_classified(self):
        seller = pull.search_seller(ROW_SELLER_SHOWN)
        self.assertEqual(seller["name"], "CSAA")
        self.assertEqual(seller["class"], "insurance")
        self.assertEqual(seller["source"], "search.scn")
        self.assertFalse(seller["identity_withheld"])

    def test_scn_is_read_even_when_copart_hides_it(self):
        # The bug this guards: reading showSeller instead of scn turns a
        # 25%-coverage field into a 5% one. 14 of 18 named rows are this shape.
        self.assertFalse(ROW_SELLER_HIDDEN["showSeller"])
        seller = pull.search_seller(ROW_SELLER_HIDDEN)
        self.assertEqual(seller["name"], "Farmers Insurance")
        self.assertEqual(seller["class"], "insurance")
        self.assertFalse(seller["show_seller_flag"])
        self.assertIsNotNone(seller["social_media"])

    def test_absent_seller_is_unknown_never_non_insurance(self):
        seller = pull.search_seller(ROW_NO_SELLER)
        self.assertIsNone(seller["name"])
        self.assertEqual(seller["class"], "unknown")
        self.assertEqual(seller["basis"], "not_published")


class DetailProbeTests(unittest.TestCase):
    def test_real_detail_payload_yields_no_seller(self):
        # Documents the actual contract: the endpoint has no seller field.
        # If Copart ever adds one, this test fails and tells us to use it.
        fields = pull.parse_detail_json(REAL_DETAIL_PAYLOAD)
        self.assertEqual(fields["seller"]["class"], "unknown")
        self.assertIsNone(fields["seller"]["name"])

    def test_failed_detail_does_not_clobber_search_seller(self):
        """The exact bug that produced 73/73 unknown on the live run."""
        class Session:
            def get(self, url, referer=None):
                return 403, IMPERVA_BODY, {"Content-Type": "text/html"}

        record = {
            "lot_number": "55977946", "search": ROW_SELLER_SHOWN,
            "seller": pull.search_seller(ROW_SELLER_SHOWN),
            "detail_url": "https://www.copart.com/lot/55977946/x",
        }
        detail, attempts = pull.fetch_detail(Session(), record)
        self.assertEqual(attempts, 2)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["attempts"][0]["error"], "imperva_challenge")
        # No seller key at all, so the merge cannot downgrade the record.
        self.assertNotIn("seller", detail["fields"])
        merged = pull.better_seller(
            record["seller"], (detail.get("fields") or {}).get("seller"))
        self.assertEqual(merged["class"], "insurance")

    def test_better_seller_prefers_a_resolved_class(self):
        unknown = pull.classify_seller()
        known = pull.classify_seller("Geico")
        self.assertEqual(pull.better_seller(unknown, known)["class"], "insurance")
        self.assertEqual(pull.better_seller(known, unknown)["class"], "insurance")
        self.assertEqual(pull.better_seller(unknown, None)["class"], "unknown")

    def test_detail_images_are_absolute_downstream_urls(self):
        parsed = pull.parse_detail_html(
            '<img data-src="/content/photo.jpg"><img src="data:image/png,x">'
        )
        self.assertEqual(parsed["image_urls"],
                         ["https://www.copart.com/content/photo.jpg"])

    def test_detail_uses_first_party_endpoint(self):
        class Session:
            def __init__(self):
                self.urls = []

            def get(self, url, referer=None):
                self.urls.append(url)
                return 200, json.dumps(REAL_DETAIL_PAYLOAD), {
                    "Content-Type": "application/json"}

        session = Session()
        detail, attempts = pull.fetch_detail(session, {
            "lot_number": "64951306",
            "detail_url": "https://www.copart.com/lot/64951306/x",
            "search": {"ln": 64951306, "lcy": 2018},
        })
        self.assertEqual(attempts, 1)
        self.assertEqual(detail["status"], "ok")
        self.assertEqual(session.urls, [
            "https://www.copart.com/public/data/lotdetails/solr/64951306"])


class VinMaskingTests(unittest.TestCase):
    def test_public_vins_are_masked(self):
        # 73/73 on the live cohort. This is why the vPIC adapter is not
        # downstream of this script.
        for row in (ROW_CANADA, ROW_SELLER_SHOWN, ROW_NO_SELLER):
            self.assertTrue(pull.vin_is_masked(row["fv"]))

    def test_full_vin_is_not_masked(self):
        self.assertFalse(pull.vin_is_masked("WAUC4CF52KA123456"))

    def test_absent_vin_is_not_reported_as_masked(self):
        self.assertFalse(pull.vin_is_masked(None))
        self.assertFalse(pull.vin_is_masked(""))


class ArchiveTests(unittest.TestCase):
    def payload(self, rows):
        return {
            "returnCode": 1, "returnCodeDesc": "Success",
            "data": {"query": {"page": 0, "size": 100},
                     "results": {"totalElements": len(rows), "content": rows,
                                 "facetFields": [], "spellCheckList": [],
                                 "suggestions": []}},
        }

    def run_main(self, rows_by_year, argv_extra=()):
        responses = [
            (200, json.dumps(self.payload(rows)), {"Content-Type": "application/json"})
            for rows in rows_by_year
        ]

        class Session:
            def __init__(self):
                self.responses = iter(responses)

            def post_form(self, url, form, referer):
                return next(self.responses)

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "copart_web.json"
            with mock.patch.object(pull, "HttpSession", Session), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = pull.main([
                    "--year-range", f"2018-{2017 + len(rows_by_year)}",
                    "--delay", "0", "--out", str(destination), *argv_extra,
                ])
            self.assertEqual(code, 0)
            return json.loads(destination.read_text(encoding="utf-8"))

    def test_raw_keeps_contamination_but_records_are_exact(self):
        rs5 = dict(ROW_NO_SELLER, lm="RS5", ln=65000000, lotNumberStr="65000000")
        archive = self.run_main([[ROW_NO_SELLER, rs5]])
        self.assertEqual(archive["counts"]["records"], 1)
        self.assertEqual(archive["queries"][0]["excluded_identity_count"], 1)
        raw_models = [row["lm"] for row in
                      archive["queries"][0]["pages"][0]["raw"]["data"]["results"]["content"]]
        self.assertEqual(raw_models, ["S5", "RS5"])

    def test_counts_report_seller_and_market_without_details(self):
        # ROW_CANADA is a real 2019 lot, so it must arrive in the 2019 batch —
        # the identity gate would (correctly) drop it from a 2018 query.
        archive = self.run_main([[ROW_SELLER_SHOWN, ROW_SELLER_HIDDEN,
                                  ROW_NO_SELLER], [ROW_CANADA]])
        counts = archive["counts"]
        self.assertEqual(counts["details_attempted"], 0)
        self.assertEqual(counts["seller_class"], {"insurance": 2, "unknown": 2})
        self.assertEqual(counts["seller_named"], 2)
        # The Canadian lot must be visible to the adapter, not buried in a
        # UnitedStates count of 4.
        self.assertEqual(counts["market_observed"],
                         {"UnitedStates": 3, "Canada": 1})
        self.assertEqual(counts["non_us_lot_numbers"], {"Canada": ["46178876"]})
        self.assertEqual(counts["vin_masked"], 4)
        self.assertEqual(counts["vin_usable_for_vpic"], 0)

    def test_every_record_carries_a_seller(self):
        archive = self.run_main([[ROW_SELLER_HIDDEN, ROW_NO_SELLER]])
        for record in archive["records"]:
            self.assertIn("seller", record)
            self.assertIn(record["seller"]["class"], ("insurance", "unknown"))
            self.assertTrue(record["vin_masked"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
