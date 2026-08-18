"""Shared seller taxonomy for the Copart analytics pipeline.

Who consigned the lot is the single most predictive field for a rebuild
decision, and no source publishes it reliably:

  * APIBara has ``seller.type`` on 100% of Copart records, but it is
    demonstrably wrong for named companies.  On the 2018-2023 Audi S5 ended
    cohort (n=290) it typed *Csaa* as ``non_insurance`` (4 lots) and
    ``unknown`` (1 lot) — never ``insurance`` — although CSAA Insurance Group
    is a AAA carrier.  It typed *Santander*, *Bridgecrest Acceptance* and
    *Gmfinancials* as ``non_insurance`` although all three are lenders.
  * Copart's own web search row publishes a seller *name* (``scn``) on about a
    quarter of lots and never publishes a type at all.

So the name is the better evidence when we have it, and this module resolves a
name to a class before it will trust any upstream ``type``.  That is the same
conclusion the IAAI side already reached — see the "seller.type under-reports"
note in analytics/schema/iaai_csv_schema.md.

    from copart_seller import classify
    classify(name="Csaa")                      -> class "insurance"
    classify(name="Flagship Credit Impounds")  -> class "finance"
    classify(published_type="non_insurance")   -> class "non_insurance"
    classify()                                 -> class "unknown"

WHY THE CLASSES ARE DRAWN THIS WAY
----------------------------------
They are not cosmetic buckets; each implies a different damage story:

    insurance      total-loss claim.  Damage is a single recorded event, the
                   car was in retail ownership and maintained, title usually
                   goes salvage.  The core rebuild target.
    finance        repossession or impound by a lender.  Usually mechanically
                   sound with a clean title; "damage" is neglect and storage,
                   not collision.  Priced differently and often a better buy.
    dealer         trade-in or wholesale dross a retailer would not retail.
    non_insurance  a commercial consignor that is neither of the above
                   (fleet, rental, salvage reseller such as CarBrain).
    unknown        Copart published nothing.  Never collapse this into
                   non_insurance: absence of evidence is not evidence.

``identity_withheld`` marks the rows where the class is known but the company
is not — APIBara's literal "Insurance Company" / "Non-insurance Company"
placeholders.  They are usable for class-level analysis and useless for
carrier-level analysis, so they are flagged rather than silently mixed in.
"""
from __future__ import annotations

import re

CLASSES = ("insurance", "finance", "dealer", "non_insurance", "unknown")

# APIBara placeholder names: a class assertion with the identity stripped out.
PLACEHOLDER_NAMES = {
    "insurance company": "insurance",
    "non insurance company": "non_insurance",
    "noninsurance company": "non_insurance",
    "unknown": "unknown",
}

# Curated name -> class.  Keys are normalize() output.  Every entry observed in
# a real pull carries the count and archive it came from, so the table can be
# audited instead of trusted.  Unobserved entries are major US carriers and
# auto lenders added so the first sighting is not a miss.
SELLER_REGISTRY = {
    # --- insurance: observed on the 2018-2023 Audi S5 ended cohort ----------
    "geico": "insurance",                     # 40 lots
    "usaa": "insurance",                      # 32 lots
    "farmers insurance": "insurance",         # 6 lots
    "progressive": "insurance",               # 6 lots
    "bristol west insurance": "insurance",    # 4 lots; Farmers non-standard arm
    "csaa": "insurance",                      # 5 lots; APIBara says non_insurance
    "aig insurance": "insurance",             # 1 lot
    # --- insurance: not yet observed here ----------------------------------
    "state farm": "insurance",
    "allstate": "insurance",
    "nationwide": "insurance",
    "liberty mutual": "insurance",
    "travelers": "insurance",
    "safeco": "insurance",
    "esurance": "insurance",
    "american family": "insurance",
    "auto owners": "insurance",
    "erie": "insurance",
    "mercury": "insurance",
    "kemper": "insurance",
    "mapfre": "insurance",
    "hartford": "insurance",
    "national general": "insurance",
    "plymouth rock": "insurance",
    "root": "insurance",
    "elephant": "insurance",
    "infinity": "insurance",
    "dairyland": "insurance",
    "the general": "insurance",
    "wawanesa": "insurance",
    "amica": "insurance",
    "sentry": "insurance",
    "clearcover": "insurance",
    "hugo": "insurance",
    # --- finance / repossession: observed ----------------------------------
    "flagship credit impounds": "finance",    # 1 lot; Flagship Credit Acceptance
    "jpmorgan chase bank pip": "finance",     # 1 lot
    "bridgecrest acceptance": "finance",      # 2 lots; APIBara says non_insurance
    "gmfinancials": "finance",                # 1 lot, arrived as "Gmfinancials.jpg"
    "santander": "finance",                   # 1 lot; APIBara says non_insurance
    # --- finance / repossession: not yet observed --------------------------
    "ally": "finance",
    "ally financial": "finance",
    "americredit": "finance",
    "capital one": "finance",
    "credit acceptance": "finance",
    "exeter finance": "finance",
    "westlake financial": "finance",
    "world omni": "finance",
    "td auto finance": "finance",
    "regional acceptance": "finance",
    "consumer portfolio services": "finance",
    "united auto credit": "finance",
    "prestige financial": "finance",
    "global lending services": "finance",
    "first investors financial": "finance",
    "wells fargo": "finance",
    "us bank": "finance",
    "pnc bank": "finance",
    # --- dealer / retail ---------------------------------------------------
    "carmax": "dealer",
    "carvana": "dealer",
    "drivetime": "dealer",
    "hertz": "dealer",
    "avis": "dealer",
    "enterprise": "dealer",
    # --- non-insurance commercial consignors -------------------------------
    "carbrain": "non_insurance",              # 3 lots; buys damaged cars retail
    "copart": "non_insurance",
    "peddle": "non_insurance",
    "wheelzy": "non_insurance",
}

# Substring rules for names the registry does not know.  Checked in this order:
# insurance first, because carrier names collide with the finance vocabulary
# ("Liberty Mutual" contains "mutual"; "Bristol West Insurance" would otherwise
# never be reached).  Each tuple is (needle, why-it-is-safe).
INSURANCE_PATTERNS = (
    "insurance", "ins co", "ins. co", "assurance", "casualty", "indemnity",
    "underwriter", "reciprocal", "mutual", "auto club", "insurer",
)
FINANCE_PATTERNS = (
    "financial", "finance", "credit union", "credit acceptance", "acceptance",
    "lending", "lender", "loan", "leasing", "bank", "bancorp", "capital",
    "funding", "impound", "repossession", "recovery services", "fcu",
)
DEALER_PATTERNS = (
    "auto sales", "auto group", "automotive group", "motors", "dealership",
    "car sales", "motor company",
)

# APIBara seller.type -> class.  Consulted only after the name rules, and
# "unknown" is deliberately absent so it falls through rather than asserting.
PUBLISHED_TYPE_MAP = {
    "insurance": "insurance",
    "finance": "finance",
    "dealer": "dealer",
    "non_insurance": "non_insurance",
    "non-insurance": "non_insurance",
    "noninsurance": "non_insurance",
}

# Copart/APIBara occasionally leak a logo filename into the name field —
# "Gmfinancials.jpg" is a real observed value.  Strip it before matching.
_IMAGE_SUFFIX = re.compile(r"\.(?:jpe?g|png|gif|webp|svg)$", re.IGNORECASE)


def normalize(name):
    """'Gmfinancials.jpg' -> 'gmfinancials'; 'Non-insurance Company' -> 'non insurance company'."""
    text = str(name or "").strip()
    if not text:
        return ""
    text = _IMAGE_SUFFIX.sub("", text)
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _pattern_class(key):
    for needle in INSURANCE_PATTERNS:
        if needle in key:
            return "insurance"
    for needle in FINANCE_PATTERNS:
        if needle in key:
            return "finance"
    for needle in DEALER_PATTERNS:
        if needle in key:
            return "dealer"
    return None


def classify(name=None, published_type=None, source=None):
    """Resolve a seller to a class.  Returns a dict, never raises.

    Precedence is evidence-ordered, not source-ordered:

        1. curated registry hit on the name   (beats any published type)
        2. APIBara placeholder name           (class known, identity withheld)
        3. substring patterns on the name
        4. the upstream published type
        5. unknown

    ``basis`` records which rule fired and ``source`` where the raw value came
    from, so every classification in an archive can be re-derived by hand.
    """
    raw_name = str(name or "").strip() or None
    raw_type = str(published_type or "").strip() or None
    key = normalize(raw_name)
    result = {
        "name": raw_name,
        "name_normalized": key or None,
        "published_type": raw_type,
        "class": "unknown",
        "basis": "not_published",
        "source": source,
        "identity_withheld": False,
    }

    if key in SELLER_REGISTRY:
        result.update(**{"class": SELLER_REGISTRY[key], "basis": "registry"})
        return result

    if key in PLACEHOLDER_NAMES:
        result.update(**{
            "class": PLACEHOLDER_NAMES[key],
            "basis": "placeholder_name",
            "identity_withheld": PLACEHOLDER_NAMES[key] != "unknown",
        })
        return result

    if key:
        matched = _pattern_class(key)
        if matched:
            result.update(**{"class": matched, "basis": "name_pattern"})
            return result

    mapped = PUBLISHED_TYPE_MAP.get(re.sub(r"[\s-]+", "_", str(raw_type or "").casefold()))
    if mapped:
        result.update(**{
            "class": mapped,
            "basis": "published_type",
            # A type with no name is a class assertion with no identity.
            "identity_withheld": not key and mapped != "unknown",
        })
        return result

    if key:
        # A real company name we cannot place. It is emphatically not unknown —
        # Copart published an identity — but we decline to guess the class.
        result.update(**{"class": "non_insurance", "basis": "unrecognized_name"})
    return result


def seller_class(name=None, published_type=None):
    """Class string only, for call sites that do not want the audit dict."""
    return classify(name, published_type)["class"]
