"""Mailgun transactional email integration (agent login 2FA codes)."""

from .client import MailgunError, send_email

__all__ = ["MailgunError", "send_email"]
