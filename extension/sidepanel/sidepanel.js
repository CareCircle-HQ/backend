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
// Nice-to-have fields surfaced in the checklist but not blocking.
const RECOMMENDED_FIELDS = [];

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

// ---------- Client context + required checklist ----------
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
    ["Case ID", ctx.case_id || "—"],
    ["Screening ID", ctx.screening_id || "—"],
  ];
  box.innerHTML =
    "<dl>" +
    rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("") +
    "</dl>";
}

function renderChecklist(ctx) {
  const list = $("checklist");
  const items = [
    ...REQUIRED_FIELDS.map((f) => ({ ...f, required: true })),
    ...RECOMMENDED_FIELDS.map((f) => ({ ...f, required: false })),
  ];
  list.innerHTML = items
    .map((f) => {
      const ok = fieldPresent(ctx, f.key);
      const mark = ok ? "\u2705" : f.required ? "\u274C" : "\u26A0\uFE0F";
      const cls = ok ? "ok" : f.required ? "missing" : "optional";
      const tag = f.required ? "" : ' <span class="muted">(optional)</span>';
      return `<li class="${cls}"><span class="mark">${mark}</span> ${f.label}${tag}</li>`;
    })
    .join("");

  const met = requiredMet(ctx);
  $("validateBtn").disabled = !met;
  $("reqHint").textContent = met
    ? ""
    : "Waiting for required information to be detected from the page...";
}

async function loadContext() {
  const { uw_context } = await chrome.storage.local.get("uw_context");
  currentContext = uw_context || null;
  renderContext(currentContext);
  renderChecklist(currentContext);
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
    } else if (res.status === 404) {
      setValidation("err", "Client not found");
      setFormsUnlocked(false);
    } else if (res.status === 401 || res.status === 403) {
      setValidation("err", "Auth error");
      setFormsUnlocked(false);
    } else {
      setValidation("err", `Error ${res.status}`);
      setFormsUnlocked(false);
    }
  } catch (err) {
    setValidation("err", "Network error");
    setFormsUnlocked(false);
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
  if (name !== "client") loadFrame(name);
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

  // Live-update when the content script captures a new/changed client.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.uw_context) {
      currentContext = changes.uw_context.newValue;
      renderContext(currentContext);
      renderChecklist(currentContext);
      setValidation("", "");
      setFormsUnlocked(false);
      activateTab("client");
      maybeAutoValidate();
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
