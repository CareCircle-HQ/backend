"""Mapping of GoHighLevel contact custom fields to ``Client`` data.

Each entry maps a GHL custom field id to a resolver that returns the value for a
given client (or ``None``/"" to skip). Add fields here incrementally -- the ids
come from ``python manage.py ghl_fields`` (raw dump in ghl_custom_fields.json).

Kept separate from contacts.py so the field catalog is easy to read, extend, and
remove when the external CRM is retired.
"""

# GHL custom field ids (location: HwYldKwhYiZywGFkXr0y).
FIELD_ENROLLMENT_CLIENT_ID = "iWw4cIFFBCFcKGlUeffm"  # contact.enrollment_client_id
FIELD_TOTAL_HOUSEHOLD_MEMBERS = "7Y8aipXfNywpogNFsEk0"  # contact.total_household_members


def _enrollment_client_id(client):
    return str(client.pk)


def _total_household_members(client):
    return client.total_family_members or client.household_size


# (field_id, resolver) pairs. Only non-empty resolved values are sent.
# Extend this list as we confirm each field's mapping/options.
CONTACT_FIELD_RESOLVERS = [
    (FIELD_ENROLLMENT_CLIENT_ID, _enrollment_client_id),
    (FIELD_TOTAL_HOUSEHOLD_MEMBERS, _total_household_members),
]


def build_custom_fields(client):
    """Return the GHL ``customFields`` array for a client (skips empties)."""
    out = []
    for field_id, resolver in CONTACT_FIELD_RESOLVERS:
        try:
            value = resolver(client)
        except Exception:
            value = None
        if value not in (None, ""):
            out.append({"id": field_id, "field_value": value})
    return out
