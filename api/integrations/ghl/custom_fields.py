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

# Doctor/PCP fields
FIELD_DOCTORS_NAME = "VDp9dccvMPl8Yood6e9O"
FIELD_DOCTORS_STREET_ADDRESS = "Yvfn5jNSITA7oDZ9qc0G"
FIELD_DOCTORS_PHONE = "XtwIwYRKfgaTe88T92wB"
FIELD_DOCTORS_FAX = "XJyU9CjxrH5dID7vWtcC"
FIELD_DOCTORS_EMAIL = "ViDnbjtmh5VhDJHby2hW"


def _enrollment_client_id(client):
    return str(client.pk)


def _total_household_members(client):
    return client.total_family_members or client.household_size


def _doctors_name(client):
    return client.doctors_name


def _doctors_street_address(client):
    return client.doctors_street_address


def _doctors_phone(client):
    return client.doctors_phone


def _doctors_fax(client):
    return client.doctors_fax


def _doctors_email(client):
    return client.doctors_email


# (field_id, resolver) pairs. Only non-empty resolved values are sent.
# Extend this list as we confirm each field's mapping/options.
CONTACT_FIELD_RESOLVERS = [
    (FIELD_ENROLLMENT_CLIENT_ID, _enrollment_client_id),
    (FIELD_TOTAL_HOUSEHOLD_MEMBERS, _total_household_members),
    (FIELD_DOCTORS_NAME, _doctors_name),
    (FIELD_DOCTORS_STREET_ADDRESS, _doctors_street_address),
    (FIELD_DOCTORS_PHONE, _doctors_phone),
    (FIELD_DOCTORS_FAX, _doctors_fax),
    (FIELD_DOCTORS_EMAIL, _doctors_email),
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
