"""Zero-network tests for apibara_json2csv_copart_01.py.

Run from the repository root:

    python3 test/test_copart_json2csv_01.py
"""
import contextlib
import copy
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import apibara_json2csv_copart_01 as flat  # noqa: E402
import csv_image_urls  # noqa: E402


class CopartJson2CsvTests(unittest.TestCase):
    def record(self):
        return {
            "platform": "copart",
            "lot_number": "64198246",
            "vin": "WAUC4CF53JA063799",
            "year": 2018,
            "make": "AUDI",
            "model": "S5",
            "title": "2018 AUDI S5 PRESTIGE",
            "auction": {
                "state": "finished",
                "auction_at": "2026-08-17T14:00:00+00:00",
                "last_sold_day": "2026-08-17",
                "last_sold_status": "Sold",
                "is_timed": False,
                "is_buy_now": False,
                "sold_buy_now": False,
                "sold_timed": False,
                "bid_type": "Minimum Bid",
                "sale_status": "MINIMUM_BID",
                "seller_reserve_met": False,
            },
            "condition": {
                "primary_damage": "Front end",
                "secondary_damage": None,
                "has_key": True,
                "run_condition": {"value": "RUNS AND DRIVES"},
            },
            "facility": {"zip": "12542", "lat": 41.5, "lng": -74.0},
            "location": {"display": "Newburgh (NY)", "send_from": "NY"},
            "media": {
                "thumbs_count": 1,
                "items": [
                    {"type": "image", "large": "https://cs.copart.com/a.jpg"},
                    {"type": "video", "url": "https://cs.copart.com/a.mp4"},
                ],
            },
            "odometer": {"mi": 92417},
            "pricing": {
                "current_bid_usd": 10200,
                "buy_now_usd": None,
                "last_sold_price_usd": 10200,
                "estimated_retail_value_usd": 21241,
                "estimated_cost": {"from": 325, "to": 71500, "text": "$325 - $71,500"},
            },
            "sale_document": {
                "name": "MV-907A SALVAGE CERTIFICATE",
                "sale_document_group": "warning",
                "is_pending": False,
                "export": True,
                "registration": True,
            },
            "seller": {"name": "Non-insurance Company", "type": "non_insurance"},
            "subLot": False,
            "vehicle_specs": {
                "trim": "quattro Prestige",
                "series": "Sportback",
                "body_style": "Hatchback/Liftback/Notchback",
                "doors": 4,
                "vehicle_type": "PASSENGER CAR",
                "manufacturer": "AUDI AG",
                "engine": {
                    "raw": "3.0L 6", "size_l": "3.0", "hp": 354,
                    "cylinders": 6, "configuration": "V-Shaped",
                },
                "transmission": "Automatic",
                "drive_type": "ALL WHEEL DRIVE",
                "fuel_type": "Gas",
                "exterior_color": "Gray",
                "country_of_origin": "GERMANY",
            },
            "enrichment": {
                "nhtsa_vpic": {
                    "status": "decoded",
                    "decoded_at": "2026-08-17T20:42:00+00:00",
                    "decoded_year": 2018,
                    "year_mismatch": False,
                    "error_codes": ["0"],
                    "conflicts": [],
                    "raw_nonempty": {"EngineCylinders": "6"},
                }
            },
            "_mode": "ended",
            "_source_file": "adapted.json",
            "_raw_source_file": "raw.json",
            "_pulled_at": "2026-08-17T11:18:58-07:00",
            "_adapted_at": "2026-08-17T20:42:00+00:00",
        }

    def test_adapted_record_flattens_to_copart_schema(self):
        row = flat.flatten(self.record())
        self.assertEqual(set(row), set(flat.COLUMNS))
        self.assertEqual(row["trim"], "quattro Prestige")
        self.assertEqual(row["engine_hp"], 354)
        self.assertEqual(row["cylinders"], 6)
        self.assertEqual(row["last_sold_price_usd"], 10200)
        self.assertEqual(row["estimated_retail_value_usd"], 21241)
        self.assertIsNone(row["acv_usd"])
        # APIBara's generic non-insurance assertion contradicted Stat.vin on a
        # live lot, so retain it as evidence but do not treat it as a class.
        self.assertEqual(row["seller_class"], "unknown")
        self.assertEqual(row["seller_class_basis"], "untrusted_non_insurance")
        self.assertEqual(row["seller_identity_withheld"], True)
        self.assertEqual(row["primary_damage_group"], "FRONT")
        self.assertEqual(row["vpic_status"], "decoded")
        self.assertEqual(row["copart_video_url"], "https://cs.copart.com/a.mp4")
        self.assertEqual(row["raw_source_file"], "raw.json")
        self.assertEqual(row["bid_type"], "Minimum Bid")
        self.assertEqual(row["sale_status_raw"], "MINIMUM_BID")
        self.assertIs(row["seller_reserve_met"], False)
        self.assertEqual(
            row["bid_condition"],
            "Minimum Bid: Seller reserve not yet met",
        )

    def test_named_lender_beats_apibara_non_insurance_type(self):
        """Santander/Bridgecrest/GM Financial all arrive typed non_insurance."""
        record = self.record()
        record["seller"] = {"name": "Santander", "type": "non_insurance"}
        row = flat.flatten(record)
        self.assertEqual(row["seller_class"], "finance")
        self.assertEqual(row["seller_class_basis"], "registry")
        self.assertEqual(row["seller_identity_withheld"], False)
        # the raw APIBara value is still carried, unaltered
        self.assertEqual(row["seller_type"], "non_insurance")

    def test_absent_seller_stays_unknown(self):
        record = self.record()
        record["seller"] = {}
        row = flat.flatten(record)
        self.assertEqual(row["seller_class"], "unknown")

    def test_coupe_and_convertible_body_families_match_feed_variants(self):
        self.assertTrue(flat.style_matches("COUPE", "coupe"))
        self.assertTrue(flat.style_matches("2 Door Coupe", "coupe"))
        self.assertTrue(flat.style_matches("Convertible/Cabriolet", "convertible"))
        self.assertTrue(flat.style_matches("CABRIOLET", "convertible"))
        self.assertFalse(
            flat.style_matches("Hatchback/Liftback/Notchback", "coupe")
        )

    def test_member_csv_retail_value_never_leaks_into_acv(self):
        record = self.record()
        record["pricing"]["estimated_retail_value_usd"] = None
        record["enrichment"]["copart_sales_csv"] = {
            "estimated_retail_value": 22000,
        }
        row = flat.flatten(record)
        self.assertEqual(row["estimated_retail_value_usd"], 22000)
        self.assertIsNone(row["acv_usd"])

    def test_canadian_money_stays_native_not_usd(self):
        record = self.record()
        record["location"]["display"] = "Toronto (ON)"
        record["facility"] = {"zip": "L1E 0L1", "lat": None, "lng": None}
        row = flat.flatten(record)
        self.assertEqual(row["market"], "Canada")
        self.assertEqual(row["currency"], "CAD")
        self.assertEqual(row["last_sold_price_native"], 10200)
        self.assertIsNone(row["last_sold_price_usd"])
        self.assertIsNone(row["distance_mi"])

    def test_canadian_name_without_parentheses_gets_province(self):
        record = self.record()
        record["location"]["display"] = "ONTARIO AUCTION"
        record["facility"] = {"zip": "L1E 0L1", "lat": None, "lng": None}
        self.assertEqual(flat.branch_state(record), "ON")
        self.assertEqual(flat.market(record), "Canada")

    def test_unknown_market_never_gets_usd_values(self):
        record = self.record()
        record["location"] = {"display": "Unknown Yard", "send_from": ""}
        record["facility"] = {"zip": None, "state": None, "lat": None, "lng": None}
        row = flat.flatten(record)
        self.assertIsNone(row["market"])
        self.assertIsNone(row["currency"])
        self.assertEqual(row["last_sold_price_native"], 10200)
        self.assertIsNone(row["last_sold_price_usd"])

    def test_zero_current_bid_is_preserved_but_zero_buy_now_is_blank(self):
        record = self.record()
        record["pricing"]["current_bid_usd"] = 0
        record["pricing"]["buy_now_usd"] = 0
        row = flat.flatten(record)
        self.assertEqual(row["current_bid_native"], 0.0)
        self.assertEqual(row["current_bid_usd"], 0.0)
        self.assertIsNone(row["buy_now_usd"])

    def test_new_raw_observation_keeps_old_vpic_static_data(self):
        adapted = self.record()
        adapted["_pulled_at"] = "2026-08-17T10:00:00+00:00"
        raw = copy.deepcopy(adapted)
        raw["_pulled_at"] = "2026-08-18T10:00:00+00:00"
        raw["_source_file"] = "new_raw.json"
        raw["_adapted_at"] = None
        raw["_raw_source_file"] = None
        raw["enrichment"] = {}
        raw["vehicle_specs"]["trim"] = None
        raw["vehicle_specs"]["body_style"] = None
        raw["pricing"]["current_bid_usd"] = 12000

        merged = flat.merge_observations([adapted, raw])
        self.assertEqual(merged["pricing"]["current_bid_usd"], 12000)
        self.assertEqual(merged["vehicle_specs"]["trim"], "quattro Prestige")
        self.assertEqual(merged["vehicle_specs"]["body_style"], "Hatchback/Liftback/Notchback")
        self.assertEqual(flat.vpic(merged)["status"], "decoded")
        self.assertEqual(merged["_source_file"], "new_raw.json")
        self.assertEqual(merged["_raw_source_file"], "raw.json")

    def test_web_masked_and_apibara_full_vin_are_one_lot(self):
        apibara = self.record()
        apibara["_pulled_at"] = "2026-08-18T10:00:00+00:00"
        web = copy.deepcopy(apibara)
        web["vin"] = "WAUC4CF53JA******"
        web["_pulled_at"] = "2026-08-18T11:00:00+00:00"
        web["_source_file"] = "adapted_copartweb.json"
        web["pricing"]["current_bid_usd"] = 12500
        web["enrichment"] = {"copart_web": {"seller": {"class": "unknown"}}}

        self.assertEqual(flat.observation_key(apibara), flat.observation_key(web))
        merged = flat.merge_observations([apibara, web])
        self.assertEqual(merged["vin"], "WAUC4CF53JA063799")
        self.assertEqual(merged["pricing"]["current_bid_usd"], 12500)
        self.assertEqual(flat.vpic(merged)["status"], "decoded")
        self.assertEqual(merged["_source_file"], "adapted_copartweb.json")

    def test_open_archive_main_writes_csv(self):
        record = self.record()
        for key in list(record):
            if key.startswith("_"):
                record.pop(key)
        record["auction"].update({"state": "future", "last_sold_day": None})
        archive = {
            "generated_at": "2026-08-17T12:00:00+00:00",
            "adapted_at": "2026-08-17T12:01:00+00:00",
            "platform": "copart",
            "mode": "open",
            "adapter": {"source": {"path": "analytics/data/open/json-raw/copart/raw.json"}},
            "pages": [{"status": 200, "raw": {"data": [record], "meta": {}}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "adapted_open.json"
            destination = tmp / "open.csv"
            source.write_text(json.dumps(archive), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = flat.main([str(source), "--out", str(destination)])
            self.assertEqual(code, 0)
            with destination.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["listing_state"], "Open")
            self.assertEqual(rows[0]["source_file"], source.name)

    def test_every_column_has_a_source_hint(self):
        self.assertEqual(set(flat.COLUMNS) - set(flat.SOURCE_HINTS), set())

    def test_copart_image_urls_match_downstream_contract(self):
        record = self.record()
        record["media"]["items"] = [
            {"type": "image", "large": "https://cs.copart.com/path/first_hrs.jpg"},
            {"type": "image", "large": "https://cs.copart.com/path/second_vhrs.jpg"},
        ]
        row = flat.flatten(record)
        self.assertEqual(
            csv_image_urls.image_urls(row, 1600, 1200),
            [
                ("1", "https://cs.copart.com/path/first_hrs.jpg"),
                ("2", "https://cs.copart.com/path/second_vhrs.jpg"),
            ],
        )

    def test_load_records_defensively_excludes_canada(self):
        us = self.record()
        canada = copy.deepcopy(us)
        canada["lot_number"] = "61361386"
        canada["location"] = {"display": "Toronto (ON)", "send_from": ""}
        canada["facility"] = {"zip": "L1E 0L1", "lat": None, "lng": None}
        for record in (us, canada):
            for key in list(record):
                if key.startswith("_"):
                    record.pop(key)
        archive = {
            "generated_at": "2026-08-17T12:00:00+00:00",
            "platform": "copart", "mode": "ended",
            "pages": [{"status": 200, "raw": {"data": [us, canada]}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "mixed.json"
            source.write_text(json.dumps(archive), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                records = flat.load_records([source])
        self.assertEqual([record["lot_number"] for record in records], ["64198246"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
