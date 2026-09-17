# Vendor portal — plan

**Status: planning. No code written.**

The surface external vendors use to do the work: confirm appointments, run the
Dwelling Assessment at the member's home, capture photos and signatures, and
complete work orders. It is what makes a dispatch order move past
`PENDING SCHEDULE` — until it exists, orders sit there and the Unite Us upload
queue stays empty by design.

Companion to `housing-assessment-order-plan.md`, which covers the CRM side.

---

## Hard requirements (from the operator)

1. Vendor users must reach **neither the CRM nor any CRM API**.
2. They get **their own API**, reusing the partner-API pattern.
3. Their own **subdomain**, and the frontend should **behave like a mobile app**.
4. A **new repo**, alongside `backend/` and `frontend/`.
5. **Photos** taken in the app.
6. **Offline** operation.
7. Photos should be **processed by Django**, not merely dumped in a bucket.
8. **No GPS.** Reminders by **SMS**.
9. The device shows **name, phone, address and address notes only** — no Medicaid
   ID, no DOB, no Medicaid plan.

---

## Isolation — extend the pattern, not the credential

The partner API already isolates itself three independent ways, and each comment
in `api/partner/auth.py` explains why:

```
1. nginx vhost              partners.carecircleinternal.com serves partner routes
2. PartnerHostMiddleware    serves ONLY api.partner.urls on that hostname
3. auth kept OUT of DEFAULT_AUTHENTICATION_CLASSES
      -> a partner token at a CRM endpoint is 401 UNRECOGNISED rather than
         merely unauthorised, and an agent JWT is unrecognised here
```

Plus one decision worth repeating verbatim, because a vendor token would fall into
exactly the same trap:

> Credentials are a `client_id` + `client_secret` exchanged for a short-lived
> OPAQUE bearer token. Opaque, not a JWT: the project's default authenticators
> include JWT classes, so a partner JWT signed with the shared key would
> authenticate against the whole CRM.

**So the vendor API copies the pattern with a new principal.** The existing
principal is a `DeliveryCompanyApiClient` — machine-to-machine, explicitly "not a
Django user". A human vendor needs email + password, which is a different principal
(`VendorUser`, already modelled) and gets its own host:

```
vendor.carecircleinternal.com
    /             ->  the PWA (static files)
    /api/...      ->  nginx proxies to gunicorn
                      VendorHostMiddleware serves only api.vendor.urls
                      opaque vendor tokens, every query scoped to the vendor
```

**One host for both the app and its API**, deliberately: same-origin means no
CORS, one certificate and a single middleware rule. The wildcard
`*.carecircleinternal.com` certificate on the ALB already covers the subdomain, so
this is a Route 53 alias plus an nginx vhost — no certificate work, exactly as
`partners` was.

A **separate host from `partners`** rather than new routes on it, so a delivery
company's credential and a vendor user's token never coexist on one hostname.

---

## Frontend — a PWA in the CRM's stack, Capacitor-ready

**Vite + React + TypeScript + Tailwind**, with `vite-plugin-pwa` (Workbox) for the
service worker and `idb` for offline storage.

Why this rather than React Native:

- **The form template will change.** A web deploy ships instantly; an app-store
  release takes days of review. That alone rules out native-first.
- **Same stack as `frontend/`** — one set of conventions, and existing components
  transfer.
- **Installable to the home screen**: full screen, no browser chrome, launcher
  icon. That is the "behaves like a mobile app" requirement.
- **Camera works today** via `<input type="file" accept="image/*"
  capture="environment">`; `getUserMedia` if in-app framing is wanted later.
- **Signatures are a canvas**, which is what the vendor's current tool already
  does ("Hand the tablet to the member to sign").

### iOS caveats, recorded because they bite later

- **No Background Sync API in Safari.** Uploads progress while the app is OPEN,
  not in the background. For a vendor who fills a form in a basement and pockets
  the phone, "open the app to finish uploading" must be an explicit, visible queue
  rather than a silent one.
- **Storage eviction**: an uninstalled PWA can lose IndexedDB after ~7 days of
  inactivity. Mitigated by requiring install plus `navigator.storage.persist()`.
- **Push needs iOS 16.4+ and an installed app** — which is part of why reminders
  are SMS.

**Capacitor is the escape hatch.** It wraps the same React codebase into a native
shell if background upload, store distribution or MDM later become necessary.
Designing PWA-first with that in mind costs nothing now and avoids a rewrite.

### Repo layout

```
/Users/alex/Projects/ext/
    backend/        Django -- gains api/vendor/
    frontend/       the CRM
    extension/
    ext-v2/
    vendor-app/     NEW: the vendor PWA, its own git repo
```

---

## Photos — presign, upload, confirm

The POD partner API already implements all three options, and the third is the one
to use:

```
POST /v1/deliveries/<id>/proofs/          multipart through Django
POST /v1/deliveries/<id>/proofs/base64/   inline base64, capped at 8 MB
POST /v1/deliveries/<id>/proofs/presign/  -> device PUTs straight to S3
                                             (staged under pod-inbox/<company>/...)
POST /v1/deliveries/<id>/proofs/confirm/  -> Django "hashes and re-keys the object
                                             into the canonical content-addressed
                                             location"
```

**Presign + confirm satisfies "process it in Django" without tying up a worker.**
The bytes go device -> S3; Django still handles every image in `confirm` (hash,
re-key, and anything else — EXIF stripping, thumbnails — there or in a Celery task
reading from S3).

That distinction matters at the current worker count. Production runs
`--workers 9`; a 5 MB photo over cellular can hold a worker for 30+ seconds, so a
handful of concurrent vendor uploads would compete with agents using the CRM. The
multipart path is fine for one or two images and the wrong shape for a ten-photo
visit.

**One photo at a time, queued** — not one big submit. Capture -> queue -> upload
opportunistically -> allow submit only once every artefact is confirmed. A single
50 MB submit is fragile: one dropped connection loses everything, whereas per-photo
uploads retry independently. That is what the **content hash** and
**client-generated UUIDs** are for: a retry must never create a second photo or a
second finding.

**Client-side compression before queueing**, because a 12 MP photo is ~5 MB and ten
of them over cellular is slow and expensive for the vendor.

---

## PHI on the device

The largest risk in the feature: offline drafts mean member data and photographs of
their home sitting in IndexedDB on a vendor employee's phone, which is not a
managed device.

### Decided: cache the minimum

```
SHOW / CACHE    name, phone, address, address notes
NEVER SENT      Medicaid ID, date of birth, Medicaid plan
```

This works because **the PDF is rendered server-side** — the server has those
fields, so the cover sheet still prints correctly while the device never receives
them. Not caching beats wiping later, and this is the strongest control available.

⚠️ Note this is a REDUCTION from the vendor's current tool, whose cover sheets
print Medicaid ID and Medicaid plan. Worth telling them, or it reads as missing
data.

### Decided: compress and wipe on submit, with three corrections

The operator's instinct is right; three details change the implementation:

- **Wipe per artefact on CONFIRMED receipt, not all at the end.** A wipe that runs
  after "submit" deletes evidence that may never have landed.
- **The exposure window is exactly the risky period** — while the vendor is in the
  field, when a phone is most likely to be lost. Wiping afterwards shortens
  exposure; it does not remove it.
- **The wipe is conditional on ever submitting.** No signal, app closed, vendor
  goes home, and the draft sits for days. So there must ALSO be a time-based purge
  independent of submit, plus a visible "N items pending upload" queue so the app
  is never silently holding PHI.

### Still to decide

- Session length and re-authentication; remote revocation of a lost device.
- Shared tablets: vendor staff will pass one device around, so fast user switching
  and a short PIN beat retyping a password at every house.
- A **BAA** with each vendor company.
- **PHI access audit**: which vendor user opened which member's record.
- Rate limiting on the login endpoint, which is on the public internet.

---

## Reminders — SMS, and Twilio is not wired yet

No SMS provider exists. Twilio is a decision already made and never implemented:

```
api/models.py:3179        "Delivery is intended to be SMS (Twilio). Until that's
                           wired up the code is ..."
api/views_member_app.py   "TODO(twilio): replace this with an SMS to mobile_number"
```

The member app currently EMAILS its 2FA codes for this reason. So vendor reminders
need that integration built, and it should share the existing TODO rather than
duplicate it.

**No GPS on check-in** — decided. Check-in records a timestamp only.

Open: the earlier spec says the reminder is for the VENDOR ("block the vendor user
calendar and a reminder for him"). If the MEMBER is also to be reminded, that is
the one needing `consent_to_text`, which the assessment-order wizard already
captures.

---

## What the vendor app does

```
sign in                 email + password, opaque token, scoped to one vendor
my work                 assigned orders: assessments and work orders
confirm appointment     pick from the member's offered availability
                        -> moves the order to CONFIRMED, fires the calendar
                           event and the SMS reminder
start visit             timestamp; no GPS
assessment form         the modules the referral authorises (2.1 and/or 2.2b),
                        offline-capable, drafts held locally
photos                  capture, compress, queue, upload, confirm, purge
signatures              assessor and member, drawn on canvas
submit                  gated on >=1 dwelling photo + both signatures
work orders             per-item completion and proof of service
```

### The submit gate, corrected

The real form states it outright:

> "At least one photo of the dwelling, your signature, and the member's signature
> are all required before you can submit."

That is **one photo per assessment**, NOT one per finding. The CRM's current
`missing_for_submission` requires a photo for every finding, which would reject
all three of the real submissions supplied (each carries `Photos (1)` against ~10
ticked risks). **This must be fixed when the vendor API lands**: the per-finding
rule belongs to work orders, where proof of service is per item installed.

### Forced app update

A stale PWA holding template v1 must not submit against v2. The app should refuse
to submit on a template version it does not recognise and prompt to reload.
Silently dropping unknown questions is the failure mode to design out — which is
the whole reason `DispatchQuestionnaire` pins `template_version` and freezes
`schema_snapshot`.

---

## Open questions

1. **Can a vendor see a member's PAST assessments**, or only their own current
   assignments? Affects both the API scope and how much PHI the device ever holds.
2. **Does a work-order photo attach to a specific ITEM** — proof that this
   particular grab bar was fitted — or just to the visit? Decides whether the
   per-finding photo rule applies to work orders.
3. **`Case A000167`** — the case number printed at the top of every form. We store
   nothing like it (the nearest field is `payer_authorization_number`). Vendor
   assigned, or should the CRM generate it?
4. Can a vendor admin **reset their own staff's passwords**, or does that stay a
   CRM action? The CRM currently resets only the vendor admin.
