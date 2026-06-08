# CareCircle Documentation

CareCircle is a **Chrome Extension + Django REST API** that bridges [Unite Us](https://uniteus.com/) (a social care coordination platform) with the [GoHighLevel](https://www.gohighlevel.com/) CRM for **Met Council - SCN - PHS** care coordinators. The Chrome extension captures client, case, and screening data directly from the Unite Us facesheet (via DOM scraping plus authenticated calls to the Unite Us core API), compares it against records already stored in the Django backend, and lets a coordinator save/upsert that data and pre-fill embedded enrollment forms — all without leaving the Unite Us tab.

## Documentation index

| Doc | Description |
|---|---|
| [setup.md](./setup.md) | Getting-started guide: backend, extension, and environment variables. |
| [architecture.md](./architecture.md) | System architecture, component diagram, and end-to-end data flow. |
| [chrome-extension.md](./chrome-extension.md) | Extension overview: manifest, file layout, background worker, config, DNR rules. |
| [content-scripts.md](./content-scripts.md) | Deep dive on the three content scripts (`uw_netcapture.js`, `uniteus.js`, `formfill.js`). |
| [sidepanel.md](./sidepanel.md) | Side panel UI: tabs, auth, gating, schema-driven comparison, save flow, dev tools. |
| [django-api.md](./django-api.md) | REST API reference: auth, resource endpoints, bulk upsert, filtering. |
| [data-models.md](./data-models.md) | All Django models, the ER diagram, and key enumerations. |
| [etl-import.md](./etl-import.md) | Data ingestion: CSV import, GoHighLevel CRM client, service-token command. |
| [authentication.md](./authentication.md) | Auth and security: JWT, DRF tokens, extension auth flow, CORS, PII/PHI notes. |
| [feature-roadmap.md](./feature-roadmap.md) | Known TODOs, planned features, and current limitations. |
| [known-issues.md](./known-issues.md) | Operational troubleshooting guide. |

## Quick start

New here? Start with **[setup.md](./setup.md)** to get the backend running and the
extension loaded, then read **[architecture.md](./architecture.md)** for the big picture.
