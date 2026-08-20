"""Zero-network tests for the shared Copart seller taxonomy.

The registry entries and the "APIBara disagrees" cases below are all taken from
the 2018-2023 Audi S5 ended cohort (n=290, APIBara) and the matching open web
pull (n=73, copart.com). Names are real consignors, not examples.

    python3 test/test_copart_seller_01.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analytics" / "scripts"))

import copart_seller  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_logo_filename_suffix_is_stripped(self):
        # "Gmfinancials.jpg" is a real observed seller.name — a leaked asset
        # filename. Without stripping it the lender never matches.
        self.assertEqual(copart_seller.normalize("Gmfinancials.jpg"), "gmfinancials")

    def test_punctuation_and_case_collapse(self):
        self.assertEqual(copart_seller.normalize("Non-insurance Company"),
                         "non insurance company")
        self.assertEqual(copart_seller.normalize("  JPMORGAN  chase  "),
                         "jpmorgan chase")

    def test_empty_inputs(self):
        self.assertEqual(copart_seller.normalize(None), "")
        self.assertEqual(copart_seller.normalize("   "), "")


class RegistryBeatsPublishedTypeTests(unittest.TestCase):
    """The whole reason this module exists: APIBara's seller.type is wrong."""

    CASES = [
        # (name, apibara seller.type, correct class, what the company is)
        ("Csaa", "non_insurance", "insurance", "CSAA Insurance Group, a AAA carrier"),
        ("Csaa", "unknown", "insurance", "same name, APIBara typed it two ways"),
        ("Santander", "non_insurance", "finance", "Santander Consumer USA, lender"),
        ("Bridgecrest Acceptance", "non_insurance", "finance", "Carvana/DriveTime servicer"),
        ("Gmfinancials.jpg", "non_insurance", "finance", "GM Financial, captive lender"),
    ]

    def test_name_wins_over_wrong_published_type(self):
        for name, published, expected, why in self.CASES:
            with self.subTest(name=name, why=why):
                result = copart_seller.classify(name, published)
                self.assertEqual(result["class"], expected)
                self.assertEqual(result["basis"], "registry")
                self.assertEqual(result["published_type"], published)

    def test_agreeing_cases_are_unchanged(self):
        for name, expected in (("Geico", "insurance"), ("Usaa", "insurance"),
                               ("Progressive", "insurance"),
                               ("Aig Insurance", "insurance"),
                               ("Bristol West Insurance", "insurance"),
                               ("Farmers Insurance", "insurance"),
                               ("Flagship Credit Impounds", "finance"),
                               ("Jpmorgan Chase Bank Pip", "finance"),
                               ("Carbrain", "non_insurance")):
            with self.subTest(name=name):
                self.assertEqual(copart_seller.seller_class(name), expected)


class PlaceholderTests(unittest.TestCase):
    def test_insurance_placeholder_keeps_class_and_flags_missing_identity(self):
        insurance = copart_seller.classify("Insurance Company", "insurance")
        self.assertEqual(insurance["class"], "insurance")
        self.assertTrue(insurance["identity_withheld"])
        self.assertEqual(insurance["basis"], "placeholder_name")

    def test_non_insurance_placeholder_is_untrusted(self):
        other = copart_seller.classify("Non-insurance Company", "non_insurance")
        self.assertEqual(other["class"], "unknown")
        self.assertEqual(other["basis"], "untrusted_non_insurance")
        self.assertTrue(other["identity_withheld"])

    def test_literal_unknown_name_is_unknown_and_not_withheld(self):
        result = copart_seller.classify("unknown", "unknown")
        self.assertEqual(result["class"], "unknown")
        self.assertFalse(result["identity_withheld"])

    def test_type_without_name_is_withheld_identity(self):
        result = copart_seller.classify(None, "insurance")
        self.assertEqual(result["class"], "insurance")
        self.assertEqual(result["basis"], "published_type")
        self.assertTrue(result["identity_withheld"])


class PatternTests(unittest.TestCase):
    def test_unregistered_carriers_match_on_insurance_vocabulary(self):
        for name in ("Mapfre Usa Insurance", "American Access Casualty Group",
                     "Some County Mutual", "Acme Indemnity Co"):
            with self.subTest(name=name):
                self.assertEqual(copart_seller.seller_class(name), "insurance")

    def test_insurance_is_checked_before_finance(self):
        # "Liberty Mutual" contains no finance needle, but a name like this one
        # contains both vocabularies; insurance must win.
        self.assertEqual(
            copart_seller.seller_class("Farmers Insurance Capital Group"),
            "insurance")

    def test_unregistered_lenders_match_on_finance_vocabulary(self):
        for name in ("Acme Auto Finance Llc", "Regional Credit Union",
                     "Statewide Impound Recovery", "First National Bank"):
            with self.subTest(name=name):
                self.assertEqual(copart_seller.seller_class(name), "finance")

    def test_dealer_vocabulary(self):
        self.assertEqual(copart_seller.seller_class("Bob's Auto Sales"), "dealer")


class AbsenceTests(unittest.TestCase):
    def test_nothing_published_is_unknown(self):
        result = copart_seller.classify()
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "not_published")
        self.assertFalse(result["identity_withheld"])

    def test_unknown_type_alone_does_not_assert_a_class(self):
        self.assertEqual(copart_seller.classify(None, "unknown")["class"], "unknown")

    def test_non_insurance_type_alone_does_not_assert_a_class(self):
        result = copart_seller.classify(None, "non_insurance")
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "untrusted_non_insurance")

    def test_unrecognised_name_keeps_identity_but_not_an_invented_class(self):
        # The company identity is known, but its business type is not. Calling
        # an unfamiliar insurer non_insurance would be a false negative.
        result = copart_seller.classify("Zzz Holdings Llc")
        self.assertEqual(result["name"], "Zzz Holdings Llc")
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "unrecognized_name")

    def test_unrecognised_name_does_not_inherit_unreliable_published_type(self):
        result = copart_seller.classify("Zzz Holdings Llc", "non_insurance")
        self.assertEqual(result["class"], "unknown")
        self.assertEqual(result["basis"], "unrecognized_name")
        self.assertEqual(result["published_type"], "non_insurance")

    def test_operator_supplied_reference_names(self):
        expected = {
            "Bridgecrest Acceptance": "finance",
            "Carbrain": "non_insurance",
            "Csaa": "insurance",
            "Gmfinancials": "finance",
            "Flagship Credit Impounds": "finance",
            "Jpmorgan Chase Bank Pip": "finance",
            "Aig Insurance": "insurance",
            "Bristol West Insurance": "insurance",
            "Farmers Insurance": "insurance",
            "Geico": "insurance",
            "Insurance Company": "insurance",
            "Progressive": "insurance",
            "Usaa": "insurance",
        }
        for name, seller_class in expected.items():
            with self.subTest(name=name):
                result = copart_seller.classify(name)
                self.assertEqual(result["class"], seller_class)

    def test_every_class_is_declared(self):
        for name, published in (("Geico", None), ("Santander", None),
                                ("Bob's Auto Sales", None), ("Carbrain", None),
                                (None, None)):
            self.assertIn(copart_seller.classify(name, published)["class"],
                          copart_seller.CLASSES)


class RegistryHygieneTests(unittest.TestCase):
    def test_registry_keys_are_already_normalized(self):
        for key in copart_seller.SELLER_REGISTRY:
            self.assertEqual(key, copart_seller.normalize(key),
                             f"registry key {key!r} would never match")

    def test_placeholder_keys_are_already_normalized(self):
        for key in copart_seller.PLACEHOLDER_NAMES:
            self.assertEqual(key, copart_seller.normalize(key))

    def test_registry_classes_are_valid(self):
        for key, value in copart_seller.SELLER_REGISTRY.items():
            self.assertIn(value, copart_seller.CLASSES, key)


if __name__ == "__main__":
    unittest.main(verbosity=2)
