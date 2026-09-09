"""Delivery-partner API: the ONLY surface exposed to delivery companies.

Served on its own hostname (``settings.PARTNER_API_HOST``) via
``api.partner.urls``, which the main site never includes -- so CRM routes do not
exist on the partner host and partner routes do not exist on the CRM host.

See docs/delivery_partner_api_plan.md.
"""
