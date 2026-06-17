"""Custom model fields.

``EncryptedTextField`` transparently encrypts its value at rest using Fernet
(symmetric AES). The ciphertext is stored in a normal TEXT column; values are
encrypted on write and decrypted on read. The key comes from
``settings.FIELD_ENCRYPTION_KEY`` and is only required at runtime — migrations
do not need it (the field deconstructs to a plain TextField definition).
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

# Marks ciphertext we produced so we can distinguish it from legacy plaintext
# and avoid double-decrypting values that were never encrypted.
_PREFIX = "enc::"


def _get_fernet():
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY is not set; cannot read/write encrypted fields. "
            "Generate one with: python -c \"from cryptography.fernet import "
            "Fernet; print(Fernet.generate_key().decode())\""
        )
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)


class EncryptedTextField(models.TextField):
    """A TextField whose value is Fernet-encrypted in the database."""

    def from_db_value(self, value, expression, connection):
        if value is None:
            return value
        if not value.startswith(_PREFIX):
            # Legacy/plaintext value written before encryption; return as-is.
            return value
        token = value[len(_PREFIX):].encode()
        try:
            return _get_fernet().decrypt(token).decode()
        except InvalidToken:
            # Wrong/rotated key — surface clearly rather than silently corrupt.
            raise ImproperlyConfigured(
                "Failed to decrypt an EncryptedTextField; FIELD_ENCRYPTION_KEY "
                "may be wrong or rotated."
            )

    def get_prep_value(self, value):
        if value is None:
            return value
        value = str(value)
        if value == "":
            return value
        if value.startswith(_PREFIX):
            # Already encrypted (e.g. re-saving a freshly-read value path).
            return value
        token = _get_fernet().encrypt(value.encode()).decode()
        return f"{_PREFIX}{token}"
