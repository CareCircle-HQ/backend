"""Generate the delivery-partner integration guide for a company's developers.

Three outputs from one source of truth (:data:`ENDPOINTS`):

* ``build_markdown`` -- the readable guide,
* ``build_pdf``      -- the same guide for handing over (reportlab, already a dep),
* ``build_openapi``  -- a machine-readable spec their tooling can generate from.

The OpenAPI document is written BY HAND here rather than generated from the Django
project: auto-generating would risk publishing the whole CRM surface, which is
exactly what the partner host is designed to prevent.

The client secret is only ever included when the caller passes it in (straight
from a create/rotate response). We store a hash, so a later download renders a
placeholder.
"""

import io

from django.conf import settings

SECRET_PLACEHOLDER = "<your client_secret>"

# Every endpoint, described once. (path, method, title, scope, fields, notes)
ENDPOINTS = [
    {
        "path": "/v1/token/",
        "method": "POST",
        "title": "Get an access token",
        "scope": "",
        "auth": "none (this is the credential exchange)",
        "fields": [
            ("client_id", "string", True, "Your client id (shown below)."),
            ("client_secret", "string", True, "Your secret."),
        ],
        "returns": "access_token, token_type=Bearer, expires_in (seconds), scope",
        "notes": "Tokens are short-lived. Cache one and re-exchange when it expires.",
    },
    {
        "path": "/v1/whoami/",
        "method": "GET",
        "title": "Verify your credential",
        "scope": "",
        "auth": "Bearer",
        "fields": [],
        "returns": "client_id, delivery_company, scopes, token_expires_at",
        "notes": "Use this to confirm your setup before sending real data.",
    },
    {
        "path": "/v1/deliveries/{order_id}/status/",
        "method": "POST",
        "title": "Report a delivery outcome (no photo)",
        "scope": "pod:status",
        "auth": "Bearer",
        "fields": [
            ("status", "string", False, "delivered | failed | returned | cancelled."),
            ("delivered_at", "ISO 8601", False, "When it was delivered/attempted."),
            ("delivery_date", "MM/DD/YYYY", False, "Alternative to delivered_at."),
            ("delivery_time", "HH:MM AM/PM", False, "Used with delivery_date."),
            ("driver", "string", False, "Your driver id/name."),
            ("route_id", "string", False, "Your route id."),
            ("note", "string", False, "Free-text delivery note."),
        ],
        "returns": "order_id, status, delivered_at, updated_fields",
        "notes": "Use for failed/returned attempts, or to set the status separately.",
    },
    {
        "path": "/v1/deliveries/{order_id}/proofs/",
        "method": "POST",
        "title": "Upload proof photos (multipart)",
        "scope": "pod:photo",
        "auth": "Bearer",
        "content_type": "multipart/form-data",
        "fields": [
            ("file", "file", True, "One or more images. Repeat the field for several."),
            ("status", "string", False, "Optional, as above."),
            ("delivered_at", "ISO 8601", False, "As above."),
            ("driver", "string", False, "As above."),
            ("route_id", "string", False, "As above."),
            ("note", "string", False, "As above."),
        ],
        "returns": "proofs[], stored, duplicates, status, updated_fields",
        "notes": "Simplest option. Max 25 MB per image.",
    },
    {
        "path": "/v1/deliveries/{order_id}/proofs/base64/",
        "method": "POST",
        "title": "Upload proof photos (inline base64)",
        "scope": "pod:photo",
        "auth": "Bearer",
        "content_type": "application/json",
        "fields": [
            ("photos", "array", True,
             "[{filename, content_type, data}] where data is base64. A bare "
             "base64 string is also accepted."),
            ("status", "string", False, "Optional, as above."),
            ("delivered_at", "ISO 8601", False, "As above."),
            ("driver", "string", False, "As above."),
            ("route_id", "string", False, "As above."),
            ("note", "string", False, "As above."),
        ],
        "returns": "proofs[], stored, duplicates, status, updated_fields",
        "notes": "Convenient for pure-JSON clients. Max 8 MB of base64 per image.",
    },
    {
        "path": "/v1/deliveries/{order_id}/proofs/presign/",
        "method": "POST",
        "title": "Upload proof photos (direct to storage) - step 1",
        "scope": "pod:photo",
        "auth": "Bearer",
        "content_type": "application/json",
        "fields": [
            ("files", "array", True, "[{filename, content_type}] - up to 20."),
        ],
        "returns": "uploads[{filename, s3_key, upload_url, content_type}], confirm_url",
        "notes": "Best for many or large images: PUT each upload_url, then confirm.",
    },
    {
        "path": "/v1/deliveries/{order_id}/proofs/confirm/",
        "method": "POST",
        "title": "Upload proof photos (direct to storage) - step 2",
        "scope": "pod:photo",
        "auth": "Bearer",
        "content_type": "application/json",
        "fields": [
            ("s3_keys", "array", True, "The s3_key values from step 1."),
            ("status", "string", False, "Optional, as above."),
            ("delivered_at", "ISO 8601", False, "As above."),
            ("driver", "string", False, "As above."),
            ("route_id", "string", False, "As above."),
            ("note", "string", False, "As above."),
        ],
        "returns": "proofs[], stored, duplicates, status, updated_fields",
        "notes": "Registers the uploaded objects against the order.",
    },
]

ERRORS = [
    ("400", "invalid_request / invalid_status / invalid_delivered_at / no_file",
     "The payload was rejected; the message says which field."),
    ("401", "invalid_client / invalid token", "Bad credentials, or an expired token."),
    ("403", "insufficient_scope", "Your credential lacks the scope for that call."),
    ("404", "order_not_found", "That order id is not one of your deliveries."),
    ("429", "throttled", "Rate limited. Back off and retry."),
    ("503", "storage_unavailable", "Direct upload is unavailable; use multipart."),
]


def partner_base_url():
    """The vendor-facing base URL, from ``PARTNER_API_HOST``."""
    host = (getattr(settings, "PARTNER_API_HOST", "") or "").strip()
    if not host:
        return "https://<partner-api-host>"
    scheme = "http" if host.startswith(("localhost", "127.0.0.1")) else "https"
    return f"{scheme}://{host}"


def build_markdown(client, *, secret=""):
    """The integration guide as Markdown."""
    base = partner_base_url()
    sec = secret or SECRET_PLACEHOLDER
    company = client.delivery_company.name
    out = [
        f"# CareCircle Proof-of-Delivery API - {company}",
        "",
        "Send us proof of delivery (status and photos) for the orders we assign you.",
        "This is the only CareCircle API available to you; no other endpoint exists",
        "on this host.",
        "",
        "## Your credentials",
        "",
        f"- **Base URL:** `{base}`",
        f"- **client_id:** `{client.client_id}`",
        f"- **client_secret:** `{sec}`",
        f"- **Scopes:** `{' '.join(client.scopes or [])}`",
        "",
    ]
    if not secret:
        out += [
            "> The secret is stored hashed and cannot be re-displayed. If it was",
            "> lost, ask your CareCircle contact to rotate it - the previous secret",
            "> keeps working for a short overlap so you can redeploy safely.",
            "",
        ]
    out += [
        "## 1. Get an access token",
        "",
        "```bash",
        f"curl -X POST {base}/v1/token/ \\",
        '  -H "Content-Type: application/json" \\',
        f"""  -d '{{"client_id": "{client.client_id}", "client_secret": "{sec}"}}'""",
        "```",
        "",
        "Response:",
        "",
        "```json",
        '{ "access_token": "ccat_...", "token_type": "Bearer",',
        '  "expires_in": 3600, "scope": "pod:status pod:photo" }',
        "```",
        "",
        "Send it on every other call as `Authorization: Bearer <access_token>`.",
        "",
        "## 2. Endpoints",
        "",
    ]
    for ep in ENDPOINTS:
        out += [
            f"### {ep['method']} `{ep['path']}`",
            "",
            f"{ep['title']}.",
            "",
            f"- **Auth:** {ep['auth']}",
        ]
        if ep.get("scope"):
            out.append(f"- **Scope:** `{ep['scope']}`")
        if ep.get("content_type"):
            out.append(f"- **Content-Type:** `{ep['content_type']}`")
        out.append("")
        if ep["fields"]:
            out += ["| Field | Type | Required | Notes |", "| --- | --- | --- | --- |"]
            for name, typ, req, note in ep["fields"]:
                out.append(f"| `{name}` | {typ} | {'yes' if req else 'no'} | {note} |")
            out.append("")
        out += [f"**Returns:** {ep['returns']}", "", f"_{ep['notes']}_", ""]

    out += [
        "## 3. Example: photo + status in one call",
        "",
        "```bash",
        f'curl -X POST {base}/v1/deliveries/$ORDER_ID/proofs/ \\',
        '  -H "Authorization: Bearer $ACCESS_TOKEN" \\',
        '  -F "file=@/path/to/photo.jpg" \\',
        '  -F "status=delivered" \\',
        '  -F "delivered_at=2026-09-08T15:04:00Z" \\',
        '  -F "driver=D-77" -F "note=Left with doorman"',
        "```",
        "",
        "## 4. Order ids",
        "",
        "`order_id` is the **ORDER #** from the delivery manifest we send you.",
        "An order that is not yours returns `404 order_not_found`.",
        "",
        "## 5. Retries and duplicates",
        "",
        "Safe to retry. Photos are de-duplicated by content, so re-sending the same",
        "image never creates a second proof (the response reports it under",
        "`duplicates`). Re-sending the same status is also harmless.",
        "",
        "## 6. Errors",
        "",
        "| HTTP | code | Meaning |",
        "| --- | --- | --- |",
    ]
    out += [f"| {code} | `{name}` | {desc} |" for code, name, desc in ERRORS]
    out += ["", "All errors are JSON: `{\"error\": \"<code>\", \"detail\": \"...\"}`.", ""]
    return "\n".join(out)


def build_openapi(client):
    """A hand-curated OpenAPI 3.0 document covering only the partner endpoints."""
    base = partner_base_url()
    paths = {}
    for ep in ENDPOINTS:
        body = None
        if ep["fields"]:
            ctype = ep.get("content_type", "application/json")
            props = {}
            required = []
            for name, typ, req, note in ep["fields"]:
                schema = {"description": note}
                if typ == "array":
                    schema["type"] = "array"
                    schema["items"] = {"type": "object"}
                elif typ == "file":
                    schema["type"] = "string"
                    schema["format"] = "binary"
                else:
                    schema["type"] = "string"
                props[name] = schema
                if req:
                    required.append(name)
            body = {
                "required": bool(required),
                "content": {
                    ctype: {
                        "schema": {
                            "type": "object",
                            "properties": props,
                            **({"required": required} if required else {}),
                        }
                    }
                },
            }
        op = {
            "summary": ep["title"],
            "description": f"{ep['notes']} Returns: {ep['returns']}",
            "responses": {
                "200": {"description": "OK"},
                "201": {"description": "Created"},
                "400": {"description": "Invalid payload"},
                "401": {"description": "Invalid credentials or token"},
                "403": {"description": "Missing scope"},
                "404": {"description": "Order not found for this company"},
                "429": {"description": "Rate limited"},
            },
        }
        if ep["auth"] != "none (this is the credential exchange)":
            op["security"] = [{"bearerAuth": []}]
        else:
            op["security"] = []
        if body:
            op["requestBody"] = body
        if "{order_id}" in ep["path"]:
            op["parameters"] = [{
                "name": "order_id", "in": "path", "required": True,
                "schema": {"type": "string", "format": "uuid"},
                "description": "The ORDER # from your delivery manifest.",
            }]
        paths.setdefault(ep["path"], {})[ep["method"].lower()] = op

    return {
        "openapi": "3.0.3",
        "info": {
            "title": f"CareCircle Proof-of-Delivery API ({client.delivery_company.name})",
            "version": "1.0.0",
            "description": (
                "Submit proof of delivery for CareCircle orders assigned to you. "
                "Authenticate by exchanging your client_id/client_secret at "
                "/v1/token/ for a short-lived bearer token."
            ),
        },
        "servers": [{"url": base}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer"}
            }
        },
        "paths": paths,
    }


def build_pdf(client, *, secret=""):
    """The same guide as a PDF, for handing to a vendor. Uses reportlab, which
    is already a dependency (no new packages)."""
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
    )
    from reportlab.lib import colors

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER, title="CareCircle POD API",
        leftMargin=0.8 * inch, rightMargin=0.8 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
    )
    styles = getSampleStyleSheet()
    mono = ParagraphStyle(
        "mono", parent=styles["BodyText"], fontName="Courier", fontSize=8,
        leading=10, alignment=TA_LEFT,
    )
    body = styles["BodyText"]
    base = partner_base_url()
    sec = secret or SECRET_PLACEHOLDER

    story = [
        Paragraph(f"CareCircle Proof-of-Delivery API", styles["Title"]),
        Paragraph(client.delivery_company.name, styles["Heading2"]),
        Spacer(1, 10),
        Paragraph(
            "Send us proof of delivery (status and photos) for the orders we assign "
            "you. This is the only CareCircle API available to you.", body,
        ),
        Spacer(1, 12),
        Paragraph("Your credentials", styles["Heading2"]),
    ]
    cred_rows = [
        ["Base URL", base],
        ["client_id", client.client_id],
        ["client_secret", sec],
        ["Scopes", " ".join(client.scopes or [])],
    ]
    t = Table(cred_rows, colWidths=[1.4 * inch, 5.0 * inch])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (1, 0), (1, -1), "Courier"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story += [t, Spacer(1, 8)]
    if not secret:
        story += [Paragraph(
            "The secret is stored hashed and cannot be re-displayed. If it was lost, "
            "ask your CareCircle contact to rotate it -- the previous secret keeps "
            "working for a short overlap so you can redeploy safely.", body,
        ), Spacer(1, 8)]

    story += [
        Paragraph("1. Get an access token", styles["Heading2"]),
        Paragraph(
            f"POST {base}/v1/token/<br/>"
            f'{{"client_id": "{client.client_id}", "client_secret": "{sec}"}}', mono,
        ),
        Spacer(1, 6),
        Paragraph(
            "Send the returned token on every other call as "
            "<font face='Courier'>Authorization: Bearer &lt;access_token&gt;</font>.", body,
        ),
        Spacer(1, 12),
        Paragraph("2. Endpoints", styles["Heading2"]),
    ]

    for ep in ENDPOINTS:
        story += [
            Paragraph(f"{ep['method']} {ep['path']}", styles["Heading3"]),
            Paragraph(ep["title"], body),
        ]
        meta = f"Auth: {ep['auth']}"
        if ep.get("scope"):
            meta += f" &nbsp;|&nbsp; Scope: {ep['scope']}"
        if ep.get("content_type"):
            meta += f" &nbsp;|&nbsp; Content-Type: {ep['content_type']}"
        story.append(Paragraph(meta, mono))
        if ep["fields"]:
            rows = [["Field", "Type", "Req", "Notes"]] + [
                [n, t_, "yes" if r else "no", Paragraph(note, mono)]
                for n, t_, r, note in ep["fields"]
            ]
            tt = Table(rows, colWidths=[1.3 * inch, 0.9 * inch, 0.4 * inch, 3.8 * inch])
            tt.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTNAME", (0, 1), (0, -1), "Courier"),
                ("FONTSIZE", (0, 0), (-1, -1), 7.5),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]))
            story += [Spacer(1, 4), tt]
        story += [
            Spacer(1, 4),
            Paragraph(f"<b>Returns:</b> {ep['returns']}", body),
            Paragraph(f"<i>{ep['notes']}</i>", body),
            Spacer(1, 10),
        ]

    story += [
        PageBreak(),
        Paragraph("Order ids", styles["Heading2"]),
        Paragraph(
            "order_id is the ORDER # from the delivery manifest we send you. An order "
            "that is not yours returns 404 order_not_found.", body,
        ),
        Spacer(1, 10),
        Paragraph("Retries and duplicates", styles["Heading2"]),
        Paragraph(
            "Safe to retry. Photos are de-duplicated by content, so re-sending the "
            "same image never creates a second proof (reported under 'duplicates'). "
            "Re-sending the same status is harmless.", body,
        ),
        Spacer(1, 10),
        Paragraph("Errors", styles["Heading2"]),
    ]
    err_rows = [["HTTP", "code", "Meaning"]] + [
        [c, Paragraph(n, mono), Paragraph(d, body)] for c, n, d in ERRORS
    ]
    et = Table(err_rows, colWidths=[0.6 * inch, 2.2 * inch, 3.6 * inch])
    et.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cccccc")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(et)

    doc.build(story)
    return buf.getvalue()
