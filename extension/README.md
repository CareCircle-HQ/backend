# Unite Us Client Workflow — Chrome Extension

Captures the `client_id` from a Unite Us facesheet URL, validates it against the
backend API, and embeds the agent forms with the **Enrollment Platform Member ID**
field pre-filled. The Verification and SDUF forms unlock only after the client is
validated.

## Features

1. **URL trigger** — activates on `https://app.uniteus.io/*`; extracts `client_id`
   from `…/facesheet/<client_id>` and also `case_id` / `screening_id` from combined
   URLs.
2. **Client detection** — content script reads IDs from the URL plus a best-effort
   scrape of name/DOB, and stores them for the side panel.
3. **Backend validation** — the side panel calls `GET /api/clients/<client_id>/`
   and shows ✅ valid / ❌ not found / session errors.
4. **Embedded forms** — three agent forms rendered in iframes inside the side panel:
   - Screening & Eligibility (always available)
   - Verification (unlocked after validation)
   - SDUF (unlocked after validation)
5. **Auto-fill** — a content script running inside the form iframe fills the
   *Enrollment Platform Member ID* field with the captured `client_id`.

## File layout

```
extension/
  manifest.json            MV3 manifest
  background.js            service worker (side panel enable/open)
  content/
    uniteus.js             captures IDs from the Unite Us page
    formfill.js            fills the member ID field inside the forms
  sidepanel/
    sidepanel.html|css|js  side panel UI + validation + form tabs
  rules/
    rules.json             strips X-Frame-Options/CSP so forms can be iframed
```

## Install (developer mode)

1. Open `chrome://extensions`, enable **Developer mode**.
2. Click **Load unpacked** and select the `extension/` folder.
3. Pin the extension. Navigate to a Unite Us facesheet, then click the toolbar
   icon to open the side panel.

## Configure the backend

1. In the side panel, click the ⚙ icon.
2. Set the **API base URL** (default `http://127.0.0.1:8000`).
3. Enter your username/password and click **Connect** (uses `/api/auth/token/`,
   stores the JWT access token in `chrome.storage.local`).

## How forms are embedded

`scnlp.metcouncil.org` normally blocks framing via `X-Frame-Options` /
`Content-Security-Policy: frame-ancestors`. The `rules/rules.json` declarativeNetRequest
ruleset removes those response headers for sub-frame requests to that domain so the
forms can render inside the side panel iframe.

## Notes & limitations

- Token validation requires the backend running and reachable from the browser.
- The Unite Us name/DOB scrape is heuristic (their DOM is not guaranteed); the flow
  works with `client_id` alone.
- The member-ID field is matched by label/placeholder/aria text containing
  "Enrollment Platform Member ID". If the live form uses different wording, adjust
  `TARGET_LABEL` in `content/formfill.js`.
- Header stripping via DNR is required for iframing third-party forms; this only
  applies to the configured form domain.
