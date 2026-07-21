"""Served-states allow-list.

We only take clients/cases from certain US states. The allowed states live in
the admin-editable :class:`AllowedState` table (Settings > Allowed States); a
state's PRESENCE means it is enabled. A client whose PRIMARY-address state is
not enabled triggers a (non-blocking) warning on the Verification modal and in
the extension. Matching is on the 2-letter USPS code, case-insensitive.

The canonical 50 states + DC list lives here so the check endpoint and the
Settings page can share one source of truth.
"""

# USPS code -> full name. 50 states + DC.
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


def normalize_state(value):
    """The 2-letter USPS code for a raw state cell, upper-cased. Accepts a code
    ("ny") or a full name ("New York"); returns "" when it can't be resolved."""
    s = (value or "").strip()
    if not s:
        return ""
    up = s.upper()
    if len(up) == 2 and up in US_STATES:
        return up
    # Match a full name (case-insensitive).
    for code, name in US_STATES.items():
        if name.upper() == up:
            return code
    return ""


def allowed_state_codes():
    """The set of enabled 2-letter state codes (empty when none configured)."""
    from api.models import AllowedState

    return {s.code.upper() for s in AllowedState.objects.all()}


def is_state_allowed(state_value, *, allowed=None):
    """True when ``state_value`` (raw code or name) is an enabled state.

    When no states are configured at all, treats every state as allowed so the
    feature stays inert until an admin opts in (never blocks on an empty list).
    """
    if allowed is None:
        allowed = allowed_state_codes()
    if not allowed:
        return True
    code = normalize_state(state_value)
    if not code:
        # Unknown/blank state can't be judged; don't warn.
        return True
    return code in allowed
