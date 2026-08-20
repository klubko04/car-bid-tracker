"""Zero-network tests for the stat.vin seller/VIN enrichment path.

Markup fixtures are trimmed from a real rendered search page
(``statvin_copart_open_audi_a5_2018_2023_*``, 32 lots over 2 pages). The card
container class, the photo ``title`` identity string and the ``Seller:`` block
are reproduced as the site emits them.

    python3 test/test_statvin_01.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import copart_seller  # noqa: E402
import copart_statvin_enrich_01 as enrich  # noqa: E402
import pull_statvin_web_01 as pull  # noqa: E402


def card(lot="62595706", vin="WAUPNAF58JA008428", year="2018",
         vehicle="AUDI A5", seller="Insurance", descriptor="Insurance company"):
    seller_block = "" if seller is None else f'''
      <div class="text-gray lh-sm mb-1">Seller:</div>
      <div class="fs-13">
        <div class="d-flex align-items-center gap-1">
          <svg width="15" height="15"><path d="M7.5 0C11.6"/></svg>
          {seller} {descriptor}
        </div>
      </div>'''
    return f'''<div class="app-box app-listing-card">
      <div class="auction auction-copart"></div>
      <img class="app-listing-card-img lazy" src="https://cdn17.stat.vin/x.webp"
           alt="{vehicle} {year}. Lot# {lot}. VIN {vin}. Photo 1"
           title="{vehicle} {year}. Lot# {lot}. VIN {vin}. Auction COPART"
           data-lazy-loaded="1">
      {seller_block}
    </div>'''


def page(cards, total=32):
    return ("<html><body>"
            f"<div>Displaying 1 - {len(cards)} existing records in total {total} results</div>"
            + "".join(cards) + "</body></html>")


class UrlTests(unittest.TestCase):
    def test_copart_auction_selector_and_year_window(self):
        url = pull.search_url("Audi", "A5", 2018, 2023)
        self.assertIn("make=Audi", url)
        self.assertIn("model=A5", url)
        self.assertIn("auction%5B%5D=2", url)   # auction[]=2 is Copart
        self.assertIn("year_from=2018", url)
        self.assertIn("year_to=2023", url)
        self.assertNotIn("page=", url)          # page 1 carries no page param

    def test_pagination_parameter(self):
        self.assertIn("page=2", pull.search_url("Audi", "A5", 2018, 2023, 2))

    def test_only_the_robots_allowed_search_path_is_ever_built(self):
        # robots.txt disallows /vin/, */ajax/, /public/ and /livewire/.
        # Nothing in this module may construct one of those.
        url = pull.search_url("Audi", "RS5", 2018, 2023, 3)
        self.assertTrue(url.startswith("https://stat.vin/search-auto?"))
        for forbidden in ("/vin/", "/ajax/", "/public/", "/livewire/"):
            self.assertNotIn(forbidden, url)
        source = (ROOT / "analytics" / "scripts" / "pull_statvin_web_01.py").read_text()
        self.assertNotIn('"/vin/"', source)


class ParseTests(unittest.TestCase):
    def test_identity_comes_from_the_photo_title(self):
        parsed = pull.parse_page(page([card()]))
        self.assertEqual(len(parsed["records"]), 1)
        record = parsed["records"][0]
        self.assertEqual(record["lot_number"], "62595706")
        self.assertEqual(record["vin"], "WAUPNAF58JA008428")
        self.assertEqual(record["year"], 2018)
        self.assertEqual(record["auction"], "COPART")

    def test_total_results_drives_pagination(self):
        self.assertEqual(pull.parse_page(page([card()], total=32))["total_results"], 32)

    def test_seller_badges_observed_in_the_wild(self):
        for badge, descriptor, expected in (
            ("Insurance", "Insurance company", "insurance"),
            ("Insurance", "Insurance", "insurance"),
            ("Dealer", "Non-insurance", "dealer"),
        ):
            with self.subTest(badge=badge, descriptor=descriptor):
                parsed = pull.parse_page(page([card(seller=badge, descriptor=descriptor)]))
                self.assertEqual(parsed["records"][0]["seller_class"], expected)

    def test_unknown_badge_is_surfaced_not_forced_into_a_bin(self):
        parsed = pull.parse_page(page([card(seller="Charity", descriptor="Donation")]))
        record = parsed["records"][0]
        self.assertIsNone(record["seller_class"])
        self.assertIn("Charity", record["seller_label"])

    def test_missing_seller_block_is_absence_not_a_class(self):
        parsed = pull.parse_page(page([card(seller=None)]))
        record = parsed["records"][0]
        self.assertIsNone(record["seller_class"])
        self.assertIsNone(record["seller_label"])

    def test_card_without_identity_is_skipped_not_guessed(self):
        parsed = pull.parse_page(page(['<div class="app-box app-listing-card">x</div>']))
        self.assertEqual(parsed["records"], [])
        self.assertEqual(len(parsed["skipped"]), 1)

    def test_challenge_page_is_detected(self):
        self.assertTrue(pull.is_challenge("<title>Just a moment...</title>"))
        self.assertFalse(pull.is_challenge(page([card()])))


class DealerMappingTests(unittest.TestCase):
    def test_dealer_badge_survives_as_dealer(self):
        # Flattening this to non_insurance would erase the label the cut and
        # the gallery stage filter on.
        self.assertEqual(enrich.STATVIN_CLASS["dealer"], "dealer")
        self.assertEqual(enrich.STATVIN_CLASS["insurance"], "insurance")


class TrustedTypeTests(unittest.TestCase):
    """stat.vin asserts a type per lot; APIBara's placeholder does not."""

    def test_statvin_non_insurance_is_trusted(self):
        result = copart_seller.classify(None, "non_insurance", source="statvin.search")
        self.assertEqual(result["class"], "non_insurance")
        self.assertEqual(result["basis"], "trusted_published_type")

    def test_apibara_non_insurance_stays_untrusted(self):
        result = copart_seller.classify(None, "non_insurance", source="apibara.seller")
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "untrusted_non_insurance")


class PrecedenceTests(unittest.TestCase):
    def record(self, lot="62595706", vin="WAUPNAF58JA******", year=2018,
               name=None, source=None, withheld=False):
        classification = {"name": name, "class": "insurance" if name else "unknown",
                          "source": source, "identity_withheld": withheld}
        return {"lot_number": lot, "year": year, "vin": vin,
                "seller": {"name": name, "classification": classification}}

    def feed(self, lot="62595706", vin="WAUPNAF58JA008428", year=2018,
             seller_class="dealer"):
        return {"lot_number": lot, "vin": vin, "year": year,
                "seller_class": seller_class, "seller_label": "Dealer Non-insurance"}

    def test_copart_name_outranks_statvin(self):
        record = self.record(name="GEICO", source="search.scn")
        outcome, conflicts = enrich.apply_feed(record, self.feed())
        self.assertEqual(conflicts, [])
        self.assertIn("seller_kept_copart_name", outcome)
        self.assertEqual(record["seller"]["classification"]["name"], "GEICO")

    def test_apibara_placeholder_does_not_block_statvin(self):
        # The bug this guards: copart_web_adapt copies APIBara's generic
        # "Non-insurance Company" into seller.name, which would otherwise look
        # like a Copart name and lock out the source that actually knows.
        record = self.record(name="Non-insurance Company",
                             source="apibara.seller", withheld=True)
        outcome, _ = enrich.apply_feed(record, self.feed())
        self.assertIn("seller", outcome)
        self.assertNotIn("kept_copart_name", outcome)
        self.assertEqual(record["seller"]["classification"]["class"], "dealer")

    def test_statvin_fills_an_empty_seller(self):
        record = self.record()
        outcome, _ = enrich.apply_feed(record, self.feed(seller_class="insurance"))
        self.assertIn("seller", outcome)
        classification = record["seller"]["classification"]
        self.assertEqual(classification["class"], "insurance")
        # A bin, not an identity — flagged so carrier analysis excludes it.
        self.assertTrue(classification["identity_withheld"])

    def test_masked_vin_is_completed(self):
        record = self.record()
        outcome, _ = enrich.apply_feed(record, self.feed())
        self.assertIn("vin", outcome)
        self.assertEqual(record["vin"], "WAUPNAF58JA008428")
        self.assertEqual(record["vin_masked_source"], "WAUPNAF58JA******")

    def test_existing_full_vin_is_never_overwritten(self):
        record = self.record(vin="WAUPNAF58JA008428")
        outcome, _ = enrich.apply_feed(record, self.feed())
        self.assertNotIn("vin", outcome.split("+"))
        self.assertEqual(record["vin"], "WAUPNAF58JA008428")


class IdentityGateTests(unittest.TestCase):
    def base(self, **kwargs):
        return PrecedenceTests().record(**kwargs)

    def test_vin_prefix_conflict_rejects_the_whole_feed(self):
        record = self.base(vin="WAUPNAF58JA******")
        feed = {"lot_number": "62595706", "vin": "ZZZZZZZZZZZZZZZZZ",
                "year": 2018, "seller_class": "dealer"}
        outcome, conflicts = enrich.apply_feed(record, feed)
        self.assertEqual(outcome, "identity_conflict")
        self.assertEqual(conflicts[0]["field"], "vin_prefix")
        # Nothing may be written on a conflict.
        self.assertEqual(record["vin"], "WAUPNAF58JA******")
        self.assertNotIn("statvin_search", record.get("enrichment", {}))

    def test_year_conflict_rejects_the_whole_feed(self):
        record = self.base(year=2018)
        feed = {"lot_number": "62595706", "vin": "WAUPNAF58JA008428",
                "year": 2021, "seller_class": "dealer"}
        outcome, conflicts = enrich.apply_feed(record, feed)
        self.assertEqual(outcome, "identity_conflict")
        self.assertEqual(conflicts[0]["field"], "year")

    def test_lot_numbers_normalize_before_joining(self):
        self.assertEqual(enrich.normalize_lot("062595706"), "62595706")
        self.assertEqual(enrich.normalize_lot("Lot# 62595706"), "62595706")
        self.assertIsNone(enrich.normalize_lot(None))


class ArchiveTests(unittest.TestCase):
    def test_enricher_writes_provenance_and_leaves_input_untouched(self):
        record = PrecedenceTests().record()
        document = {"platform": "copart",
                    "pages": [{"raw": {"records": [record]}}]}
        feed_doc = {"records": [PrecedenceTests().feed()]}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "adapted.json"
            statvin = tmp / "statvin.json"
            out = tmp / "out.json"
            source.write_text(json.dumps(document))
            statvin.write_text(json.dumps(feed_doc))
            enrich.main([str(source), "--statvin", str(statvin), "--out", str(out)])
            written = json.loads(out.read_text())
            unchanged = json.loads(source.read_text())
        meta = written["statvin_enrichment"]
        self.assertEqual(meta["source"], "statvin-search")
        self.assertEqual(meta["feed_lots"], 1)
        enriched = written["pages"][0]["raw"]["records"][0]
        self.assertEqual(enriched["enrichment"]["statvin_search"]["source"],
                         "statvin-search")
        # the adapted input is immutable
        self.assertNotIn("enrichment", unchanged["pages"][0]["raw"]["records"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
