"""Which price, and which fee, apply to a given vendor.

ONE function decides this. The alternative -- resolving it at each call site -- is
how an invoice ends up disagreeing with the screen that produced it.

Two independent fallbacks, and neither stores a copy of what it falls back to:

    price   a VendorPrice row if this vendor has one, else the base
            BillableItem.vendor_price
    fee     Vendor.admin_fee_percent if set, else
            BillingSettings.admin_fee_percent

NULL meaning "inherit" rather than 0 is the point. A vendor nobody has negotiated
with follows the house rate, and changing that rate moves them with it; a stored
copy would leave every vendor stale the day it changed.

INVOICES MUST NOT READ THIS AT RENDER TIME. A bill already sent must not move when
a price is renegotiated, so an invoice line stores the price and fee it used. This
module is what the NEXT invoice is built from, not what an existing one is
displayed from.
"""
from decimal import ROUND_HALF_UP, Decimal

from ..models import BillableItem, BillingSettings, VendorPrice

CENTS = Decimal("0.01")


def default_fee_percent():
    return BillingSettings.get().admin_fee_percent


def fee_percent_for(vendor):
    """This vendor's mark-up, falling back to the house rate."""
    if vendor is not None and vendor.admin_fee_percent is not None:
        return vendor.admin_fee_percent
    return default_fee_percent()


def admin_fee(price, percent):
    """Half-up to the cent, matching the pricing sheet: 456.75 x 10% = 45.675, and
    the sheet shows 45.68. Truncating would leave every odd price a cent light."""
    return (Decimal(price) * (Decimal(percent) / Decimal("100"))).quantize(
        CENTS, rounding=ROUND_HALF_UP,
    )


def price_list_for(vendor, *, active_only=True):
    """Every billable item, resolved for this vendor.

    Returns dicts rather than model instances because each row is a MIX of the base
    item, this vendor's override and a derived total -- there is no single object
    that holds it, and inventing one would imply a stored row that does not exist.

    ``is_custom`` is what the UI needs in order to show which prices were actually
    negotiated, as opposed to inherited.
    """
    items = BillableItem.objects.all()
    if active_only:
        items = items.filter(is_active=True)
    items = list(items)

    overrides = {}
    if vendor is not None:
        overrides = {
            vp.billable_item_id: vp
            for vp in VendorPrice.objects.filter(
                vendor=vendor, billable_item__in=items,
            )
        }

    percent = fee_percent_for(vendor)
    inherits_fee = vendor is None or vendor.admin_fee_percent is None

    rows = []
    for item in items:
        override = overrides.get(item.pk)
        price = override.price if override else item.vendor_price
        fee = admin_fee(price, percent)
        rows.append({
            "billable_item_id": str(item.pk),
            "item": item.item,
            "option_code": item.option_code,
            "billing_category": item.billing_category,
            "main_category": item.main_category,
            "hcpcs_code": item.hcpcs_code,
            "modifiers": item.modifiers,
            # Both, so the UI can show what was negotiated AGAINST the base rather
            # than only the number in force.
            "base_price": str(item.vendor_price),
            "price": str(price),
            "is_custom": override is not None,
            "note": override.note if override else "",
            "admin_fee_percent": str(percent),
            "fee_is_inherited": inherits_fee,
            "admin_fee": str(fee),
            "billed_price": str(price + fee),
        })
    return rows


def resolved_price(vendor, option_code):
    """``(price, fee_percent, billed)`` for one intervention option, or None.

    Keyed on ``option_code`` because that is what an assessment RECOMMENDS -- the
    join that makes a recommended intervention billable. Returns None when the
    option has no price row at all, which the caller has to handle rather than
    silently bill zero.
    """
    item = BillableItem.objects.filter(
        option_code=option_code, is_active=True,
    ).first()
    if item is None:
        return None
    override = VendorPrice.objects.filter(
        vendor=vendor, billable_item=item,
    ).first() if vendor is not None else None
    price = override.price if override else item.vendor_price
    percent = fee_percent_for(vendor)
    fee = admin_fee(price, percent)
    return price, percent, price + fee
