# Delivery Partner API — proof-of-delivery ingestion

Lets a delivery company's backend push proof of delivery (status + photos) to us,
on a hostname that exposes **nothing else** from the CRM or the Chrome extension.

Complements `proof_of_delivery_plan.md`, which covers the per-company CSV report
importer. Both entry points share one ingestion service, so they cannot drift.

---

## 1. Isolation — how vendors reach only this API

Two independent layers. The point is that CRM routes **do not exist** on the
partner hostname, rather than existing-but-forbidden (a permission check can be
misconfigured; a missing route cannot).

**Layer 1 — nginx.** A `server` block for `partners.<domain>` that proxies only
`/v1/` and 404s everything else. See §7.

**Layer 2 — Django.** `api.middleware.PartnerHostMiddleware` swaps
`request.urlconf` to `api.partner.urls` when the Host matches
`settings.PARTNER_API_HOST`. `backend/urls.py` never includes that module, and
that module never includes the CRM's, so:

| Request | Result |
| --- | --- |
| `partners.…/api/clients/` | **404** (route absent) |
| `partners.…/api/portal/…` | **404** |
| `www.…/v1/whoami/` | **404** (partner routes absent) |
| partner token → `www.…/api/…` | **401** (auth class not registered there) |
| agent JWT → `partners.…/v1/…` | **401** (partner views take partner auth only) |

All five are covered by tests in `DeliveryPartnerApiTest`.

The middleware reads the **Host header only**, never `X-Forwarded-Host`, so a
client cannot select — or escape — the partner surface with a spoofed proxy
header.

Partner views also pin `renderer_classes = [JSONRenderer]`: the project enables
`BrowsableAPIRenderer` globally, which would otherwise hand vendor developers a
clickable HTML explorer of the surface.

## 2. Credentials

`DeliveryCompanyApiClient` — **one per company** (OneToOne):

| Field | Notes |
| --- | --- |
| `client_id` | public, e.g. `ccpod_qari_7f3a91c2`; identifiable in logs / secret scanners |
| `secret_hash` | PBKDF2 via Django's hashers. The raw secret is returned **once** |
| `previous_secret_hash` + `previous_secret_expires_at` | rotation overlap (default 7 days) |
| `scopes` | `pod:status`, `pod:photo` |
| `allowed_ips` | optional allowlist (IPs or CIDRs); empty = any |
| `expires_at`, `revoked_at`, `last_used_at`, `last_used_ip` | lifecycle + audit |

**Rotation is zero-downtime:** issuing a new secret keeps the old one valid until
the grace window closes, so the vendor can redeploy without an outage.

### Why opaque access tokens, not JWTs

`DEFAULT_AUTHENTICATION_CLASSES` contains two JWT authenticators. A partner JWT
signed with the shared key would therefore authenticate against **the entire
CRM**. So `PartnerAccessToken` is a random opaque string, stored as a sha256
hash, ~1h TTL — meaningless to those authenticators, and revocable with a single
row update. Revoking a credential also revokes every token already issued from
it.

## 3. Endpoints (`/v1/`, push-only)

| Endpoint | Scope | Notes |
| --- | --- | --- |
| `POST /v1/token/` | — | `client_id` + `client_secret` → opaque bearer. The only unauthenticated route |
| `GET /v1/whoami/` | any | credential check that touches no data |
| `POST /v1/deliveries/{order_id}/status/` | `pod:status` | outcome without a photo (failed / returned) |
| `POST /v1/deliveries/{order_id}/proofs/` | `pod:photo` | **multipart** — one or more `file` parts |
| `POST /v1/deliveries/{order_id}/proofs/base64/` | `pod:photo` | **inline base64** JSON, capped at 8 MB/image |
| `POST /v1/deliveries/{order_id}/proofs/presign/` | `pod:photo` | **direct-to-S3**, step 1 |
| `POST /v1/deliveries/{order_id}/proofs/confirm/` | `pod:photo` | step 2: register the uploaded keys |

`order_id` is `DeliveryOrder.delivery_order_id` — the same `ORDER #` vendors
already receive on the manifest, so there is no new identifier for them.

**Deliberately absent:** any endpoint that lists our data. Partners submit; they
do not read.

### Scoping

Every order is resolved through `_order_for()`, which filters by the caller's
company. An order that exists but belongs to someone else returns **404, not
403** — a 403 would confirm the id is real.

`/confirm/` additionally refuses any S3 key that wasn't issued for that company
*and* that order, so a partner cannot name an arbitrary object in our bucket.

### Statuses

A partner may set only `delivered`, `failed`, `returned`, `cancelled`. They
cannot move an order back into our own workflow states (e.g. `pending`).

## 4. One ingestion path

`services/pod_ingest.py` owns the rules both entry points must share:

- the S3 key layout (`pod/<order>/<hash>.<ext>` — content-addressed),
- **de-duplication by sha256 of the bytes**, so retries and re-imports never
  create a second proof,
- which `DeliveryOrder` fields a delivery report may change
  (`status`, `delivered_at`, `delivery_company` — nothing else).

`pod_import.py` (CSV) now delegates to it, so the two cannot diverge. A test
asserts the same image sent by **multipart** and by **base64** de-duplicates,
proving both transports hash identically.

API-sourced proofs are tagged `source_report = "api:<client_id>"`, so they are
distinguishable from CSV imports.

## 5. Rate limiting

The project had **no throttling at all**. Two scopes were added, applied only to
the partner surface (agent/extension traffic is untouched):

- `partner` — default `600/min` (`PARTNER_THROTTLE_RATE`)
- `partner_token` — default `20/min` (`PARTNER_TOKEN_THROTTLE_RATE`), tighter as a
  brute-force guard on the credential exchange

Throttling keys off the credential, so the limit is per delivery company.

## 6. Settings UI — Delivery Company → POD API access

**Management only** (`IsManagementAgent`). The rest of Settings is
`IsPortalAgent`, which would have let CS / Logistics / Nutritionist mint
third-party credentials.

- create / rotate / revoke; the secret is displayed **once**, with a copy button
- shows client_id, base URL, status, scopes, last used + IP, live token count,
  and the rotation overlap deadline
- **exports the integration guide** in three formats:
  - `?fmt=md` — Markdown
  - `?fmt=pdf` — PDF (reportlab, already a dependency)
  - `?fmt=openapi` — a **hand-curated** OpenAPI 3 spec

> The OpenAPI document is written by hand in `services/partner_docs.py` rather
> than generated from the Django project: auto-generation would risk publishing
> the whole CRM surface, which is exactly what this feature avoids. A test
> asserts every path in the spec starts with `/v1/`.

The guide embeds the real secret **only** when it is passed straight through from
a create/rotate response (the one-time "integration pack"). Later downloads
render `<your client_secret>` and tell the vendor to ask for a rotation.

NB the query parameter is `fmt`, not `format` — DRF reserves `format` for
renderer negotiation and answers an unknown value with its own 404.

## 7. Deployment

No new AWS infrastructure: same EC2 box, same gunicorn, same RDS/S3, same 443 —
so no load balancer, target group or security-group change.

1. **DNS** — `A` record `partners.<domain>` → the same server IP as `www`.
2. **TLS** — `sudo certbot --nginx -d partners.<domain>` (skip if a wildcard cert
   already covers it).
3. **nginx** — install [`deploy/nginx-partners-api.conf`](../deploy/nginx-partners-api.conf):

```bash
sudo cp deploy/nginx-partners-api.conf /etc/nginx/sites-available/partners-api
sudo ln -s /etc/nginx/sites-available/partners-api /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

It mirrors the main `carecircle` vhost: `listen 80` with TLS terminated
**upstream** (hence the `$forwarded_proto` map) and gunicorn reached over
`unix:/home/ubuntu/backend/gunicorn.sock`. It proxies only `/v1/` and returns a
JSON 404 for everything else, and deliberately has **no `/static/` alias** —
partners have no UI, so none of our assets should be reachable there.

Two things it depends on:

- `Host` is forwarded unchanged (`$http_host`), because `PartnerHostMiddleware`
  matches on it to select the partner URLConf. Rewriting it would serve the CRM.
- `$forwarded_proto` is defined **once**, in the `carecircle` vhost. `map` is
  http{}-level and shared across included files, so it must not be redefined.

4. **`.env.prod`**:

```bash
PARTNER_API_HOST=partners.carecircleinternal.com
DJANGO_ALLOWED_HOSTS=www.carecircleinternal.com,carecircleinternal.com,partners.carecircleinternal.com
# Optional:
# PARTNER_TOKEN_TTL_SECONDS=3600
# PARTNER_ROTATION_GRACE_SECONDS=604800
# PARTNER_THROTTLE_RATE=600/min
# PARTNER_TOKEN_THROTTLE_RATE=20/min
```

**Do not** add the partner host to `CORS_ALLOWED_ORIGINS` or
`CSRF_TRUSTED_ORIGINS`: it is server-to-server and should not be callable from a
browser.

Steps 1–3 sit outside the usual `git pull` deploy, so re-run them if the box is
rebuilt. Until `PARTNER_API_HOST` is set the feature is **inert** — the middleware
no-ops and the partner routes are unreachable everywhere, which makes deploying
the code safe ahead of the DNS work.

### Local testing without DNS

```bash
curl -H "Host: partners.local" http://127.0.0.1:8000/v1/whoami/
```

with `PARTNER_API_HOST=partners.local` and `partners.local` in
`DJANGO_ALLOWED_HOSTS`.

## 8. Rollout

1. Deploy (inert until the host is configured).
2. Point DNS + nginx at it; confirm `/v1/whoami/` and that `/api/clients/` 404s
   on that host.
3. Issue a credential to one vendor, hand over the PDF + OpenAPI.
4. Run the API **alongside** the CSV import for that vendor and reconcile proof
   counts.
5. Retire that vendor's CSV.

## 9. What was retired

Delivery companies previously had an **outbound** `DeliveryCompanyIntegration`
(EMAIL / API — "how they receive orders"). It was never wired up (nothing read
`config.endpoint`/`apiKey`, and production had **zero rows**), so the email option
is gone and the section now hosts inbound POD API access instead.

Kitchens keep their own separate email/API integration — untouched.
