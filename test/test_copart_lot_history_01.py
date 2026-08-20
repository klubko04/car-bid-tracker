"""Zero-network lifecycle tests for Copart history and image archiving."""
import contextlib
import csv
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import lot_history_01 as history  # noqa: E402
import pull_images_01 as images  # noqa: E402


class CopartHistoryTests(unittest.TestCase):
    def record(self, lot, auction_at="2026-08-20T17:00:00+00:00",
               mode="open", sold_day=None, sold_status=None, sold_price=None,
               buy_now_sold=False, bid_type="Pure Sale", reserve_met=True):
        ended = mode == "ended"
        return {
            "platform": "copart", "lot_number": str(lot),
            "vin": "WAUC4CF50JA000001", "year": 2018,
            "make": "Audi", "model": "S5", "ad": auction_at,
            "auction": {
                "state": "finished" if ended else "open",
                "auction_at": auction_at,
                "last_sold_day": sold_day,
                "last_sold_status": sold_status,
                "sold_buy_now": buy_now_sold,
                "is_buy_now": False,
                "bid_type": bid_type,
                "sale_status": str(bid_type).upper().replace(" ", "_"),
                "seller_reserve_met": reserve_met,
            },
            "pricing": {
                "current_bid_usd": sold_price or 1000,
                "buy_now_usd": None,
                "last_sold_price_usd": sold_price,
            },
            "location": {"display": "WA - NORTH SEATTLE (WA)"},
            "facility": {"state": "WA", "zip": "98001"},
            "media": {"thumbs_count": 2, "items": [
                {"type": "image", "large": "https://cs.copart.com/a_hrs.jpg"},
                {"type": "image", "large": "https://cs.copart.com/b_hrs.jpg"},
            ]},
        }

    def archive(self, path, generated_at, records, *, mode="open",
                truncated=False, source="copart-web-adapted",
                year_min=2018, year_max=2023):
        document = {
            "generated_at": generated_at,
            "adapted_at": generated_at,
            "platform": "copart", "source": source, "mode": mode,
            "search_params": {
                "make": "Audi", "model": "S5",
                "year_min": year_min, "year_max": year_max,
                "identity_policy": "exact_year_make_model",
            } if "web" in source else {},
            "server_params": ({
                "platform": "copart", "make": "Audi", "model": "S5",
                "year_from": year_min, "year_to": year_max,
                "lot_sub_status": "Ended" if mode == "ended" else mode.title(),
            } if "web" not in source else {}),
            "counts": {"records": len(records), "truncated": truncated},
            "pages": [{"status": 200, "raw": {"data": records}}],
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def load(self, paths):
        with contextlib.redirect_stdout(io.StringIO()):
            records = history.load_records(paths, "copart")
        return history.build_history(records, paths, "copart")

    def test_complete_web_disappearance_is_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = self.archive(tmp / "first.json", "2026-08-17T10:00:00+00:00",
                                 [self.record("64982206")])
            second = self.archive(tmp / "second.json", "2026-08-18T10:00:00+00:00", [])
            result = self.load([first, second])["64982206"]
        self.assertEqual(result["exit_state"], "gone")
        self.assertEqual(result["exit_reason"], "disappeared_from_copart")
        self.assertEqual(result["exit_price_usd"], "")

    def test_truncated_or_apibara_state_slice_cannot_prove_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = self.archive(tmp / "first.json", "2026-08-17T10:00:00+00:00",
                                 [self.record("64982206")])
            truncated = self.archive(
                tmp / "truncated.json", "2026-08-18T10:00:00+00:00", [], truncated=True
            )
            api = self.archive(
                tmp / "api.json", "2026-08-19T10:00:00+00:00", [],
                source="apibara", mode="open",
            )
            result = self.load([first, truncated, api])["64982206"]
        self.assertEqual(result["exit_state"], "still_listed")

    def test_year_scoped_web_snapshot_cannot_remove_out_of_scope_lot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = self.archive(tmp / "first.json", "2026-08-17T10:00:00+00:00",
                                 [self.record("64982206")], year_min=2018,
                                 year_max=2018)
            later = self.archive(tmp / "later.json", "2026-08-18T10:00:00+00:00",
                                 [], year_min=2019, year_max=2023)
            result = self.load([first, later])["64982206"]
        self.assertEqual(result["exit_state"], "still_listed")

    def test_missing_then_reappearing_is_relist_not_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = self.archive(tmp / "first.json", "2026-08-17T10:00:00+00:00",
                                 [self.record("64982206", "2026-08-18T17:00:00+00:00")])
            missing = self.archive(tmp / "missing.json", "2026-08-18T10:00:00+00:00", [])
            again = self.archive(tmp / "again.json", "2026-08-19T10:00:00+00:00",
                                 [self.record("64982206", "2026-08-25T17:00:00+00:00")])
            result = self.load([first, missing, again])["64982206"]
        self.assertEqual(result["exit_state"], "still_listed")
        self.assertEqual(result["relist_count"], 1)
        self.assertEqual(result["auction_at_prior"], "2026-08-18T17:00:00+00:00")

    def test_bid_condition_change_is_visible_in_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            first = self.archive(
                tmp / "first.json", "2026-08-17T10:00:00+00:00",
                [self.record("64982206", bid_type="On Approval", reserve_met=True)],
            )
            second = self.archive(
                tmp / "second.json", "2026-08-18T10:00:00+00:00",
                [self.record("64982206", bid_type="Minimum Bid", reserve_met=False)],
            )
            result = self.load([first, second])["64982206"]
        self.assertEqual(result["bid_condition_first_seen"], "On Approval")
        self.assertEqual(result["bid_condition_prior"], "On Approval")
        self.assertEqual(result["bid_condition_changes"], 1)
        self.assertEqual(result["record_versions"], 2)

    def test_confirmed_ended_price_and_approval_bid_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            sold = self.archive(
                tmp / "sold.json", "2026-08-18T12:00:00+00:00",
                [self.record("64982206", mode="ended", sold_day="2026-08-18",
                             sold_status="Sold", sold_price=7200)],
                source="apibara", mode="ended",
            )
            approval = self.archive(
                tmp / "approval.json", "2026-08-18T12:01:00+00:00",
                [self.record("59548156", mode="ended", sold_day="2026-08-18",
                             sold_status="Sold on Approval", sold_price=6800)],
                source="apibara", mode="ended",
            )
            result = self.load([sold, approval])
        self.assertEqual(result["64982206"]["exit_reason"], "sold_at_auction")
        self.assertEqual(result["64982206"]["exit_price_usd"], 7200)
        self.assertEqual(result["64982206"]["exit_price_source"], "apibara_ended")
        self.assertEqual(result["59548156"]["exit_reason"], "sold_on_approval")
        self.assertEqual(result["59548156"]["exit_price_source"],
                         "apibara_ended_approval_bid")

    def test_active_after_approval_reconciles_as_relist(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            approval = self.archive(
                tmp / "approval.json", "2026-08-18T10:00:00+00:00",
                [self.record("59548156", mode="ended", sold_day="2026-08-17",
                             sold_status="Sold on Approval", sold_price=6800)],
                source="apibara", mode="ended",
            )
            active = self.archive(
                tmp / "active.json", "2026-08-19T10:00:00+00:00",
                [self.record("59548156", "2026-08-25T17:00:00+00:00")],
            )
            result = self.load([approval, active])["59548156"]
        self.assertEqual(result["exit_state"], "still_listed")
        self.assertEqual(result["exit_price_usd"], "")
        self.assertEqual(result["declined_approval"], "confirmed")
        self.assertGreaterEqual(result["relist_count"], 1)

    def test_copart_folder_moves_to_sold_and_relist_can_move_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_root = images.IMAGES_ROOT
            images.IMAGES_ROOT = Path(tmp) / "images"
            try:
                source = (images.IMAGES_ROOT / "open" / "Audi S5" / "FRONT" /
                          "copart" / "2018-64982206-WAUC4CF50JA000001")
                source.mkdir(parents=True)
                (source / "64982206_001.jpg").write_bytes(b"photo")
                departed = {"64982206"}
                audit = {"64982206": {
                    "exit_reason": "sold_at_auction", "exit_price_usd": 7200,
                }}
                moved, skipped = images.archive_sold(
                    "copart", precomputed=(departed, audit)
                )
                destination = moved[0][1]
                self.assertFalse(source.exists())
                self.assertTrue(destination.exists())
                self.assertEqual(skipped, [])

                reopened, moved_from = images.resolve_folder(
                    "copart", "64982206", "WAUC4CF50JA000001", "FRONT",
                    "Audi S5", year="2018", bucket="open",
                )
                self.assertTrue(reopened.exists())
                self.assertFalse(destination.exists())
                self.assertIn("sold/", moved_from)
            finally:
                images.IMAGES_ROOT = old_root

    def test_image_resume_replaces_entire_same_csv_manifest_snapshot(self):
        original_httpx = sys.modules.get("httpx")
        if original_httpx is None:
            fake_httpx = types.ModuleType("httpx")

            class Client:
                def __init__(self, *args, **kwargs):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            fake_httpx.Client = Client
            fake_httpx.HTTPError = Exception
            sys.modules["httpx"] = fake_httpx
        import app.image_pipeline as app_images

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            csv_path = tmp / "open.csv"
            fieldnames = [
                "platform", "lot_number", "vin", "year", "make", "model",
                "primary_damage", "copart_image_urls",
            ]

            def write_csv(rows):
                with csv_path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

            retained = {
                "platform": "copart", "lot_number": "64982206",
                "vin": "WAUC4CF50JA000001", "year": "2018",
                "make": "Audi", "model": "S5", "primary_damage": "Rear End",
                "copart_image_urls": "https://cs.copart.com/a_hrs.jpg",
            }
            dropped = {
                **retained, "lot_number": "65476646",
                "vin": "WAUSNAF55JA000002",
                "copart_image_urls": "https://cs.copart.com/b_hrs.jpg",
            }
            write_csv([retained, dropped])

            original_project_root = images.ROOT
            original_root = images.IMAGES_ROOT
            original_download = app_images._download

            def download(_client, _url, destination):
                destination.write_bytes(b"image")
                return True

            images.ROOT = tmp
            images.IMAGES_ROOT = tmp / "images"
            app_images._download = download
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(images.main([
                        str(csv_path), "--platform", "copart", "--delay", "0",
                    ]), 0)
                write_csv([retained])
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(images.main([
                        str(csv_path), "--platform", "copart", "--delay", "0",
                    ]), 0)
            finally:
                images.ROOT = original_project_root
                images.IMAGES_ROOT = original_root
                app_images._download = original_download
                if original_httpx is None:
                    sys.modules.pop("httpx", None)

            with (tmp / "images" / "open" / "manifest_open.csv").open(
                encoding="utf-8"
            ) as stream:
                manifest = list(csv.DictReader(stream))
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["lot_number"], "64982206")

    def test_offline_derivatives_are_one_logical_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "data"
            raw_dir = root / "open" / "json-raw" / "copart"
            adapted_dir = root / "open" / "json-adapted" / "copart"
            raw_dir.mkdir(parents=True)
            adapted_dir.mkdir(parents=True)
            generated = "2026-08-19T10:00:00+00:00"
            raw = {
                "generated_at": generated, "platform": "copart",
                "source": "copart-web", "mode": "open",
                "search_params": {
                    "make": "Audi", "model": "S5",
                    "year_min": 2018, "year_max": 2023,
                },
                "counts": {"records": 1, "truncated": False},
                "records": [],
            }
            (raw_dir / "raw.json").write_text(json.dumps(raw))
            adapted = self.archive(
                adapted_dir / "adapted.json", generated,
                [self.record("64982206")],
            )
            old_data = history.DATA_DIR
            history.DATA_DIR = root
            try:
                selected = history.all_archives("copart")
            finally:
                history.DATA_DIR = old_data
        self.assertEqual(selected, [adapted])


if __name__ == "__main__":
    unittest.main()
