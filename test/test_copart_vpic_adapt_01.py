"""Zero-network regression checks for the Copart -> NHTSA vPIC adapter.

Run from the repository root:

    python3 test/test_copart_vpic_adapt_01.py

Unlike the API probe scripts beside it, this spends no APIBara or vPIC calls.
"""
import copy
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import copart_vpic_adapt_01 as adapter  # noqa: E402


class CopartVpicAdapterTests(unittest.TestCase):
    def record(self):
        return {
            "platform": "copart",
            "lot_number": "64198246",
            "vin": "WAUC4CF53JA063799",
            "year": 2018,
            "make": "AUDI",
            "model": "S5",
            "facility": {"zip": "12542", "state": None},
            "location": {"display": "Newburgh (NY)"},
            "vehicle_specs": {
                "body_style": None,
                "engine": {"raw": "3.0L 6", "size_l": "3.0", "hp": None},
                "fuel_type": "Gas",
                "drive_type": "ALL WHEEL DRIVE",
                "transmission": "Automatic",
            },
        }

    def entry(self):
        return {
            "fetched_at": "2026-08-17T12:00:00+00:00",
            "request_model_year": 2018,
            "retried_without_year": False,
            "result": {
                "VIN": "WAUC4CF53JA063799",
                "ModelYear": "2018",
                "Make": "AUDI",
                "Model": "S5",
                "Trim": "Prestige",
                "Series": "quattro",
                "BodyClass": "Hatchback/Liftback/Notchback",
                "Doors": "4",
                "EngineCylinders": "6",
                "DisplacementL": "2.0",  # must not replace APIBara's 3.0
                "EngineHP": "354",
                "FuelTypePrimary": "Gasoline",  # must not replace "Gas"
                "DriveType": "AWD/All-Wheel Drive",
                "TransmissionStyle": "Automatic",
                "PlantCountry": "GERMANY",
                "ErrorCode": "0",
                "ErrorText": "0 - VIN decoded clean. Check Digit (9th position) is correct",
            },
        }

    def test_fill_only_preserves_apibara_values(self):
        record = self.record()
        original = copy.deepcopy(record)
        adapter.adapt_record(record, self.entry())

        specs = record["vehicle_specs"]
        self.assertEqual(specs["body_style"], "Hatchback/Liftback/Notchback")
        self.assertEqual(specs["trim"], "Prestige")
        self.assertEqual(specs["engine"]["cylinders"], 6)
        self.assertEqual(specs["engine"]["hp"], 354)
        self.assertEqual(specs["engine"]["size_l"], original["vehicle_specs"]["engine"]["size_l"])
        self.assertEqual(specs["fuel_type"], "Gas")
        self.assertEqual(specs["drive_type"], "ALL WHEEL DRIVE")
        self.assertEqual(record["year"], 2018)

        vp = record["enrichment"]["nhtsa_vpic"]
        self.assertEqual(vp["status"], "decoded")
        self.assertFalse(vp["year_mismatch"])
        self.assertEqual(vp["conflicts"], [])
        self.assertIn("vehicle_specs.body_style", vp["filled_paths"])
        self.assertIn("ErrorCode", vp["raw_nonempty"])

    def test_known_source_year_mismatch_is_explicit_and_not_overwritten(self):
        record = self.record()
        record.update({
            "lot_number": "69268225",
            "vin": "WAUCGBFR7FA001382",
            "year": 2018,
        })
        entry = {
            "fetched_at": "2026-08-17T12:00:00+00:00",
            "request_model_year": 2018,
            "retried_without_year": True,
            "validation_result": {
                "VIN": "WAUCGBFR7FA001382",
                "ModelYear": "2018",
                "ErrorCode": "0,12,14",
                "ErrorText": "Model year mismatch; incomplete VIN decode",
            },
            "result": {
                "VIN": "WAUCGBFR7FA001382",
                "ModelYear": "2015",
                "Make": "AUDI",
                "Model": "S5",
                "Trim": "quattro Plus",
                "BodyClass": "Coupe",
                "EngineCylinders": "6",
                "DisplacementL": "3.0",
                "EngineHP": "333",
                "ErrorCode": "0",
            },
        }

        adapter.adapt_record(record, entry)
        vp = record["enrichment"]["nhtsa_vpic"]
        self.assertEqual(record["year"], 2018)
        self.assertEqual(vp["decoded_year"], 2015)
        self.assertTrue(vp["year_mismatch"])
        self.assertTrue(vp["retried_without_year"])
        self.assertIn("12", vp["year_validation"]["error_codes"])
        self.assertEqual(vp["conflicts"][0]["field"], "year")
        self.assertEqual(vp["conflicts"][0]["resolution"], "kept_apibara")

    def test_invalid_vin_never_requires_a_decode(self):
        record = self.record()
        record["vin"] = "WAUC4CF53JA******"
        adapter.adapt_record(record, None)
        vp = record["enrichment"]["nhtsa_vpic"]
        self.assertEqual(vp["status"], "not_decoded")
        self.assertEqual(vp["filled_paths"], [])

    def test_mode_controls_output_bucket(self):
        source = Path("apibara_copart_open_audi_s5.json")
        open_path = adapter.output_path(source, {"mode": "open"})
        live_path = adapter.output_path(source, {"mode": "live"})
        sold_path = adapter.output_path(source, {"mode": "ended"})
        self.assertIn("/open/json-adapted/copart/", open_path.as_posix())
        self.assertIn("/open/json-adapted/copart/", live_path.as_posix())
        self.assertIn("/sold/json-adapted/copart/", sold_path.as_posix())

    def test_web_raw_archive_requires_web_adapter_first(self):
        archive = {
            "platform": "copart",
            "source": "copart-web",
            "mode": "open",
            "records": [],
        }
        with self.assertRaisesRegex(ValueError, "copart_web_adapt_01.py"):
            adapter.validate_archive(Path("copartweb_copart_open.json"), archive)

    def test_web_adapted_archive_is_not_sent_back_to_vpic(self):
        archive = {
            "platform": "copart",
            "source": "copart-web-adapted",
            "mode": "open",
            "pages": [{"status": 200, "raw": {"data": [self.record()]}}],
        }
        with self.assertRaisesRegex(ValueError, "vPIC needs APIBara's full VIN"):
            adapter.validate_archive(Path("adapted_copartweb_open.json"), archive)

    def test_market_scope_removes_canada_before_enrichment(self):
        us = self.record()
        canada = copy.deepcopy(us)
        canada.update({"lot_number": "61361386", "vin": "WAUC4CF54JA099999"})
        canada["location"] = {"display": "ONTARIO AUCTION"}
        canada["facility"] = {"zip": "L1E 0L1", "state": None}
        unknown = copy.deepcopy(us)
        unknown.update({"lot_number": "00000000", "vin": "WAUC4CF54JA088888"})
        unknown["location"] = {"display": "Unknown Yard"}
        unknown["facility"] = {"zip": None, "state": None}
        archive = {
            "pages": [{"status": 200, "raw": {"data": [us, canada, unknown]}}],
        }
        summary = adapter.apply_us_market_scope(archive)
        records = list(adapter.archive_records(archive))
        self.assertEqual([record["lot_number"] for record in records], ["64198246"])
        self.assertEqual(summary["kept_records"], 1)
        self.assertEqual(summary["excluded_by_market"], {"Canada": 1, "unknown": 1})
        self.assertEqual(summary["excluded_lot_numbers"]["Canada"], ["61361386"])
        self.assertEqual(summary["excluded_lot_numbers"]["unknown"], ["00000000"])

    def test_open_archive_runs_end_to_end_from_cache(self):
        record = self.record()
        archive = {
            "generated_at": "2026-08-17T12:00:00+00:00",
            "platform": "copart",
            "mode": "open",
            "pages": [{"status": 200, "raw": {"data": [record], "meta": {}}}],
            "counts": {"records": 1, "calls_used": 1, "truncated": False},
        }
        cache = {
            "schema_version": 1,
            "updated_at": "2026-08-17T12:00:00+00:00",
            "decodes": {record["vin"]: self.entry()},
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "apibara_copart_open_audi_s5.json"
            cache_path = tmp / "vpic_cache.json"
            destination = tmp / "adapted_open.json"
            source.write_text(json.dumps(archive), encoding="utf-8")
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = adapter.main([
                    str(source), "--cache", str(cache_path), "--cache-only",
                    "--out", str(destination),
                ])
            self.assertEqual(code, 0)
            adapted = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(adapted["mode"], "open")
            self.assertEqual(adapted["counts"]["records"], 1)
            self.assertEqual(adapted["counts"]["source_records"], 1)
            self.assertEqual(adapted["counts"]["excluded_non_us"], 0)
            self.assertEqual(adapted["counts"]["calls_used"], 1)
            self.assertFalse(adapted["counts"]["truncated"])
            vp = adapted["pages"][0]["raw"]["data"][0]["enrichment"]["nhtsa_vpic"]
            self.assertEqual(vp["status"], "decoded")
            self.assertEqual(vp["source_vin"], record["vin"])

    def test_empty_live_archive_is_a_valid_zero_work_snapshot(self):
        archive = {
            "generated_at": "2026-08-19T12:00:00+00:00",
            "platform": "copart",
            "mode": "live",
            "pages": [{"status": 200, "raw": {"data": [], "meta": {}}}],
            "counts": {"records": 0, "calls_used": 1, "truncated": False},
        }
        cache = {"schema_version": 1, "updated_at": None, "decodes": {}}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "apibara_copart_live_audi_a5.json"
            cache_path = tmp / "vpic_cache.json"
            destination = tmp / "adapted_live.json"
            source.write_text(json.dumps(archive), encoding="utf-8")
            cache_path.write_text(json.dumps(cache), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = adapter.main([
                    str(source), "--cache", str(cache_path), "--cache-only",
                    "--out", str(destination),
                ])
            self.assertEqual(code, 0)
            adapted = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(adapted["counts"]["records"], 0)
            stats = adapted["adapter"]["nhtsa_vpic"]
            self.assertEqual(stats["records"], 0)
            self.assertEqual(stats["filled_values"], 0)
            self.assertEqual(stats["decode_errors"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
