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
  const U = UUID_RE.source;
  const grab = (re) => {
    const m = path.match(re);
    return m ? m[1].toLowerCase() : null;
  };

  // Client ID:
  //   facesheet:   /facesheet/<client_id>
  //   eligibility: /facesheet/<client_id>/eligibility
  //   case view:   /dashboard/cases/open/<case_id>/contact/<client_id>
  ids.client_id =
    grab(new RegExp(`/facesheet/(${U})`, "i")) ||
    grab(new RegExp(`/contact/(${U})`, "i"));

  // Case ID: /dashboard/cases/<status>/<case_id>/...
  ids.case_id =
    grab(new RegExp(`/cases?/[^/]+/(${U})`, "i")) ||
    grab(new RegExp(`/cases?/(${U})`, "i"));

  // Screening ID: /screenings/v2/facesheet/<client_id>/submission/<screening_id>
  ids.screening_id =
    grab(new RegExp(`/submission/(${U})`, "i")) ||
    grab(new RegExp(`/screenings?/(${U})`, "i"));

  // Fallback: if still no client_id, take the first UUID in the path.
  if (!ids.client_id) {
    const all = path.match(UUID_RE_G);
    if (all && all.length) ids.client_id = all[0].toLowerCase();
  }
  return ids;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const cleanText = (s) => (s || "").replace(/\s+/g, " ").trim();

// Facesheet navigation tabs we walk to reveal hidden sections. These are safe,
// read-only views. We intentionally do NOT click arbitrary aria-expanded toggles
// because on Unite Us those include action buttons (Upload, Assign, Take Action)
// that must never be triggered automatically.
const FACESHEET_TABS = [
  "Overview",
  "Profile",
  "Cases",
  "Screenings",
  "Eligibility Assessments",
  "Forms",
  "Uploads",
  "Referrals",
  "Resources",
];

// Facesheet tabs that render a table of records, mapped to our record type.
const TAB_RECORD_TYPE = {
  Cases: "case",
  Screenings: "screening",
  "Eligibility Assessments": "eligibility",
};

function getFacesheetTabs() {
  return [...document.querySelectorAll('[role="tab"]')].filter((t) =>
    FACESHEET_TABS.includes(cleanText(t.innerText))
  );
}

function clickTabByLabel(label) {
  const tab = getFacesheetTabs().find((t) => cleanText(t.innerText) === label);
  if (tab) {
    try {
      tab.click();
      return true;
    } catch (_) {}
  }
  return false;
}

// ---------------------------------------------------------------------------
// Harvest label -> value pairs from the current DOM state.
// ---------------------------------------------------------------------------
function harvestFields(into) {
  const add = (label, value) => {
    label = cleanText(label);
    value = cleanText(value);
    if (!label || !value || label === value) return;
    if (label.length > 80 || value.length > 500) return;
    if (!(label in into)) into[label] = value;
  };

  // Definition lists: <dt>/<dd>
  document.querySelectorAll("dl").forEach((dl) => {
    const dts = dl.querySelectorAll("dt");
    const dds = dl.querySelectorAll("dd");
    dts.forEach((dt, i) => dds[i] && add(dt.textContent, dds[i].textContent));
  });

  // Table rows: <th>/<td>
  document.querySelectorAll("tr").forEach((tr) => {
    const th = tr.querySelector("th");
    const td = tr.querySelector("td");
    if (th && td) add(th.textContent, td.textContent);
  });

  // Form fields, resolved to their label / aria-label / name / placeholder
  document.querySelectorAll("input, select, textarea").forEach((el) => {
    if (el.type === "password" || el.type === "hidden") return;
    let label = "";
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) label = l.textContent;
    }
    if (!label && el.closest("label")) label = el.closest("label").textContent;
    if (!label)
      label =
        el.getAttribute("aria-label") ||
        el.getAttribute("name") ||
        el.getAttribute("placeholder") ||
        "";
    let val = el.value;
    if (el.tagName === "SELECT" && el.selectedOptions && el.selectedOptions[0]) {
      val = el.selectedOptions[0].textContent;
    }
    if (el.type === "checkbox" || el.type === "radio") {
      if (!el.checked) return;
      val = val || "checked";
    }
    add(label, val);
  });

  // Generic "label" elements followed by a value sibling
  document
    .querySelectorAll("[class*='label'], [data-testid*='label']")
    .forEach((lab) => {
      const next = lab.nextElementSibling;
      if (next) add(lab.textContent, next.textContent);
    });

  return into;
}

// Scan anchor hrefs for record IDs (a client can have many cases / screenings /
// eligibility records). Each record also captures the visible row text as a
// best-effort summary of its details.
function collectRecords(map) {
  const U = UUID_RE.source;
  const pats = [
    { type: "case", re: new RegExp(`/cases?/[^/]+/(${U})`, "i") },
    { type: "screening", re: new RegExp(`/submission/(${U})`, "i") },
    { type: "eligibility", re: new RegExp(`eligibilit(?:y|ies)[^?#]*?(${U})`, "i") },
  ];
  document.querySelectorAll("a[href]").forEach((a) => {
    const href = a.getAttribute("href") || "";
    for (const p of pats) {
      const m = href.match(p.re);
      if (!m) continue;
      const id = m[1].toLowerCase();
      const key = `${p.type}:${id}`;
      if (!map.has(key)) {
        const row =
          a.closest(
            'tr, li, [role="row"], [class*="row"], [class*="card"], [class*="item"]'
          ) || a.parentElement;
        const summary = cleanText(row ? row.innerText : a.innerText).slice(0, 300);
        map.set(key, { type: p.type, id, href, summary });
      }
      break;
    }
  });
}

// Parse visible record tables (Cases / Screenings / Eligibility) into one record
// per row, capturing every column keyed by its header.
function harvestTableRecords(type, into) {
  document.querySelectorAll("table").forEach((table) => {
    if (table.offsetParent === null) return; // skip hidden tables

    let headers = [...table.querySelectorAll("thead th")].map((th) =>
      cleanText(th.innerText)
    );
    if (!headers.length) {
      headers = [...table.querySelectorAll("tr th")].map((th) =>
        cleanText(th.innerText)
      );
    }

    let rows = [...table.querySelectorAll("tbody tr")];
    if (!rows.length) rows = [...table.querySelectorAll("tr")];

    rows.forEach((tr) => {
      const cells = [...tr.children].filter((c) => c.tagName === "TD");
      if (!cells.length) return; // header / empty row

      const fields = {};
      cells.forEach((td, i) => {
        const key = (headers[i] && headers[i].trim()) || `Column ${i + 1}`;
        const val = cleanText(td.innerText);
        if (val) fields[key] = val;
      });
      if (!Object.keys(fields).length) return;

      let id = null;
      let href = null;
      const a = tr.querySelector("a[href]");
      if (a) {
        href = a.getAttribute("href");
        const m = href.match(UUID_RE);
        if (m) id = m[0].toLowerCase();
      }

      const summary = Object.values(fields).join(" \u2022 ").slice(0, 300);
      const key = `${type}:${id || summary}`;
      if (!into.has(key)) into.set(key, { type, id, href, fields, summary });
    });
  });
}

// Light scrape harvests whatever is currently visible (Overview header gives
// name/DOB/TEL/ADDRESS). Deep scrape additionally walks each facesheet tab and
// collects every case / screening / eligibility record it finds.
async function scrapePage(deep) {
  const pairs = {};
  const recordMap = new Map();
  harvestFields(pairs);
  collectRecords(recordMap);
  if (!deep) return { pairs, records: [...recordMap.values()] };

  const labels = getFacesheetTabs().map((t) => cleanText(t.innerText));
  for (const label of labels) {
    if (label === "Overview") continue;
    if (!clickTabByLabel(label)) continue;
    await sleep(700); // let the tab's content load
    harvestFields(pairs);

    const type = TAB_RECORD_TYPE[label];
    if (type) {
      const before = recordMap.size;
      harvestTableRecords(type, recordMap);
      // The table data loads asynchronously; if nothing appeared yet, wait once.
      if (recordMap.size === before) {
        await sleep(900);
        harvestTableRecords(type, recordMap);
      }
    }
    collectRecords(recordMap);
  }

  // Restore the user's view to the Overview tab.
  clickTabByLabel("Overview");
  await sleep(150);
  collectRecords(recordMap);
  return { pairs, records: [...recordMap.values()] };
}

// Strip UI affordance text that gets concatenated into the link's innerText
// (e.g. "Copy link", "Remove item", "Edit").
function cleanName(t) {
  // The link's innerText concatenates UI affordances with no separating space,
  // e.g. "Copy linkJUAN FERNANDEZ", so we strip the phrases without requiring a
  // trailing word boundary.
  return cleanText(
    (t || "")
      .replace(/copy\s*link/gi, "")
      .replace(/remove\s*item/gi, "")
  );
}

function getClientName() {
  const sel = [
    '[data-testid="popover-client-profile-link"]',
    '[data-testid*="client-profile-link"]',
    "[data-test-element='client-name']",
    "[data-testid='client-name']",
  ];
  for (const s of sel) {
    const el = document.querySelector(s);
    const t = el && cleanName(el.innerText);
    if (t) return t;
  }
  return "";
}

function parseDob(raw) {
  if (!raw) return "";
  const m = raw.match(/\d{1,2}[\/-]\d{1,2}[\/-]\d{2,4}/);
  return m ? m[0] : cleanText(raw);
}

function findVal(pairs, needles) {
  for (const key of Object.keys(pairs)) {
    const lk = key.toLowerCase();
    if (lk.includes("user")) continue;
    if (needles.some((n) => lk === n || lk.includes(n))) return pairs[key];
  }
  return "";
}

function deriveKnownFields(pairs) {
  // Name: prefer the header client-profile link; else combine First/Last fields
  // (Profile tab); else a generic name field.
  let name = getClientName();
  if (!name) {
    const first = findVal(pairs, ["first name", "legal first"]);
    const last = findVal(pairs, ["last name", "legal last"]);
    if (first || last) name = cleanText(`${first} ${last}`);
  }
  if (!name) {
    name = findVal(pairs, ["full name", "client name", "preferred name", "name"]);
  }

  // DOB header looks like "8/29/1991 (Age 34)" -> keep just the date.
  let dob = parseDob(
    pairs["DOB"] ||
      findVal(pairs, ["date of birth", "birth date", "birthdate", "dob"])
  );

  const phone = pairs["TEL"] || findVal(pairs, ["phone", "tel", "mobile"]);
  const address = pairs["ADDRESS"] || findVal(pairs, ["address"]);

  return {
    name: cleanText(name),
    dob: cleanText(dob),
    phone: cleanText(phone),
    address: cleanText(address),
  };
}

let lastSerialized = "";
let scraping = false;
let lastPublished = null;
let lastRecords = { clientId: null, list: [] };

async function publishContext(deep = false) {
  const ids = parseIdsFromUrl();
  if (!ids.client_id) return; // nothing useful yet
  if (scraping) return;
  scraping = true;
  try {
    const { pairs, records } = await scrapePage(deep);
    const known = deriveKnownFields(pairs);

    // Preserve records gathered by a previous deep scan on the same client so a
    // later light scrape doesn't wipe them.
    let finalRecords = records;
    if (finalRecords.length) {
      lastRecords = { clientId: ids.client_id, list: finalRecords };
    } else if (lastRecords.clientId === ids.client_id) {
      finalRecords = lastRecords.list;
    }
    const idsByType = (t) =>
      finalRecords.filter((r) => r.type === t).map((r) => r.id);

    const ctx = {
      ...ids,
      client_name: known.name,
      client_dob: known.dob,
      client_phone: known.phone,
      client_address: known.address,
      records: finalRecords,
      case_ids: idsByType("case"),
      screening_ids: idsByType("screening"),
      eligibility_ids: idsByType("eligibility"),
      scraped: pairs,
      scraped_count: Object.keys(pairs).length,
      source_url: location.href,
      captured_at: new Date().toISOString(),
    };
    lastPublished = ctx;
    const serialized = JSON.stringify(ctx);
    if (serialized !== lastSerialized) {
      lastSerialized = serialized;
      chrome.storage.local.set({ uw_context: ctx });
    }
  } finally {
    lastHref = location.href;
    scraping = false;
  }
}

// Publish the client_id quickly (before the slower full scrape) so the panel
// can show the detected client immediately.
function publishIdsOnly() {
  const ids = parseIdsFromUrl();
  if (!ids.client_id) return;
  chrome.storage.local.get("uw_context", ({ uw_context }) => {
    if (uw_context && uw_context.client_id === ids.client_id) return;
    chrome.storage.local.set({
      uw_context: {
        ...ids,
        client_name: "",
        client_dob: "",
        scraped: {},
        scraped_count: 0,
        source_url: location.href,
        captured_at: new Date().toISOString(),
      },
    });
  });
}

// Allow the side panel to trigger a fresh scrape on demand.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "RESCRAPE") {
    lastSerialized = ""; // force re-publish
    publishContext(true).then(() => sendResponse({ ok: true }));
    return true; // async response (deep scrape walks all tabs)
  }
});

function coreReady() {
  return !!(
    lastPublished &&
    lastPublished.client_name &&
    lastPublished.client_dob
  );
}

// The facesheet renders asynchronously (data arrives via API after load), so we
// retry the light header scrape a few times until the core fields show up.
function scheduleLightScrapes() {
  const delays = [600, 1500, 2500, 4000, 6000, 9000, 13000, 18000];
  delays.forEach((d) =>
    setTimeout(() => {
      if (!coreReady() && !scraping) publishContext(false);
    }, d)
  );
}

// Initial run: light header scrape (no tab walking) so validation can proceed.
publishIdsOnly();
scheduleLightScrapes();

// Re-scan on SPA navigation (Unite Us is a single-page app).
let lastHref = location.href;
setInterval(() => {
  if (scraping) return; // ignore route changes we trigger ourselves
  if (location.href !== lastHref) {
    lastHref = location.href;
    lastPublished = null;
    publishIdsOnly();
    scheduleLightScrapes();
  }
}, 1000);
