"""Zero-network contract tests for the repository-owned Copart runner."""
from __future__ import annotations

import subprocess
import os
import shutil
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "analytics" / "scripts" / "run_copart_pipeline.sh"


class CopartPipelineRunnerTests(unittest.TestCase):
    def run_runner(self, *args):
        return subprocess.run(
            [str(RUNNER), *args], cwd=ROOT, text=True,
            capture_output=True, check=False,
        )

    def test_shell_syntax_and_executable_bit(self):
        self.assertTrue(RUNNER.stat().st_mode & 0o111)
        result = subprocess.run(
            ["bash", "-n", str(RUNNER)], cwd=ROOT,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dry_run_is_s5_only_ordered_and_non_mutating(self):
        run_id = "20991231T235959Z"
        run_dir = ROOT / "analytics" / "data" / "runs" / "copart" / "s5" / run_id
        self.assertFalse(run_dir.exists())
        result = self.run_runner(
            "--dry-run", "--run-id", run_id,
            "--ended-from", "2099-06-30", "--ended-to", "2099-12-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(run_dir.exists())
        output = result.stdout
        ordered = [
            "01 apibara-ended", "02 copart-web-open", "03 apibara-open",
            "04 apibara-live", "05-07 vPIC adapters", "08 lot-number merge",
            "09 sold csv-raw", "11 preliminary open csv-cut selection",
            "12-13 selected gallery", "14 final open csv-raw",
            "16 sold/open image lifecycle",
        ]
        positions = [output.index(label) for label in ordered]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("expected ~17 calls; hard cap 45", output)
        self.assertIn("expected 6 calls (one/year); hard cap 120", output)
        self.assertIn("2018-2023 Audi S5", output)
        self.assertIn("--exclude-body-style coupe\\,convertible", output)
        self.assertIn("--max-odometer 99999", output)
        self.assertIn("--max-distance 2999", output)
        self.assertIn("nocoupe_noconv_lt100k_lt3000mi", output)
        self.assertNotIn("--body-style", output)
        for unsupported in ("Audi A4", "Audi S4", "Audi A5", "Audi RS5", "Audi RS 5"):
            self.assertNotIn(unsupported, output)
        self.assertIn("No files, browser sessions, API calls, or image downloads", output)

    def test_a5_is_an_explicit_independent_scope(self):
        run_id = "20991231T235958Z"
        run_dir = ROOT / "analytics" / "data" / "runs" / "copart" / "a5" / run_id
        self.assertFalse(run_dir.exists())
        result = self.run_runner(
            "--dry-run", "--model", "A5", "--gallery-workers", "3",
            "--run-id", run_id,
            "--ended-from", "2099-06-30", "--ended-to", "2099-12-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(run_dir.exists())
        self.assertIn("2018-2023 Audi A5", result.stdout)
        self.assertIn("--model A5", result.stdout)
        self.assertIn("model-folder Audi\\ A5", result.stdout)
        self.assertIn("ended <= 50", result.stdout)
        self.assertIn("expected ~40 calls; hard cap 70", result.stdout)
        self.assertIn("workers: 3 isolated tab(s)", result.stdout)
        self.assertIn("--workers 3", result.stdout)
        self.assertIn("--exclude-body-style coupe\\,convertible", result.stdout)
        self.assertIn("--max-odometer 99999", result.stdout)
        self.assertIn("--max-distance 2999", result.stdout)
        self.assertIn("--tier 2", result.stdout)
        self.assertIn("nocoupe_noconv", result.stdout)

    def test_s4_uses_pre_gallery_cut_and_five_workers(self):
        result = self.run_runner(
            "--dry-run", "--model", "S4", "--gallery-workers", "5",
            "--run-id", "20991231T235957Z",
            "--ended-from", "2099-06-30", "--ended-to", "2099-12-31",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2018-2023 Audi S4", result.stdout)
        self.assertIn("workers: 5 isolated tab(s)", result.stdout)
        self.assertIn("--workers 5", result.stdout)
        self.assertIn("--lots-from-csv", result.stdout)
        self.assertIn("preliminary open csv-cut selection", result.stdout)
        self.assertLess(
            result.stdout.index("preliminary open csv-cut selection"),
            result.stdout.index("selected gallery reuse/browser completion"),
        )
        self.assertIn("--exclude-body-style coupe\\,convertible", result.stdout)
        self.assertIn("--max-odometer 99999", result.stdout)
        self.assertIn("--max-distance 2999", result.stdout)
        self.assertIn("--tier 1", result.stdout)

    def test_models_outside_validated_set_fail_closed(self):
        # RS5 became a validated cohort on 2026-08-20; RS3 stands in as a model
        # that has had no audited page-cap/tier decision.
        result = self.run_runner("--dry-run", "--model", "RS3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RS5, S5, A5, or S4", result.stderr)

    def test_rs5_is_a_validated_cohort(self):
        result = self.run_runner("--dry-run", "--model", "RS5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Smallest cohort: 26 open lots on the 2026-08-19 probe, so a small cap.
        self.assertIn("ended <= 15", result.stdout)
        self.assertIn("--exclude-body-style coupe\\,convertible", result.stdout)
        self.assertIn("--max-odometer 99999", result.stdout)
        self.assertIn("--max-distance 2999", result.stdout)
        self.assertIn("--tier 1", result.stdout)

    def test_pm_pass_uses_its_own_namespace(self):
        am = self.run_runner("--dry-run", "--pass", "am")
        pm = self.run_runner("--dry-run", "--pass", "pm")
        self.assertEqual(am.returncode, 0, am.stdout + am.stderr)
        self.assertEqual(pm.returncode, 0, pm.stdout + pm.stderr)
        # A date-only namespace made the second run of the day a no-op.
        self.assertIn("T000000Z", am.stdout.splitlines()[0])
        self.assertIn("T120000Z", pm.stdout.splitlines()[0])

    def test_pm_budget_always_matches_the_inheritance_decision(self):
        """Whether a reusable AM run exists depends on the machine's history,
        so assert the invariant rather than one branch: the budget the operator
        reads must agree with the inheritance line above it. An earlier version
        of this test hard-coded the no-AM-run branch and started failing the
        moment a real AM run succeeded."""
        result = self.run_runner("--dry-run", "--pass", "pm")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        inherited = "— SKIPPED" in result.stdout
        if inherited:
            self.assertIn("ended: INHERITED from", result.stdout)
            self.assertIn("expected ~2 (open + live) calls", result.stdout)
            self.assertNotIn("FULL cost", result.stdout)
        else:
            self.assertIn("no usable AM sold artifacts", result.stdout)
            self.assertIn("FULL cost", result.stdout)
            self.assertNotIn("ended: INHERITED", result.stdout)

    def test_pm_namespace_must_differ_from_am(self):
        result = self.run_runner("--dry-run", "--pass", "pm",
                                 "--run-id", "20260820T000000Z")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distinct from the AM run", result.stderr)

    def test_unknown_pass_fails_closed(self):
        result = self.run_runner("--dry-run", "--pass", "midday")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--pass must be am, pm, or full", result.stderr)

    def test_invalid_run_id_fails_before_any_work(self):
        result = self.run_runner("--dry-run", "--run-id", "today")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("YYYYMMDDTHHMMSSZ", result.stderr)

    def test_gallery_workers_are_capped_at_five(self):
        result = self.run_runner("--dry-run", "--gallery-workers", "6")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("between 1 and 5", result.stderr)

    def test_failure_and_checkpoint_guards_are_not_output_filters(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn("set -Eeuo pipefail", source)
        self.assertIn("returned success but failed artifact validation", source)
        self.assertIn("checkpoint exists but its artifact no longer validates", source)
        self.assertNotIn("| grep -E", source)
        self.assertLess(
            source.index("validate_apibara_ended()"),
            source.index("run_stage 01-apibara-ended"),
        )

    def test_cut_migration_does_not_invalidate_statvin(self):
        source = RUNNER.read_text(encoding="utf-8")
        self.assertIn('migration_stages=(10-history-sold', source)
        self.assertIn('if [[ ",$CHANGED_CONFIG_KEYS," == *,statvin,*', source)
        self.assertIn('migration_stages=(08a-statvin-pull 08b-statvin-enrich', source)

    def test_stubbed_full_run_resumes_without_reexecuting_stages(self):
        stub = r'''#!/usr/bin/env python3
import csv, json, os, sys
from pathlib import Path

def records(document):
    for page in document.get("pages") or []:
        for record in (page.get("raw") or {}).get("data") or []:
            yield record

def normalize_lot(value):
    return str(value or "").strip()

def needs_gallery_capture(record):
    provenance = ((record.get("enrichment") or {}).get("copart_authorized_image_feed") or {})
    complete = provenance.get("capture_completeness") == "first_party_lot_images_response"
    count = sum(i.get("type") == "image" for i in (record.get("media") or {}).get("items", []))
    return count <= 1 and not complete

def lot_numbers_from_csv(path):
    with open(path, encoding="utf-8", newline="") as stream:
        return [row["lot_number"] for row in csv.DictReader(stream)]

def output_arg():
    return Path(sys.argv[sys.argv.index("--out") + 1])

def write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")

def selected_model():
    return sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "S5"

def canonical(complete=False):
    items = [{"type": "image", "large": "https://cs.copart.com/a_hrs.jpg"}]
    enrichment = {}
    if complete:
        items.append({"type": "image", "large": "https://cs.copart.com/b_hrs.jpg"})
        enrichment["copart_authorized_image_feed"] = {
            "capture_completeness": "first_party_lot_images_response"
        }
    return {
        "platform": "copart", "lot_number": "64982206", "vin": "WAUC4CF50JA000001",
        "year": 2018, "make": "Audi", "model": selected_model(), "enrichment": enrichment,
        "media": {"items": items, "thumbs_count": len(items)},
    }

if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    with open(os.environ["STUB_CALL_LOG"], "a", encoding="utf-8") as log:
        log.write(name + "\n")
    if os.environ.get("FAIL_STAGE") == name:
        raise SystemExit(7)
    if name == "pull_apibara_01.py":
        mode = sys.argv[2]
        write_json(output_arg(), {
            "platform": "copart", "mode": mode,
            "pages": [{"status": 200, "raw": {"data": [canonical()]}}],
            "counts": {"records": 1, "calls_used": 1, "truncated": False},
        })
    elif name == "pull_copart_web_01.py":
        write_json(output_arg(), {
            "platform": "copart", "source": "copart-web", "mode": "open",
            "search_params": {"make": "Audi", "model": selected_model(),
                              "year_min": 2018, "year_max": 2023},
            "queries": [{"pages": [{"status": 200}]} for _ in range(6)],
            "counts": {"records": 1, "truncated": False, "failed_queries": 0},
        })
    elif name == "copart_vpic_adapt_01.py":
        source = json.load(open(sys.argv[1], encoding="utf-8"))
        source["adapter"] = {"name": "copart_vpic_adapt_01",
                             "market_scope": {"policy": "us_only"}}
        write_json(output_arg(), source)
    elif name == "copart_web_adapt_01.py":
        write_json(output_arg(), {
            "platform": "copart", "source": "copart-web-adapted", "mode": "open",
            "pages": [{"status": 200, "raw": {"data": [canonical()]}}],
            "counts": {"records": 1, "truncated": False},
            "adapter": {"market_scope": {"policy": "us_only"}},
        })
    elif name == "pull_statvin_web_01.py":
        write_json(output_arg(), {
            "platform": "copart", "source": "statvin-search", "mode": "open",
            "records": [{"lot_number": "64794106", "vin": "WAUPNAF57JA000001",
                         "year": 2018, "seller_class": "dealer",
                         "seller_label": "Dealer Non-insurance"}],
            "counts": {"records": 1, "truncated": False},
        })
    elif name == "copart_statvin_enrich_01.py":
        document = json.load(open(sys.argv[1], encoding="utf-8"))
        write_json(output_arg(), document)
    elif name in {"copart_image_enrich_01.py", "copart_browser_enrich_01.py"}:
        document = json.load(open(sys.argv[1], encoding="utf-8"))
        document["pages"][0]["raw"]["data"] = [canonical(complete=True)]
        write_json(output_arg(), document)
    elif name in {"apibara_json2csv_copart_01.py", "data_pull_01.py"}:
        path = output_arg()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=["platform", "lot_number", "model", "market",
                            "body_style", "odometer_mi", "distance_mi"],
            )
            writer.writeheader()
            writer.writerow({"platform": "copart", "lot_number": "64982206",
                             "model": selected_model(),
                             "market": "UnitedStates", "body_style": "HATCHBACK",
                             "odometer_mi": "99999", "distance_mi": "2999"})
    elif name == "pull_images_01.py":
        root = Path(__file__).resolve().parents[2]
        manifest = root / "images" / "open" / "manifest_open.csv"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("lot_number\n64982206\n", encoding="utf-8")
        print("Done. 0 downloaded, 2 already present, 0 failed")
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            scripts = tmp / "analytics" / "scripts"
            scripts.mkdir(parents=True)
            copied_runner = scripts / RUNNER.name
            shutil.copy2(RUNNER, copied_runner)
            stage_names = [
                "pull_apibara_01.py", "pull_copart_web_01.py",
                "copart_vpic_adapt_01.py", "copart_web_adapt_01.py",
                "pull_statvin_web_01.py", "copart_statvin_enrich_01.py",
                "copart_image_enrich_01.py", "copart_browser_enrich_01.py",
                "apibara_json2csv_copart_01.py", "data_pull_01.py",
                "pull_images_01.py",
            ]
            for name in stage_names:
                path = scripts / name
                path.write_text(stub, encoding="utf-8")
                path.chmod(0o755)
            call_log = tmp / "calls.log"
            # The production runner checks its interpreter before spending any
            # API calls. A zero-byte test module satisfies that preflight
            # without requiring the host test Python to install dependencies.
            (tmp / "httpx.py").write_text("", encoding="utf-8")
            env = {
                **os.environ,
                "STUB_CALL_LOG": str(call_log),
                "PYTHONPATH": str(tmp),
            }
            args = [
                str(copied_runner), "--run-id", "20991231T120000Z",
                "--ended-from", "2099-06-30", "--ended-to", "2099-12-31",
            ]
            first = subprocess.run(args, cwd=tmp, env=env, text=True,
                                   capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            calls_after_first = call_log.read_text(encoding="utf-8").splitlines()
            second = subprocess.run(args, cwd=tmp, env=env, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(call_log.read_text(encoding="utf-8").splitlines(),
                             calls_after_first)
            self.assertGreaterEqual(second.stdout.count("SKIP"), 13)
            manifest = (tmp / "analytics" / "data" / "runs" / "copart" /
                        "s5" / "20991231T120000Z" / "manifest.json")
            self.assertTrue(manifest.is_file())

            failure_args = [
                str(copied_runner), "--run-id", "20991231T130000Z",
                "--ended-from", "2099-06-30", "--ended-to", "2099-12-31",
            ]
            before_failure = len(call_log.read_text(encoding="utf-8").splitlines())
            failed = subprocess.run(
                failure_args, cwd=tmp,
                env={**env, "FAIL_STAGE": "pull_copart_web_01.py"},
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(failed.returncode, 0)
            failure_calls = call_log.read_text(encoding="utf-8").splitlines()[before_failure:]
            self.assertEqual(failure_calls,
                             ["pull_apibara_01.py", "pull_copart_web_01.py"])
            failure_dir = (tmp / "analytics" / "data" / "runs" / "copart" /
                           "s5" / "20991231T130000Z")
            self.assertTrue((failure_dir / "01-apibara-ended.done").is_file())
            self.assertFalse((failure_dir / "02-copart-web-open.done").exists())


class CohortSweepTests(unittest.TestCase):
    """The bare `am`/`pm` form, matching run_iaai_pipeline.sh."""

    def run_runner(self, *args):
        return subprocess.run([str(RUNNER), *args], cwd=ROOT, text=True,
                              capture_output=True, check=False)

    def test_bare_pass_sweeps_every_cohort(self):
        result = self.run_runner("am", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for cohort in ("S5", "A5", "S4", "RS5"):
            self.assertIn(f"Copart {cohort} (am)", result.stdout)
        self.assertIn("sweep complete", result.stdout)

    def test_sweep_forwards_dry_run_to_every_cohort(self):
        """Regression, and it cost real money.

        The sweep read "$@" AFTER the parse loop had already consumed it, so
        --dry-run never reached the child: a dry run started a live S5 chain
        and spent 15 metered APIBara calls off a 100-call month before it was
        killed. SWEEP_ARGS now captures argv before parsing.
        """
        result = self.run_runner("am", "--dry-run")
        self.assertEqual(result.stdout.count("DRY RUN"), 4)
        self.assertNotIn("pipeline run", result.stdout)
        self.assertNotIn("START 01-apibara-ended", result.stdout)

    def test_flags_other_than_dry_run_also_reach_the_children(self):
        result = self.run_runner("am", "--dry-run", "--gallery-workers", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("workers: 3 isolated tab(s)"), 4)

    def test_naming_a_model_runs_only_that_cohort(self):
        result = self.run_runner("am", "--model", "RS5", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count("DRY RUN"), 1)
        self.assertNotIn("sweep complete", result.stdout)

    def test_flag_form_keeps_its_single_cohort_behaviour(self):
        # `--pass am` and a bare `--dry-run` predate the sweep and must not
        # silently start running four cohorts.
        for args in (("--pass", "am", "--dry-run"), ("--dry-run",)):
            with self.subTest(args=args):
                result = self.run_runner(*args)
                self.assertEqual(result.stdout.count("DRY RUN"), 1)

    def test_bare_pm_selects_the_pm_namespace(self):
        result = self.run_runner("pm", "--model", "S5", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("T120000Z", result.stdout.splitlines()[0])


class StatvinWiringTests(unittest.TestCase):
    def run_runner(self, *args):
        return subprocess.run([str(RUNNER), *args], cwd=ROOT, text=True,
                              capture_output=True, check=False)

    def test_statvin_stages_run_before_the_selection(self):
        plan = self.run_runner("am", "--model", "A5", "--dry-run").stdout
        self.assertIn("pull_statvin_web_01.py", plan)
        self.assertIn("copart_statvin_enrich_01.py", plan)
        # Seller class must exist before the cut, because the cut is what keeps
        # dealer lots out of the gallery stage.
        self.assertLess(plan.index("copart_statvin_enrich_01.py"),
                        plan.index("data_pull_01.py"))

    def test_each_cohort_carries_a_statvin_option_value(self):
        for model in ("S5", "A5", "S4", "RS5"):
            with self.subTest(model=model):
                result = self.run_runner("am", "--model", model, "--dry-run")
                self.assertEqual(result.returncode, 0, result.stderr)
                # A bare model name silently returns an empty stat.vin page.
                self.assertIn("_group_id_", result.stdout)

    def test_rs5_uses_the_shared_s5_group(self):
        # stat.vin groups RS5 under S5/RS5 exactly as Copart does; an RS5-only
        # search returns nothing at all.
        rs5 = self.run_runner("am", "--model", "RS5", "--dry-run").stdout
        self.assertIn("S5_group_id_24870", rs5)
        self.assertNotIn("RS5_group_id", rs5)

    def test_dealer_lots_are_excluded_from_the_cut(self):
        plan = self.run_runner("am", "--model", "A5", "--dry-run").stdout
        self.assertIn("--exclude-seller-class dealer", plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)
