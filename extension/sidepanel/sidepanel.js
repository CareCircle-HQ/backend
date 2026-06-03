// Side panel logic: client detection, required-info gating, backend validation,
// and tabbed embedded forms. Auth is transparent via the baked service token.

// Form URLs per tab. Leave a value empty ("") to show a "not configured" notice.
const FORMS = {
  eligibility: "https://links.carecirclecs.com/widget/form/fg6YKsPnZCb4qOtzZ1GU",
  verification: "", // TODO: set verification form URL
  form4: "", // TODO: set form 4 URL
};
// Tabs that stay locked until the client is validated.
const GATED_TABS = ["eligibility", "verification", "form4"];

// Information that must be scraped before we allow validation.
const REQUIRED_FIELDS = [
  { key: "client_id", label: "Client ID" },
  { key: "client_name", label: "Name" },
  { key: "client_dob", label: "Date of Birth" },
];

const BAKED = (typeof window !== "undefined" && window.EXT_CONFIG) || {};
const DEFAULT_BACKEND = BAKED.backendUrl || "http://127.0.0.1:8000";

const $ = (id) => document.getElementById(id);
let currentContext = null;
let isValidated = false;
const loadedFrames = new Set();

// ---------- Config (transparent auth) ----------
async function getConfig() {
  const { uw_config } = await chrome.storage.local.get("uw_config");
  const stored = uw_config || {};
  return {
    backendUrl: stored.backendUrl || DEFAULT_BACKEND,
    token: stored.token || BAKED.apiToken || "",
    scheme: stored.scheme || BAKED.authScheme || "Token",
  };
}

function authHeader(cfg) {
  return { Authorization: `${cfg.scheme} ${cfg.token}` };
}

// ---------- Client context + required fields ----------
function fieldPresent(ctx, key) {
  return !!(ctx && ctx[key] && String(ctx[key]).trim());
}

function requiredMet(ctx) {
  return REQUIRED_FIELDS.every((f) => fieldPresent(ctx, f.key));
}

function renderContext(ctx) {
  const box = $("context");
  if (!ctx || !ctx.client_id) {
    box.innerHTML =
      '<p class="muted">Open a Unite Us facesheet page to detect a client.</p>';
    return;
  }
  const rows = [
    ["Client ID", ctx.client_id],
    ["Name", ctx.client_name || "—"],
    ["DOB", ctx.client_dob || "—"],
    ["Phone", ctx.client_phone || "—"],
    ["Address", ctx.client_address || "—"],
    ["Case ID", ctx.case_id || "—"],
    ["Screening ID", ctx.screening_id || "—"],
  ];
  box.innerHTML =
    "<dl>" +
    rows
      .map(([k, v]) => `<dt>${k}</dt><dd>${escapeHtml(v)}</dd>`)
      .join("") +
    "</dl>";
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderScraped(ctx) {
  const box = $("scraped");
  const count = $("scrapeCount");
  const data = (ctx && ctx.scraped) || {};
  const keys = Object.keys(data);
  count.textContent = keys.length ? `(${keys.length})` : "";
  if (!keys.length) {
    box.innerHTML = '<p class="muted">No data scraped yet.</p>';
    return;
  }
  box.innerHTML =
    "<dl>" +
    keys
      .map(
        (k) =>
          `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(data[k])}</dd>`
      )
      .join("") +
    "</dl>";
}

// This function is serialized and injected into the page by executeScript,
// so it must be fully self-contained (no references to outer scope).
function inspectPageFn() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 120);
  const uniq = (a) => [...new Set(a.filter(Boolean))];

  const tabSelectors = [
    '[role="tab"]',
    '[role="tablist"] button',
    '[role="tablist"] a',
    '[class*="tab"]',
    '[data-testid*="tab"]',
    'nav button',
    '[class*="Tab"]',
  ];
  const tabs = uniq(
    [...document.querySelectorAll(tabSelectors.join(","))].map((el) =>
      clean(el.innerText || el.getAttribute("aria-label"))
    )
  ).slice(0, 40);

  const expandables = [...document.querySelectorAll("[aria-expanded]")]
    .map((el) => ({
      text: clean(el.innerText),
      expanded: el.getAttribute("aria-expanded"),
    }))
    .filter((x) => x.text)
    .slice(0, 40);

  const testids = uniq(
    [...document.querySelectorAll("[data-testid]")].map((el) =>
      el.getAttribute("data-testid")
    )
  ).slice(0, 80);
  const dataTestElems = uniq(
    [...document.querySelectorAll("[data-test-element]")].map((el) =>
      el.getAttribute("data-test-element")
    )
  ).slice(0, 80);

  const pairs = {};
  const add = (k, v) => {
    k = clean(k);
    v = clean(v);
    if (k && v && k !== v && !(k in pairs)) pairs[k] = v;
  };
  document.querySelectorAll("dl").forEach((dl) => {
    const dt = dl.querySelectorAll("dt");
    const dd = dl.querySelectorAll("dd");
    dt.forEach((d, i) => dd[i] && add(d.innerText, dd[i].innerText));
  });
  document.querySelectorAll("tr").forEach((tr) => {
    const th = tr.querySelector("th");
    const td = tr.querySelector("td");
    if (th && td) add(th.innerText, td.innerText);
  });
  document
    .querySelectorAll("[class*='label'],[data-testid*='label']")
    .forEach((l) => {
      if (l.nextElementSibling) add(l.innerText, l.nextElementSibling.innerText);
    });

  const headings = uniq(
    [...document.querySelectorAll("h1,h2,h3")].map((h) => clean(h.innerText))
  ).slice(0, 30);

  return {
    url: location.href,
    title: document.title,
    headings,
    tabsFound: tabs,
    expandablesSample: expandables,
    dataTestIds: testids,
    dataTestElements: dataTestElems,
    labelValueSample: pairs,
    counts: {
      dl: document.querySelectorAll("dl").length,
      tables: document.querySelectorAll("table").length,
      ariaTabs: document.querySelectorAll('[role="tab"]').length,
      ariaExpanded: document.querySelectorAll("[aria-expanded]").length,
      inputs: document.querySelectorAll("input,select,textarea").length,
    },
  };
}

async function runDiagnostic() {
  const btn = $("diagnosticBtn");
  const out = $("diagnosticOut");
  const copyBtn = $("copyReportBtn");
  btn.disabled = true;
  btn.textContent = "Inspecting...";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id == null) throw new Error("No active tab");
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: inspectPageFn,
    });
    const report = results && results[0] && results[0].result;
    out.value = JSON.stringify(report, null, 2);
    out.classList.remove("hidden");
    copyBtn.classList.remove("hidden");
  } catch (err) {
    out.value =
      "Could not inspect this tab. Make sure you are on a Unite Us page " +
      "(app.uniteus.io).\n\n" +
      (err && err.message ? err.message : String(err));
    out.classList.remove("hidden");
  } finally {
    btn.disabled = false;
    btn.textContent = "Inspect Page";
  }
}

async function copyReport() {
  const out = $("diagnosticOut");
  const copyBtn = $("copyReportBtn");
  try {
    await navigator.clipboard.writeText(out.value);
    copyBtn.textContent = "Copied!";
    setTimeout(() => (copyBtn.textContent = "Copy report"), 1500);
  } catch (_) {
    out.select();
  }
}

async function rescan() {
  const btn = $("rescanBtn");
  btn.disabled = true;
  btn.textContent = "Scanning...";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.id != null) {
      await chrome.tabs.sendMessage(tab.id, { type: "RESCRAPE" });
    }
  } catch (_) {
    // content script may not be present on this tab
  } finally {
    btn.disabled = false;
    btn.textContent = "Re-scan";
  }
}

function renderRecords(ctx) {
  const box = $("records");
  const recs = (ctx && ctx.records) || [];
  if (!recs.length) {
    box.innerHTML =
      '<p class="muted">No records found yet. Click Re-scan to gather cases, screenings, and eligibility.</p>';
    return;
  }
  const labels = {
    case: "Cases",
    screening: "Screenings",
    eligibility: "Eligibility",
  };
  const groups = { case: [], screening: [], eligibility: [] };
  recs.forEach((r) => {
    (groups[r.type] || (groups[r.type] = [])).push(r);
  });
  let html = "";
  ["case", "screening", "eligibility"].forEach((type) => {
    const list = groups[type] || [];
    if (!list.length) return;
    html += `<h3>${labels[type]} (${list.length})</h3><ul class="record-list">`;
    list.forEach((r) => {
      const idLine = r.id
        ? `<div class="rec-id">${escapeHtml(r.id)}</div>`
        : "";
      let body;
      if (r.fields && Object.keys(r.fields).length) {
        body = Object.entries(r.fields)
          .filter(([, v]) => v)
          .map(
            ([k, v]) =>
              `<div class="rec-field"><span class="rec-k">${escapeHtml(
                k
              )}</span> ${escapeHtml(v)}</div>`
          )
          .join("");
      } else {
        body = `<div class="rec-summary">${escapeHtml(r.summary || "")}</div>`;
      }
      html += `<li>${idLine}${body}</li>`;
    });
    html += "</ul>";
  });
  box.innerHTML = html;
}

// CRM import status. null = unknown/pending, true = imported (exists in CRM),
// false = not imported.
let importStatus = { client: null };
// Records found in the CRM backend for the current client (keyed by type).
let backendRecords = { case: [], screening: [], eligibility: [] };

function statusMark(state) {
  if (state === true) return '<span class="smark ok">\u2705</span>';
  if (state === false) return '<span class="smark missing">\u274C</span>';
  return '<span class="smark pending">\u2014</span>';
}

function setClientImported(state) {
  importStatus.client = state;
  renderCrmStatus(currentContext);
}

function asList(payload) {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.results)) return payload.results;
  return [];
}

// Pull the client's cases, screenings, and eligibility assessments from the
// backend so the CRM table can show them as imported, independent of scraping.
async function fetchBackendRecords(cfg, clientId) {
  try {
    const headers = authHeader(cfg);
    const [casesRes, scrRes, eligRes] = await Promise.all([
      fetch(`${cfg.backendUrl}/api/cases/?client=${clientId}`, { headers }),
      fetch(`${cfg.backendUrl}/api/screenings/?client=${clientId}`, { headers }),
      fetch(`${cfg.backendUrl}/api/eligibility/?client=${clientId}`, { headers }),
    ]);
    const cases = casesRes.ok ? asList(await casesRes.json()) : [];
    const screenings = scrRes.ok ? asList(await scrRes.json()) : [];
    const eligibility = eligRes.ok ? asList(await eligRes.json()) : [];
    backendRecords = {
      case: cases.map((c) => ({
        id: String(c.case_id),
        // Use the subtype to match what the Unite Us cases table displays.
        label: c.service_subtype || c.service_type || String(c.case_id),
      })),
      screening: screenings.map((s) => ({
        id: String(s.enhanced_screen_id),
        label: s.screen_type || String(s.enhanced_screen_id),
      })),
      eligibility: eligibility.map((e) => ({
        id: String(e.eligibility_id),
        label: e.screen_type || e.eligible_status || String(e.eligibility_id),
      })),
    };
  } catch (_) {
    backendRecords = { case: [], screening: [], eligibility: [] };
  }
  renderCrmStatus(currentContext);
}

// Merge page-detected records (ctx.records) with backend-imported records,
// keyed by id so the same record shows ticks in both columns.
function mergedRecords(ctx, type) {
  const map = new Map();
  (ctx && ctx.records ? ctx.records : [])
    .filter((r) => r.type === type)
    .forEach((r, i) => {
      const key = r.id || `pg:${type}:${i}`;
      const label =
        r.id ||
        (r.fields && (r.fields["Service Type"] || r.fields["Column 1"])) ||
        (r.summary || "").slice(0, 40) ||
        `${type} ${i + 1}`;
      map.set(key, { label, detected: true, imported: false });
    });
  (backendRecords[type] || []).forEach((b) => {
    if (map.has(b.id)) map.get(b.id).imported = true;
    else map.set(b.id, { label: b.label, detected: false, imported: true });
  });
  return [...map.values()];
}

function renderCrmStatus(ctx) {
  const box = $("crmStatus");
  const met = requiredMet(ctx);
  $("validateBtn").disabled = !met;
  $("reqHint").textContent = met
    ? ""
    : "Waiting for required client info (ID, name, DOB) to be detected...";

  if (!ctx || !ctx.client_id) {
    box.innerHTML = '<p class="muted">No client detected yet.</p>';
    return;
  }

  const clientRow =
    `<tr><th>${escapeHtml(ctx.client_name || "Client")}</th>` +
    `<td>${statusMark(true)}</td>` +
    `<td>${statusMark(importStatus.client)}</td></tr>`;

  const section = (title, type) => {
    const list = mergedRecords(ctx, type);
    if (!list.length) return "";
    let h = `<tr class="section"><th colspan="3">${title} (${list.length})</th></tr>`;
    list.forEach((r) => {
      h +=
        `<tr><th class="rec-row" title="${escapeHtml(r.label)}">${escapeHtml(
          r.label
        )}</th>` +
        `<td>${statusMark(r.detected || null)}</td>` +
        `<td>${statusMark(r.imported || null)}</td></tr>`;
    });
    return h;
  };

  box.innerHTML =
    '<table class="status-table">' +
    "<thead><tr><th></th><th>Detected</th><th>Imported</th></tr></thead>" +
    "<tbody>" +
    clientRow +
    section("Cases", "case") +
    section("Screenings", "screening") +
    section("Eligibility", "eligibility") +
    "</tbody></table>";
}

async function loadContext() {
  const { uw_context } = await chrome.storage.local.get("uw_context");
  currentContext = uw_context || null;
  renderContext(currentContext);
  renderCrmStatus(currentContext);
  renderScraped(currentContext);
  renderRecords(currentContext);
  await maybeAutoValidate();
}

// Validate automatically once required info is present (service token = no login).
async function maybeAutoValidate() {
  if (!requiredMet(currentContext)) return;
  const cfg = await getConfig();
  if (cfg.token) validateClient();
}

// ---------- Validation ----------
function setValidation(state, message) {
  const badge = $("validationStatus");
  badge.className = "badge " + (state || "");
  badge.textContent = message || "";
}

function setFormsUnlocked(unlocked) {
  isValidated = unlocked;
  document.querySelectorAll(".tab").forEach((tab) => {
    if (GATED_TABS.includes(tab.dataset.tab)) {
      tab.disabled = !unlocked;
      tab.classList.toggle("locked", !unlocked);
    }
  });
}

async function validateClient() {
  if (!requiredMet(currentContext)) return;
  const cfg = await getConfig();
  if (!cfg.token) {
    setValidation("err", "No API token configured");
    return;
  }
  setValidation("warn", "Checking...");
  try {
    const res = await fetch(
      `${cfg.backendUrl}/api/clients/${currentContext.client_id}/`,
      { headers: authHeader(cfg) }
    );
    if (res.status === 200) {
      setValidation("ok", "Valid \u2713");
      setFormsUnlocked(true);
      setClientImported(true);
      fetchBackendRecords(cfg, currentContext.client_id);
    } else if (res.status === 404) {
      setValidation("err", "Client not found");
      setFormsUnlocked(false);
      setClientImported(false);
    } else if (res.status === 401 || res.status === 403) {
      setValidation("err", "Auth error");
      setFormsUnlocked(false);
      setClientImported(null);
    } else {
      setValidation("err", `Error ${res.status}`);
      setFormsUnlocked(false);
      setClientImported(null);
    }
  } catch (err) {
    setValidation("err", "Network error");
    setFormsUnlocked(false);
    setClientImported(null);
  }
}

// ---------- Tabs / panels ----------
function loadFrame(name) {
  if (loadedFrames.has(name)) return;
  const url = FORMS[name];
  const frame = $(`frame-${name}`);
  const placeholder = $(`ph-${name}`);
  if (!frame) return;
  if (url) {
    frame.src = url;
    frame.classList.remove("hidden");
    if (placeholder) placeholder.classList.add("hidden");
    loadedFrames.add(name);
  } else {
    frame.classList.add("hidden");
    if (placeholder) placeholder.classList.remove("hidden");
  }
}

function activateTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name)
  );
  document.querySelectorAll(".panel").forEach((p) =>
    p.classList.toggle("active", p.dataset.panel === name)
  );
  if (name in FORMS) loadFrame(name);
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (tab.disabled) return;
      activateTab(tab.dataset.tab);
    });
  });
  activateTab("client"); // open by default
}

// ---------- Wire up ----------
function init() {
  initTabs();
  setFormsUnlocked(false);
  loadContext();

  $("validateBtn").addEventListener("click", validateClient);
  $("rescanBtn").addEventListener("click", rescan);
  $("diagnosticBtn").addEventListener("click", runDiagnostic);
  $("copyReportBtn").addEventListener("click", copyReport);

  // Live-update when the content script captures a new/changed client.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.uw_context) {
      const prev = currentContext;
      currentContext = changes.uw_context.newValue;
      const clientChanged =
        !prev ||
        prev.client_id !== (currentContext && currentContext.client_id);
      if (clientChanged) {
        importStatus = { client: null };
        backendRecords = { case: [], screening: [], eligibility: [] };
      }
      renderContext(currentContext);
      renderCrmStatus(currentContext);
      renderScraped(currentContext);
      renderRecords(currentContext);
      // Only reset gating/tab when a different client is detected, so a Re-scan
      // of the same client doesn't pull the user off the Detected Data tab.
      if (clientChanged) {
        setValidation("", "");
        setFormsUnlocked(false);
        activateTab("client");
      }
      maybeAutoValidate();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
