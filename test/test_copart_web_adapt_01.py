"""Zero-network regression tests for the Copart web/APIBara lot-number join.

    python3 test/test_copart_web_adapt_01.py

Fixtures use the real field names and nesting captured from Copart web and
APIBara. No HTTP request or API quota is used.
"""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import apibara_json2csv_copart_01 as flatten  # noqa: E402
import copart_web_adapt_01 as adapter  # noqa: E402


class CopartWebAdapterTests(unittest.TestCase):
    def web_wrapper(self, lot="62830586", country="USA", seller=None):
        return {
            "lot_number": lot,
            "keyword": "2018 Audi S5",
            "detail_url": f"https://www.copart.com/lot/{lot}/salvage-audi-s5",
            "vin_masked": True,
            "seller": {
                "name": seller,
                "published_type": None,
                "class": "insurance" if seller == "Csaa" else "unknown",
            },
            "search": {
                "siteCodes": ["CPRTUS"],
                "dynamicLotDetails": {
                    "currentBid": 1250,
                    "buyTodayBid": 0,
                    "saleStatus": "PURE_SALE",
                    "sellerReserveMet": True,
                },
                "memberVehicleType": "AUTOMOBILE",
                "lotNumberStr": lot,
                "ln": int(lot),
                "mkn": "AUDI",
                "lmg": "S5/RS5",
                "lm": "S5",
                "ltd": "PRESTIGE",
                "lcy": 2018,
                "fv": "WAUC4CF5XJA******",
                "la": 21241.0,
                "lotPlugAcv": 22075.0,
                "rc": 27804.11,
                "orr": 94750.0,
                "ord": "ACTUAL",
                "egn": "3.0L 6",
                "cy": "6",
                "ld": "2018 AUDI S5 PRESTIGE",
                "cuc": "USD",
                "ad": 1787148000000,
                "hb": 1250.0,
                "aan": 1201,
                "bnp": 0.0,
                "ts": "OH",
                "td": "CERT OF TITLE-SALVAGE",
                "tgc": "TITLEGROUP_S",
                "dd": "FRONT END",
                "sdd": "UNDERCARRIAGE",
                "tims": "https://cs.copart.com/path/first_thb.jpg",
                "ynumb": 166,
                "long": -84.22077,
                "lat": 39.6901,
                "zip": "45439 1950",
                "locCountry": country,
                "locCity": "MORAINE",
                "locState": "OH" if country == "USA" else "AB",
                "tsmn": "AUTOMATIC",
                "bstl": "HATCHBACK",
                "lcd": "RUNS AND DRIVES",
                "clr": "WHITE",
                "ft": "GAS",
                "hk": "YES",
                "drv": "ALL WHEEL DRIVE",
                "ess": "Pure Sale",
                "scn": seller,
            },
            "detail": None,
        }

    def source_record(self, lot="62830586", vin="WAUC4CF5XJA132505",
                      seller_name="Non-insurance Company",
                      seller_type="non_insurance", vpic=True):
        record = {
            "platform": "copart",
            "platform_id": 1,
            "lot_number": lot,
            "vin": vin,
            "year": 2018,
            "make": "AUDI",
            "model": "S5",
            "vehicle_specs": {
                "trim": "quattro Prestige",
                "body_style": "Hatchback/Liftback/Notchback",
                "doors": 4,
                "engine": {"raw": "3.0L 6", "hp": 354,
                           "configuration": "V-Shaped"},
            },
            "seller": {"name": seller_name, "type": seller_type},
            "pricing": {"current_bid_usd": 275,
                        "estimated_cost": {"from": 200, "to": 15200}},
            "media": {
                "thumbs_count": 2,
                "items": [
                    {"type": "image", "large": "https://cs.copart.com/a_hrs.jpg"},
                    {"type": "image", "large": "https://cs.copart.com/b_hrs.jpg"},
                ],
            },
            "enrichment": {},
        }
        if vpic:
            record["enrichment"]["nhtsa_vpic"] = {
                "status": "decoded", "filled_paths": ["vehicle_specs.doors"],
                "raw_nonempty": {"EngineHP": "354"},
            }
        return record

    def source_candidate(self, record=None):
        return {
            "record": record or self.source_record(),
            "rank": (1, 1, 2, "2026-08-18T14:35:20-07:00"),
            "path": Path("vpic_open.json"),
            "generated_at": "2026-08-18T14:35:20-07:00",
            "adapted_at": "2026-08-18T14:36:00-07:00",
        }

    def web_document(self, records):
        return {
            "generated_at": "2026-08-18T14:35:03-07:00",
            "platform": "copart",
            "source": "copart-web",
            "mode": "open",
            "search_params": {"make": "Audi", "model": "S5",
                              "year_min": 2018, "year_max": 2023},
            "records": records,
            "counts": {"records": len(records), "truncated": False},
        }

    def source_document(self, record, adapted=True):
        return {
            "generated_at": "2026-08-18T14:35:20-07:00",
            "adapted_at": "2026-08-18T14:36:00-07:00" if adapted else None,
            "platform": "copart",
            "mode": "open",
            "pages": [{"status": 200, "raw": {"data": [record]}}],
        }

    def test_lot_number_normalizes_web_integer_and_apibara_string(self):
        self.assertEqual(adapter.normalize_lot(62830586), "62830586")
        self.assertEqual(adapter.normalize_lot("62830586"), "62830586")
        self.assertEqual(adapter.normalize_lot("62830586.0"), "62830586")

    def test_real_masked_prefix_accepts_matching_full_vin(self):
        web = adapter.adapt_web_record(self.web_wrapper())
        conflicts = adapter.identity_conflicts(web, self.source_record())
        self.assertEqual(conflicts, [])

        status, conflicts = adapter.enrich_record(
            web, self.web_wrapper(), self.source_candidate()
        )
        self.assertEqual(status, "matched")
        self.assertEqual(conflicts, [])
        self.assertEqual(web["vin"], "WAUC4CF5XJA132505")
        self.assertEqual(web["_source_join"]["key"], "lot_number")
        self.assertEqual(web["_source_join"]["web_vin_prefix"], "WAUC4CF5XJA")

    def test_join_fills_vpic_and_media_but_web_bid_and_trim_stay_current(self):
        wrapper = self.web_wrapper()
        web = adapter.adapt_web_record(wrapper)
        adapter.enrich_record(web, wrapper, self.source_candidate())

        self.assertEqual(web["pricing"]["current_bid_usd"], 1250.0)
        self.assertEqual(web["pricing"]["estimated_cost"], {"from": 200, "to": 15200})
        self.assertEqual(web["vehicle_specs"]["trim"], "PRESTIGE")
        self.assertEqual(web["vehicle_specs"]["doors"], 4)
        self.assertEqual(web["enrichment"]["nhtsa_vpic"]["status"], "decoded")
        self.assertEqual(len(web["media"]["items"]), 2)

    def test_unmatched_web_thumbnail_survives_csv_image_contract(self):
        web = adapter.adapt_web_record(self.web_wrapper())
        row = flatten.flatten(web)
        self.assertEqual(
            row["copart_image_urls"],
            "https://cs.copart.com/path/first_thb.jpg",
        )
        self.assertEqual(row["image_count"], 1)
        self.assertEqual(web["enrichment"]["copart_web"]["vin_status"], "masked")
        self.assertFalse(web["enrichment"]["copart_web"]["vpic_eligible"])

    def test_web_money_and_item_fields_do_not_conflate_erv_with_acv(self):
        web = adapter.adapt_web_record(self.web_wrapper())
        row = flatten.flatten(web)

        self.assertEqual(web["pricing"]["estimated_retail_value_usd"], 21241.0)
        self.assertEqual(row["estimated_retail_value_usd"], 21241.0)
        self.assertIsNone(row["acv_usd"])
        self.assertIsNone(row["est_repair_usd"])
        self.assertEqual(row["copart_lot_plug_acv_raw"], 22075.0)
        self.assertEqual(row["copart_rc_raw"], 27804.11)
        self.assertEqual(row["auction_item_number"], 1201)

    def test_observed_zero_current_bid_is_not_treated_as_missing(self):
        wrapper = self.web_wrapper()
        wrapper["search"]["dynamicLotDetails"]["currentBid"] = 0
        web = adapter.adapt_web_record(wrapper)
        row = flatten.flatten(web)
        self.assertEqual(web["pricing"]["current_bid_usd"], 0.0)
        self.assertEqual(row["current_bid_usd"], 0.0)

    def test_minimum_bid_and_unmet_reserve_survive_json_and_csv(self):
        wrapper = self.web_wrapper()
        wrapper["search"]["ess"] = "Minimum Bid"
        wrapper["search"]["dynamicLotDetails"].update({
            "saleStatus": "MINIMUM_BID",
            "sellerReserveMet": False,
        })
        web = adapter.adapt_web_record(wrapper)
        row = flatten.flatten(web)
        self.assertEqual(web["auction"]["bid_type"], "Minimum Bid")
        self.assertIs(web["auction"]["seller_reserve_met"], False)
        self.assertEqual(row["sale_status_raw"], "MINIMUM_BID")
        self.assertIs(row["seller_reserve_met"], False)
        self.assertEqual(
            row["bid_condition"],
            "Minimum Bid: Seller reserve not yet met",
        )

    def test_lot_collision_with_wrong_vin_prefix_fails_closed(self):
        wrapper = self.web_wrapper()
        web = adapter.adapt_web_record(wrapper)
        wrong = self.source_record(vin="WAUB4CF53KA094371")
        status, conflicts = adapter.enrich_record(
            web, wrapper, self.source_candidate(wrong)
        )

        self.assertEqual(status, "conflict")
        self.assertEqual(conflicts[0]["field"], "vin_prefix")
        self.assertEqual(web["vin"], "WAUC4CF5XJA******")
        self.assertNotIn("nhtsa_vpic", web["enrichment"])

    def test_web_seller_name_beats_wrong_apibara_type(self):
        wrapper = self.web_wrapper(seller="Csaa")
        source = self.source_record(seller_name="Csaa", seller_type="non_insurance")
        web = adapter.adapt_web_record(wrapper)
        adapter.enrich_record(web, wrapper, self.source_candidate(source))

        classification = web["seller"]["classification"]
        self.assertEqual(classification["class"], "insurance")
        self.assertEqual(classification["basis"], "registry")
        self.assertEqual(classification["published_type"], "non_insurance")

    def test_unknown_named_seller_is_not_forced_to_non_insurance(self):
        wrapper = self.web_wrapper(seller="Zzz Holdings Llc")
        record = adapter.adapt_web_record(wrapper)
        adapter.enrich_record(record, wrapper, self.source_candidate())
        result = record["seller"]["classification"]
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "unrecognized_name")
        self.assertEqual(result["published_type"], "non_insurance")

    def test_apibara_non_insurance_placeholder_stays_unknown(self):
        wrapper = self.web_wrapper()
        record = adapter.adapt_web_record(wrapper)
        adapter.enrich_record(record, wrapper, self.source_candidate())
        result = record["seller"]["classification"]
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "untrusted_non_insurance")

    def test_load_enrichment_prefers_vpic_copy_of_same_lot(self):
        raw = self.source_record(vpic=False)
        enriched = self.source_record(vpic=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            raw_path = tmp / "apibara_open.json"
            vp_path = tmp / "vpic_apibara_open.json"
            raw_path.write_text(json.dumps(self.source_document(raw, adapted=False)))
            vp_path.write_text(json.dumps(self.source_document(enriched, adapted=True)))
            selected = adapter.load_enrichment([raw_path, vp_path])

        self.assertEqual(
            selected["62830586"]["record"]["enrichment"]["nhtsa_vpic"]["status"],
            "decoded",
        )

    def test_masked_vin_body_style_uses_unanimous_full_vpic_descriptor(self):
        records = []
        for lot, vin in (("100", "WAUYNGF59JN000001"),
                         ("101", "WAUYNGF59JN000002")):
            records.append({
                "platform": "copart", "lot_number": lot, "vin": vin,
                "year": 2018, "make": "AUDI", "model": "A5",
                "vehicle_specs": {"body_style": "Convertible/Cabriolet"},
                "enrichment": {"nhtsa_vpic": {"status": "decoded"}},
            })
        document = self.source_document(records[0])
        document["pages"][0]["raw"]["data"] = records
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vpic.json"
            path.write_text(json.dumps(document))
            descriptors = adapter.load_body_style_descriptors([path])

        masked = {
            "vin": "WAUYNGF59JN******", "year": 2018,
            "make": "AUDI", "model": "A5", "vehicle_specs": {},
        }
        self.assertTrue(adapter.infer_body_style(masked, descriptors))
        self.assertEqual(
            masked["vehicle_specs"]["body_style"], "Convertible/Cabriolet"
        )
        evidence = masked["enrichment"]["nhtsa_vpic_descriptor"]
        self.assertEqual(evidence["supporting_full_vins"], 2)
        self.assertFalse(evidence["masked_vin_submitted_to_vpic"])

    def test_body_style_descriptor_conflict_fails_closed(self):
        masked = {
            "vin": "WAUYNGF59JN******", "year": 2018,
            "make": "AUDI", "model": "A5", "vehicle_specs": {},
        }
        # Conflicting descriptor populations are intentionally omitted by the
        # loader; infer_body_style must leave the web record untouched.
        self.assertFalse(adapter.infer_body_style(masked, {}))
        self.assertNotIn("body_style", masked["vehicle_specs"])

    def test_body_style_family_normalizes_sedan_saloon(self):
        self.assertEqual(adapter.body_style_family("Sedan/Saloon"), "Sedan/Saloon")
        self.assertEqual(adapter.body_style_family("SEDAN"), "Sedan/Saloon")

    def test_main_excludes_canada_and_writes_flattener_compatible_json(self):
        us = self.web_wrapper()
        ca = self.web_wrapper(lot="57404776", country="CANADA")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            web_path = tmp / "copartweb.json"
            source_path = tmp / "vpic.json"
            out_path = tmp / "adapted.json"
            web_path.write_text(json.dumps(self.web_document([us, ca])))
            source_path.write_text(json.dumps(
                self.source_document(self.source_record())
            ))
            with contextlib.redirect_stdout(io.StringIO()):
                code = adapter.main([
                    str(web_path), "--enrich-from", str(source_path),
                    "--out", str(out_path),
                ])
            document = json.loads(out_path.read_text())
            records = flatten.load_records([out_path])

        self.assertEqual(code, 0)
        self.assertEqual(document["counts"]["source_records"], 2)
        self.assertEqual(document["counts"]["records"], 1)
        self.assertEqual(document["counts"]["excluded_non_us"], 1)
        self.assertEqual(document["counts"]["join"], {"matched": 1})
        self.assertEqual(
            document["adapter"]["market_scope"]["excluded_lot_numbers"],
            {"Canada": ["57404776"]},
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["vin"], "WAUC4CF5XJA132505")

    def test_reference_archive_is_74_raw_to_72_us_with_audited_canada(self):
        source = (
            ROOT / "analytics" / "data" / "open" / "json-raw" / "copart" /
            "copartweb_copart_open_audi_s5_2018_2023_20260817T174721.json"
        )
        self.assertTrue(source.is_file(), "reference Copart web archive is missing")

        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "adapted_reference.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = adapter.main([str(source), "--out", str(out_path)])
            document = json.loads(out_path.read_text())

        records = document["pages"][0]["raw"]["data"]
        required_blocks = {
            "vehicle_specs", "condition", "odometer", "pricing", "auction",
            "seller", "sale_document", "location", "facility", "media",
        }
        self.assertEqual(code, 0)
        self.assertEqual(document["counts"]["source_records"], 74)
        self.assertEqual(document["counts"]["records"], 72)
        self.assertEqual(document["counts"]["excluded_non_us"], 2)
        self.assertEqual(
            document["adapter"]["market_scope"]["excluded_lot_numbers"],
            {"Canada": ["57404776", "46178876"]},
        )
        self.assertEqual(document["counts"]["full_vins"], 0)
        self.assertEqual(document["counts"]["masked_or_missing_vins"], 72)
        self.assertEqual(len(document["adapter"]["source"]["sha256"]), 64)
        self.assertTrue(all(required_blocks <= set(record) for record in records))
        self.assertTrue(all(
            record["enrichment"]["copart_web"]["vin_status"] == "masked" and
            not record["enrichment"]["copart_web"]["vpic_eligible"]
            for record in records
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
