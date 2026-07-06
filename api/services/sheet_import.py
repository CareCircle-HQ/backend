"""Shared helpers for the Kitchen-Assignment / member-roster spreadsheet imports.

Centralises the mappings and parsers used by more than one management command
(``import_kitchen_assignments``, ``sync_member_data``, ...): facility -> kitchen,
cadence codes, allergy labels, the single-string delivery-address splitter, and a
tiny stdlib .xlsx reader (no openpyxl/pandas dependency).
"""
import re
import zipfile
from collections import namedtuple
from xml.etree import ElementTree as ET

from api.models import DeliveryCadence, FoodAllergy

# --- value maps ------------------------------------------------------------
# Sheet Facility code (lowercased) -> Kitchen.name.
FACILITY_TO_KITCHEN = {"eng": "ENG", "ast": "AST", "hicksville": "Hicksville"}
# Sheet Cadence code -> meal DeliveryCadence (Boxes handled separately).
CADENCE_TO_DELIVERY = {"a": DeliveryCadence.MON_THU, "b": DeliveryCadence.TUE_FRI}
# Sheet Cadence code -> enrollment.delivery_weekdays codes. Mirrors the canonical
# mapping in api.services.delivery (CADENCE_WEEKDAYS + BOX_DELIVERY_WEEKDAY):
# meals land Mon/Thu or Tue/Fri; boxes ship Wednesdays. The distinct weekday set
# lets a reader recover the cadence (and meals-vs-boxes) from the DB alone.
CADENCE_TO_WEEKDAYS = {
    "a": ["mon", "thu"],
    "b": ["tue", "fri"],
    "boxes": ["wed"],
}
# Allergy label (lowercased) -> FoodAllergy code, built from the model choices.
ALLERGY_BY_LABEL = {label.lower(): code for code, label in FoodAllergy.choices}
ALLERGY_BY_LABEL["others"] = "other"  # sheet uses the plural

BLANKS = {"", "#n/a", "n/a", "none"}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR",
}
# Trailing "<STATE> <ZIP>" of an address string.
_TAIL_RE = re.compile(r",?\s*([A-Za-z]{2})\.?\s+(\d{5})(?:-\d{4})?\s*$")
# An apartment / unit designator embedded in the street line.
_UNIT_RE = re.compile(
    r"\b(?:APT|APARTMENT|UNIT|STE|SUITE|FL|FLOOR|RM|ROOM|BLDG|#)\s*\.?\s*([A-Za-z0-9\-]+)\b",
    re.I,
)

ParsedAddress = namedtuple("ParsedAddress", "street unit city state zip")


# --- scalar cleaners -------------------------------------------------------
def clean(value):
    """Trim whitespace/quotes and normalise placeholder blanks ('#N/A', ...)."""
    v = (value or "").strip().strip('"').strip("\u201c\u201d").strip()
    return "" if v.lower() in BLANKS else v


def state_from_zip(zip_):
    """NY is authoritative from the ZIP (10001-14975) -- the sheets are full of
    "Ne" typos for NY that would otherwise parse as Nebraska."""
    try:
        n = int((zip_ or "")[:5])
    except ValueError:
        return ""
    return "NY" if 10001 <= n <= 14975 else ""


def parse_address(raw):
    """Split "476 EAST NEW YORK AVENUE APT 1, BROOKLYN, NY 11225" into a
    :class:`ParsedAddress`. Best-effort: any component may be blank when the
    source string is malformed."""
    a = clean(raw)
    state = zip_ = ""
    m = _TAIL_RE.search(a)
    if m:
        zip_ = m.group(2)
        state = state_from_zip(zip_) or (
            m.group(1).upper() if m.group(1).upper() in _US_STATES else m.group(1).upper()
        )
        a = a[: m.start()].strip().rstrip(",")
    parts = [p.strip() for p in a.split(",") if p.strip()]
    street = parts[0] if parts else ""
    city = parts[-1] if len(parts) > 1 else ""
    unit = ""
    um = _UNIT_RE.search(street)
    if um:
        unit = um.group(1)
        street = (street[: um.start()] + street[um.end():]).strip().rstrip(",").strip()
    return ParsedAddress(street, unit, city.title(), state, zip_)


def parse_allergies(raw):
    """Return ``(codes, unknown_labels)`` from a ';'-separated allergy string."""
    codes, unknown = [], []
    for tok in re.split(r"[;,]", raw or ""):
        tok = tok.strip().strip('"')
        low = tok.lower()
        if low in BLANKS:
            continue
        code = ALLERGY_BY_LABEL.get(low)
        if code and code != "none":
            codes.append(code)
        elif not code:
            unknown.append(tok)
    return list(dict.fromkeys(codes)), unknown


def resolve_kitchen(kitchens_by_norm, facility):
    """Map a sheet Facility value to a Kitchen using a case/whitespace-insensitive
    lookup. ``kitchens_by_norm`` is ``{kitchen.name.strip().lower(): Kitchen}``.
    Returns ``(kitchen_or_None, normalized_target_name)``."""
    fac = clean(facility).lower()
    target = FACILITY_TO_KITCHEN.get(fac, fac).strip().lower()
    return kitchens_by_norm.get(target), target


# --- .xlsx reader (pure stdlib) --------------------------------------------
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col(ref):
    return re.match(r"[A-Z]+", ref).group(0)


def read_xlsx(path):
    """Parse every worksheet of an .xlsx. Returns a list of
    ``(sheet_name, rows)`` where each row is ``{column_letter: value}`` (the
    header row is included as the first row). Pure stdlib -- no dependency.

    Reading by COLUMN LETTER (not header label) is deliberate: these sheets have
    DUPLICATE header labels (two "cadence" columns), so label-keyed access would
    silently drop one.
    """
    try:
        z = zipfile.ZipFile(path)
    except (FileNotFoundError, zipfile.BadZipFile) as exc:
        raise ValueError(f"Cannot open workbook {path!r}: {exc}")
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in tree.findall(_NS + "si"):
            shared.append("".join(n.text or "" for n in si.iter(_NS + "t")))
    sheets = sorted(
        n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", n)
    )
    out = []
    for sh in sheets:
        ws = ET.fromstring(z.read(sh))
        rows = []
        for r in ws.iter(_NS + "row"):
            cells = {}
            for c in r.findall(_NS + "c"):
                v = c.find(_NS + "v")
                if v is None:
                    continue
                val = shared[int(v.text)] if c.get("t") == "s" else v.text
                cells[_col(c.get("r"))] = (val or "").strip()
            if cells:
                rows.append(cells)
        out.append((sh, rows))
    return out
