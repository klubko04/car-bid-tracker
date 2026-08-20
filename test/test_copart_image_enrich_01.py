"""Zero-network tests for explicit authorized-broker Copart media parsing."""
import contextlib
import base64
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import copart_image_enrich_01 as images  # noqa: E402


class CopartImageEnrichTests(unittest.TestCase):
    def record(self):
        return {
            "platform": "copart", "lot_number": "64982206",
            "vin": "WAUC4CF52JA******", "year": 2018,
            "make": "AUDI", "model": "S5",
            "media": {"thumbs_count": 1, "items": [
                {"type": "image", "thumb": "https://cs.copart.com/one_thb.jpg"}
            ]},
        }

    def lot(self):
        return {
            "source": "copart_direct", "inventoryAuction": "Copart",
            "lotNumber": 64982206, "id": 64982206,
            "vin": "WAUC4CF52JA039705", "year": 2018,
            "make": "Audi", "model": "S5",
            "images": [
                {"thumbnail": "https://cs.copart.com/a_thb.jpg",
                 "full": "https://cs.copart.com/a_ful.jpg",
                 "hdr": "https://cs.copart.com/a_hrs.jpg", "sequence": 0},
                {"thumbnail": "https://cs.copart.com/b_thb.jpg",
                 "full": "https://cs.copart.com/b_ful.jpg",
                 "hdr": "https://cs.copart.com/b_hrs.jpg", "sequence": 1},
            ],
            "engineVideo": {"url": "https://cs.copart.com/engine.mp4",
                            "type": "engine"},
        }

    def html(self, lot=None):
        state = {"queries": [{
            "queryKey": ["lot-info-data", "Lot:64982206_copart"],
            "state": {"data": {"lot": lot or self.lot()}},
        }]}
        return "<script>window.__REACT_QUERY_STATE__ = " + json.dumps(state) + ";</script>"

    def document(self):
        return {
            "platform": "copart", "mode": "open",
            "pages": [{"status": 200, "raw": {"data": [self.record()]}}],
        }

    def har(self, lot="64982206"):
        payload = json.dumps({"images": [
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/one_thb.jpg",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/one_ful.jpg",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/one_hrs.jpg",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/two_hrs.jpg",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/poster_thb.jpg",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/engine_O.mp4",
            r"https:\/\/cs.copart.com\/v1\/AUTH_svc.pdoc00001\/lpp\/0826\/engine_vthb.jpg",
            r"https:\/\/cs.copart.com\/v1\/website\/logo.png",
            r"https:\/\/example.com\/not-copart.jpg",
        ]})
        return {"log": {"entries": [
            {
                "_resourceType": "document",
                "request": {"url": f"https://www.copart.com/lot/{lot}/car"},
                "response": {"content": {"mimeType": "text/html", "text": ""}},
            },
            {
                "_resourceType": "xhr",
                "request": {"url": "https://www.copart.com/gallery/data"},
                "response": {"content": {
                    "mimeType": "application/json",
                    "encoding": "base64",
                    "text": base64.b64encode(payload.encode()).decode(),
                }},
            },
        ]}}

    def structured_har(self, lot="64982206"):
        document = self.har(lot)
        gallery = {
            "returnCode": 1,
            "data": {"imagesList": {
                "IMAGE": [
                    {
                        "ln": int(lot), "imageSeqNumber": 1,
                        "imageLabelCode": "DSFA",
                        "thumbnailUrl": "https://cs.copart.com/v1/AUTH_x/lpp/a_thb.jpg",
                        "fullUrl": "https://cs.copart.com/v1/AUTH_x/lpp/a_ful.jpg",
                        "highResUrl": "https://cs.copart.com/v1/AUTH_x/lpp/a_hrs.jpg",
                    },
                    {
                        "lotNumberStr": lot, "imageSeqNumber": 14,
                        "imageLabelCode": "VINS",
                        "thumbnailUrl": "https://cs.copart.com/v1/AUTH_x/lpp/vin_vthb.jpg",
                        "fullUrl": "https://cs.copart.com/v1/AUTH_x/lpp/vin_vful.jpg",
                        "highResUrl": "https://cs.copart.com/v1/AUTH_x/lpp/vin_vhrs.jpg",
                    },
                ],
                "EXTERIOR_360": [{
                    "ln": int(lot), "imageSeqNumber": 35,
                    "thumbnailUrl": "https://cs.copart.com/v1/AUTH_x/lpp/ext_thb.jpg",
                    "fullUrl": "https://c-static.copart.com/v1/AUTH_x/lpp/ext_ful.jpg",
                    "image360Url": "https://c-static.copart.com/v1/AUTH_x/lpp/ext_frames_0.jpg",
                }],
                "ENGINE_VIDEO_SOUND": [{
                    "ln": int(lot), "imageSeqNumber": 90,
                    "highResUrl": "https://cs.copart.com/v1/AUTH_x/lpp/engine_O.mp4",
                }],
            }},
        }
        document["log"]["entries"].append({
            "_resourceType": "xhr",
            "request": {"url": (
                "https://www.copart.com/public/data/lotdetails/solr/lot-images/"
            )},
            "response": {"content": {
                "mimeType": "application/json", "text": json.dumps(gallery),
            }},
        })
        return document

    def test_explicit_urls_and_video_are_copied_without_rewriting(self):
        feed = images.parse_feed(self.html(), self.record())
        media = feed["media"]
        self.assertEqual(feed["identity_conflicts"], [])
        self.assertEqual(media["thumbs_count"], 2)
        self.assertEqual(media["items"][0]["large"],
                         "https://cs.copart.com/a_hrs.jpg")
        self.assertEqual(media["items"][1]["full"],
                         "https://cs.copart.com/b_ful.jpg")
        self.assertEqual(media["items"][2]["url"],
                         "https://cs.copart.com/engine.mp4")

    def test_browser_har_extracts_only_explicit_lot_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(self.har()))
            feed = images.parse_browser_har(path, self.record())
        media = feed["media"]
        self.assertEqual(feed["identity_conflicts"], [])
        self.assertEqual(feed["explicit_url_count"], 7)
        self.assertEqual(feed["thumb_only_image_count"], 1)
        self.assertEqual(media["thumbs_count"], 2)
        self.assertEqual(media["items"][0]["thumb"],
                         "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0826/one_thb.jpg")
        self.assertEqual(media["items"][0]["large"],
                         "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0826/one_hrs.jpg")
        self.assertEqual(media["items"][1]["large"],
                         "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0826/two_hrs.jpg")
        self.assertTrue(media["has_video"])

    def test_browser_har_rejects_a_different_lot_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(self.har("65000000")))
            feed = images.parse_browser_har(path, self.record())
        self.assertEqual(feed["identity_conflicts"][0]["field"], "lot_number")

    def test_browser_har_prefers_structured_first_party_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            path.write_text(json.dumps(self.structured_har()))
            feed = images.parse_browser_har(path, self.record())
        media = feed["media"]
        self.assertEqual(feed["identity_conflicts"], [])
        self.assertEqual(feed["capture_completeness"],
                         "first_party_lot_images_response")
        self.assertEqual(media["thumbs_count"], 2)
        self.assertEqual(media["items"][1]["large"],
                         "https://cs.copart.com/v1/AUTH_x/lpp/vin_vhrs.jpg")
        self.assertEqual(media["items"][2]["video_type"],
                         "engine_video_sound")
        self.assertTrue(media["has_360"])
        self.assertEqual(media["panoramas"][0]["frame_url"],
                         "https://c-static.copart.com/v1/AUTH_x/lpp/ext_frames_0.jpg")

    def test_structured_gallery_lot_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capture.har"
            document = self.structured_har()
            gallery_entry = document["log"]["entries"][-1]
            gallery = json.loads(gallery_entry["response"]["content"]["text"])
            for candidates in gallery["data"]["imagesList"].values():
                for item in candidates:
                    item.pop("lotNumberStr", None)
                    item["ln"] = 65000000
            gallery_entry["response"]["content"]["text"] = json.dumps(gallery)
            path.write_text(json.dumps(document))
            feed = images.parse_browser_har(path, self.record())
        self.assertTrue(any(
            conflict.get("reason") == "lot-images response belonged to another lot"
            for conflict in feed["identity_conflicts"]
        ))

    def test_browser_har_cli_enriches_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "adapted.json"
            har = tmp / "capture.har"
            destination = tmp / "images.json"
            source.write_text(json.dumps(self.document()))
            har.write_text(json.dumps(self.har()))
            with contextlib.redirect_stdout(io.StringIO()):
                code = images.main([
                    str(source), "--har", f"64982206={har}",
                    "--out", str(destination),
                ])
            output = json.loads(destination.read_text())
        record = output["pages"][0]["raw"]["data"][0]
        self.assertEqual(code, 0)
        self.assertEqual(record["media"]["thumbs_count"], 2)
        self.assertEqual(
            record["enrichment"]["copart_authorized_image_feed"]["source"],
            images.BROWSER_SOURCE,
        )
        self.assertEqual(output["image_enrichment"]["counts"], {"enriched": 1})

    def test_force_refreshes_equal_count_from_structured_gallery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            document = self.document()
            record = document["pages"][0]["raw"]["data"][0]
            record["media"]["items"].append({
                "type": "image", "thumb": "https://cs.copart.com/two_thb.jpg",
            })
            record["media"]["thumbs_count"] = 2
            source = tmp / "adapted.json"
            har = tmp / "capture.har"
            destination = tmp / "images.json"
            source.write_text(json.dumps(document))
            har.write_text(json.dumps(self.structured_har()))
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([
                    str(source), "--har", f"64982206={har}", "--force",
                    "--out", str(destination),
                ])
            output = json.loads(destination.read_text())
        record = output["pages"][0]["raw"]["data"][0]
        provenance = record["enrichment"]["copart_authorized_image_feed"]
        self.assertEqual(output["image_enrichment"]["counts"],
                         {"verified_refresh": 1})
        self.assertEqual(provenance["capture_completeness"],
                         "first_party_lot_images_response")

    def test_non_copart_media_host_is_rejected(self):
        lot = self.lot()
        lot["images"] = [{"hdr": "https://example.com/guessed_hrs.jpg"}]
        feed = images.parse_feed(self.html(lot), self.record())
        self.assertEqual(feed["image_count"], 0)
        self.assertEqual(feed["rejected_media_count"], 1)

    def test_vin_prefix_conflict_fails_identity_validation(self):
        lot = self.lot()
        lot["vin"] = "WAUB4CF53KA094371"
        feed = images.parse_feed(self.html(lot), self.record())
        self.assertEqual(feed["identity_conflicts"][0]["field"], "vin_prefix")

    def test_saved_page_cli_enriches_media_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "adapted.json"
            page = tmp / "lot.html"
            destination = tmp / "images.json"
            source.write_text(json.dumps(self.document()))
            page.write_text(self.html())
            with contextlib.redirect_stdout(io.StringIO()):
                code = images.main([
                    str(source), "--html", f"64982206={page}",
                    "--out", str(destination),
                ])
            output = json.loads(destination.read_text())
        record = output["pages"][0]["raw"]["data"][0]
        self.assertEqual(code, 0)
        self.assertEqual(record["vin"], "WAUC4CF52JA******")
        self.assertEqual(record["media"]["thumbs_count"], 2)
        self.assertEqual(output["image_enrichment"]["counts"], {"enriched": 1})

    def test_lot_without_a_capture_is_reported_not_fetched(self):
        """The broker HTTP route is retired; a bare run must fetch nothing."""
        second = self.record()
        second["lot_number"] = "65000000"
        document = self.document()
        document["pages"][0]["raw"]["data"].append(second)

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "adapted.json"
            destination = tmp / "images.json"
            source.write_text(json.dumps(document))
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([str(source), "--out", str(destination)])
            output = json.loads(destination.read_text())

        enrichment = output["image_enrichment"]
        # Every candidate is an explicit gap, never a silent pass.
        self.assertEqual(enrichment["counts"], {"no_capture_supplied": 2})
        self.assertEqual(enrichment["network"], "retired_no_http_requests")
        for entry in enrichment["audit"]:
            self.assertEqual(entry["status"], "no_capture_supplied")
            self.assertIn("--har", entry["hint"])

    def test_no_http_transport_remains(self):
        # Guards against a fetcher being reintroduced into this stage.
        self.assertFalse(hasattr(images, "Session"))
        self.assertFalse(hasattr(images, "USER_AGENT"))
        parser = images.build_arg_parser()
        flags = {action.option_strings[0] for action in parser._actions
                 if action.option_strings}
        self.assertNotIn("--delay", flags)
        self.assertNotIn("--timeout", flags)

    def test_reuse_only_run_is_not_labelled_broker_sourced(self):
        """A pure reuse run advertised broker provenance it never had."""
        prior = self.document()
        prior_record = prior["pages"][0]["raw"]["data"][0]
        prior_record["media"] = images.media_from_lot(self.lot())[0]
        prior_record.setdefault("enrichment", {})[
            "copart_authorized_image_feed"
        ] = {"source": images.BROWSER_SOURCE, "image_count": 2}

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prior_path = tmp / "prior.json"
            source = tmp / "adapted.json"
            destination = tmp / "images.json"
            prior_path.write_text(json.dumps(prior))
            source.write_text(json.dumps(self.document()))
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([str(source), "--out", str(destination),
                             "--reuse-from", str(prior_path), "--reuse-only"])
            output = json.loads(destination.read_text())

        enrichment = output["image_enrichment"]
        # The media came from a browser capture, so that is what it must say.
        self.assertEqual(enrichment["sources"], [images.BROWSER_SOURCE])
        self.assertNotIn(images.SOURCE, enrichment["sources"])

    def test_prior_media_can_be_reused_without_network(self):
        prior = self.document()
        prior_record = prior["pages"][0]["raw"]["data"][0]
        prior_record["media"] = images.media_from_lot(self.lot())[0]
        prior_record.setdefault("enrichment", {})[
            "copart_authorized_image_feed"
        ] = {"source": images.SOURCE, "image_count": 2}

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "regenerated.json"
            prior_path = tmp / "prior.json"
            destination = tmp / "images.json"
            source.write_text(json.dumps(self.document()))
            prior_path.write_text(json.dumps(prior))
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([
                    str(source), "--reuse-from", str(prior_path), "--reuse-only",
                    "--out", str(destination),
                ])
            output = json.loads(destination.read_text())
        record = output["pages"][0]["raw"]["data"][0]
        self.assertEqual(record["media"]["thumbs_count"], 2)
        self.assertEqual(output["image_enrichment"]["counts"], {"reused": 1})

    def test_csv_cut_allowlist_prevents_reuse_for_filtered_lot(self):
        current = self.document()
        second = json.loads(json.dumps(current["pages"][0]["raw"]["data"][0]))
        second["lot_number"] = "65476646"
        current["pages"][0]["raw"]["data"].append(second)

        prior = json.loads(json.dumps(current))
        for record in prior["pages"][0]["raw"]["data"]:
            record["media"] = images.media_from_lot(self.lot())[0]

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "current.json"
            prior_path = tmp / "prior.json"
            selection = tmp / "selection.csv"
            destination = tmp / "output.json"
            source.write_text(json.dumps(current))
            prior_path.write_text(json.dumps(prior))
            selection.write_text("lot_number\n64982206\n")
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([
                    str(source), "--reuse-from", str(prior_path), "--reuse-only",
                    "--lots-from-csv", str(selection), "--out", str(destination),
                ])
            output = json.loads(destination.read_text())

        first, filtered = output["pages"][0]["raw"]["data"]
        self.assertEqual(images.image_count(first), 2)
        self.assertEqual(images.image_count(filtered), 1)
        self.assertEqual(output["image_enrichment"]["counts"], {"reused": 1})
        self.assertEqual(output["image_enrichment"]["lot_allowlist_count"], 1)

    def test_verified_one_photo_gallery_is_complete_and_reusable(self):
        prior = self.document()
        prior_record = prior["pages"][0]["raw"]["data"][0]
        prior_record.setdefault("enrichment", {})[
            "copart_authorized_image_feed"
        ] = {
            "source": images.BROWSER_SOURCE,
            "image_count": 1,
            "capture_completeness": "first_party_lot_images_response",
        }

        regenerated = self.document()
        current = regenerated["pages"][0]["raw"]["data"][0]
        self.assertTrue(images.needs_gallery_capture(current))

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            source = tmp / "regenerated.json"
            prior_path = tmp / "prior.json"
            destination = tmp / "images.json"
            source.write_text(json.dumps(regenerated))
            prior_path.write_text(json.dumps(prior))
            with contextlib.redirect_stdout(io.StringIO()):
                images.main([
                    str(source), "--reuse-from", str(prior_path), "--reuse-only",
                    "--out", str(destination),
                ])
            output = json.loads(destination.read_text())

        record = output["pages"][0]["raw"]["data"][0]
        self.assertEqual(output["image_enrichment"]["counts"], {"reused": 1})
        self.assertTrue(images.gallery_is_complete(record))
        self.assertFalse(images.needs_gallery_capture(record))



class ReconstructedConstantTests(unittest.TestCase):
    """Pins the seven constants lost to an editing accident on 2026-08-19.

    The file was untracked, so there was no baseline to restore from. Three
    constants are byte-exact from surviving grep output; four were rebuilt from
    their call sites and verified against the archived corpus (11,354 .jpg and
    163 .mp4 URLs, 9,413 unique, 515 galleries). These assertions encode what
    that corpus proved, so a future edit cannot quietly change the contract.
    """

    def test_media_suffix_parses_every_observed_variant(self):
        # Observed token counts in the corpus: thb 3497, ful 3536, hrs 3536,
        # o 163, vthb 259, vhrs 267, vful 259 — and nothing unparsed.
        for token in ("thb", "ful", "hrs", "o", "vthb", "vhrs", "vful"):
            path = f"/v1/AUTH_svc.pdoc00001/lpp/0826/deadbeef_{token}.jpg"
            match = images.MEDIA_SUFFIX_RE.search(path)
            self.assertIsNotNone(match, token)
            self.assertEqual(match.group(1), token)

    def test_asset_key_collapses_variants_of_one_photo(self):
        base = "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0826/deadbeef"
        keys = {images.image_asset_key(f"{base}_{v}.jpg")
                for v in ("thb", "ful", "hrs")}
        self.assertEqual(len(keys), 1)

    def test_extension_sets_match_the_corpus(self):
        # Only these two extensions exist across every archived Copart asset.
        self.assertIn(".jpg", images.IMAGE_EXTENSIONS)
        self.assertIn(".mp4", images.VIDEO_EXTENSIONS)
        self.assertFalse(images.IMAGE_EXTENSIONS & images.VIDEO_EXTENSIONS)

    def test_host_filter_accepts_copart_media_only(self):
        good = "https://cs.copart.com/v1/AUTH_svc.pdoc00001/lpp/0826/a_hrs.jpg"
        self.assertEqual(images.https_copart_url(good), good)
        for bad in ("http://cs.copart.com/lpp/0826/a_hrs.jpg",      # not https
                    "https://evil.example.com/lpp/0826/a_hrs.jpg",  # wrong host
                    "https://www.autobidmaster.com/lpp/a_hrs.jpg"):
            self.assertIsNone(images.https_copart_url(bad), bad)

    def test_full_vin_regex_rejects_masked_and_invalid_vins(self):
        self.assertTrue(images.FULL_VIN_RE.fullmatch("WAUC4CF52KA123456"))
        for bad in ("WAUC4CF52KA******",   # Copart's public mask
                    "WAUC4CF52KA12345",    # 16 chars
                    "WAUI4CF52KA123456"):  # I is not a legal VIN character
            self.assertFalse(images.FULL_VIN_RE.fullmatch(bad), bad)

    def test_lot_page_regex_extracts_the_lot_number(self):
        match = images.LOT_PAGE_RE.match("https://www.copart.com/lot/64982206/x")
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "64982206")


if __name__ == "__main__":
    unittest.main(verbosity=2)
