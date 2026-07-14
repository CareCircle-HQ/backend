"""Delivery-address format detection.

Single source of truth for spotting badly-formatted delivery addresses so the
Customer Service -> Delivery Address page can surface only the ones that need a
human fix. Read-only heuristics; no writes.

The headline check is a UNIT embedded in the street line (``123 Main St Apt 4``)
instead of the separate unit field. A few other clear format problems are
flagged alongside it. Detection favours PRECISION over recall: we only flag
explicit markers (``Apt``, ``Suite``, ``#3`` ...) so a legitimate street name
with a unit-like word isn't over-flagged.
"""

import re

# --- Stable issue codes ----------------------------------------------------
UNIT_IN_STREET = "unit_in_street"
DUPLICATE_UNIT = "duplicate_unit"
PO_BOX = "po_box"
MISSING_STREET = "missing_street"
MISSING_CITY = "missing_city"
MISSING_STATE = "missing_state"

ISSUE_LABELS = {
    UNIT_IN_STREET: "Unit in street line",
    DUPLICATE_UNIT: "Unit in both street & unit field",
    PO_BOX: "PO Box",
    MISSING_STREET: "Missing street",
    MISSING_CITY: "Missing city",
    MISSING_STATE: "Missing / invalid state",
}

# Explicit unit markers. Word-boundaried and specific so real street names
# ("Flatbush Ave", "Lott Pl") don't match. A trailing bare number/letter is
# intentionally NOT treated as a unit -- too ambiguous (false positives).
_UNIT_KEYWORDS = re.compile(
    r"\b("
    r"apt|apartment|unit|suite|ste|fl|flr|floor|rm|room|"
    r"bldg|building|trlr|trailer|lot|dept|department|"
    r"basement|bsmt|penthouse|ph"
    r")\b\.?",
    re.IGNORECASE,
)
# A '#' followed by an alphanumeric unit token (e.g. "#3", "# 4B").
_HASH_UNIT = re.compile(r"#\s*\w+")
# PO Box in various spellings: "PO Box", "P.O. Box", "P O Box".
_PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.IGNORECASE)


def _has_unit_marker(text):
    text = text or ""
    return bool(_UNIT_KEYWORDS.search(text) or _HASH_UNIT.search(text))


def detect_address_issues(street="", unit="", city="", state="", zip_code=""):
    """Return the list of issue codes for a delivery address (empty when clean).

    ``zip_code`` is accepted for completeness but ZIP normalization is handled
    by ``normalize_zip_codes``, so it is not re-flagged here.
    """
    street = (street or "").strip()
    unit = (unit or "").strip()
    city = (city or "").strip()
    state = (state or "").strip()

    issues = []

    if _PO_BOX.search(street):
        issues.append(PO_BOX)

    if _has_unit_marker(street):
        # A unit lives in the street line. If the unit field is ALSO populated
        # it's a duplicate (still needs cleaning); otherwise it belongs in the
        # unit field.
        issues.append(DUPLICATE_UNIT if unit else UNIT_IN_STREET)

    if not street:
        issues.append(MISSING_STREET)
    if not city:
        issues.append(MISSING_CITY)
    if len(state) != 2 or not state.isalpha():
        issues.append(MISSING_STATE)

    return issues


def issue_label(code):
    return ISSUE_LABELS.get(code, code)
