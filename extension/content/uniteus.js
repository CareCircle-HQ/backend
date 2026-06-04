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
// that must never be triggered automatically. We only walk the data tabs we
// extract from; Forms, Uploads, Referrals and Resources are skipped.
const FACESHEET_TABS = [
  "Overview",
  "Profile",
  "Cases",
  "Screenings",
  "Eligibility Assessments",
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

  return {
    client,
    address,
    insurance: harvestInsurance(),
    coverage_scraped: coverageSectionsPresent(),
  };
}

// Insurance Information + Social Care Coverage. Each saved record renders as a
// repeated card:
//   - Insurance:            div[data-testid="payments-profile-view"]
//   - Social Care Coverage: div[data-testid="social-insurance-profile-view"]
// Plan Name / Start Date / End Date have value-level testids; Member ID and
// Group ID are label <p> + value <p> pairs; SCC Status is its own testid.
// We capture EVERY record (so the CRM can reconcile active/inactive on save) and
// tag each with an `active` flag:
//   - Insurance:            active = End Date >= today OR no expiration.
//   - Social Care Coverage: active = Status == Enrolled AND End Date >= today.
// "No expiration" = "--" (placeholder) or a far-future date like 12/31/9999
// (year 9999 is Unite Us's sentinel for "never expires").
function harvestInsurance() {
  const out = [];
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  const endDateOk = (raw) => {
    const v = cleanText(raw);
    if (!v || v === "--") return true; // no expiration
    if (/\b9999\b/.test(v)) return true; // 12/31/9999 sentinel = never expires
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

  // Insurance records: active when the end date hasn't passed. `index` is the
  // card's position among the insurance cards, used to target it for verify.
  [...document.querySelectorAll('[data-testid="payments-profile-view"]')].forEach(
    (root, index) => {
      const rec = {
        group: "insurance",
        index,
        plan_name: valOf(root, "insPlanNameValue"),
        member_id: labelValue(root, "Member ID"),
        group_id: labelValue(root, "Group ID"),
        start_date: valOf(root, "insPlanStartDateValue"),
        end_date: valOf(root, "insPlanEndDateValue"),
        status: "",
      };
      if (rec.plan_name) {
        rec.active = endDateOk(rec.end_date);
        out.push(rec);
      }
    }
  );

  // Social Care Coverage records: active when Enrolled AND not expired.
  [...document.querySelectorAll('[data-testid="social-insurance-profile-view"]')].forEach(
    (root, index) => {
      const rec = {
        group: "social_care_coverage",
        index,
        plan_name: valOf(root, "sccPlanNameValue"),
        member_id: labelValue(root, "Member ID"),
        group_id: labelValue(root, "Group ID"),
        start_date: valOf(root, "sccPlanStartDateValue"),
        end_date: valOf(root, "sccPlanEndDateValue"),
        status: valOf(root, "sccEnrollStatus"),
      };
      if (rec.plan_name) {
        rec.active = /^enrolled$/i.test(rec.status) && endDateOk(rec.end_date);
        out.push(rec);
      }
    }
  );

  return out;
}

// Click the native "Verify Insurance" link on the matching insurance card and
// wait for the inline "Insurance Verification" results to render. Targets the
// card by plan name (falling back to index). Returns a short result summary.
async function verifyInsurance(msg) {
  const norm = (s) => String(s || "").trim().toLowerCase();
  const cards = [...document.querySelectorAll('[data-testid="payments-profile-view"]')];
  if (!cards.length) return { ok: false, error: "No insurance cards on this page" };

  const planOf = (c) => {
    const el = c.querySelector('[data-testid="insPlanNameValue"]');
    return el ? cleanText(el.textContent) : "";
  };
  let card = null;
  if (msg.plan_name) card = cards.find((c) => norm(planOf(c)) === norm(msg.plan_name));
  if (!card && Number.isInteger(msg.index) && cards[msg.index]) card = cards[msg.index];
  if (!card) return { ok: false, error: "Insurance card not found" };

  // The "Verify Insurance" affordance: the element whose own text is exactly
  // "Verify Insurance" (the checkmark icon carries no text). Prefer an actual
  // interactive element, else its nearest clickable ancestor.
  const matches = [...card.querySelectorAll('a,button,[role="button"],span,div')].filter(
    (el) => /^verify insurance$/i.test(cleanText(el.textContent))
  );
  if (!matches.length) {
    return { ok: false, error: "No 'Verify Insurance' link on this card" };
  }
  const interactive =
    matches.find((el) => /^(a|button)$/i.test(el.tagName) || el.getAttribute("role") === "button") ||
    matches[matches.length - 1].closest('a,button,[role="button"]') ||
    matches[matches.length - 1];

  interactive.scrollIntoView({ block: "center" });
  interactive.click();

  // Wait for the inline verification results to appear within the card.
  const deadline = Date.now() + 15000;
  const resultPhrases = /(match found|no match|not found|unable to verify|coverage status as of)/i;
  while (Date.now() < deadline) {
    if (/insurance verification/i.test(cleanText(card.textContent))) {
      const lines = [...card.querySelectorAll("p,span,div,li")]
        .map((el) => cleanText(el.textContent))
        .filter((t) => t && t.length < 90 && resultPhrases.test(t));
      const summary = [...new Set(lines)].slice(0, 4).join(" \u00b7 ");
      if (summary) return { ok: true, summary };
    }
    await sleep(500);
  }
  return { ok: true, summary: "Verification requested (see the Unite Us page)" };
}

// True when the profile page's coverage sections are actually present (even if a
// client has zero policies). Used as the authoritative-reconcile guard so we
// never deactivate a client's stored insurances off a page that didn't load the
// coverage sections.
function coverageSectionsPresent() {
  if (
    document.querySelector(
      '[data-testid="payments-profile-view"], [data-testid="social-insurance-profile-view"]'
    )
  ) {
    return true;
  }
  return [...document.querySelectorAll("h1,h2,h3,h4,h5")].some((el) =>
    /insurance information|social care coverage/i.test(cleanText(el.textContent))
  );
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
let accum = { clientId: null, pairs: {}, records: new Map(), captured: { client: {}, address: {} }, insurance: [], coverageScraped: false };

function recordKey(r) {
  return `${r.type}:${r.id || r.summary || ""}`;
}

function resetAccum(clientId) {
  accum = { clientId, pairs: {}, records: new Map(), captured: { client: {}, address: {} }, insurance: [], coverageScraped: false };
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
    // When the coverage sections were actually on the page, take that scrape's
    // full coverage list as authoritative (even when empty -- all policies may
    // have been removed). Otherwise keep whatever a prior profile scrape found.
    if (captured.coverage_scraped) {
      accum.insurance = Array.isArray(captured.insurance) ? captured.insurance : [];
      accum.coverageScraped = true;
    } else if (Array.isArray(captured.insurance) && captured.insurance.length) {
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
      coverageScraped: accum.coverageScraped,
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
        coverageScraped: !!uw_accum.coverageScraped,
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
      coverage_scraped: accum.coverageScraped,
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

// Cases / screenings / eligibility are captured per-client. When the user
// switches to a different client we must drop the previous client's data (both
// the panel-facing results and any in-progress auto-walk state) so the tabs
// don't show stale info from the prior client.
function clearClientScopedData() {
  chrome.storage.local.remove([
    "uw_screenings",
    "uw_eligibility",
    "uw_cases",
    "uw_scr_scan",
    "uw_elig_scan",
    "uw_case_scan",
  ]);
}

// Publish the client_id quickly (before the slower full scrape) so the panel
// can show the detected client immediately.
function publishIdsOnly() {
  const ids = parseIdsFromUrl();
  if (!ids.client_id) return;
  chrome.storage.local.get("uw_context", ({ uw_context }) => {
    if (uw_context && uw_context.client_id === ids.client_id) return;
    // A different client (or first detection) -> purge the previous client's
    // cases / screenings / eligibility before publishing the new context.
    clearClientScopedData();
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

// Reveal the screenings list. The list lives behind the "Screenings" facesheet
// tab, whose state is NOT reliably deep-linkable, so we don't navigate to a
// list URL; instead we ensure we're on the facesheet and click the tab in-app.
// On a fresh page load the facesheet tabs render asynchronously, so we keep
// retrying the click (it's a no-op until the tab exists) until the table shows.
async function ensureScreeningList(timeout = 12000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (screeningTableReady()) return true;
    clickTabByLabel("Screenings"); // no-op while the tab hasn't rendered yet
    await sleep(400);
  }
  return screeningTableReady();
}

// After the table appears its rows may still be streaming in (especially right
// after a client switch, when the auto-walk starts before the data finished
// loading). Wait until the Met Council rows show up, or until the total row
// count holds steady across a few polls (so a client that genuinely has no
// Met Council screenings doesn't hang). Returns the harvested filtered list.
async function waitForScreeningListSettled(timeout = 12000) {
  const deadline = Date.now() + timeout;
  let lastFiltered = -1;
  let lastTotal = -1;
  let stable = 0;
  while (Date.now() < deadline) {
    const filtered = getFilteredScreeningRows().length;
    const t = findScreeningTable();
    const total = t
      ? [...t.querySelectorAll("tbody tr")].filter((r) => r.querySelector("td")).length
      : 0;
    // Settle only once BOTH the total and the filtered (Met Council) row counts
    // stop changing across consecutive polls AND the table actually has rows.
    // Breaking on the first matching row (the old behaviour) harvested a partial
    // list while rows were still streaming, so scan.total came out too low and we
    // skipped screenings. A still-loading table (total 0) is never "settled".
    if (total > 0 && filtered === lastFiltered && total === lastTotal) {
      stable += 1;
      if (stable >= 3) break;
    } else {
      stable = 0;
    }
    lastFiltered = filtered;
    lastTotal = total;
    await sleep(400);
  }
  return harvestScreeningList();
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

  // Strategy 1: Look for need cards with the specific class structure from diagnostic
  document.querySelectorAll(".need-card .need-card__name, .displayed-needs .need-card__name").forEach((el) => {
    const t = cleanText(el.innerText);
    if (!t || t.length > 80) return;
    if (!isAllowedNeed(t)) return; // only keep the 5 allowed needs
    const low = t.toLowerCase();
    if (seen.has(low)) return;
    seen.add(low);
    out.push(t);
  });

  // Strategy 2: Look in the "Screening Results" section for any text matching allowed needs
  const resultsSection = [...document.querySelectorAll("[aria-expanded='true']")].find(el =>
    /screening results/i.test(cleanText(el.innerText))
  );
  if (resultsSection) {
    const section = resultsSection.closest("section, div, [class*='risk-display']") || document.body;
    const allText = cleanText(section.innerText);
    ALLOWED_NEEDS.forEach(need => {
      if (allText.toLowerCase().includes(need)) {
        if (!seen.has(need)) {
          seen.add(need);
          out.push(need.charAt(0).toUpperCase() + need.slice(1));
        }
      }
    });
  }

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
    if (!q || q.length > 300) return;

    // Skip section headers (h3 tags like "Screening Questions", "Screening Details")
    if (labelEl.tagName === "H3") return;
    // Skip headers that contain ":" (like "Screening Name:", "Screening Organization:")
    if (q.includes(":")) return;
    // Skip the "Screening Duration" - we handle that separately
    if (/screening duration/i.test(q)) return;
    // Skip section title headers
    if (/^screening (details|questions)$/i.test(q)) return;

    const a = valueEl ? cleanText(valueEl.innerText) : "";
    if (!a || a === q) return;

    const key = q.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    items.push({ q, a });
  });

  // Fallback 1: Use all div/span/p elements like the diagnostic does
  if (items.length === 0) {
    const allElements = [...document.querySelectorAll("div, span, p")];
    for (let i = 0; i < allElements.length; i++) {
      const el = allElements[i];
      const text = cleanText(el.innerText);
      if (!text || text.length > 300) continue;

      // Look for elements that look like questions (contain ? or are statements like "Within the past...")
      const looksLikeQuestion = text.endsWith("?") ||
        /^(within the past|in the past|think about|do you want|does the member|is the client|was an|which language|who responded|screening modality)/i.test(text);

      if (!looksLikeQuestion) continue;
      if (/screening duration/i.test(text)) continue;

      // Find answer - look for next sibling or parent's next sibling
      let answerEl = el.nextElementSibling;
      if (!answerEl && el.parentElement) {
        answerEl = el.parentElement.nextElementSibling;
      }

      // If still no answer, look in DOM order
      if (!answerEl) {
        for (let j = i + 1; j < allElements.length && j < i + 5; j++) {
          const nextEl = allElements[j];
          const nextText = cleanText(nextEl.innerText);
          if (!nextText || nextText.length > 200) continue;
          // Stop if we hit another question-like element
          if (nextText.endsWith("?") || /^(within the past|in the past|think about|do you want)/i.test(nextText)) break;
          answerEl = nextEl;
          break;
        }
      }

      if (answerEl) {
        const answer = cleanText(answerEl.innerText);
        if (answer && answer !== text && answer.length < 400) {
          const key = text.toLowerCase();
          if (!seen.has(key)) {
            seen.add(key);
            items.push({ q: text, a: answer });
          }
        }
      }
    }
  }

  // Fallback 2: Line-by-line text extraction from body
  if (items.length === 0) {
    const allText = document.body.innerText || "";
    const lines = allText.split(/\n/).map(l => cleanText(l)).filter(Boolean);
    const questionPatterns = [
      /\?$/,  // ends with ?
      /^within the past/i,
      /^in the past/i,
      /^think about/i,
      /^do you want/i,
      /^does the member/i,
      /^is the client/i,
      /^was an/i,
      /^which language/i,
      /^who responded/i,
      /^screening modality/i
    ];

    for (let i = 0; i < lines.length - 1; i++) {
      const line = lines[i];
      const isQuestion = questionPatterns.some(p => p.test(line));
      if (!isQuestion) continue;
      if (/screening duration/i.test(line)) continue;

      const nextLine = lines[i + 1];
      if (!nextLine) continue;
      // Skip if next line is also a question
      if (questionPatterns.some(p => p.test(nextLine))) continue;

      const key = line.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      items.push({ q: line, a: nextLine });
    }
  }

  // Extract Screening Duration - look for the number near "Screening Duration" label.
  // The value renders as two spans: the number ("8") and the unit ("Minutes").
  let duration = null;
  let durationUnit = "";
  document.querySelectorAll(".ui-form-renderer-question-display").forEach((container) => {
    const labelEl = container.querySelector(".ui-form-renderer-question-display__label");
    if (!labelEl) return;
    const labelText = cleanText(labelEl.innerText);
    if (!/screening duration/i.test(labelText)) return;

    // Look for a number in the container text
    const containerText = cleanText(container.innerText);
    const numMatch = containerText.match(/(\d+)/);
    if (numMatch) duration = numMatch[1];
    const unitMatch = containerText.match(/\b(minutes?|hours?|hrs?|mins?)\b/i);
    if (unitMatch) durationUnit = unitMatch[1];
  });

  // Fallback duration extraction
  if (!duration) {
    const durationMatch = document.body.innerText.match(/Screening Duration\s*(\d+)\s*(minutes?|hours?)?/i);
    if (durationMatch) {
      duration = durationMatch[1];
      if (durationMatch[2]) durationUnit = durationMatch[2];
    }
  }

  // Track Screening Duration as a Q&A item too (in order, at the end of the form)
  if (duration) {
    if (!durationUnit) durationUnit = duration === "1" ? "Minute" : "Minutes";
    const durAnswer = `${duration} ${durationUnit}`.trim();
    const durKey = "screening duration";
    if (!seen.has(durKey)) {
      seen.add(durKey);
      items.push({ q: "Screening Duration", a: durAnswer });
    }
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

  // Fast path: already on this client's facesheet -> reveal the Screenings tab
  // (in-app) and harvest once rows render. The list isn't reliably deep-linkable,
  // so when we're NOT on the facesheet we navigate to it (a real, reloadable URL)
  // and the crawler resumes + clicks the tab on the next page load.
  const onClient = parseIdsFromUrl().client_id === clientId;
  if (onClient && getFacesheetTabs().length) {
    if (await ensureScreeningList(9000)) {
      await beginScreeningWalk(scan);
      return { ok: true, count: scan.total };
    }
  }
  location.assign(`${location.origin}/facesheet/${clientId}`);
  return { ok: true, count: null };
}

// Harvest the filtered list, then start visiting each screening by clicking its
// row (rows are clickable <tr role="button"> with no link/id).
async function beginScreeningWalk(scan) {
  await ensureScreeningList(12000);
  const list = await waitForScreeningListSettled(12000);
  scan.list = list;
  scan.total = list.length;
  scan.index = 0;
  scan.details = [];
  scan.phase = "detail";
  // Return to the facesheet (a real, reloadable URL); the resume logic re-opens
  // the Screenings tab there before clicking the next row.
  scan.returnUrl = `${location.origin}/facesheet/${scan.clientId}`;
  scan.note = list.length ? "" : "No Met Council - SCN - PHS screenings in the list.";
  await saveScan(scan);
  publishScreenings(scan);

  if (!list.length) return finishScan(scan);
  visitScreeningIndex(scan); // clicks the row -> full navigation
}

// On the list page: click the index-th filtered row to open its detail page.
async function visitScreeningIndex(scan) {
  if (scan.index >= scan.total) return finishScan(scan);
  if (!(await ensureScreeningList(12000))) return;
  // The list re-streams its rows each time we return here, so the index-th row
  // may not exist yet right after the tab opens. Wait until all the rows we
  // harvested are present (or the count settles) before clicking -- otherwise the
  // index maps to the wrong row, or row is undefined and we wrongly skip ahead.
  await waitFor(() => getFilteredScreeningRows().length >= scan.total, 12000, 400);
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

// Guard against concurrent runs (page-load + SPA-interval can both fire).
let screeningScanBusy = false;

// Runs on every page load: if a scan is in progress and we've landed on the
// expected detail page, harvest it and move on.
async function maybeContinueScreeningScan() {
  if (screeningScanBusy) return; // already handling a page; don't double-fire
  screeningScanBusy = true;
  try {
    await _maybeContinueScreeningScan();
  } finally {
    screeningScanBusy = false;
  }
}

async function _maybeContinueScreeningScan() {
  const { uw_scr_scan: scan } = await chrome.storage.local.get("uw_scr_scan");
  if (!scan || scan.status !== "running") return;
  if (Date.now() - new Date(scan.startedAt).getTime() > SCREENING_SCAN_TTL_MS) {
    scan.status = "done";
    await saveScan(scan);
    return; // stale scan; abandon
  }
  const ids = parseIdsFromUrl();
  if (ids.client_id && scan.clientId && ids.client_id !== scan.clientId) return;

  // Don't act on a sibling crawler's pages. The eligibility list shares the same
  // table shape (FORM + ORGANIZATION) as screenings, so without this guard a
  // still-running screening scan would hijack the eligibility/case list rows.
  const onScreeningDetail = new RegExp(`submission/(${UUID_RE.source})`, "i").test(location.href);
  if (!onScreeningDetail &&
      (/\/eligibility(\/|$)/.test(location.pathname) ||
       /\/cases(\/|$)/.test(location.pathname) ||
       /\/dashboard\/cases\//.test(location.pathname))) {
    return;
  }

  // Phase 1: we navigated to the facesheet and need to reveal + harvest the list.
  if (scan.phase === "list") {
    if (await ensureScreeningList(12000)) {
      await beginScreeningWalk(scan);
    } else if (getFacesheetTabs().length) {
      // We're on the facesheet but the Screenings list never appeared. Finish
      // (with a note) instead of leaving the scan "running" forever, which would
      // hang the Profile reload and block the eligibility/case walks.
      scan.note = "Couldn't open the Screenings list.";
      await finishScan(scan);
    }
    return;
  }

  // On a screening detail page: capture it, then return to the list.
  const m = location.href.match(new RegExp(`submission/(${UUID_RE.source})`, "i"));
  if (m) {
    if (scan.index >= scan.total) return; // nothing pending

    // Keep harvesting until the form has fully rendered. The questions load
    // asynchronously AFTER the duration/results paint, so breaking on the first
    // item would capture only the duration. Instead we wait until the captured
    // item count STABILIZES (same value across consecutive polls) and we have
    // more than just the duration row.
    let detail = { id: null, items: [], results: [], duration: null };
    const deadline = Date.now() + 25000;
    let lastCount = -1;
    let stableTicks = 0;
    while (Date.now() < deadline) {
      detail = harvestScreeningDetail();
      const count = (detail.items || []).length;
      // Count real questions (exclude the Screening Duration row)
      const realQs = (detail.items || []).filter(
        (it) => !/screening duration/i.test(it.q || "")
      ).length;

      if (count === lastCount && realQs >= 1) {
        stableTicks += 1;
        // Two consecutive identical counts = form has finished rendering
        if (stableTicks >= 2) break;
      } else {
        stableTicks = 0;
      }
      lastCount = count;
      await new Promise((r) => setTimeout(r, 400));
    }

    // Require at least one real question (not just the duration). If after the
    // polling deadline we still have none, we skip this screening (below) rather
    // than retry, since a static detail page won't re-fire this handler.
    const realQCount = (detail.items || []).filter(
      (it) => !/screening duration/i.test(it.q || "")
    ).length;
    if (realQCount === 0) {
      // Waited the full deadline without any real questions rendering (unexpected
      // layout, or a genuinely empty form). Record what we captured and move on
      // so the walk can't hang on this page forever and block the rest.
      scan.note = "";
      scan.details[scan.index] = {
        id: m[1].toLowerCase(),
        items: detail.items || [],
        results: detail.results || [],
        duration: detail.duration || null,
        capturedAt: new Date().toISOString(),
        partial: true,
      };
      scan.index += 1;
      await saveScan(scan);
      publishScreenings(scan);
      if (scan.index >= scan.total) {
        await finishScan(scan);
      } else if (scan.returnUrl) {
        location.assign(scan.returnUrl);
      }
      return;
    }

    scan.note = "";
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

  // Back on the facesheet with screenings still to visit -> re-open the list
  // tab and click the next row (visitScreeningIndex calls ensureScreeningList).
  if (scan.index < scan.total) visitScreeningIndex(scan);
  else finishScan(scan);
}

// ---------------------------------------------------------------------------
// Eligibility auto-walk crawler. Mirrors the screening crawler: it filters the
// eligibility list by the target org, then visits each assessment detail page
// (/facesheet/<client>/eligibility/view/<id>) to capture the ordered Q&A and the
// "Client May Be Eligible" programs. State lives in uw_elig_scan so the flow
// survives full page reloads. The list table has the same columns as the
// screening list, so we reuse findScreeningTable / screeningTableReady /
// getFilteredScreeningRows / harvestScreeningList.
// ---------------------------------------------------------------------------
const ELIGIBILITY_SCAN_TTL_MS = SCREENING_SCAN_TTL_MS;

// Parse the eligibility list (same columns as screenings) for the target org,
// AND capture each row's assessment id/href so we can navigate to the detail
// page by URL (the rows don't reliably navigate on click).
function harvestEligibilityList() {
  const table = findScreeningTable();
  if (!table) return [];
  let headers = [...table.querySelectorAll("thead th")].map((th) => cleanText(th.innerText));
  if (!headers.length) {
    headers = [...table.querySelectorAll("tr th")].map((th) => cleanText(th.innerText));
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
  const norm = (s) => cleanText(s).toLowerCase();
  const viewRe = new RegExp(`eligibility/view/(${UUID_RE.source})`, "i");

  const out = [];
  rows.forEach((tr) => {
    const cells = [...tr.children].filter((c) => c.tagName === "TD");
    if (!cells.length) return;
    const cell = (i) => (i >= 0 && cells[i] ? cleanText(cells[i].innerText) : "");
    const rowMatches = iOrg >= 0
      ? norm(cell(iOrg)).includes(norm(SCREENING_ORG))
      : norm(tr.innerText).includes(norm(SCREENING_ORG));
    if (!rowMatches) return;

    // Find the assessment id from a detail link or any UUID in the row markup.
    let id = null;
    let href = null;
    const a =
      tr.querySelector('a[href*="/eligibility/view/"]') ||
      tr.querySelector('a[href*="eligibility"]') ||
      tr.querySelector("a[href]");
    if (a) {
      href = a.getAttribute("href");
      const mm = (href || "").match(viewRe);
      if (mm) id = mm[1].toLowerCase();
    }
    if (!id) {
      const mm = (tr.outerHTML || "").match(viewRe);
      if (mm) id = mm[1].toLowerCase();
    }

    out.push({
      form: cell(iForm),
      submitter: cell(iSub),
      status: cell(iStatus),
      org: cell(iOrg),
      date: cell(iDate),
      id,
      href,
    });
  });
  return out;
}

// Capture the "Client May Be Eligible" programs list. We slice the visible text
// between the section's intro line and the "Add Social Care Coverage" action.
function harvestEligibilityResults() {
  const out = [];
  const seen = new Set();
  const push = (t) => {
    t = cleanText(t);
    if (!t || t.length > 160) return;
    const low = t.toLowerCase();
    if (low === "add social care coverage") return;
    if (seen.has(low)) return;
    seen.add(low);
    out.push(t);
  };
  const body = document.body ? document.body.innerText : "";
  const m = body.match(
    /connect them with these resources\.?\s*([\s\S]*?)\s*Add Social Care Coverage/i
  );
  if (m) {
    m[1].split(/\n/).forEach((line) => push(line));
  }
  return out;
}

// Capture one eligibility detail page: ordered question/answer pairs (skipping
// section headers) plus the eligible-programs results. Unlike screenings,
// colon-suffixed labels (e.g. "Modality of Outreach 1:") are real questions, so
// we do NOT drop them; multi-select answers are joined with "; ".
// Eligibility questions render as bold dark-blue labels (no form-renderer
// classes like screenings). Each question's answer follows it in the same
// per-question wrapper.
const ELIG_Q_SELECTOR = "[class*='text-dark-blue'][class*='font-black']";

// Labels that are section/structure headers, not real questions.
const ELIG_SKIP_LABELS = [
  /^client may be eligible/i,
  /^enhanced populations?$/i,
  /^clinical criteria$/i,
  /^relationships$/i,
  /^care team$/i,
  /^care coordinator$/i,
  /^family members$/i,
  /^messages$/i,
  /^notes$/i,
];

// The eligibility assessment content lives in its own column. Scope harvesting
// to the container that holds the "Eligibility Assessment for ..." heading so we
// don't pull in the Care Team / Relationships sidebar labels.
function eligibilityContentRoot() {
  const h = [...document.querySelectorAll("h1, h2, h3, h4, div, span")].find((el) => {
    const t = cleanText(el.innerText);
    return t && el.children.length === 0 && /^Eligibility Assessment for /i.test(t);
  });
  let c = h ? h.parentElement : null;
  for (let i = 0; i < 8 && c; i++) {
    if (c.querySelector(ELIG_Q_SELECTOR)) return c;
    c = c.parentElement;
  }
  return document.body;
}

function harvestEligibilityDetail() {
  const m = location.href.match(
    new RegExp(`eligibility/view/(${UUID_RE.source})`, "i")
  );
  const id = m ? m[1].toLowerCase() : null;

  const items = [];
  const seen = new Set();
  const addQA = (q, a) => {
    q = cleanText(q);
    a = cleanText(a);
    if (!q || !a || q === a) return;
    if (q.length > 400 || a.length > 1000) return;
    if (ELIG_SKIP_LABELS.some((re) => re.test(q))) return;
    const key = q.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    items.push({ q, a });
  };

  const root = eligibilityContentRoot();
  const labels = [...root.querySelectorAll(ELIG_Q_SELECTOR)];

  labels.forEach((labelEl) => {
    const q = cleanText(labelEl.innerText);
    if (!q) return;

    // Prefer the smallest wrapper that contains exactly this one question; the
    // answer is then the wrapper's text minus the question text.
    let answer = "";
    let wrap = labelEl.parentElement;
    for (let i = 0; i < 4 && wrap && wrap !== root; i++) {
      if (wrap.querySelectorAll(ELIG_Q_SELECTOR).length === 1) {
        const full = cleanText(wrap.innerText);
        const idx = full.indexOf(q);
        const rest = idx >= 0 ? full.slice(idx + q.length) : full;
        const cand = cleanText(rest);
        if (cand) {
          answer = cand;
          break;
        }
      }
      wrap = wrap.parentElement;
    }

    // Fallback: collect following sibling blocks until the next question label.
    if (!answer) {
      const parts = [];
      let sib = labelEl.nextElementSibling;
      while (sib && !sib.matches(ELIG_Q_SELECTOR) && !sib.querySelector(ELIG_Q_SELECTOR)) {
        const t = cleanText(sib.innerText);
        if (t) parts.push(t);
        sib = sib.nextElementSibling;
      }
      answer = parts.join("; ");
    }

    addQA(q, answer);
  });

  return { id, items, results: harvestEligibilityResults() };
}

function saveEligScan(scan) {
  return chrome.storage.local.set({ uw_elig_scan: scan });
}

// Publish the panel-facing view: list rows joined with any captured details.
function publishEligibility(scan) {
  const eligibilities = scan.list.map((x, i) => ({
    ...x,
    detail: (scan.details && scan.details[i]) || null,
  }));
  chrome.storage.local.set({
    uw_eligibility: {
      clientId: scan.clientId,
      org: SCREENING_ORG,
      eligibilities,
      status: scan.status,
      phase: scan.phase || null,
      note: scan.note || "",
      scannedAt: scan.startedAt,
      finishedAt: scan.finishedAt || null,
      progress: { done: scan.index, total: scan.total || scan.list.length },
    },
  });
}

async function startEligibilityScan(msg) {
  const clientId = (msg && msg.clientId) || parseIdsFromUrl().client_id;
  if (!clientId) return { ok: false, error: "Open the client's facesheet first" };

  const scan = {
    clientId,
    status: "running",
    phase: "list",
    note: "Loading eligibility assessments\u2026",
    startedAt: new Date().toISOString(),
    finishedAt: null,
    list: [],
    total: 0,
    index: 0,
    details: [],
    returnUrl: null,
  };
  await saveEligScan(scan);
  publishEligibility(scan);

  // Already on the eligibility list -> harvest in place (no reload).
  if (/\/eligibility\/all/.test(location.pathname) &&
      (await waitFor(() => screeningTableReady(), 9000))) {
    await beginEligibilityWalk(scan);
    return { ok: true, count: scan.total };
  }
  // Otherwise load the list URL; the crawler resumes on the next page load.
  location.assign(`${location.origin}/facesheet/${clientId}/eligibility/all`);
  return { ok: true, count: null };
}

async function beginEligibilityWalk(scan) {
  await waitFor(() => screeningTableReady(), 12000);
  const list = harvestEligibilityList(); // captures per-row assessment id
  scan.list = list;
  scan.total = list.length;
  scan.index = 0;
  scan.details = [];
  scan.phase = "detail";
  scan.returnUrl = location.href;
  scan.note = list.length ? "" : "No Met Council - SCN - PHS eligibility assessments in the list.";
  await saveEligScan(scan);
  publishEligibility(scan);

  if (!list.length) return finishEligibilityScan(scan);
  visitEligibilityIndex(scan);
}

// Open the index-th assessment's detail page. We navigate by URL using the
// captured assessment id (deterministic); only if we have no id do we fall back
// to clicking the row / its link.
async function visitEligibilityIndex(scan) {
  if (scan.index >= scan.total) return finishEligibilityScan(scan);
  if (!(await waitFor(() => screeningTableReady(), 9000))) return;
  const item = scan.list[scan.index];

  if (item && item.id) {
    location.assign(
      `${location.origin}/facesheet/${scan.clientId}/eligibility/view/${item.id}`
    );
    return;
  }

  // No id captured -> fall back to clicking the row or its detail link.
  const row = getFilteredScreeningRows()[scan.index];
  if (!row) {
    scan.index += 1;
    await saveEligScan(scan);
    return visitEligibilityIndex(scan);
  }
  row.scrollIntoView({ block: "center" });
  const link = row.querySelector('a[href*="/eligibility/view/"]');
  (link || row).click();
}

async function finishEligibilityScan(scan) {
  scan.status = "done";
  scan.finishedAt = new Date().toISOString();
  await saveEligScan(scan);
  publishEligibility(scan);
  if (scan.returnUrl && location.href !== scan.returnUrl) {
    location.assign(scan.returnUrl);
  }
}

let eligibilityScanBusy = false;

async function maybeContinueEligibilityScan() {
  if (eligibilityScanBusy) return;
  eligibilityScanBusy = true;
  try {
    await _maybeContinueEligibilityScan();
  } finally {
    eligibilityScanBusy = false;
  }
}

async function _maybeContinueEligibilityScan() {
  const { uw_elig_scan: scan } = await chrome.storage.local.get("uw_elig_scan");
  if (!scan || scan.status !== "running") return;
  if (Date.now() - new Date(scan.startedAt).getTime() > ELIGIBILITY_SCAN_TTL_MS) {
    scan.status = "done";
    await saveEligScan(scan);
    return;
  }
  const ids = parseIdsFromUrl();
  if (ids.client_id && scan.clientId && ids.client_id !== scan.clientId) return;

  // Only act on eligibility pages. The list (/eligibility/all) shares the screening
  // table shape, so without this guard we'd misfire on the screenings list.
  if (!/\/eligibility(\/|$)/.test(location.pathname)) return;

  // Phase 1: navigated to the list URL; harvest it.
  if (scan.phase === "list") {
    if (await waitFor(() => screeningTableReady(), 12000)) {
      await beginEligibilityWalk(scan);
    }
    return;
  }

  // On an eligibility detail page: capture it, then return to the list.
  const m = location.href.match(
    new RegExp(`eligibility/view/(${UUID_RE.source})`, "i")
  );
  if (m) {
    if (scan.index >= scan.total) return;

    // Wait until the captured question count stabilizes (form fully rendered).
    let detail = { id: null, items: [], results: [] };
    const deadline = Date.now() + 25000;
    let lastCount = -1;
    let stableTicks = 0;
    while (Date.now() < deadline) {
      detail = harvestEligibilityDetail();
      const count = (detail.items || []).length;
      if (count === lastCount && count >= 1) {
        stableTicks += 1;
        if (stableTicks >= 2) break;
      } else {
        stableTicks = 0;
      }
      lastCount = count;
      await sleep(400);
    }

    if (!detail.items || detail.items.length === 0) {
      scan.note = "Waiting for eligibility questions to load\u2026";
      await saveEligScan(scan);
      publishEligibility(scan);
      return;
    }

    scan.note = "";
    scan.details[scan.index] = {
      id: m[1].toLowerCase(),
      items: detail.items,
      results: detail.results,
      capturedAt: new Date().toISOString(),
    };
    scan.index += 1;
    await saveEligScan(scan);
    publishEligibility(scan);
    if (scan.index >= scan.total) {
      await finishEligibilityScan(scan);
    } else if (scan.returnUrl) {
      location.assign(scan.returnUrl);
    }
    return;
  }

  // Back on the list with assessments still to visit -> open the next row.
  if (screeningTableReady()) {
    if (scan.index < scan.total) visitEligibilityIndex(scan);
    else finishEligibilityScan(scan);
  }
}

// ===========================================================================
// CASE AUTO-WALK CRAWLER (Met Council - SCN - PHS)
// ===========================================================================
// Mirrors the eligibility crawler: filter the facesheet cases list by the
// target org, then visit each case detail page
// (/dashboard/cases/<state>/<caseId>/contact/<clientId>) and harvest its fields.
// The detail page renders fields as UPPERCASE label/value pairs, so we segment
// the case content text on a fixed set of known labels. State lives in
// uw_case_scan so the flow survives full page reloads / cross-area navigation.
const CASE_SCAN_TTL_MS = SCREENING_SCAN_TTL_MS;

// Known field labels on the case detail page (segmentation boundaries).
const CASE_LABELS = [
  "SERVICE TYPE",
  "PROGRAM",
  "DATE OPENED",
  "DATE CLOSED",
  "NETWORK",
  "ORGANIZATION",
  "PRIMARY WORKER",
  "CASE DESCRIPTION",
  "AUTHORIZATION STATUS",
  "AUTHORIZED AMOUNT",
  "AUTHORIZED SERVICE DELIVERY DATE(S)",
  "PROGRAM CAP",
  "NOTES",
  "UNITE US AUTHORIZATION ID",
  "SOCIAL CARE COVERAGE PLAN",
  "SOCIAL CARE COVERAGE STATUS",
];
// Section headers that terminate the field area (boundaries, not captured).
const CASE_STOPS = [
  "ATTACHED DOCUMENTS",
  "CLOSE CASE",
  "CONTRACTED SERVICE",
  "ADD NEW CONTRACTED SERVICE",
  "FORM SUBMISSIONS",
  "CASE NOTES",
  "REFERRAL NOTES",
  "REFERRAL HISTORY",
  "RELATIONSHIPS",
  "CARE TEAM",
  "FAMILY MEMBERS",
];

function findCaseTable() {
  return [...document.querySelectorAll("table")].find((t) => {
    if (t.offsetParent === null) return false;
    const heads = [...t.querySelectorAll("th")].map((th) =>
      cleanText(th.innerText).toUpperCase()
    );
    return (
      heads.includes("SERVICE TYPE") &&
      heads.some((h) => h.includes("ORGANIZATION"))
    );
  });
}

function caseTableReady() {
  const t = findCaseTable();
  if (!t) return false;
  let rows = [...t.querySelectorAll("tbody tr")];
  if (!rows.length) rows = [...t.querySelectorAll("tr")].filter((r) => r.querySelector("td"));
  return rows.length > 0;
}

// Parse the facesheet cases list for the target org, capturing each row's
// case id / detail href so we can navigate to the detail page by URL.
function harvestCaseList() {
  const table = findCaseTable();
  if (!table) return [];
  let headers = [...table.querySelectorAll("thead th")].map((th) => cleanText(th.innerText));
  if (!headers.length) {
    headers = [...table.querySelectorAll("tr th")].map((th) => cleanText(th.innerText));
  }
  const col = (name) => headers.findIndex((h) => h.toUpperCase() === name);
  const colIncl = (name) => headers.findIndex((h) => h.toUpperCase().includes(name));
  const iService = col("SERVICE TYPE");
  const iDate = colIncl("DATE OPENED");
  const iStatus = col("STATUS");
  const iOrg = colIncl("MANAGING ORGANIZATION") >= 0 ? colIncl("MANAGING ORGANIZATION") : colIncl("ORGANIZATION");
  const iUpdated = colIncl("LAST UPDATED");

  let rows = [...table.querySelectorAll("tbody tr")];
  if (!rows.length) {
    rows = [...table.querySelectorAll("tr")].filter((r) => r.querySelector("td"));
  }
  const norm = (s) => cleanText(s).toLowerCase();

  const out = [];
  rows.forEach((tr) => {
    const cells = [...tr.children].filter((c) => c.tagName === "TD");
    if (!cells.length) return;
    const cell = (i) => (i >= 0 && cells[i] ? cleanText(cells[i].innerText) : "");
    const orgText = iOrg >= 0 ? cell(iOrg) : cleanText(tr.innerText);
    if (!norm(orgText).includes(norm(SCREENING_ORG))) return;

    let id = null;
    let href = null;
    const a =
      tr.querySelector('a[href*="/cases/"]') ||
      tr.querySelector('a[href*="/case/"]') ||
      tr.querySelector("a[href]");
    if (a) {
      href = a.getAttribute("href");
      const mm = (href || "").match(UUID_RE);
      if (mm) id = mm[0].toLowerCase();
    }
    if (!id) {
      const mm = (tr.outerHTML || "").match(UUID_RE);
      if (mm) id = mm[0].toLowerCase();
    }

    out.push({
      id,
      href,
      service_type: cell(iService),
      date_opened: cell(iDate),
      status: cell(iStatus),
      org: orgText,
      updated: cell(iUpdated),
    });
  });
  return out;
}

function parseCaseIdFromUrl() {
  const m = location.href.match(
    new RegExp(`/dashboard/cases/[^/]+/(${UUID_RE.source})`, "i")
  );
  return m ? m[1].toLowerCase() : null;
}

function caseDetailUrl(item, clientId) {
  if (item && item.href) {
    try {
      return new URL(item.href, location.origin).href;
    } catch (_) {}
  }
  if (item && item.id) {
    return `${location.origin}/dashboard/cases/open/${item.id}/contact/${clientId}`;
  }
  return null;
}

// Climb from the "Case for ..." heading to the container that holds the case
// fields, so we segment only the case content (not the client sidebar).
function caseContentRoot() {
  const h = [...document.querySelectorAll("h1, h2, h3, h4")].find((e) =>
    /^case for /i.test(cleanText(e.innerText))
  );
  let c = h ? h.parentElement : null;
  for (let i = 0; i < 12 && c; i++) {
    const t = c.innerText || "";
    if (/SERVICE TYPE/.test(t) && /DATE OPENED/.test(t)) return c;
    c = c.parentElement;
  }
  return document.body;
}

function caseDetailReady() {
  if (!parseCaseIdFromUrl()) return false;
  const t = caseContentRoot().innerText || "";
  return /SERVICE TYPE/.test(t) && /DATE OPENED/.test(t);
}

// Segment the case content text into label/value fields + the case status.
function harvestCaseDetail() {
  const id = parseCaseIdFromUrl();
  const root = caseContentRoot();
  const text = cleanText(root.innerText);

  let status = "";
  const sm = text.match(
    /\b(OPEN|CLOSED|MANAGED|DRAFT|CANCELLED|PENDING AUTHORIZATION|OFF PLATFORM)\s+SERVICE TYPE/
  );
  if (sm) status = sm[1];

  const boundaries = [];
  const addPos = (label, isField) => {
    const idx = text.indexOf(label);
    if (idx >= 0) boundaries.push({ label, idx, end: idx + label.length, isField });
  };
  CASE_LABELS.forEach((l) => addPos(l, true));
  CASE_STOPS.forEach((l) => addPos(l, false));
  boundaries.sort((a, b) => a.idx - b.idx);

  const fields = {};
  for (let i = 0; i < boundaries.length; i++) {
    const b = boundaries[i];
    if (!b.isField) continue;
    const next = boundaries[i + 1];
    const val = text.slice(b.end, next ? next.idx : text.length).trim();
    if (val) fields[b.label] = val;
  }

  return { id, status, fields };
}

function saveCaseScan(scan) {
  return chrome.storage.local.set({ uw_case_scan: scan });
}

function publishCases(scan) {
  const cases = scan.list.map((x, i) => ({
    ...x,
    detail: (scan.details && scan.details[i]) || null,
  }));
  chrome.storage.local.set({
    uw_cases: {
      clientId: scan.clientId,
      org: SCREENING_ORG,
      cases,
      status: scan.status,
      phase: scan.phase || null,
      note: scan.note || "",
      scannedAt: scan.startedAt,
      finishedAt: scan.finishedAt || null,
      progress: { done: scan.index, total: scan.total || scan.list.length },
    },
  });
}

async function startCaseScan(msg) {
  const clientId = (msg && msg.clientId) || parseIdsFromUrl().client_id;
  if (!clientId) return { ok: false, error: "Open the client's facesheet first" };

  const scan = {
    clientId,
    status: "running",
    phase: "list",
    note: "Loading cases\u2026",
    startedAt: new Date().toISOString(),
    finishedAt: null,
    list: [],
    total: 0,
    index: 0,
    details: [],
    returnUrl: null,
  };
  await saveCaseScan(scan);
  publishCases(scan);

  if (/\/cases\/?$/.test(location.pathname) && (await waitFor(() => caseTableReady(), 9000))) {
    await beginCaseWalk(scan);
    return { ok: true, count: scan.total };
  }
  location.assign(`${location.origin}/facesheet/${clientId}/cases`);
  return { ok: true, count: null };
}

async function beginCaseWalk(scan) {
  await waitFor(() => caseTableReady(), 12000);
  const list = harvestCaseList();
  scan.list = list;
  scan.total = list.length;
  scan.index = 0;
  scan.details = [];
  scan.phase = "detail";
  scan.returnUrl = location.href;
  scan.note = list.length ? "" : "No Met Council - SCN - PHS cases in the list.";
  await saveCaseScan(scan);
  publishCases(scan);

  if (!list.length) return finishCaseScan(scan);
  visitCaseIndex(scan);
}

async function visitCaseIndex(scan) {
  if (scan.index >= scan.total) return finishCaseScan(scan);
  const item = scan.list[scan.index];
  const url = caseDetailUrl(item, scan.clientId);
  if (url) {
    location.assign(url);
    return;
  }
  // No id/href -> skip this row.
  scan.index += 1;
  await saveCaseScan(scan);
  return visitCaseIndex(scan);
}

async function finishCaseScan(scan) {
  scan.status = "done";
  scan.finishedAt = new Date().toISOString();
  await saveCaseScan(scan);
  publishCases(scan);
  if (scan.returnUrl && location.href !== scan.returnUrl) {
    location.assign(scan.returnUrl);
  }
}

let caseScanBusy = false;

async function maybeContinueCaseScan() {
  if (caseScanBusy) return;
  caseScanBusy = true;
  try {
    await _maybeContinueCaseScan();
  } finally {
    caseScanBusy = false;
  }
}

async function _maybeContinueCaseScan() {
  const { uw_case_scan: scan } = await chrome.storage.local.get("uw_case_scan");
  if (!scan || scan.status !== "running") return;
  if (Date.now() - new Date(scan.startedAt).getTime() > CASE_SCAN_TTL_MS) {
    scan.status = "done";
    await saveCaseScan(scan);
    return;
  }
  const ids = parseIdsFromUrl();
  if (ids.client_id && scan.clientId && ids.client_id !== scan.clientId) return;

  // Phase 1: navigated to the cases list; harvest it.
  if (scan.phase === "list") {
    if (await waitFor(() => caseTableReady(), 12000)) {
      await beginCaseWalk(scan);
    }
    return;
  }

  // On a case detail page: capture it, then move to the next case.
  if (parseCaseIdFromUrl()) {
    if (scan.index >= scan.total) return;

    let detail = { id: null, status: "", fields: {} };
    const deadline = Date.now() + 25000;
    let lastCount = -1;
    let stableTicks = 0;
    while (Date.now() < deadline) {
      detail = harvestCaseDetail();
      const count = Object.keys(detail.fields || {}).length;
      if (count === lastCount && count >= 3) {
        stableTicks += 1;
        if (stableTicks >= 2) break;
      } else {
        stableTicks = 0;
      }
      lastCount = count;
      await sleep(400);
    }

    if (!detail.fields || Object.keys(detail.fields).length === 0) {
      scan.note = "Waiting for case details to load\u2026";
      await saveCaseScan(scan);
      publishCases(scan);
      return;
    }

    scan.note = "";
    scan.details[scan.index] = {
      id: detail.id || (scan.list[scan.index] && scan.list[scan.index].id),
      status: detail.status,
      fields: detail.fields,
      capturedAt: new Date().toISOString(),
    };
    scan.index += 1;
    await saveCaseScan(scan);
    publishCases(scan);
    if (scan.index >= scan.total) {
      await finishCaseScan(scan);
    } else {
      visitCaseIndex(scan);
    }
    return;
  }

  // Back on the cases list with cases still to visit -> open the next one.
  if (caseTableReady() && scan.index < scan.total) {
    visitCaseIndex(scan);
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
  if (msg && msg.type === "ELIGIBILITY_RESCRAPE") {
    startEligibilityScan(msg).then((r) => sendResponse(r)).catch((e) =>
      sendResponse({ ok: false, error: String(e) })
    );
    return true;
  }
  if (msg && msg.type === "VERIFY_INSURANCE") {
    verifyInsurance(msg).then((r) => sendResponse(r)).catch((e) =>
      sendResponse({ ok: false, error: String(e) })
    );
    return true;
  }
  if (msg && msg.type === "CASE_RESCRAPE") {
    startCaseScan(msg).then((r) => sendResponse(r)).catch((e) =>
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
// Resume an in-progress screening / eligibility auto-walk if one survived a
// page navigation.
maybeContinueScreeningScan();
maybeContinueEligibilityScan();
maybeContinueCaseScan();

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
    maybeContinueEligibilityScan();
    maybeContinueCaseScan();
  }
}, 1000);
