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

// ---------------------------------------------------------------------------
// Profile tab: extract clean, structured field values keyed by backend field
// names. The profile page exposes read-only "<field>-display" elements
// (data-test-element) plus well-labeled sections, which are far more reliable
// than the generic label/value harvest (that one also picks up edit-mode
// dropdown option lists). Returns { client, address, insurance } or null when
// the profile content isn't present (so other pages don't clobber the cache).
// ---------------------------------------------------------------------------
function displayValue(name) {
  const el = document.querySelector(`[data-test-element="${name}"]`);
  return el ? cleanText(el.innerText) : "";
}

function sectionTextByTitle(title) {
  const h = [...document.querySelectorAll("h1,h2,h3,h4")].find(
    (el) => cleanText(el.innerText) === title
  );
  if (!h) return "";
  const c = h.closest("section,article,div") || h.parentElement;
  return c ? cleanText(c.innerText) : "";
}

function harvestProfile() {
  const hasProfile =
    document.querySelector('[data-test-element="fname-display"]') ||
    [...document.querySelectorAll("h2,h3")].some(
      (h) => cleanText(h.innerText) === "Personal Information"
    );
  if (!hasProfile) return null;

  // "--" is the page's placeholder for an empty value.
  const norm = (v) => {
    v = cleanText(v);
    return v === "--" ? "" : v;
  };

  const client = {};
  client.first_name = norm(displayValue("fname-display"));
  client.last_name = norm(displayValue("lname-display"));
  client.middle_name = norm(displayValue("mname-display"));
  client.date_of_birth = parseDob(displayValue("dob-display"));
  client.citizenship = norm(displayValue("citizenship-display"));
  client.race = norm(displayValue("race-display"));
  client.ethnicity = norm(displayValue("ethnicity-display"));
  client.gender = norm(displayValue("gender-display"));
  client.sexuality = norm(displayValue("sexuality-display"));
  client.sexuality_other = norm(displayValue("sexuality-other-display"));
  client.marital_status = norm(displayValue("marital-display"));
  client.gross_monthly_income = norm(displayValue("monthly-income-display"));
  // Fields below have no dedicated display element. We still declare them (""),
  // which suppresses the edit-mode garbage the generic harvester surfaces, then
  // fill from the labeled section text where possible.
  client.suffix = "";
  client.household_size = "";
  client.consent_status = "";
  client.consented_at = "";
  client.preferred_spoken_language = "";
  client.preferred_written_language = "";
  client.client_phone_number = "";
  client.phone_type = "";
  client.client_email_address = "";
  client.care_coordinator = "";

  let m;
  // Contact Requirements: "Preferred Languages Spoken: English Written: English"
  const contact = sectionTextByTitle("Contact Requirements");
  m = contact.match(/Spoken:\s*([A-Za-z ,]+?)\s*(?:Written:|Methods|Times|Contact Notes|$)/i);
  if (m) client.preferred_spoken_language = cleanText(m[1]);
  m = contact.match(/Written:\s*([A-Za-z ,]+?)\s*(?:Methods|Times|Contact Notes|$)/i);
  if (m) client.preferred_written_language = cleanText(m[1]);

  // Informed Consent: "Consent Status Consent Accepted ... Received on 10/17/2025"
  const consent = sectionTextByTitle("Informed Consent");
  m = consent.match(/Consent Status\s*(.+?)\s*(?:Download|Received on|$)/i);
  if (m) client.consent_status = cleanText(m[1]);
  m = consent.match(/Received on\s*([0-9/\-]+)/i);
  if (m) client.consented_at = cleanText(m[1]);

  // Household Information: "Household Size <n> ..."
  const household = sectionTextByTitle("Household Information");
  m = household.match(/Household Size\s*(\d+)/i);
  if (m) client.household_size = m[1];

  // Contact Information: primary phone / email / primary address.
  const ci = sectionTextByTitle("Contact Information");
  m = ci.match(/([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})/);
  if (m) client.client_email_address = m[1];
  m = ci.match(/Phone\s+(\w+)\s*\([^)]*primary[^)]*\)\s*([\d()+\-. ]{7,})/i);
  if (m) {
    client.phone_type = cleanText(m[1]);
    client.client_phone_number = cleanText(m[2]);
  }

  // Care Coordinator (Care Team section): name follows the "Edit" affordance.
  const careTeam =
    sectionTextByTitle("Care Team") || sectionTextByTitle("Care Coordinator");
  m = careTeam.match(/Care Coordinator\s*Edit\s*([A-Za-z'.-]+(?:\s+[A-Za-z'.-]+){1,2})/);
  if (m && !/None Assigned/i.test(m[1])) client.care_coordinator = cleanText(m[1]);

  // Primary address from Contact Information:
  //   "Address mailing (primary) 8-13 ASTORIA BLVD ASTORIA, NY 11102 county Queens County ..."
  const address = {
    address_type: "", line1: "", line2: "", city: "",
    county: "", state: "", postal_code: "",
  };
  const am = ci.match(
    /Address\s+(\w+)\s*\([^)]*primary[^)]*\)\s*(.+?)\s+county\s+([A-Za-z ]+County)/i
  );
  if (am) {
    address.address_type = cleanText(am[1]);
    address.county = cleanText(am[3]);
    const chunk = cleanText(am[2]); // "8-13 ASTORIA BLVD ASTORIA, NY 11102"
    const cm = chunk.match(/^(.+)\s+([A-Za-z.'-]+),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)/);
    if (cm) {
      address.line1 = cleanText(cm[1]);
      address.city = cleanText(cm[2]);
      address.state = cm[3];
      address.postal_code = cm[4];
    } else {
      address.line1 = chunk;
    }
  }

  return { client, address, insurance: harvestInsurance() };
}

// Insurance Information + Social Care Coverage. Each saved record renders as a
// repeated card:
//   - Insurance:            div[data-testid="payments-profile-view"]
//   - Social Care Coverage: div[data-testid="social-insurance-profile-view"]
// Plan Name / Start Date / End Date have value-level testids; Member ID and
// Group ID are label <p> + value <p> pairs; SCC Status is its own testid.
// We keep only the records the workflow cares about:
//   - Insurance:            End Date >= today OR "--" (no expiration).
//   - Social Care Coverage: Status == Enrolled AND (End Date >= today OR "--").
function harvestInsurance() {
  const out = [];
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const endDateOk = (raw) => {
    const v = cleanText(raw);
    if (!v || v === "--") return true; // no expiration
    const d = new Date(v);
    return isNaN(d.getTime()) ? false : d >= today;
  };

  // Read the value testid's text.
  const valOf = (root, testid) => {
    const el = root.querySelector(`[data-testid="${testid}"]`);
    return el ? cleanText(el.textContent) : "";
  };

  // Member ID / Group ID have no value-level testid: they render as a caption
  // <p> (the "italic" label style) followed by a value <p>. We match the label,
  // then take the value from (1) the next <p> sibling, or (2) the non-label <p>
  // within the same container — whichever exists.
  const labelValue = (root, label) => {
    const ps = [...root.querySelectorAll("p")];
    for (const p of ps) {
      if (cleanText(p.textContent).toLowerCase() !== label.toLowerCase()) continue;
      const next = p.nextElementSibling;
      if (next && next.tagName === "P") {
        const v = cleanText(next.textContent);
        if (v) return v;
      }
      const parent = p.parentElement;
      if (parent) {
        const valEl = [...parent.querySelectorAll("p")].find(
          (x) => x !== p && !/italic/.test(x.className || "") && cleanText(x.textContent)
        );
        if (valEl) return cleanText(valEl.textContent);
      }
    }
    return "";
  };

  // Insurance records.
  [...document.querySelectorAll('[data-testid="payments-profile-view"]')].forEach(
    (root) => {
      const rec = {
        group: "insurance",
        plan_name: valOf(root, "insPlanNameValue"),
        member_id: labelValue(root, "Member ID"),
        group_id: labelValue(root, "Group ID"),
        start_date: valOf(root, "insPlanStartDateValue"),
        end_date: valOf(root, "insPlanEndDateValue"),
        status: "",
      };
      if (rec.plan_name && endDateOk(rec.end_date)) out.push(rec);
    }
  );

  // Social Care Coverage records.
  [...document.querySelectorAll('[data-testid="social-insurance-profile-view"]')].forEach(
    (root) => {
      const rec = {
        group: "social_care_coverage",
        plan_name: valOf(root, "sccPlanNameValue"),
        member_id: labelValue(root, "Member ID"),
        group_id: labelValue(root, "Group ID"),
        start_date: valOf(root, "sccPlanStartDateValue"),
        end_date: valOf(root, "sccPlanEndDateValue"),
        status: valOf(root, "sccEnrollStatus"),
      };
      if (rec.plan_name && /^enrolled$/i.test(rec.status) && endDateOk(rec.end_date)) {
        out.push(rec);
      }
    }
  );

  return out;
}

// Light scrape harvests whatever is currently visible (Overview header gives
// name/DOB/TEL/ADDRESS). Deep scrape additionally walks each facesheet tab and
// collects every case / screening / eligibility record it finds.
async function scrapePage(deep) {
  const pairs = {};
  const recordMap = new Map();
  harvestFields(pairs);
  collectRecords(recordMap);
  let captured = harvestProfile();
  if (!deep) return { pairs, records: [...recordMap.values()], captured };

  const labels = getFacesheetTabs().map((t) => cleanText(t.innerText));
  for (const label of labels) {
    if (label === "Overview") continue;
    if (!clickTabByLabel(label)) continue;
    await sleep(700); // let the tab's content load
    harvestFields(pairs);
    if (label === "Profile") {
      const c = harvestProfile();
      if (c) captured = c;
    }

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
  return { pairs, records: [...recordMap.values()], captured };
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

// Per-client accumulator. As the user navigates the facesheet sub-pages
// (Overview, Profile, Cases, Screenings, ...) we union everything captured so
// far for the SAME client_id, so the comparison shows the full picture instead
// of only what's visible on the current page. It is reset when the client_id
// changes and persisted to storage so it survives a full page reload.
let accum = { clientId: null, pairs: {}, records: new Map(), captured: { client: {}, address: {} }, insurance: [] };

function recordKey(r) {
  return `${r.type}:${r.id || r.summary || ""}`;
}

function resetAccum(clientId) {
  accum = { clientId, pairs: {}, records: new Map(), captured: { client: {}, address: {} }, insurance: [] };
}

// Merge a fresh scrape into the accumulator without clobbering existing data.
function mergeIntoAccum(clientId, pairs, records, captured) {
  if (accum.clientId !== clientId) resetAccum(clientId);
  // Fields: keep the first non-empty value; let new scrapes fill gaps and
  // upgrade values that were previously empty/shorter.
  for (const [k, v] of Object.entries(pairs)) {
    if (!v) continue;
    const prev = accum.pairs[k];
    if (!prev || (v.length > prev.length && v.includes(prev))) accum.pairs[k] = v;
  }
  // Records: keep richest version (one with parsed table fields wins).
  records.forEach((r) => {
    const key = recordKey(r);
    const prev = accum.records.get(key);
    if (!prev) accum.records.set(key, r);
    else if (r.fields && Object.keys(r.fields).length &&
             !(prev.fields && Object.keys(prev.fields).length)) {
      accum.records.set(key, { ...prev, ...r });
    }
  });
  // Structured captured profile data: only present when the profile page was
  // scraped. Keep previously-captured non-empty values; declare empty keys so
  // the comparison suppresses edit-mode garbage for fields we explicitly read.
  if (captured) {
    ["client", "address"].forEach((part) => {
      const src = captured[part] || {};
      const dst = accum.captured[part] || (accum.captured[part] = {});
      for (const [k, v] of Object.entries(src)) {
        if (v) dst[k] = v;
        else if (!(k in dst)) dst[k] = "";
      }
    });
    if (Array.isArray(captured.insurance) && captured.insurance.length) {
      accum.insurance = captured.insurance;
    }
  }
}

function persistAccum() {
  chrome.storage.local.set({
    uw_accum: {
      clientId: accum.clientId,
      pairs: accum.pairs,
      records: [...accum.records.values()],
      captured: accum.captured,
      insurance: accum.insurance,
    },
  });
}

// Restore the accumulator from storage (e.g. after a full page reload) so we
// don't lose what was captured on previously-visited sub-pages of this client.
async function restoreAccum(clientId) {
  if (accum.clientId === clientId) return;
  try {
    const { uw_accum } = await chrome.storage.local.get("uw_accum");
    if (uw_accum && uw_accum.clientId === clientId) {
      accum = {
        clientId,
        pairs: { ...uw_accum.pairs },
        records: new Map((uw_accum.records || []).map((r) => [recordKey(r), r])),
        captured: uw_accum.captured || { client: {}, address: {} },
        insurance: uw_accum.insurance || [],
      };
      return;
    }
  } catch (_) {}
  resetAccum(clientId);
}

async function publishContext(deep = false) {
  const ids = parseIdsFromUrl();
  if (!ids.client_id) return; // nothing useful yet
  if (scraping) return;
  scraping = true;
  try {
    await restoreAccum(ids.client_id);
    const { pairs, records, captured } = await scrapePage(deep);
    mergeIntoAccum(ids.client_id, pairs, records, captured);
    persistAccum();

    const mergedPairs = accum.pairs;
    const finalRecords = [...accum.records.values()];
    const known = deriveKnownFields(mergedPairs);
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
      scraped: mergedPairs,
      scraped_count: Object.keys(mergedPairs).length,
      captured: accum.captured,
      insurance: accum.insurance,
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

// ---------------------------------------------------------------------------
// Screenings: filtered list + per-screening detail capture.
//
// The screening detail page lives on a different app bundle
// (/screenings/v2/...), so visiting it is a FULL page navigation rather than an
// in-app route change. We therefore implement the "auto-walk" as a resumable
// crawler whose state lives in chrome.storage.local (uw_scr_scan): each page
// load checks whether a scan is in progress and, if so, harvests the current
// detail page and navigates to the next one until the queue is drained.
// ---------------------------------------------------------------------------
const SCREENING_ORG = "Met Council - SCN - PHS";
const SCREENING_SCAN_TTL_MS = 5 * 60 * 1000; // abandon stale scans

async function waitFor(pred, timeout = 8000, step = 200) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    try {
      if (pred()) return true;
    } catch (_) {}
    await sleep(step);
  }
  return false;
}

const absUrl = (href) => {
  try {
    return new URL(href, location.origin).href;
  } catch (_) {
    return href;
  }
};

// The visible screenings table on /facesheet/<id>/screenings.
function findScreeningTable() {
  return [...document.querySelectorAll("table")].find((t) => {
    if (t.offsetParent === null) return false;
    const heads = [...t.querySelectorAll("th")].map((th) =>
      cleanText(th.innerText).toUpperCase()
    );
    return heads.includes("FORM") && heads.includes("ORGANIZATION");
  });
}

// The table renders its header before its data rows arrive, so we must wait for
// at least one data row before harvesting (otherwise we capture 0 screenings).
function screeningTableReady() {
  const t = findScreeningTable();
  if (!t) return false;
  let rows = [...t.querySelectorAll("tbody tr")];
  if (!rows.length) rows = [...t.querySelectorAll("tr")];
  return rows.some((r) => r.querySelector("td"));
}

// The data rows for the target org. Rows are clickable (<tr role="button">) and
// carry no link/id, so navigation is by clicking the row; we track by index.
function getFilteredScreeningRows() {
  const table = findScreeningTable();
  if (!table) return [];
  let rows = [...table.querySelectorAll("tbody tr")];
  if (!rows.length) {
    rows = [...table.querySelectorAll("tr")].filter((r) => r.querySelector("td"));
  }
  const norm = (s) => cleanText(s).toLowerCase();
  return rows.filter((r) => norm(r.innerText).includes(norm(SCREENING_ORG)));
}

// Parse the screenings table, keeping only rows for the target organization.
function harvestScreeningList() {
  const table = findScreeningTable();
  if (!table) return [];
  let headers = [...table.querySelectorAll("thead th")].map((th) =>
    cleanText(th.innerText)
  );
  if (!headers.length) {
    headers = [...table.querySelectorAll("tr th")].map((th) =>
      cleanText(th.innerText)
    );
  }
  const col = (name) => headers.findIndex((h) => h.toUpperCase() === name);
  const iForm = col("FORM");
  const iSub = col("SUBMITTER");
  const iStatus = col("STATUS");
  const iOrg = col("ORGANIZATION");
  const iDate = col("LAST UPDATED");

  let rows = [...table.querySelectorAll("tbody tr")];
  if (!rows.length) {
    rows = [...table.querySelectorAll("tr")].filter((r) => r.querySelector("td"));
  }

  const out = [];
  rows.forEach((tr) => {
    const cells = [...tr.children].filter((c) => c.tagName === "TD");
    if (!cells.length) return;
    const cell = (i) => (i >= 0 && cells[i] ? cleanText(cells[i].innerText) : "");
    const norm = (s) => cleanText(s).toLowerCase();
    const org = cell(iOrg);
    // Keep only the target org. Match the dedicated column when we found it,
    // otherwise fall back to scanning the whole row text.
    const rowMatches = iOrg >= 0
      ? norm(org).includes(norm(SCREENING_ORG))
      : norm(tr.innerText).includes(norm(SCREENING_ORG));
    if (!rowMatches) return;

    out.push({
      form: cell(iForm),
      submitter: cell(iSub),
      status: cell(iStatus),
      org: cell(iOrg),
      date: cell(iDate),
    });
  });
  return out;
}

// Allowed screening results (case-insensitive match)
const ALLOWED_NEEDS = [
  "housing",
  "social support",
  "food",
  "transportation",
  "unemployment"
];

function normalizeNeed(text) {
  return cleanText(text).toLowerCase().replace(/[^a-z]/g, "");
}

function isAllowedNeed(text) {
  const normalized = normalizeNeed(text);
  return ALLOWED_NEEDS.some(need => normalized.includes(need.replace(/[^a-z]/g, "")));
}

// Best-effort: capture the "Screening Results" section's need cards.
function harvestScreeningResults() {
  const out = [];
  const seen = new Set();

  // Look for need cards with the specific class structure from diagnostic
  document.querySelectorAll(".need-card .need-card__name").forEach((el) => {
    const t = cleanText(el.innerText);
    if (!t || t.length > 80) return;
    if (!isAllowedNeed(t)) return; // only keep the 5 allowed needs
    const low = t.toLowerCase();
    if (seen.has(low)) return;
    seen.add(low);
    out.push(t);
  });

  return out;
}

// Capture one screening detail page: ordered question/answer pairs (excluding
// the client-summary header), plus the screening-results domains.
function harvestScreeningDetail() {
  const m = location.href.match(new RegExp(`submission/(${UUID_RE.source})`, "i"));
  const id = m ? m[1].toLowerCase() : null;

  const items = [];
  const seen = new Set();

  // Primary strategy: Use the specific class selectors from diagnostic data
  // .ui-form-renderer-question-display contains .__label (question) and .__value (answer)
  document.querySelectorAll(".ui-form-renderer-question-display").forEach((container) => {
    const labelEl = container.querySelector(".ui-form-renderer-question-display__label");
    const valueEl = container.querySelector(".ui-form-renderer-question-display__value");

    if (!labelEl) return;

    const q = cleanText(labelEl.innerText);
    // Skip section headers (don't end with ?)
    if (!q || !q.endsWith("?")) return;
    // Skip the "Screening Duration" question - we handle that separately
    if (/screening duration/i.test(q)) return;

    const a = valueEl ? cleanText(valueEl.innerText) : "";
    if (!a) return;

    const key = q.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    items.push({ q, a });
  });

  // Fallback: Try the label/value extraction pattern (from diagnostic's labelValueSample)
  if (items.length === 0) {
    const allText = document.body.innerText || "";
    const lines = allText.split(/\n/).map(l => cleanText(l)).filter(Boolean);

    for (let i = 0; i < lines.length - 1; i++) {
      const line = lines[i];
      if (!line.endsWith("?")) continue;
      if (/screening duration/i.test(line)) continue;

      const nextLine = lines[i + 1];
      if (!nextLine || nextLine.endsWith("?")) continue;

      const key = line.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ q: line, a: nextLine });
    }
  }

  // Extract Screening Duration - look for the number near "Screening Duration" label
  let duration = null;
  document.querySelectorAll(".ui-form-renderer-question-display").forEach((container) => {
    const labelEl = container.querySelector(".ui-form-renderer-question-display__label");
    if (!labelEl) return;
    const labelText = cleanText(labelEl.innerText);
    if (!/screening duration/i.test(labelText)) return;

    // Look for a number in the container text
    const containerText = cleanText(container.innerText);
    const match = containerText.match(/(\d+)/);
    if (match) duration = match[1];
  });

  // Fallback duration extraction
  if (!duration) {
    const durationMatch = document.body.innerText.match(/Screening Duration\s*(\d+)/i);
    if (durationMatch) duration = durationMatch[1];
  }

  return { id, items, results: harvestScreeningResults(), duration };
}

function saveScan(scan) {
  return chrome.storage.local.set({ uw_scr_scan: scan });
}

// Publish the panel-facing view: list rows joined with any captured details.
function publishScreenings(scan) {
  const screenings = scan.list.map((x, i) => ({
    ...x,
    detail: (scan.details && scan.details[i]) || null,
  }));
  chrome.storage.local.set({
    uw_screenings: {
      clientId: scan.clientId,
      org: SCREENING_ORG,
      screenings,
      status: scan.status,
      phase: scan.phase || null,
      note: scan.note || "",
      scannedAt: scan.startedAt,
      finishedAt: scan.finishedAt || null,
      progress: { done: scan.index, total: scan.total || scan.list.length },
    },
  });
}

// Kick off a scan. The Screenings list may not be the current view, so we first
// ensure we're on it (switch tab if on the facesheet, otherwise navigate to the
// list URL). Harvesting + the per-detail walk are driven by
// maybeContinueScreeningScan, which makes the whole flow survive page reloads.
async function startScreeningScan(msg) {
  const clientId = (msg && msg.clientId) || parseIdsFromUrl().client_id;
  if (!clientId) return { ok: false, error: "Open the client's facesheet first" };

  const scan = {
    clientId,
    status: "running",
    phase: "list",
    note: "Loading screenings\u2026",
    startedAt: new Date().toISOString(),
    finishedAt: null,
    list: [],
    total: 0,
    index: 0,
    details: [],
    returnUrl: null,
  };
  await saveScan(scan);
  publishScreenings(scan);

  // Fast path: already on this client's facesheet -> switch to Screenings tab
  // (in-app, no reload) and harvest once the rows have rendered.
  const onClient = parseIdsFromUrl().client_id === clientId;
  if (onClient && getFacesheetTabs().length) {
    clickTabByLabel("Screenings");
    if (await waitFor(() => screeningTableReady(), 9000)) {
      await beginScreeningWalk(scan);
      return { ok: true, count: scan.total };
    }
  }
  // Otherwise (or if the table didn't appear) load the list URL; the crawler
  // resumes and harvests on the next page load.
  location.assign(`${location.origin}/facesheet/${clientId}/screenings`);
  return { ok: true, count: null };
}

// Harvest the filtered list, then start visiting each screening by clicking its
// row (rows are clickable <tr role="button"> with no link/id).
async function beginScreeningWalk(scan) {
  await waitFor(() => screeningTableReady(), 12000);
  const list = harvestScreeningList();
  scan.list = list;
  scan.total = list.length;
  scan.index = 0;
  scan.details = [];
  scan.phase = "detail";
  scan.returnUrl = location.href;
  scan.note = list.length ? "" : "No Met Council - SCN - PHS screenings in the list.";
  await saveScan(scan);
  publishScreenings(scan);

  if (!list.length) return finishScan(scan);
  visitScreeningIndex(scan); // clicks the row -> full navigation
}

// On the list page: click the index-th filtered row to open its detail page.
async function visitScreeningIndex(scan) {
  if (scan.index >= scan.total) return finishScan(scan);
  if (!(await waitFor(() => screeningTableReady(), 9000))) return;
  const row = getFilteredScreeningRows()[scan.index];
  if (!row) {
    scan.index += 1; // can't find it; skip ahead
    await saveScan(scan);
    return visitScreeningIndex(scan);
  }
  row.scrollIntoView({ block: "center" });
  row.click(); // navigates to /screenings/v2/.../submission/<id> (full load)
}

async function finishScan(scan) {
  scan.status = "done";
  scan.finishedAt = new Date().toISOString();
  await saveScan(scan);
  publishScreenings(scan);
  if (scan.returnUrl && location.href !== scan.returnUrl) {
    location.assign(scan.returnUrl);
  }
}

// Runs on every page load: if a scan is in progress and we've landed on the
// expected detail page, harvest it and move on.
async function maybeContinueScreeningScan() {
  const { uw_scr_scan: scan } = await chrome.storage.local.get("uw_scr_scan");
  if (!scan || scan.status !== "running") return;
  if (Date.now() - new Date(scan.startedAt).getTime() > SCREENING_SCAN_TTL_MS) {
    scan.status = "done";
    await saveScan(scan);
    return; // stale scan; abandon
  }
  const ids = parseIdsFromUrl();
  if (ids.client_id && scan.clientId && ids.client_id !== scan.clientId) return;

  // Phase 1: we navigated to the list URL and need to harvest it.
  if (scan.phase === "list") {
    if (await waitFor(() => screeningTableReady(), 12000)) {
      await beginScreeningWalk(scan);
    }
    return;
  }

  // On a screening detail page: capture it, then return to the list.
  const m = location.href.match(new RegExp(`submission/(${UUID_RE.source})`, "i"));
  if (m) {
    if (scan.index >= scan.total) return; // nothing pending
    await waitFor(
      () => /Screening Duration|Screening Results/i.test(document.body.innerText),
      12000
    );
    const detail = harvestScreeningDetail();
    scan.details[scan.index] = {
      id: m[1].toLowerCase(),
      items: detail.items,
      results: detail.results,
      duration: detail.duration,
      capturedAt: new Date().toISOString(),
    };
    scan.index += 1;
    await saveScan(scan);
    publishScreenings(scan);
    if (scan.index >= scan.total) {
      await finishScan(scan);
    } else if (scan.returnUrl) {
      location.assign(scan.returnUrl); // back to the list to click the next row
    }
    return;
  }

  // Back on the list with screenings still to visit -> click the next row.
  if (screeningTableReady()) {
    if (scan.index < scan.total) visitScreeningIndex(scan);
    else finishScan(scan);
  }
}

// Allow the side panel to trigger a fresh scrape on demand.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "RESCRAPE") {
    lastSerialized = ""; // force re-publish
    publishContext(true).then(() => sendResponse({ ok: true }));
    return true; // async response (deep scrape walks all tabs)
  }
  if (msg && msg.type === "SCREENING_RESCRAPE") {
    startScreeningScan(msg).then((r) => sendResponse(r)).catch((e) =>
      sendResponse({ ok: false, error: String(e) })
    );
    return true;
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
// Resume an in-progress screening auto-walk if one survived a page navigation.
maybeContinueScreeningScan();

// Re-scan on SPA navigation (Unite Us is a single-page app).
let lastHref = location.href;
setInterval(() => {
  if (scraping) return; // ignore route changes we trigger ourselves
  if (location.href !== lastHref) {
    lastHref = location.href;
    lastPublished = null;
    publishIdsOnly();
    scheduleLightScrapes();
    maybeContinueScreeningScan(); // resume the walk on in-app route changes too
  }
}, 1000);
