# Chrome Extension

The extension ("Unite Us Client Workflow", Manifest V3) lives in `extension/`. It runs
inside the Unite Us web app, captures client/case/screening data, validates it against
the [Django API](./django-api.md), and embeds enrollment forms in the side panel. This
page covers the manifest, file layout, background worker, config, and the
declarativeNetRequest rules. For the scrapers and the UI, see
[content-scripts.md](./content-scripts.md) and [sidepanel.md](./sidepanel.md).

## Manifest summary (`extension/manifest.json`)

- **Manifest V3**, name "Unite Us Client Workflow", version `0.1.0`.
- **Permissions:** `storage`, `sidePanel`, `scripting`, `declarativeNetRequest`.
- **Host permissions:**
  - `https://app.uniteus.io/*`
  - `https://screenings-ingestion.uniteus.io/*`
  - `https://core.uniteus.io/*`
  - `https://scnlp.metcouncil.org/*`
  - `https://links.carecirclecs.com/*`
  - `http://127.0.0.1:8000/*`
  - `http://localhost:8000/*`
- **Background:** `background.js` (service worker).
- **Action:** toolbar button, title "Open Client Workflow".
- **Side panel:** default path `sidepanel/sidepanel.html`.
- **Content scripts:**
  | Script | Matches | `run_at` | World / frames |
  |---|---|---|---|
  | `content/uw_netcapture.js` | `https://app.uniteus.io/*` | `document_start` | `MAIN` world |
  | `content/uniteus.js` | `https://app.uniteus.io/*` | `document_idle` | isolated (default) |
  | `content/formfill.js` | `https://scnlp.metcouncil.org/agentforms/*`, `https://links.carecirclecs.com/widget/form/*` | `document_idle` | all frames |
- **DNR ruleset:** `rules/rules.json` (resource id `allow_iframe`, enabled).

## File layout

| File | Description |
|---|---|
| `manifest.json` | MV3 manifest: permissions, host permissions, content scripts, DNR ruleset. |
| `background.js` | Service worker — enables/opens the side panel on Unite Us tabs. |
| `config.example.js` | Template for `config.js`; defines `window.EXT_CONFIG` (backend URL + baked token). |
| `config.js` | Real config with the service token. **Gitignored** — created by you locally. |
| `content/uw_netcapture.js` | MAIN-world shim that captures Unite Us auth headers. |
| `content/uniteus.js` | Main scraper: URL IDs, DOM harvest, API enrichment, auto-walk crawler. |
| `content/formfill.js` | Fills the "Enrollment Platform Member ID" field in form iframes. |
| `sidepanel/sidepanel.html` | Side panel markup (8 tabs). |
| `sidepanel/sidepanel.css` | Side panel styles. |
| `sidepanel/sidepanel.js` | Side panel logic: detection, gating, comparison, save, dev tools. |
| `rules/rules.json` | declarativeNetRequest rule stripping framing headers for the form domains. |
| `README.md` | Extension-specific readme. |

## `background.js`

The MV3 service worker:

- Enables or disables the side panel for a tab based on whether the active tab's hostname
  is `app.uniteus.io` (`refreshSidePanel()` listens on `chrome.tabs.onUpdated` and
  `chrome.tabs.onActivated`).
- Opens the panel on toolbar-icon click via
  `chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true })`.
- Keeps a `chrome.runtime.onMessage` `PING` handler for future use / debugging.

## `config.js` / `config.example.js`

The baked service-token config. `config.example.js` is checked in as a template;
`config.js` is gitignored so the token is never committed. It defines:

```js
window.EXT_CONFIG = {
  backendUrl: "http://127.0.0.1:8000",
  apiToken: "<paste service token here>",
  authScheme: "Token",                    // sent as: Authorization: Token <apiToken>
};
```

`config.js` is loaded before `sidepanel.js`, which reads `window.EXT_CONFIG` as the
fallback auth source. Generate the token with
`python manage.py create_service_token --staff` (see
[etl-import.md](./etl-import.md#service-token-create_service_tokenpy)).

## DNR rules (`rules/rules.json`)

The declarativeNetRequest ruleset removes the `x-frame-options`,
`content-security-policy`, and `content-security-policy-report-only` response headers for
**sub-frame** requests to `scnlp.metcouncil.org` and `links.carecirclecs.com`. Those
sites normally block being framed; stripping the headers lets their forms render inside
the side panel iframe. The single rule uses `action.type = "modifyHeaders"` with
`condition.resourceTypes = ["sub_frame"]`.
