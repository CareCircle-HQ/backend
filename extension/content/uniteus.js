// Content script for https://app.uniteus.io/*
// Extracts the client_id (and optional case_id / screening_id) from the URL,
// makes a best-effort attempt to read the client's name and DOB from the page,
// then stores the context so the side panel and the form-fill script can use it.

const UUID_RE =
  /[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/;
const UUID_RE_G = new RegExp(UUID_RE.source, "gi");

function parseIdsFromUrl() {
  const path = location.pathname;
  const ids = { client_id: null, case_id: null, screening_id: null };

  // Primary pattern: /facesheet/<client_id>
  const faceMatch = path.match(/\/facesheet\/(\b[0-9a-fA-F-]{36}\b)/);
  if (faceMatch) ids.client_id = faceMatch[1].toLowerCase();

  // Keyword-based extraction for combined URLs the user may navigate to.
  const keyword = (kw, field) => {
    const re = new RegExp(`${kw}s?\\/(${UUID_RE.source})`, "i");
    const m = path.match(re);
    if (m) ids[field] = m[1].toLowerCase();
  };
  keyword("case", "case_id");
  keyword("screening", "screening_id");
  keyword("screen", "screening_id");

  // Fallback: if no client_id yet, take the first UUID in the path.
  if (!ids.client_id) {
    const all = path.match(UUID_RE_G);
    if (all && all.length) ids.client_id = all[0].toLowerCase();
  }
  return ids;
}

function textFromSelectors(selectors) {
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el && el.textContent && el.textContent.trim()) {
      return el.textContent.trim();
    }
  }
  return "";
}

// Best-effort scrape of name / DOB. Unite Us markup is not guaranteed, so these
// are heuristics and may be empty; the side panel works with client_id alone.
function scrapeClientDetails() {
  const name = textFromSelectors([
    "[data-test-element='client-name']",
    "[data-testid='client-name']",
    "h1[class*='name']",
    "header h1",
  ]);

  let dob = "";
  const bodyText = document.body ? document.body.innerText : "";
  const dobMatch = bodyText.match(
    /(?:DOB|Date of Birth)[:\s]*([0-1]?\d[\/-][0-3]?\d[\/-]\d{2,4})/i
  );
  if (dobMatch) dob = dobMatch[1];

  return { name, dob };
}

function buildContext() {
  const ids = parseIdsFromUrl();
  const details = scrapeClientDetails();
  return {
    ...ids,
    client_name: details.name,
    client_dob: details.dob,
    source_url: location.href,
    captured_at: new Date().toISOString(),
  };
}

let lastSerialized = "";

function publishContext() {
  const ctx = buildContext();
  if (!ctx.client_id) return; // nothing useful to publish
  const serialized = JSON.stringify(ctx);
  if (serialized === lastSerialized) return;
  lastSerialized = serialized;
  chrome.storage.local.set({ uw_context: ctx });
}

// Initial publish + observe SPA navigation (Unite Us is a single-page app).
publishContext();

const observer = new MutationObserver(() => publishContext());
if (document.body) {
  observer.observe(document.body, { childList: true, subtree: true });
}

// Also re-check on history navigation.
window.addEventListener("popstate", publishContext);
let lastHref = location.href;
setInterval(() => {
  if (location.href !== lastHref) {
    lastHref = location.href;
    publishContext();
  }
}, 1000);
