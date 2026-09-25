"""The three documents produced when a vendor submits a Dwelling Assessment.

    invoice     what the vendor bills US for the assessment visit itself
    quote       every product they recommended, signed by the member and by them
    assessment  the questionnaire -- questions, answers, signatures, photographs

VENDOR PRICES ONLY, IN ALL THREE. Not one of these documents may mention the admin
fee, the billed total, or anything else about what we charge Unite Us. The vendor
receives the invoice and the quote, and the member can be shown the assessment, so
our margin appearing on any of them would be handing out our commercial position.
That is also why the money here is computed from ``pricing.price_list_for`` --
the vendor's negotiated price or the base -- and never from ``billed_price``.

Generated ONCE at submission and stored, never rendered on demand. These are
records of what was attested at a point in time: re-rendering later would quietly
restate them with today's prices and today's template, which is precisely what a
signed document must not do.
"""
import logging
from decimal import Decimal
from io import BytesIO

from django.utils import timezone

logger = logging.getLogger(__name__)

# doc_type values on DispatchDocument. Stable strings: the CRM filters on them and
# they appear in stored rows, so they are not display labels.
DOC_INVOICE = "assessment_invoice"
DOC_QUOTE = "recommendation_quote"
DOC_ASSESSMENT = "assessment_report"

DOC_LABELS = {
    DOC_INVOICE: "Assessment Invoice",
    DOC_QUOTE: "Recommendation Quote",
    DOC_ASSESSMENT: "Assessment Report",
}

_TEAL = "#0f766e"
_GREY = "#6b7280"
_RULE = "#e5e7eb"


# ── shared pieces ────────────────────────────────────────────────────────────

def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1", parent=base["Heading1"], fontSize=16, leading=20,
            textColor=colors.HexColor(_TEAL), spaceAfter=2,
        ),
        "h2": ParagraphStyle(
            "h2", parent=base["Heading2"], fontSize=11, leading=14,
            textColor=colors.HexColor(_TEAL), spaceBefore=12, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "body", parent=base["BodyText"], fontSize=9, leading=12,
        ),
        "small": ParagraphStyle(
            "small", parent=base["BodyText"], fontSize=8, leading=10,
            textColor=colors.HexColor(_GREY),
        ),
    }


def _logo_flowable(vendor, max_width_inch=1.6):
    """The vendor's logo as a reportlab Image, or None.

    Sized from the STORED dimensions so the aspect ratio is right without decoding
    the file twice. Returns None on any failure: a missing logo must never be the
    reason an invoice cannot be produced.
    """
    from reportlab.lib.units import inch
    from reportlab.platypus import Image

    if not vendor or not vendor.logo_s3_key:
        return None
    try:
        from . import import_storage

        # read_bytes, NOT download_to_temp: the latter returns an OPEN FILE the
        # caller must close and unlink, and names it ".csv". read_bytes exists for
        # exactly this -- "objects known to be small (single images)".
        raw, _content_type = import_storage.read_bytes(vendor.logo_s3_key)
        width = vendor.logo_width or 600
        height = vendor.logo_height or 600
        scale = min((max_width_inch * inch) / width, (0.7 * inch) / height)
        return Image(BytesIO(raw), width=width * scale, height=height * scale)
    except Exception:  # noqa: BLE001 - a logo is decoration, not a dependency
        logger.warning(
            "dispatch_pdf: logo unavailable for vendor %s", getattr(vendor, "pk", "?"),
        )
        return None


def _letterhead(vendor, styles, title, subtitle=""):
    """The vendor's identity at the top of every document.

    THEIR letterhead, not ours: they issue the invoice and the quote, and the
    assessment is their professional work. A CareCircle logo here would imply we
    carried out the visit.
    """
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    lines = [f"<b>{vendor.name}</b>" if vendor else "<b>Vendor</b>"]
    for value in (
        getattr(vendor, "address", ""),
        getattr(vendor, "contact_phone", ""),
        getattr(vendor, "contact_email", ""),
        getattr(vendor, "website", ""),
    ):
        if value:
            lines.append(value)
    identity = Paragraph("<br/>".join(lines), styles["body"])

    heading = [Paragraph(title, styles["h1"])]
    if subtitle:
        heading.append(Paragraph(subtitle, styles["small"]))

    logo = _logo_flowable(vendor)
    left = logo if logo is not None else identity
    right = heading

    table = Table(
        [[left, right]], colWidths=[3.2 * inch, 3.3 * inch],
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.75, colors.HexColor(_TEAL)),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    out = [table]
    # With a logo, the address still has to appear -- it is an invoice.
    if logo is not None:
        out.append(identity)
    return out


def _decode_signature(data_url_or_bytes):
    """A signature image as a reportlab flowable, or None."""
    import base64

    from reportlab.lib.units import inch
    from reportlab.platypus import Image

    raw = data_url_or_bytes
    try:
        if isinstance(raw, str):
            if "," in raw:
                raw = raw.split(",", 1)[1]
            raw = base64.b64decode(raw)
        if not raw:
            return None
        return Image(BytesIO(raw), width=1.8 * inch, height=0.55 * inch, kind="bound")
    except Exception:  # noqa: BLE001
        return None


def _signature_block(signatures, styles):
    """Member and vendor signatures side by side.

    A signature line is drawn even when the image is missing, with the name and
    timestamp beneath: the FACT of the attestation is recorded in the database, and
    a blank space would read as "nobody signed" rather than "the image did not
    load".
    """
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    cells, captions = [], []
    for role, label in (("member", "Member signature"), ("vendor", "Assessor signature")):
        sig = signatures.get(role)
        image = _decode_signature(sig.get("image")) if sig else None
        cells.append(image if image is not None else Paragraph("&nbsp;", styles["body"]))
        if sig:
            when = sig.get("signed_at")
            stamp = when.strftime("%d %b %Y, %H:%M") if when else ""
            captions.append(Paragraph(
                f"<b>{label}</b><br/>{sig.get('name') or ''}<br/>{stamp}",
                styles["small"],
            ))
        else:
            captions.append(Paragraph(f"<b>{label}</b><br/>not signed", styles["small"]))

    table = Table([cells, captions], colWidths=[3.25 * inch, 3.25 * inch])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.HexColor("#111827")),
        ("TOPPADDING", (0, 1), (-1, 1), 4),
    ]))
    return table


def _money(value):
    return f"${Decimal(value):,.2f}"


def _doc(buf, title):
    """A document template that renders the same INPUT to the same BYTES.

    ``invariant`` stops reportlab stamping a creation timestamp and a random
    document id into the file. Without it, rendering the same assessment twice
    produces two different files, which quietly defeated the content-hash
    de-duplication in generate_submission_documents -- a resubmission left two
    identical invoices on the order. Found by the test for exactly that.

    It also makes a document reproducible, which is worth having on its own: "is
    this the file that was signed?" becomes a question you can answer by
    re-rendering and comparing.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate

    return SimpleDocTemplate(
        buf, pagesize=letter, title=title, invariant=1,
        topMargin=0.6 * inch, bottomMargin=0.6 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )


def _line_table(rows, styles, *, total_label="Total"):
    """A priced table: description, qty, unit, amount. VENDOR PRICES ONLY."""
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[
        Paragraph("<b>Description</b>", styles["body"]),
        Paragraph("<b>Qty</b>", styles["body"]),
        Paragraph("<b>Unit</b>", styles["body"]),
        Paragraph("<b>Amount</b>", styles["body"]),
    ]]
    total = Decimal("0")
    for row in rows:
        amount = Decimal(row["unit"]) * row["qty"]
        total += amount
        data.append([
            Paragraph(row["description"], styles["body"]),
            Paragraph(str(row["qty"]), styles["body"]),
            Paragraph(_money(row["unit"]), styles["body"]),
            Paragraph(_money(amount), styles["body"]),
        ])
    data.append([
        Paragraph(f"<b>{total_label}</b>", styles["body"]), "", "",
        Paragraph(f"<b>{_money(total)}</b>", styles["body"]),
    ])

    table = Table(
        data, colWidths=[3.7 * inch, 0.6 * inch, 1.0 * inch, 1.2 * inch],
        repeatRows=1,
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor(_TEAL)),
        ("LINEBELOW", (0, 1), (-1, -2), 0.25, colors.HexColor(_RULE)),
        ("LINEABOVE", (0, -1), (-1, -1), 0.75, colors.HexColor("#111827")),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return table, total


# ── context gathered once, shared by all three ───────────────────────────────

def _context(order):
    """Everything the three documents draw from, resolved once.

    Prices come from ``pricing.price_list_for(vendor)`` -- the vendor's negotiated
    price where one exists, otherwise the base. NEVER ``billed_price``: see the
    module docstring.
    """
    from . import dispatch as dispatch_svc
    from . import pricing

    vendor = order.vendor
    client = order.client
    form = getattr(order, "questionnaire", None)

    prices = {
        r["option_code"]: {"price": Decimal(r["price"]), "item": r["item"]}
        for r in pricing.price_list_for(vendor) if r["option_code"]
    } if vendor else {}

    recommended = []
    for entry in (form.interventions if form else []) or []:
        code = (entry or {}).get("option")
        qty = int((entry or {}).get("qty") or 0)
        row = prices.get(code)
        if not code or qty <= 0 or row is None:
            continue
        recommended.append({
            "description": row["item"], "qty": qty, "unit": row["price"],
        })
    recommended.sort(key=lambda r: r["description"])

    submission = dispatch_svc.active_submission(order)
    signatures = {}
    if submission is not None:
        for sig in submission.signatures.all():
            image = b""
            if sig.s3_key:
                try:
                    from . import import_storage

                    image, _ct = import_storage.read_bytes(sig.s3_key)
                except Exception:  # noqa: BLE001 - the FACT of signing still counts
                    logger.warning("dispatch_pdf: signature unavailable %s", sig.s3_key)
            signatures[sig.signer_role] = {
                "name": sig.signer_name, "signed_at": sig.signed_at, "image": image,
            }
    return {
        "vendor": vendor,
        "client": client,
        "member_name": f"{client.first_name} {client.last_name}".strip(),
        "form": form,
        "order": order,
        "recommended": recommended,
        "assessment_price": pricing.assessment_price(vendor),
        "signatures": signatures,
        "case_id": str(order.case.case_id) if order.case_id else "",
        "address": order.service_address,
        "submitted_at": (form.submitted_at if form else None) or timezone.now(),
    }


def _member_block(ctx, styles, *, heading="Member"):
    from reportlab.platypus import Paragraph

    lines = [f"<b>{heading}:</b> {ctx['member_name'] or '—'}"]
    if ctx["address"]:
        lines.append(f"<b>Address:</b> {ctx['address']}")
    if ctx["case_id"]:
        lines.append(f"<b>Dwelling case:</b> {ctx['case_id']}")
    lines.append(f"<b>Assessment date:</b> {ctx['submitted_at']:%d %b %Y}")
    return Paragraph("<br/>".join(lines), styles["body"])


# ── 1. the invoice: the assessment visit only ────────────────────────────────

def render_invoice(order):
    """What the vendor bills us for the ASSESSMENT ITSELF -- one line, no products.

    The recommended items are not invoiced here and must not appear: they have not
    been authorised, ordered or installed yet. Putting them on an invoice would be
    billing for work nobody has approved.
    """
    from reportlab.platypus import Paragraph, Spacer

    ctx = _context(order)
    styles = _styles()
    buf = BytesIO()
    doc = _doc(buf, "Assessment Invoice")

    story = _letterhead(
        ctx["vendor"], styles, "INVOICE",
        f"Environmental Exposure Assessment · {ctx['submitted_at']:%d %b %Y}",
    )
    story += [Spacer(1, 10), _member_block(ctx, styles, heading="Assessment for"),
              Spacer(1, 12)]

    table, _total = _line_table(
        [{
            "description": "Environmental Exposure Assessment — dwelling "
                           "assessment and Statement of Work",
            "qty": 1,
            "unit": ctx["assessment_price"],
        }],
        styles, total_label="Total due",
    )
    story += [table, Spacer(1, 14), Paragraph(
        "This invoice covers the assessment visit only. Recommended interventions "
        "are quoted separately and are not billable until authorised.",
        styles["small"],
    )]
    doc.build(story)
    return buf.getvalue()


# ── 2. the quote: everything recommended, signed ─────────────────────────────

def render_quote(order):
    """The recommended interventions, at the vendor's prices, signed by both.

    Signed by the MEMBER as well as the assessor because it records what they were
    told would be requested on their behalf -- which is the thing most likely to be
    disputed later.
    """
    from reportlab.platypus import Paragraph, Spacer

    ctx = _context(order)
    styles = _styles()
    buf = BytesIO()
    doc = _doc(buf, "Recommendation Quote")

    story = _letterhead(
        ctx["vendor"], styles, "QUOTE",
        f"Recommended interventions · {ctx['submitted_at']:%d %b %Y}",
    )
    story += [Spacer(1, 10), _member_block(ctx, styles, heading="Prepared for"),
              Spacer(1, 12)]

    if ctx["recommended"]:
        table, _total = _line_table(
            ctx["recommended"], styles, total_label="Quote total",
        )
        story.append(table)
    else:
        # Said plainly. A quote with an empty table and a $0.00 total looks like a
        # rendering fault rather than a finding.
        story.append(Paragraph(
            "<b>No interventions were recommended for this dwelling.</b>",
            styles["body"],
        ))

    story += [
        Spacer(1, 12),
        Paragraph(
            "Quantities and prices are this vendor's. Interventions are subject to "
            "authorisation before any work is carried out.",
            styles["small"],
        ),
        Spacer(1, 26),
        _signature_block(ctx["signatures"], styles),
    ]
    doc.build(story)
    return buf.getvalue()


# ── 3. the assessment report: questions, answers, signatures, photographs ────

def render_assessment_report(order):
    """The questionnaire as completed, then every photograph, last.

    Rendered from the form's FROZEN SNAPSHOT, not the live template, so the report
    shows the questions that were actually asked. A form signed against version 1
    must not acquire version 2's wording.

    Photographs go at the END, one per page, captioned with the section they
    evidence -- interleaving them with the answers would push the questionnaire
    across a dozen pages and make it unreadable as a document.
    """
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, PageBreak, Paragraph, Spacer

    ctx = _context(order)
    styles = _styles()
    form = ctx["form"]
    buf = BytesIO()
    doc = _doc(buf, "Assessment Report")

    story = _letterhead(
        ctx["vendor"], styles, "ASSESSMENT REPORT",
        f"Environmental Exposure Assessment · {ctx['submitted_at']:%d %b %Y}",
    )
    story += [Spacer(1, 10), _member_block(ctx, styles), Spacer(1, 6)]

    schema = (form.schema() if form else {}) or {}
    answers = (form.answers if form else {}) or {}
    others = (form.section_other if form else {}) or {}

    if schema.get("label"):
        story.append(Paragraph(
            f"<b>Form:</b> {schema['label']} ({schema.get('service_code', '')})",
            styles["small"],
        ))

    for section in schema.get("sections", []):
        story.append(Paragraph(section["title"], styles["h2"]))
        for group in section["groups"]:
            if group.get("label"):
                story.append(Paragraph(f"<b>{group['label']}</b>", styles["small"]))
            for question in group["questions"]:
                # A tick or a dash, so an UNANSWERED question is visibly unanswered
                # rather than absent. A report that silently omits what was not
                # found cannot be checked against the form.
                mark = "&#10003;" if answers.get(question["code"]) else "&ndash;"
                story.append(Paragraph(
                    f"{mark}&nbsp;&nbsp;{question['label']}", styles["body"],
                ))
        if section.get("allows_other") and others.get(section["code"]):
            story.append(Paragraph(
                f"<b>Other:</b> {others[section['code']]}", styles["body"],
            ))

    if ctx["recommended"]:
        story.append(Paragraph("Recommended Interventions", styles["h2"]))
        table, _total = _line_table(
            ctx["recommended"], styles, total_label="Total",
        )
        story.append(table)

    for label, value in (
        ("Clinical / Safety Justification", getattr(form, "justification", "")),
        ("Assessor Notes", getattr(form, "assessor_notes", "")),
    ):
        story.append(Paragraph(label, styles["h2"]))
        story.append(Paragraph(value or "—", styles["body"]))

    story += [Spacer(1, 24), _signature_block(ctx["signatures"], styles)]

    # ── the photographs ──────────────────────────────────────────────────────
    proofs = list(order.proofs.all().order_by("received_at"))
    if proofs:
        story.append(PageBreak())
        story.append(Paragraph("Photographs", styles["h2"]))
        section_labels = _photo_section_labels(schema)
        for index, proof in enumerate(proofs, 1):
            caption = section_labels.get(proof.intervention_group) or "Dwelling"
            story.append(Paragraph(
                f"<b>{index}. {caption}</b>"
                + (f" — {proof.caption}" if proof.caption else ""),
                styles["body"],
            ))
            image = _proof_flowable(proof)
            if image is not None:
                story.append(image)
            else:
                story.append(Paragraph(
                    "(image unavailable)", styles["small"],
                ))
            story.append(Spacer(1, 10))
            if index < len(proofs):
                story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()


def _photo_section_labels(schema):
    """Question-group code -> its heading, for captioning a photograph."""
    out = {}
    for section in schema.get("sections", []):
        for group in section["groups"]:
            out[group["code"]] = group.get("label") or section["title"]
    return out


def _proof_flowable(proof, *, max_width_inch=6.0, max_height_inch=7.0):
    """A photograph scaled to fit the page, or None.

    Scaled from its own dimensions rather than forced into a fixed box, so a
    portrait phone photo is not stretched into a landscape frame.
    """
    from PIL import Image as PILImage
    from reportlab.lib.units import inch
    from reportlab.platypus import Image

    try:
        from . import import_storage

        raw, _content_type = import_storage.read_bytes(proof.s3_key)
        # TWO separate streams over the same bytes. Handing one file object to PIL
        # and then to reportlab leaves the second reading from EOF, which is how
        # every photograph ended up as "(image unavailable)".
        with PILImage.open(BytesIO(raw)) as probe:
            width, height = probe.size
        scale = min(
            (max_width_inch * inch) / width, (max_height_inch * inch) / height, 1.0,
        )
        return Image(BytesIO(raw), width=width * scale, height=height * scale)
    except Exception:  # noqa: BLE001 - one bad photo must not lose the report
        logger.warning("dispatch_pdf: proof unavailable %s", proof.s3_key)
        return None


# ── storing them against the order ───────────────────────────────────────────

RENDERERS = {
    DOC_INVOICE: render_invoice,
    DOC_QUOTE: render_quote,
    DOC_ASSESSMENT: render_assessment_report,
}


def generate_submission_documents(order, *, vendor_user=None):
    """Render all three and attach them to the order. Returns the documents.

    Idempotent on CONTENT: a document whose bytes already exist against this order
    is not stored twice, so a resubmission after a void does not leave two
    identical invoices while a genuinely corrected one still lands.

    One failure does not lose the others -- a broken photograph should not cost the
    vendor their invoice.
    """
    import hashlib

    from ..models import DispatchDocument
    from . import import_storage

    out = []
    for doc_type, renderer in RENDERERS.items():
        try:
            pdf = renderer(order)
        except Exception:  # noqa: BLE001
            logger.exception(
                "dispatch_pdf: %s failed for order %s", doc_type, order.pk,
            )
            continue

        digest = hashlib.sha256(pdf).hexdigest()
        existing = DispatchDocument.objects.filter(
            dispatch_order=order, content_hash=digest,
        ).first()
        if existing is not None:
            out.append(existing)
            continue

        stamp = timezone.now().strftime("%Y%m%d")
        filename = f"{DOC_LABELS[doc_type].replace(' ', '-')}-{stamp}.pdf"
        key = import_storage.build_key(
            f"dispatch-documents/{order.pk}/{doc_type}-{digest[:16]}.pdf"
        )
        import_storage.upload_bytes(key, pdf, content_type="application/pdf")
        out.append(DispatchDocument.objects.create(
            dispatch_order=order, s3_key=key, content_hash=digest,
            filename=filename, doc_type=doc_type,
            uploaded_by_vendor_user=vendor_user,
        ))
    return out


# ── work order documents ─────────────────────────────────────────────────────

DOC_WORK_INVOICE = "work_order_invoice"
DOC_COMPLETION = "completion_certificate"

DOC_LABELS[DOC_WORK_INVOICE] = "Work Order Invoice"
DOC_LABELS[DOC_COMPLETION] = "Certificate of Completion"


def _work_order_context(work_order):
    """What both work-order documents draw from.

    Deliberately NOT ``_context``: that resolves the assessment's questionnaire and
    its recommended items, and a work order has neither. Its lines are the products
    actually dispatched -- ``line_items`` -- priced at THIS vendor's rate.
    """
    from . import pricing

    vendor = work_order.vendor
    client = work_order.client
    items = list(work_order.line_items.select_related("case").all())
    prices = {
        r["option_code"]: r["price"]
        for r in pricing.price_list_for(vendor) if r["option_code"]
    }
    # A line item is named after its CASE's product category ("Air Conditioner"),
    # while a price is keyed by catalogue option code. billing_category is the bridge
    # -- the same one the work-order picker uses.
    from api.models import BillableItem

    by_category = {}
    for code, category in BillableItem.objects.filter(
        option_code__in=prices, is_active=True,
    ).values_list("option_code", "billing_category"):
        if category and category not in by_category:
            by_category[category] = prices.get(code)

    lines = []
    for item in items:
        unit = by_category.get(item.item)
        lines.append({
            "description": item.item or "Installed product",
            "qty": 1,
            # None rather than 0 when unpriced: "$0.00" on an invoice reads as free,
            # and a missing price is something to ask about.
            "unit": unit,
            "item": item,
        })

    # ⚠ SAME SHAPE AS _context: a DICT keyed by signer_role, with the image as RAW
    # BYTES. _signature_block expects that, and my first version passed a list of
    # decoded images -- which renders as no signatures at all rather than failing.
    from .dispatch import active_submission

    submission = active_submission(work_order)
    signatures = {}
    if submission is not None:
        for sig in submission.signatures.all():
            image = b""
            if sig.s3_key:
                try:
                    from . import import_storage

                    image, _ct = import_storage.read_bytes(sig.s3_key)
                except Exception:  # noqa: BLE001 - the FACT of signing still counts
                    logger.warning(
                        "dispatch_pdf: signature unavailable %s", sig.s3_key,
                    )
            signatures[sig.signer_role] = {
                "name": sig.signer_name, "signed_at": sig.signed_at, "image": image,
            }

    return {
        "vendor": vendor,
        "client": client,
        "member_name": f"{client.first_name} {client.last_name}".strip(),
        "order": work_order,
        "items": items,
        "lines": lines,
        "signatures": signatures,
        "address": work_order.service_address,
        "case_id": str(work_order.case.case_id) if work_order.case_id else "",
        "completed_at": timezone.now(),
        # ⚠ _member_block READS submitted_at, and a KeyError there is caught by
        # generate_*_documents -- so the completion succeeded and zero documents
        # appeared, silently. For a work order the completion IS the submission date.
        "submitted_at": timezone.now(),
        # Present for the same reason: shared helpers expect the assessment's shape.
        "form": None,
    }


def render_work_order_invoice(work_order):
    """What the vendor bills us for THIS work order -- its products, nothing else.

    ⚠ ONLY THE PRODUCTS ON THIS ORDER. An assessment can produce several work orders,
    and invoicing the whole recommendation on the first would bill for devices nobody
    has installed. The assessment's own fee is not here either: it was invoiced when
    the assessment was submitted, and billing it again per installation would charge
    for one visit several times.
    """
    from reportlab.platypus import Paragraph, Spacer

    ctx = _work_order_context(work_order)
    styles = _styles()
    buf = BytesIO()
    doc = _doc(buf, "Work Order Invoice")
    story = _letterhead(
        ctx["vendor"], styles, "INVOICE",
        f"Installation · {ctx['completed_at']:%d %b %Y}",
    )
    story += [Spacer(1, 10), _member_block(ctx, styles, heading="Installed for"),
              Spacer(1, 12)]
    if ctx["lines"]:
        table, _total = _line_table(
            ctx["lines"], styles, total_label="Total due",
        )
        story.append(table)
    else:
        story.append(Paragraph("No products on this work order.", styles["body"]))
    unpriced = [l["description"] for l in ctx["lines"] if l["unit"] is None]
    if unpriced:
        # Named rather than silently excluded: "why is the total lower than I expect?"
        # needs an answer, and a missing price is a conversation not a zero.
        story += [
            Spacer(1, 8),
            Paragraph(
                "Not priced on your rate card, so excluded from the total: "
                + ", ".join(unpriced)
                + ". Contact CareCircle to have these added.",
                styles["small"],
            ),
        ]
    doc.build(story)
    return buf.getvalue()


def render_completion_certificate(work_order):
    """Proof the work happened: every product, its photograph, and both signatures.

    The photographs are the point. A certificate asserting an installation without
    showing it is a claim, not evidence -- so each product is shown with the photo
    taken on site, and the completion gate requires one per product precisely so this
    document cannot be produced half-empty.

    Same letterhead, palette and signature block as the assessment documents: an
    agent and a member should not have to work out whether two pieces of CareCircle
    paperwork came from the same process.
    """
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Spacer

    ctx = _work_order_context(work_order)
    styles = _styles()
    buf = BytesIO()
    doc = _doc(buf, "Certificate of Completion")
    story = _letterhead(
        ctx["vendor"], styles, "CERTIFICATE OF COMPLETION",
        f"Installation completed {ctx['completed_at']:%d %b %Y}",
    )
    story += [Spacer(1, 10), _member_block(ctx, styles, heading="Installed for"),
              Spacer(1, 12)]

    story.append(Paragraph(
        "The products listed below were installed at the address above and "
        "demonstrated to the member.",
        styles["body"],
    ))
    story.append(Spacer(1, 10))

    proofs = {}
    for proof in work_order.proofs.exclude(dispatch_item=None):
        proofs.setdefault(proof.dispatch_item_id, proof)

    for item in ctx["items"]:
        story.append(Paragraph(
            f"<b>{item.item or 'Product'}</b>"
            + (f" — {item.location}" if item.location else ""),
            styles["body"],
        ))
        image = None
        proof = proofs.get(item.dispatch_item_id)
        if proof is not None:
            image = _proof_flowable(proof, max_width_inch=4.2, max_height_inch=3.2)
        if image is not None:
            story += [Spacer(1, 4), image, Spacer(1, 10)]
        else:
            # Said plainly rather than left as a gap: the gate requires a photo, so
            # its absence here means the image could not be read, which is a
            # different problem from it never having been taken.
            story += [
                Spacer(1, 2),
                Paragraph("(photograph unavailable)", styles["small"]),
                Spacer(1, 10),
            ]

    # ⚠ APPEND, not +=. _signature_block returns a single Table flowable, not a
    # list -- "+=" iterates it and raises TypeError, which generate_* swallows, so
    # the certificate would simply never appear while the completion succeeded.
    story.append(Spacer(1, 6))
    story.append(_signature_block(ctx["signatures"], styles))
    doc.build(story)
    return buf.getvalue()


WORK_ORDER_RENDERERS = {
    DOC_WORK_INVOICE: render_work_order_invoice,
    DOC_COMPLETION: render_completion_certificate,
}


def generate_work_order_documents(work_order, *, vendor_user=None):
    """Render both and attach them to the work order. Returns the documents.

    Same storage, same idempotency and the same DispatchDocument rows as the
    assessment's documents, so they appear in the CRM's existing Documents tab with no
    second mechanism -- the tab lists an order's documents, and a work order is an
    order.

    Idempotent on CONTENT: re-completing after a correction does not leave two
    identical invoices, while a genuinely different one still lands. One failure does
    not lose the other -- a photograph that will not decode should not cost the vendor
    their invoice.
    """
    import hashlib

    from ..models import DispatchDocument
    from . import import_storage

    out = []
    for doc_type, renderer in WORK_ORDER_RENDERERS.items():
        try:
            pdf = renderer(work_order)
        except Exception:  # noqa: BLE001
            logger.exception(
                "dispatch_pdf: %s failed for work order %s", doc_type, work_order.pk,
            )
            continue
        digest = hashlib.sha256(pdf).hexdigest()
        existing = DispatchDocument.objects.filter(
            dispatch_order=work_order, content_hash=digest,
        ).first()
        if existing is not None:
            out.append(existing)
            continue
        stamp = timezone.now().strftime("%Y%m%d")
        filename = f"{DOC_LABELS[doc_type].replace(' ', '-')}-{stamp}.pdf"
        key = import_storage.build_key(
            f"dispatch-documents/{work_order.pk}/{doc_type}-{digest[:16]}.pdf"
        )
        import_storage.upload_bytes(key, pdf, content_type="application/pdf")
        out.append(DispatchDocument.objects.create(
            dispatch_order=work_order, s3_key=key, content_hash=digest,
            filename=filename, doc_type=doc_type,
            uploaded_by_vendor_user=vendor_user,
        ))
    return out
