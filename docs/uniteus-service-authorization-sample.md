# Unite Us — `service_authorization` sample response & full field inventory

A real captured response for a single service authorization, kept so we know
which fields Unite Us exposes on this resource (including ones we do **not** yet
ingest). Field mapping into our `Case` model lives in
[uniteus-api-fields.md](./uniteus-api-fields.md#service_authorization--v1service_authorizations)
(`map_case` in `api/integrations/uniteus/mappers.py`).

**Endpoint:** `GET https://core.uniteus.io/v1/service_authorizations/{id}`
(client method `UniteUsClient.get_service_authorization`). List variant:
`GET /v1/service_authorizations?filter[case]={case_id}`.

## Captured sample

Case `0ca9c4d3-0bd5-4fad-83f1-48fa4ab2208b`, auth
`4615f34f-4322-47ec-8e0e-586915d764d1` (auto-approved, MTM):

```json
{
  "data": {
    "id": "4615f34f-4322-47ec-8e0e-586915d764d1",
    "type": "service_authorization",
    "attributes": {
      "approved_cents": 873600,
      "approved_ends_at": "2027-01-16T00:00:00.000Z",
      "approved_starts_at": "2026-07-16T00:00:00.000Z",
      "approved_unit_amount": null,
      "auto_approved": true,
      "created_at": "2026-07-15T19:45:23.423Z",
      "adjudicator_note": "This authorization was automatically accepted.",
      "in_review_note": null,
      "payer_authorization_number": null,
      "requested_cents": 873600,
      "requested_ends_at": "2027-01-16T00:00:00.000Z",
      "requested_starts_at": "2026-07-16T00:00:00.000Z",
      "requested_unit_amount": null,
      "short_id": "26NQ0TSLMA",
      "state": "approved",
      "submitted_at": "2026-07-15T19:45:23.431Z",
      "update_request_note": null,
      "updated_at": "2026-07-15T19:45:23.423Z",
      "urgent": false
    },
    "relationships": {
      "assignee": { "data": null },
      "case": { "data": { "id": "0ca9c4d3-0bd5-4fad-83f1-48fa4ab2208b", "type": "case" } },
      "currently_approved_auth": { "data": { "id": "4615f34f-4322-47ec-8e0e-586915d764d1", "type": "service_authorization" } },
      "previously_approved_auth": { "data": null },
      "fee_schedule_program": { "data": { "id": "c601b023-5ce5-4f9c-aaf3-e124b140e75c", "type": "fee_schedule_program" } },
      "insurance": { "data": { "id": "e42771f3-f851-4a4e-873e-c2965c72195f", "type": "insurance" } },
      "original_service_authorization": { "data": { "id": "4615f34f-4322-47ec-8e0e-586915d764d1", "type": "service_authorization" } },
      "person": { "data": { "id": "c5d0c921-63c3-439d-9347-fbc19377c542", "type": "person" } },
      "requester": { "data": { "id": "ddcd169d-0a57-40fc-9c8e-39f0c7988fef", "type": "employee" } },
      "adjudicator": { "data": null },
      "service_authorization_denial_reason": { "data": null },
      "service_authorization_edit_reason": { "data": null },
      "service_authorization_review_reason": { "data": null },
      "fee_schedule_program_configuration": { "data": { "id": "0f9bce4d-b166-4b80-910f-0e7d6860850d", "type": "fee_schedule_program_configuration" } },
      "zcodes": { "data": [] },
      "clinical_modifications": { "data": [] }
    }
  }
}
```

## Attributes — full inventory

| Field | Type | Ingested? | Maps to (Case) |
| --- | --- | --- | --- |
| `state` | string | yes | `service_authorization_status` (+ `_status_label`) |
| `short_id` | string | yes | `unite_us_authorization_id` |
| `approved_cents` | int | yes | `authorized_amount` (÷100, formatted) |
| `requested_cents` | int | yes | `service_authorization_requested_amount` (÷100) |
| `approved_starts_at` | datetime | yes | `service_authorization_approval_starts_at` |
| `approved_ends_at` | datetime | yes | `service_authorization_approval_ends_at` |
| `requested_starts_at` | datetime | yes | `service_authorization_request_starts_at` |
| `requested_ends_at` | datetime | yes | `service_authorization_request_ends_at` |
| `adjudicator_note` | string | yes | `service_authorization_decision_note` ("Decision Note") |
| `in_review_note` | string | yes | `service_authorization_in_review_note` |
| `update_request_note` | string | yes | `service_authorization_update_request_note` |
| `payer_authorization_number` | string | yes | `payer_authorization_number` |
| `submitted_at` | datetime | yes | `service_authorization_submitted_at` |
| `auto_approved` | bool | yes | `service_authorization_auto_approved` |
| `urgent` | bool | yes | `service_authorization_urgent` |
| `approved_unit_amount` | int | yes | `authorized_units` (case level; falls back to `requested_unit_amount`) |
| `requested_unit_amount` | int | yes | fallback for `authorized_units` |
| `created_at` | datetime | no | — (auth's own create time; case uses case `created_at`) |
| `updated_at` | datetime | no | — |

## Relationships — full inventory

| Relationship | Ingested? | Notes |
| --- | --- | --- |
| `case` | implied | the auth is fetched per case |
| `person` | implied | same as case `person` |
| `insurance` | no | payer insurance id — candidate if we link auth → insurance |
| `requester` | no | employee who requested the auth |
| `adjudicator` | no | employee who decided (null when auto-approved) |
| `service_authorization_denial_reason` | yes | **coded denial reason on DENIED auths** → `service_authorization_denial_reason_id` + resolved `service_authorization_denial_reason` name (via `/service_authorization_denial_reasons/{id}`) |
| `service_authorization_edit_reason` | no | coded reason for an edit |
| `service_authorization_review_reason` | no | coded reason it went to review |
| `fee_schedule_program` | no | fee schedule program id |
| `fee_schedule_program_configuration` | no | fee schedule config id |
| `currently_approved_auth` | no | points at the active auth (self here) |
| `previously_approved_auth` | no | prior approved auth in an amendment chain |
| `original_service_authorization` | no | root auth of an amendment chain |
| `assignee` | no | assigned employee |
| `zcodes` | no | Z-code list (empty here) |
| `clinical_modifications` | no | clinical modifier list (empty here) |

> **Denial-reason name resolution is best-effort:** the reason record's attribute
> name isn't confirmed, so the resolver tries `name` / `display_name` /
> `description` / `reason` and degrades to blank (the id is still stored). Verify
> against a real **denied** authorization payload and tighten if needed.
