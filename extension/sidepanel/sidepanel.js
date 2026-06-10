// Side panel logic: client detection, required-info gating, backend validation,
// and tabbed embedded forms. Auth is transparent via the baked service token.

// Form URLs per tab. Leave a value empty ("") to show a "not configured" notice.
// The E-Form is now a custom-built form (see the E-Form section below), not an
// embedded iframe, so it is intentionally absent from this map.
const FORMS = {
  nform: "", // TODO: set N-Form URL
  vform: "", // TODO: set V-Form URL
};
// Tabs that stay locked until the client is validated.
const GATED_TABS = ["eform", "nform", "vform"];

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
let autoScanTimer = null;
const loadedFrames = new Set();

// E-Form state: which client the form was last built for, and whether the user
// has edited it (so background data refreshes don't clobber their input).
let eformBuiltFor = null;
let eformDirty = false;

// Session / view state driving the full-page home overlay.
// sessionState: "ok" | "expired"; viewState.onClient: whether the user is
// currently viewing a client on Unite Us (vs. search / dashboard / login).
let sessionState = "ok";
let viewState = { onClient: false };

// Identity shown in the header. agentUser = auto-detected Unite Us employee
// (uw_uu_user); agentCode = the manually-entered Met Council Agent Code
// (uw_agent_code), gated on first load and used to prefill the E-Form.
let agentUser = null;
let agentCode = "";

// Decide whether to show the full-page home / session screen. Shown when the
// Unite Us session has expired, or when the user is not on a client page. The
// last client's captured data stays mounted underneath, so returning to that
// client shows it instantly (we only clear data when a DIFFERENT client loads).
function updateHomeOverlay() {
  const overlay = $("homeOverlay");
  if (!overlay) return;
  // The agent-code gate takes priority: until a code is set we keep the home
  // screen up and show the prompt instead of the client/session messaging.
  const needAgent = !agentCode;
  const expired = sessionState === "expired";
  // Track the LIVE view: show the home screen whenever the user isn't looking at
  // a client, even though the previous client's data stays cached underneath.
  const offClient = viewState && viewState.onClient === false;
  const show = needAgent || expired || offClient;

  overlay.classList.toggle("hidden", !show);
  overlay.classList.toggle("expired", expired && !needAgent);

  const gate = $("agentGate");
  const def = $("homeDefault");
  if (gate) gate.classList.toggle("hidden", !needAgent);
  if (def) def.classList.toggle("hidden", needAgent);
  if (needAgent) {
    const inp = $("agentCodeInput");
    if (inp && document.activeElement !== inp) inp.focus();
    return;
  }

  $("homeRetryBtn").classList.toggle("hidden", !expired);
  if (expired) {
    $("homeTitle").textContent = "Unite Us session expired";
    $("homeMsg").textContent =
      "Your Unite Us session ended. Log back in to Unite Us (or refresh the tab), then click Retry.";
  } else {
    $("homeTitle").textContent = "Open a client in Unite Us";
    $("homeMsg").textContent =
      "Search for a client on Unite Us and open their facesheet to start capturing their information here.";
  }
}

// Persist the entered Agent Code, clear the gate, and prefill the E-Form.
async function saveAgentCode() {
  const inp = $("agentCodeInput");
  const code = ((inp && inp.value) || "").trim();
  if (!code) {
    if (inp) inp.focus();
    return;
  }
  agentCode = code;
  try {
    await chrome.storage.local.set({ uw_agent_code: code });
  } catch (_) {}
  renderAgentTag();
  updateHomeOverlay();
  refreshEformIfPristine(); // push the code into the form if untouched
}

// Log out of the extension session: drop the Agent Code so the gate returns.
// (Does not touch the Unite Us session itself.)
async function logout() {
  agentCode = "";
  try {
    await chrome.storage.local.remove("uw_agent_code");
  } catch (_) {}
  const inp = $("agentCodeInput");
  if (inp) inp.value = "";
  renderAgentTag();
  updateHomeOverlay();
  refreshEformIfPristine(); // clear the prefilled code from the form if untouched
}

// Retry after a session expiry: force a profile reload, which bootstraps a
// fresh token (re-captured once the page makes its own API call post-login) and
// republishes the session state. If it succeeds the overlay clears itself.
async function retrySession() {
  $("homeMsg").textContent = "Reconnecting\u2026";
  try {
    await deepScrape();
  } catch (_) {}
}

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
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// ---------------------------------------------------------------------------
// Schema-driven Captured-vs-CRM comparison
// ---------------------------------------------------------------------------
// Each field: key = backend/CRM field name, label = display, aliases = lowercase
// substrings used to match the labels scraped from the Unite Us page.
const SCHEMA = {
  client: {
    title: "Client",
    fields: [
      { key: "first_name", label: "First Name", aliases: ["first name", "legal first"] },
      { key: "middle_name", label: "Middle Initial", aliases: ["middle name", "middle initial", "middle"] },
      { key: "last_name", label: "Last Name", aliases: ["last name", "legal last"] },
      { key: "suffix", label: "Suffix", aliases: ["suffix"] },
      { key: "date_of_birth", label: "Date of Birth", aliases: ["date of birth", "dob", "birth"] },
      { key: "gender", label: "Gender", aliases: ["gender", "sex assigned", "sex"] },
      { key: "sexuality", label: "Sexuality", aliases: ["sexuality", "sexual orientation"] },
      { key: "race", label: "Race", aliases: ["race"] },
      { key: "ethnicity", label: "Ethnicity", aliases: ["ethnicity"] },
      { key: "marital_status", label: "Marital Status", aliases: ["marital"] },
      { key: "citizenship", label: "Citizenship", aliases: ["citizen"] },
      { key: "client_phone_number", label: "Phone", aliases: ["phone", "tel", "mobile"] },
      { key: "phone_type", label: "Phone Type", aliases: ["phone type"] },
      { key: "client_email_address", label: "Email", aliases: ["email", "e-mail"] },
      { key: "preferred_spoken_language", label: "Spoken Language", aliases: ["spoken language", "language"] },
      { key: "preferred_written_language", label: "Written Language", aliases: ["written language"] },
      { key: "preferred_communication_method", label: "Contact Method", aliases: ["communication method", "preferred contact", "contact method"] },
      { key: "lead_source", label: "Lead Source", aliases: ["lead source", "source"] },
      { key: "enrollment_from", label: "Enrollment From", aliases: ["enrollment from", "enrolled from"] },
      { key: "consent_status", label: "Consent", aliases: ["consent"] },
      { key: "consented_at", label: "Consent Received", aliases: ["received on", "consent received"] },
      { key: "eligible_for", label: "Eligible For", aliases: ["eligible for", "eligible services"] },
      { key: "referred_for", label: "Referred For", aliases: ["referred for"] },
      { key: "is_family", label: "Is Family", aliases: ["is family", "family"] },
      { key: "total_family_members", label: "Family Members", aliases: ["family members", "household members"] },
      { key: "gross_monthly_income", label: "Monthly Income", aliases: ["monthly income", "gross income", "income"] },
      { key: "household_size", label: "Household Size", aliases: ["household size"] },
      { key: "adults_in_household", label: "Adults in Household", aliases: ["adults"] },
      { key: "children_in_household", label: "Children in Household", aliases: ["children"] },
      { key: "care_coordinator", label: "Care Coordinator", aliases: ["care coordinator", "coordinator"] },
      { key: "agent_code", label: "Agent Code", aliases: ["agent code", "agent"] },
    ],
  },
  address: {
    title: "Address",
    nested: "addresses",
    fields: [
      { key: "address_type", label: "Type", aliases: ["address type"] },
      { key: "line1", label: "Street", aliases: ["address", "street", "line 1"] },
      { key: "line2", label: "Street 2", aliases: ["line 2", "apt", "unit", "suite"] },
      { key: "city", label: "City", aliases: ["city"] },
      { key: "county", label: "County", aliases: ["county"] },
      { key: "state", label: "State", aliases: ["state"] },
      { key: "postal_code", label: "ZIP", aliases: ["zip", "postal"] },
    ],
  },
  // capKey = key in the page-captured coverage record; key = CRM field name.
  insurance: {
    title: "Insurance",
    nested: "insurances",
    fields: [
      { key: "plan_name", capKey: "plan_name", label: "Plan Name" },
      { key: "external_member_id", capKey: "member_id", label: "Member ID" },
      { key: "external_group_id", capKey: "group_id", label: "Group ID" },
      { key: "enrolled_at", capKey: "start_date", label: "Start Date" },
      { key: "expired_at", capKey: "end_date", label: "End Date" },
    ],
  },
  social_care_coverage: {
    title: "Social Care Coverage",
    fields: [
      { key: "plan_name", capKey: "plan_name", label: "Plan Name" },
      { key: "external_member_id", capKey: "member_id", label: "Member ID" },
      { key: "external_group_id", capKey: "group_id", label: "Group ID" },
      { key: "enrolled_at", capKey: "start_date", label: "Start Date" },
      { key: "expired_at", capKey: "end_date", label: "End Date" },
      { key: "status", capKey: "status", label: "Status" },
    ],
  },
  case: {
    title: "Cases",
    record: true,
    fields: [
      { key: "service_type", label: "Service Type", aliases: ["service type"] },
      { key: "service_subtype", label: "Service Subtype", aliases: ["subtype", "service type"] },
      { key: "case_status", label: "Status", aliases: ["status"] },
      { key: "provider_name", label: "Provider", aliases: ["provider"] },
      { key: "program_name", label: "Program", aliases: ["program"] },
      { key: "network_name", label: "Network", aliases: ["network"] },
      { key: "primary_worker_name", label: "Worker", aliases: ["worker", "navigator"] },
      { key: "created_at", label: "Created", aliases: ["created", "opened", "date"] },
      { key: "updated_at", label: "Updated", aliases: ["updated", "last updated"] },
    ],
  },
  screening: {
    title: "Screenings",
    record: true,
    fields: [
      { key: "screen_type", label: "Type", aliases: ["type"] },
      { key: "screen_status", label: "Status", aliases: ["status"] },
      { key: "provider_name", label: "Provider", aliases: ["provider"] },
      { key: "language", label: "Language", aliases: ["language"] },
      { key: "consent", label: "Consent", aliases: ["consent"] },
      { key: "screen_created_at", label: "Created", aliases: ["created", "date"] },
    ],
  },
  eligibility: {
    title: "Eligibility",
    record: true,
    fields: [
      { key: "screen_type", label: "Type", aliases: ["type"] },
      { key: "screen_status", label: "Status", aliases: ["status"] },
      { key: "eligible_status", label: "Eligible", aliases: ["eligible"] },
      { key: "provider_name", label: "Provider", aliases: ["provider"] },
      { key: "screen_created_at", label: "Created", aliases: ["created", "date"] },
    ],
  },
};

// Format any CRM/captured value for display ("" when empty).
function fmtValue(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (Array.isArray(v)) return v.filter(Boolean).join(", ");
  if (typeof v === "object") {
    // e.g. preferred_communication_time_of_day {monday:[...], ...}
    const parts = Object.entries(v)
      .filter(([, val]) => Array.isArray(val) && val.length)
      .map(([day, val]) => `${day}: ${val.join("/")}`);
    return parts.join("; ");
  }
  return String(v).trim();
}

// Build a searchable index of label->value pairs captured from the page.
function buildCapturedIndex(pairs) {
  return Object.entries(pairs || {}).map(([label, value]) => ({
    l: label.toLowerCase(),
    value: String(value || ""),
  }));
}

// Find the best captured value for a schema field using its aliases.
function findCaptured(index, field) {
  for (const alias of field.aliases || []) {
    const hit = index.find((e) => !e.l.includes("user") && e.l.includes(alias));
    if (hit && hit.value) return hit.value;
  }
  return "";
}

// One comparison row: status dot + field + captured + crm.
function compareRow(label, captured, crm) {
  const cap = fmtValue(captured);
  const cr = fmtValue(crm);
  let cls = "empty";
  if (cap && cr) cls = "both";
  else if (cap) cls = "cap-only";
  else if (cr) cls = "crm-only";
  const cell = (val) =>
    val ? escapeHtml(val) : '<span class="miss">\u2014</span>';
  return (
    `<tr class="cmp ${cls}"><td class="dot"></td>` +
    `<th>${escapeHtml(label)}</th>` +
    `<td>${cell(cap)}</td><td>${cell(cr)}</td></tr>`
  );
}

function compareTable(rows, capturedN, crmN, total, opts = {}) {
  return (
    `<table class="cmp-table${opts.noDot ? " no-dot" : ""}">` +
    `<thead><tr><th class="dot"></th><th>Field</th>` +
    `<th>Captured <span class="cnt">${capturedN}/${total}</span></th>` +
    `<th>CRM <span class="cnt">${crmN}/${total}</span></th></tr></thead>` +
    `<tbody>${rows}</tbody></table>`
  );
}

// Render a flat (single-object) section: client / address / insurance(one).
// capturedObj is the structured page-captured object keyed by field key/capKey;
// when a field key is present there (even if ""), it is authoritative and we do
// NOT fall back to fuzzy label matching (which can surface edit-mode garbage).
function renderObjectSection(def, capturedObj, capturedPairs, crmObj, opts = {}) {
  const index = buildCapturedIndex(capturedPairs);
  capturedObj = capturedObj || {};
  let capN = 0;
  let crmN = 0;
  const rows = def.fields
    .map((f) => {
      const ck = f.capKey || f.key;
      const cap = ck in capturedObj ? capturedObj[ck] : findCaptured(index, f);
      const crm = crmObj ? crmObj[f.key] : "";
      if (fmtValue(cap)) capN++;
      if (fmtValue(crm)) crmN++;
      return compareRow(f.label, cap, crm);
    })
    .join("");
  return compareTable(rows, capN, crmN, def.fields.length, opts);
}

// Render a record-type section (cases/screenings/eligibility): union by id.
function renderRecordSection(def, type, ctx) {
  const pageRecs = (ctx && ctx.records ? ctx.records : []).filter(
    (r) => r.type === type
  );
  const crmRecs = crm[type] || [];
  const idKey = { case: "case_id", screening: "enhanced_screen_id", eligibility: "eligibility_id" }[type];

  // Union of ids (plus page records without an id).
  const byId = new Map();
  crmRecs.forEach((c) => byId.set(String(c[idKey]), { crm: c, page: null }));
  pageRecs.forEach((p, i) => {
    const id = p.id ? String(p.id) : `pg:${i}`;
    if (byId.has(id)) byId.get(id).page = p;
    else byId.set(id, { crm: null, page: p });
  });

  if (!byId.size) {
    return '<p class="muted">None detected or imported yet.</p>';
  }

  let html = "";
  let n = 0;
  byId.forEach(({ crm: crmObj, page }, id) => {
    n++;
    const index = buildCapturedIndex(page && page.fields ? page.fields : {});
    let capN = 0;
    let crmN = 0;
    const rows = def.fields
      .map((f) => {
        const cap = findCaptured(index, f);
        const crmVal = crmObj ? crmObj[f.key] : "";
        if (fmtValue(cap)) capN++;
        if (fmtValue(crmVal)) crmN++;
        return compareRow(f.label, cap, crmVal);
      })
      .join("");
    const tags =
      (page ? '<span class="tag det">Detected</span>' : "") +
      (crmObj ? '<span class="tag imp">In CRM</span>' : "");
    html +=
      `<div class="rec-block"><div class="rec-head">` +
      `<span class="rec-n">${def.title.replace(/s$/, "")} ${n}</span> ${tags}</div>` +
      (id.startsWith("pg:") ? "" : `<div class="rec-id">${escapeHtml(id)}</div>`) +
      compareTable(rows, capN, crmN, def.fields.length) +
      `</div>`;
  });
  return html;
}

// Render a coverage group (insurance / social care coverage): union of the
// page-captured records for that group (already filtered by the content script
// per the workflow rules) and any matching CRM insurances, matched best-effort
// by plan name. Falls back to a single empty schema table.
function renderCoverageSection(ctx, group, def, crmList, opts = {}) {
  // We capture every record (active + inactive) so the CRM can reconcile, but
  // the profile only DISPLAYS active coverage (End Date >= today or no
  // expiration). Captured records carry the active flag; CRM records are shown
  // unless their status is inactive.
  const capList = (ctx && Array.isArray(ctx.insurance) ? ctx.insurance : []).filter(
    (c) => (c.group || "insurance") === group && c.active !== false
  );
  crmList = (crmList || []).filter(
    (c) => String(c.status || "").toLowerCase() !== "inactive"
  );
  if (!capList.length && !crmList.length) {
    return renderObjectSection(def, {}, {}, null, opts);
  }
  const norm = (s) => String(s || "").trim().toLowerCase();
  const usedCap = new Set();
  const blocks = [];
  crmList.forEach((crmObj, i) => {
    const capIdx = capList.findIndex(
      (c, idx) => !usedCap.has(idx) && norm(c.plan_name) && norm(c.plan_name) === norm(crmObj.plan_name)
    );
    let capObj = {};
    if (capIdx >= 0) {
      capObj = capList[capIdx];
      usedCap.add(capIdx);
    }
    blocks.push({ capObj, crmObj, label: crmObj.plan_name || `${def.title} ${i + 1}` });
  });
  capList.forEach((c, idx) => {
    if (usedCap.has(idx)) return;
    blocks.push({ capObj: c, crmObj: null, label: c.plan_name || def.title });
  });
  return blocks
    .map((b) => {
      const tags =
        (b.capObj && Object.keys(b.capObj).length ? '<span class="tag det">Detected</span>' : "") +
        (b.crmObj ? '<span class="tag imp">In CRM</span>' : "") +
        (b.capObj && b.capObj.active === false
          ? '<span class="tag inactive">Inactive</span>'
          : "");
      return (
        `<div class="rec-block"><div class="rec-head">` +
        `<span class="rec-n">${escapeHtml(b.label)}</span> ${tags}</div>` +
        renderObjectSection(def, b.capObj, {}, b.crmObj, opts) +
        `</div>`
      );
    })
    .join("");
}

// Compact summary card at the top of the Profile tab, populated from the data
// we already capture (falling back to the derived header fields).
function buildClientSummaryHtml(ctx) {
  const cap = (ctx.captured && ctx.captured.client) || {};
  const addr = (ctx.captured && ctx.captured.address) || {};

  const fullName =
    [cap.first_name, cap.middle_name, cap.last_name].filter(Boolean).join(" ") ||
    ctx.client_name ||
    "";
  const dob = cap.date_of_birth || ctx.client_dob || "";
  const phone = cap.client_phone_number || ctx.client_phone || "";

  let fullAddress = "";
  const cityLine = [addr.city, addr.state].filter(Boolean).join(", ");
  const addrParts = [addr.line1, [cityLine, addr.postal_code].filter(Boolean).join(" ").trim()]
    .filter(Boolean)
    .join(", ");
  fullAddress = addrParts || ctx.client_address || "";

  const row = (label, val) =>
    `<div class="sum-row"><span class="sum-k">${label}</span>` +
    `<span class="sum-v">${val ? escapeHtml(val) : '<span class="miss">\u2014</span>'}</span></div>`;

  return (
    `<div class="client-summary">` +
    row("Client ID", ctx.client_id) +
    row("Full Name", fullName) +
    row("DOB", dob) +
    row("Phone", phone) +
    row("Full Address", fullAddress) +
    `</div>`
  );
}

// ---- Client Snapshot (per-client tracking on the Profile tab) -------------
// Returns the captured walk data only when it belongs to the current client, so
// stale data from a previous client never leaks into the snapshot.
function snapDataFor(data, clientId) {
  return data && data.clientId === clientId ? data : null;
}

function parseDateMaybe(s) {
  if (!s) return null;
  const t = Date.parse(s);
  return isNaN(t) ? null : t;
}

// True when the (parseable) date is within the last `months` months.
function withinMonths(dateStr, months) {
  const t = parseDateMaybe(dateStr);
  if (t == null) return false;
  const cutoff = new Date();
  cutoff.setMonth(cutoff.getMonth() - months);
  return t >= cutoff.getTime();
}

// Short locale date; em dash when empty/unparseable.
function fmtDate(s) {
  const t = parseDateMaybe(s);
  return t == null ? s || "\u2014" : new Date(t).toLocaleDateString();
}

// Most recent date among rows; falls back to the first non-empty raw string when
// the dates aren't parseable.
function latestDateStr(rows, getDate) {
  let bestT = null;
  let bestStr = "";
  (rows || []).forEach((r) => {
    const s = getDate(r);
    if (!s) return;
    const t = parseDateMaybe(s);
    if (t != null) {
      if (bestT == null || t > bestT) {
        bestT = t;
        bestStr = s;
      }
    } else if (!bestStr) {
      bestStr = s;
    }
  });
  return bestStr;
}

// Minutes captured for one screening (from the "Screening Duration" Q&A answer
// like "8 Minutes", or the raw numeric duration).
function screeningDurationMinutes(s) {
  const d = s && s.detail;
  if (!d) return 0;
  const item = (d.items || []).find((it) => /screening duration/i.test(it.q || ""));
  const raw = item ? item.a : d.duration || "";
  const m = String(raw).match(/(\d+(?:\.\d+)?)/);
  return m ? parseFloat(m[1]) : 0;
}

// The "Client Snapshot" card shown right under the client summary table. Each
// row shows what we tracked; when we couldn't extract it, a "Pull" button lets
// the user fetch it on the spot (a small re-pull control is always available).
function buildSnapshotHtml(ctx) {
  const cap = (ctx.captured && ctx.captured.client) || {};
  const clientId = ctx.client_id;

  // Consent
  const consentStr = [cap.consent_status, cap.consented_at].filter(Boolean).join(" \u00b7 ");

  // Screenings
  const sd = snapDataFor(screeningData, clientId);
  const screenings = (sd && sd.screenings) || [];
  const scrMin = screenings.reduce((sum, s) => sum + screeningDurationMinutes(s), 0);
  const scrLast = latestDateStr(screenings, (s) => s.date);

  // Eligibility
  const ed = snapDataFor(eligibilityData, clientId);
  const eligs = (ed && ed.eligibilities) || [];
  const eligLast = latestDateStr(eligs, (e) => e.date);

  // Cases
  const cd = snapDataFor(caseData, clientId);
  const cases = (cd && cd.cases) || [];
  const caseLast = latestDateStr(cases, (c) => c.date_opened);
  const caseStatus = (c) => (c.detail && c.detail.status) || c.status || "";
  const openCases = cases.filter((c) => /open|active|authorized/i.test(caseStatus(c))).length;
  const closedCases = cases.filter((c) => /close|complete|resolved/i.test(caseStatus(c))).length;

  // CRM presence (from the backend lookup) + when the record was added.
  const crmClient = crm && crm.client;
  const crmExists = !!crmClient;
  const crmAdded = crmClient && (crmClient.created_at || crmClient.updated_at);

  // Status rule: green when at least one record's most-recent date is <= 6 months
  // old (and there is at least one record); red otherwise.
  const recentOk = (rows, last) => rows.length > 0 && withinMonths(last, 6);

  const mark = (ok) =>
    ok
      ? '<span class="snap-mark ok" title="OK">\u2713</span>'
      : '<span class="snap-mark bad" title="Needs attention">\u2717</span>';
  const repull = (key) =>
    `<button class="snap-pull" data-pull="${key}" title="Re-pull">\u21bb</button>`;
  const row = (label, valueHtml, key, missing, status) => {
    let v;
    if (missing) {
      v = key
        ? `<span class="snap-v miss">Not captured <button class="snap-pull" data-pull="${key}">Pull</button></span>`
        : '<span class="snap-v miss">Not captured</span>';
    } else {
      v = `<span class="snap-v">${valueHtml}${key ? " " + repull(key) : ""}</span>`;
    }
    const s = status == null ? "" : mark(status);
    return (
      `<div class="snap-row"><span class="snap-k">${label}</span>${v}` +
      `<span class="snap-s">${s}</span></div>`
    );
  };

  const dash = "\u2014";
  let html = '<div class="snapshot"><div class="snap-h">Client Snapshot</div>';
  html += row(
    "Consent",
    escapeHtml(consentStr),
    "consent",
    !consentStr,
    /accept/i.test(cap.consent_status || "")
  );
  html += row(
    "Met Council Screenings",
    `<strong>${screenings.length}</strong> \u00b7 last ${escapeHtml(scrLast || dash)}` +
      (scrMin ? ` \u00b7 ${scrMin} min total` : ""),
    "screening",
    !sd,
    recentOk(screenings, scrLast)
  );
  html += row(
    "Met Council Eligibility",
    `<strong>${eligs.length}</strong> \u00b7 last ${escapeHtml(eligLast || dash)}`,
    "eligibility",
    !ed,
    recentOk(eligs, eligLast)
  );
  html += row(
    "Met Council Cases",
    `<strong>${cases.length}</strong> (${openCases} open, ${closedCases} closed)` +
      ` \u00b7 last ${escapeHtml(caseLast || dash)}`,
    "cases",
    !cd,
    recentOk(cases, caseLast)
  );
  // Medicaid: true when any active captured insurance has plan_type "medicaid".
  const insurances = Array.isArray(ctx.insurance) ? ctx.insurance : [];
  const hasMedicaid = insurances.some(
    (i) =>
      i &&
      i.active !== false &&
      String(i.plan_type || "").toLowerCase().includes("medicaid")
  );
  html += row("Medicaid", hasMedicaid ? "Yes" : "No", null, false, hasMedicaid);

  // Social Care Coverage: count valid (active) social-care coverage records.
  // 0 -> red X, >=1 -> green check.
  const sccCount = insurances.filter(
    (i) => i && i.active !== false && i.group === "social_care_coverage"
  ).length;
  html += row(
    "Social Care Coverage",
    `<strong>${sccCount}</strong> active`,
    null,
    false,
    sccCount >= 1
  );

  html += row(
    "In CRM",
    crmExists ? `Yes \u00b7 added ${escapeHtml(fmtDate(crmAdded))}` : "Not in CRM",
    null,
    false,
    crmExists
  );
  html += "</div>";
  return html;
}

// Wire the snapshot "Pull"/re-pull buttons (re-bound after every render since
// renderComparison replaces #cmp-profile's innerHTML).
function bindSnapshotPull() {
  ["cmp-profile", "comparison"].forEach((boxId) => {
    const box = $(boxId);
    if (!box) return;
    box.querySelectorAll(".snap-pull").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        btn.disabled = true;
        const k = btn.getAttribute("data-pull");
        if (k === "consent") deepScrape();
        else if (k === "screening") scrRescan();
        else if (k === "eligibility") eligRescan();
        else if (k === "cases") caseRescan();
      });
    });
    // Collapsible Client/Address sections.
    box.querySelectorAll(".acc-head").forEach((h) => {
      h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
    });
  });
}

// ---- Consent gating -------------------------------------------------------
// A client can only be worked once consent is Accepted. "Unknown" (not yet
// captured), pending, declined, revoked and expired all count as "no consent".
const NO_CONSENT_MSG =
  "No consent on file \u2014 reload the Profile to capture consent first";

function consentAccepted() {
  const cap = currentContext && currentContext.captured && currentContext.captured.client;
  return /accept/i.test((cap && cap.consent_status) || "");
}

// Without consent the ONLY allowed action is the Profile reload (so the user can
// re-scan to pick up consent if it wasn't captured first time). Every other
// reload + all save buttons are disabled. When consent is present we hand control
// back to each feature's own enable logic.
function refreshConsentGate() {
  const ok = consentAccepted();
  const rescanBtns = ["scrRescanBtn", "eligRescanBtn", "caseRescanBtn"];
  if (!ok) {
    [...rescanBtns, "scrSaveBtn", "eligSaveBtn", "caseSaveBtn", "saveBtn"].forEach((id) => {
      const b = $(id);
      if (b) {
        b.disabled = true;
        b.title = NO_CONSENT_MSG;
      }
    });
  } else {
    rescanBtns.forEach((id) => {
      const b = $(id);
      if (b) {
        b.disabled = false;
        b.title = "";
      }
    });
    updateScrSaveBtn();
    updateEligSaveBtn();
    updateCaseSaveBtn();
    const saveBtn = $("saveBtn");
    if (saveBtn) saveBtn.disabled = !(currentContext && currentContext.client_id);
  }
  // The Profile reload buttons (rescanBtn / rescanBtn2) stay enabled regardless.
}

// Wrap a profile section in a collapsible accordion (same markup as the
// screening/case accordions). `open` controls the initial expanded state.
function profileAccordion(title, bodyHtml, open) {
  return (
    `<div class="acc${open ? " open" : ""}">` +
    `<div class="acc-head"><span class="acc-title">${escapeHtml(title)}</span></div>` +
    `<div class="acc-body">${bodyHtml}</div></div>`
  );
}

// Profile view = Client + Address + Insurance comparison.
function buildProfileHtml(ctx) {
  const pairs = ctx.scraped || {};
  const captured = ctx.captured || { client: {}, address: {} };
  const crmClient = crm.client;
  let html = buildClientSummaryHtml(ctx);
  html += buildSnapshotHtml(ctx);

  // Client: structured captured profile data wins; pairs are only a fallback.
  const clientPairs = {
    ...pairs,
    "date of birth": ctx.client_dob || pairs["DOB"] || "",
    phone: ctx.client_phone || "",
  };
  html += profileAccordion(
    SCHEMA.client.title,
    renderObjectSection(SCHEMA.client, captured.client, clientPairs, crmClient),
    true
  );

  // Address (current/primary): first CRM address vs captured primary address.
  const addr = crmClient && (crmClient.addresses || [])[0];
  const addrPairs = { ...pairs, address: ctx.client_address || pairs["ADDRESS"] || "" };
  html += profileAccordion(
    SCHEMA.address.title,
    renderObjectSection(SCHEMA.address, captured.address, addrPairs, addr, { noDot: true }),
    false
  );

  // Insurance + Social Care Coverage: captured records (filtered per the
  // workflow rules) unioned with CRM insurances, matched best-effort by plan
  // name. Both groups persist to the single CRM Insurance table, so we route
  // each stored plan to the section whose captured plan name it matches.
  const crmInsurances = (crmClient && crmClient.insurances) || [];
  const norm = (s) => String(s || "").trim().toLowerCase();
  const sccNames = new Set(
    (Array.isArray(ctx.insurance) ? ctx.insurance : [])
      .filter((c) => (c.group || "insurance") === "social_care_coverage")
      .map((c) => norm(c.plan_name))
      .filter(Boolean)
  );
  const crmScc = crmInsurances.filter((c) => sccNames.has(norm(c.plan_name)));
  const crmIns = crmInsurances.filter((c) => !sccNames.has(norm(c.plan_name)));
  html += profileAccordion(
    SCHEMA.insurance.title,
    renderCoverageSection(ctx, "insurance", SCHEMA.insurance, crmIns, { noDot: true }),
    false
  );
  html += profileAccordion(
    SCHEMA.social_care_coverage.title,
    renderCoverageSection(ctx, "social_care_coverage", SCHEMA.social_care_coverage, crmScc, { noDot: true }),
    false
  );
  return html;
}

// One record type comparison (cases / screenings / eligibility).
function buildRecordHtml(type, ctx) {
  return renderRecordSection(SCHEMA[type], type, ctx);
}

function renderComparison(ctx) {
  const empty = !ctx || !ctx.client_id;
  const fill = (id, html, emptyMsg) => {
    const el = $(id);
    if (el) el.innerHTML = empty ? `<p class="muted">${emptyMsg}</p>` : html;
  };
  const openMsg = "Open a Unite Us facesheet page to begin capturing data.";

  const profileHtml = empty ? "" : buildProfileHtml(ctx);
  const caseHtml = empty ? "" : buildRecordHtml("case", ctx);
  const screeningHtml = empty ? "" : buildRecordHtml("screening", ctx);
  const eligibilityHtml = empty ? "" : buildRecordHtml("eligibility", ctx);

  // Per-tab views. The dedicated Screening tab is rendered separately from the
  // captured screening detail (renderScreenings); screeningHtml below feeds only
  // the full CRM comparison on the Data tab.
  fill("cmp-profile", profileHtml, openMsg);
  // Note: cmp-cases and cmp-eligibility now host the captured accordions
  // (renderCases / renderEligibility); their CRM comparisons live on the Data tab.

  // Full comparison on the Data tab.
  fill(
    "comparison",
    profileHtml +
      `<h3>${SCHEMA.case.title}</h3>${caseHtml}` +
      `<h3>${SCHEMA.screening.title}</h3>${screeningHtml}` +
      `<h3>${SCHEMA.eligibility.title}</h3>${eligibilityHtml}`,
    openMsg
  );

  // Profile-tab save controls + last-updated line.
  const saveBtn = $("saveBtn");
  if (saveBtn) saveBtn.disabled = empty;
  renderProfileMeta();
  bindSnapshotPull();
  refreshConsentGate(); // disable everything but Profile reload when no consent
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

  // Full dump of every visible table (headers + rows) so we can see the exact
  // column layout for things like the Insurance / Social Care Coverage grids.
  const cellText = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const tables = [...document.querySelectorAll("table")]
    .filter((t) => t.offsetParent !== null)
    .slice(0, 12)
    .map((table) => {
      let headerCells = [...table.querySelectorAll("thead th")];
      if (!headerCells.length) headerCells = [...table.querySelectorAll("tr th")];
      const tHeaders = headerCells.map((th) => cellText(th.innerText));
      let rowEls = [...table.querySelectorAll("tbody tr")];
      if (!rowEls.length) rowEls = [...table.querySelectorAll("tr")];
      const rows = rowEls
        .slice(0, 25)
        .map((tr) =>
          [...tr.children]
            .filter((c) => c.tagName === "TD")
            .map((td) => cellText(td.innerText))
        )
        .filter((r) => r.length);
      return { headers: tHeaders, rows };
    });

  // Focused structural dump of the coverage sections so we can build accurate
  // parsers. Emits a compact element outline (tag + data-test attrs + leaf text)
  // plus a trimmed outerHTML fallback.
  const coverageDump = (title) => {
    const h = [...document.querySelectorAll("h1,h2,h3,h4,h5")].find(
      (el) => clean(el.innerText).toLowerCase() === title.toLowerCase()
    );
    if (!h) return { title, found: false };
    // The heading lives in a "header" wrapper; the actual records render as
    // SIBLINGS of that wrapper. Climb to the parent section and keep climbing
    // until the container holds more than just the title + "Add" button.
    let root = h.closest('[data-testid$="header"],[class*="header"]') || h.parentElement;
    for (let i = 0; i < 4; i++) {
      const up = root.parentElement;
      if (!up) break;
      root = up;
      const t = (root.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > clean(title).length + 40) break;
    }
    const lines = [];
    const walk = (el, depth) => {
      if (lines.length > 400 || depth > 16) return;
      const tag = el.tagName.toLowerCase();
      const id = el.id ? ` #${el.id}` : "";
      const dte = el.getAttribute("data-test-element");
      const dti = el.getAttribute("data-testid");
      const own = el.children.length
        ? ""
        : (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 80);
      const attrs = (dte ? ` dte=${dte}` : "") + (dti ? ` dti=${dti}` : "");
      if (id || attrs || own) {
        lines.push("  ".repeat(depth) + tag + id + attrs + (own ? ` "${own}"` : ""));
      }
      [...el.children].forEach((c) => walk(c, depth + 1));
    };
    walk(root, 0);
    return {
      title,
      found: true,
      text: (root.innerText || "").replace(/\s+/g, " ").trim().slice(0, 4000),
      outline: lines,
      html: (root.outerHTML || "").slice(0, 12000),
    };
  };
  const coverageSections = [
    coverageDump("Insurance Information"),
    coverageDump("Social Care Coverage"),
  ];

  // For each section heading, capture the raw text of its container so we can
  // see how insurance cards / labeled rows are structured even without tables.
  const sectionTexts = [...document.querySelectorAll("h1,h2,h3,h4")]
    .map((h) => {
      const title = clean(h.innerText);
      if (!title) return null;
      const container = h.closest("section,article,div") || h.parentElement;
      const text = container
        ? (container.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1200)
        : "";
      return { title, text };
    })
    .filter((s) => s && s.text)
    .slice(0, 40);

  // Screenings list: how each row links to its detail page, so we can drive the
  // auto-walk (anchor href / role=link / button / data-* / onclick).
  const screeningRows = (() => {
    const table = [...document.querySelectorAll("table")].find((t) => {
      const heads = [...t.querySelectorAll("th")].map((th) =>
        clean(th.innerText).toUpperCase()
      );
      return heads.includes("FORM") && heads.includes("ORGANIZATION");
    });
    if (!table) return { found: false };
    let rows = [...table.querySelectorAll("tbody tr")];
    if (!rows.length) {
      rows = [...table.querySelectorAll("tr")].filter((r) => r.querySelector("td"));
    }
    const sample = rows.slice(0, 2).map((tr) => ({
      text: clean(tr.innerText),
      anchors: [...tr.querySelectorAll("a[href]")].map((a) => a.getAttribute("href")),
      roleLinks: [...tr.querySelectorAll('[role="link"],[role="button"]')].map((e) =>
        clean(e.innerText)
      ),
      buttons: [...tr.querySelectorAll("button")].map((b) =>
        clean(b.innerText || b.getAttribute("aria-label"))
      ),
      dataAttrs: [...tr.querySelectorAll("[data-testid],[data-test-element],[data-id]")]
        .slice(0, 16)
        .map(
          (e) =>
            e.getAttribute("data-testid") ||
            e.getAttribute("data-test-element") ||
            e.getAttribute("data-id")
        ),
      html: (tr.outerHTML || "").slice(0, 3000),
    }));
    return { found: true, rowCount: rows.length, sample };
  })();

  // Screening DETAIL page: dump the HTML around the Questions and Results areas
  // so we can write reliable Q&A / result selectors.
  const screeningDetail = (() => {
    if (!/submission\//i.test(location.href)) return { onDetail: false };
    const grabAround = (re) => {
      const el = [
        ...document.querySelectorAll("h1,h2,h3,h4,h5,p,div,span,strong,label"),
      ].find((e) => {
        const t = clean(e.innerText);
        return t && t.length < 50 && re.test(t);
      });
      if (!el) return null;
      let c = el;
      for (let i = 0; i < 5; i++) {
        if (!c.parentElement) break;
        c = c.parentElement;
        if ((c.innerText || "").length > 500) break;
      }
      return (c.outerHTML || "").slice(0, 9000);
    };

    // Extract all Q&A pairs for debugging (same logic as harvestScreeningDetail)
    const qaPairs = [];
    const allElements = [...document.querySelectorAll("div, span, p, h1, h2, h3, h4, h5, h6")];
    for (let i = 0; i < allElements.length; i++) {
      const el = allElements[i];
      const text = clean(el.innerText);
      if (!text || text.length > 300) continue;
      if (!text.endsWith("?")) continue;
      // Skip section headers
      if (text.toLowerCase().includes("screening")) continue;

      let answerEl = null;
      if (el.nextElementSibling) answerEl = el.nextElementSibling;
      else if (el.parentElement && el.parentElement.nextElementSibling) {
        answerEl = el.parentElement.nextElementSibling;
      } else {
        for (let j = i + 1; j < allElements.length && j < i + 5; j++) {
          const nextEl = allElements[j];
          const nextText = clean(nextEl.innerText);
          if (!nextText || nextText.endsWith("?")) break;
          if (nextText.toLowerCase().includes("screening")) break;
          if (nextText && nextText.length < 200) {
            answerEl = nextEl;
            break;
          }
        }
      }

      if (answerEl) {
        const answer = clean(answerEl.innerText);
        if (answer && answer !== text && answer.length < 400) {
          qaPairs.push({ q: text.slice(0, 100), a: answer.slice(0, 100) });
        }
      }
    }

    // Extract all leaf nodes that might be results (for debugging over-capture)
    const allLeafTexts = [];
    document.querySelectorAll("*").forEach((el) => {
      if (el.children.length === 0) {
        const t = clean(el.innerText);
        if (t && t.length > 2 && t.length < 80) {
          allLeafTexts.push(t);
        }
      }
    });

    return {
      onDetail: true,
      questionsHtml: grabAround(/^screening questions/i),
      resultsHtml: grabAround(/^screening results/i),
      detailsHtml: grabAround(/^screening details/i),
      qaPairsFound: qaPairs.length,
      qaPairsSample: qaPairs.slice(0, 20),
      allLeafTextsSample: allLeafTexts.slice(0, 50),
    };
  })();

  // Eligibility DETAIL page (/eligibility/view/<id>): dump the structure so we
  // can write reliable Q&A selectors when the form-renderer classes differ.
  const eligibilityDetail = (() => {
    if (!/\/eligibility\/view\//i.test(location.href)) return { onDetail: false };
    const formRenderer = document.querySelectorAll(".ui-form-renderer-question-display").length;
    // Distinct class names of elements that contain visible label-like text.
    const classSet = new Set();
    document.querySelectorAll("div, span, p, dt, dd, label").forEach((el) => {
      if (el.children.length) return;
      const t = clean(el.innerText);
      if (t && t.length < 200 && /[?:]$/.test(t)) {
        const cls = (el.className || "").toString().slice(0, 120);
        if (cls) classSet.add(cls);
      }
    });
    // First 4000 chars of the main content HTML for selector inspection.
    const main =
      document.querySelector("main, [class*='renderer'], [class*='eligibility']") ||
      document.body;
    return {
      onDetail: true,
      formRendererCount: formRenderer,
      labelClassesSample: [...classSet].slice(0, 25),
      mainHtml: (main.outerHTML || "").slice(0, 9000),
    };
  })();

  // Case DETAIL page (/dashboard/cases/.../contact/...): dump structure so we
  // can map the visible fields (service type, status, dates, provider, program,
  // worker, description, outcome, service authorization, etc.) to the Case model.
  const caseDetail = (() => {
    if (!/\/dashboard\/cases\//i.test(location.href)) return { onDetail: false };
    // Label/value pairs: a label element whose next sibling holds the value.
    const lv = {};
    const addLV = (k, v) => {
      k = clean(k);
      v = clean(v);
      if (k && v && k !== v && !(k in lv)) lv[k] = v;
    };
    document
      .querySelectorAll("[class*='label'], [data-testid*='label'], dt, label, h3, h4, strong")
      .forEach((l) => {
        if (l.children.length) return;
        const sib = l.nextElementSibling;
        if (sib) addLV(l.innerText, sib.innerText);
        else if (l.parentElement) {
          const t = clean(l.parentElement.innerText);
          const k = clean(l.innerText);
          if (t.startsWith(k) && t.length > k.length) addLV(k, t.slice(k.length));
        }
      });
    // Distinct label-ish class names (leaf text ending with ':' or short bold).
    const classSet = new Set();
    document.querySelectorAll("div, span, p, dt, label").forEach((el) => {
      if (el.children.length) return;
      const t = clean(el.innerText);
      if (t && t.length < 60 && /:$/.test(t)) {
        const cls = (el.className || "").toString().slice(0, 120);
        if (cls) classSet.add(cls);
      }
    });
    // Section headings + their container text within the case content area.
    const sections = [...document.querySelectorAll("h1, h2, h3, h4, h5")]
      .map((h) => {
        const title = clean(h.innerText);
        if (!title) return null;
        const c = h.closest("section, article, div") || h.parentElement;
        const text = c ? (c.innerText || "").replace(/\s+/g, " ").trim().slice(0, 1500) : "";
        return { title, text };
      })
      .filter((s) => s && s.text)
      .slice(0, 40);
    const main =
      document.querySelector("main, [class*='case'], [class*='content']") || document.body;
    return {
      onDetail: true,
      labelValuePairs: lv,
      labelClassesSample: [...classSet].slice(0, 25),
      sections,
      mainHtml: (main.outerHTML || "").slice(0, 14000),
    };
  })();

  return {
    url: location.href,
    title: document.title,
    headings,
    tabsFound: tabs,
    expandablesSample: expandables,
    dataTestIds: testids,
    dataTestElements: dataTestElems,
    labelValueSample: pairs,
    tables,
    screeningRows,
    screeningDetail,
    eligibilityDetail,
    caseDetail,
    coverageSections,
    sectionTexts,
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

// Toggle a button's busy state without destroying its icon markup. Text
// buttons keep their label; icon buttons spin via the .busy CSS rule.
function setBtnBusy(btn, busy) {
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle("busy", busy);
}

// Deep profile/overview/records scrape (awaitable; walks data tabs in-place).
async function deepScrape() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.id != null) {
      await chrome.tabs.sendMessage(tab.id, { type: "RESCRAPE" });
    }
  } catch (_) {
    // content script may not be present on this tab
  }
}

const SCAN_MAX_MS = 5 * 60 * 1000; // give each auto-walk up to 5 min

// Resolve once the given storage key reports the scan finished (status==="done").
// Also bails early when shouldAbort() turns true (e.g. the user switched client
// or a newer scan started), so the caller's spinner can't get stuck waiting on a
// scan that will never finish for the page we've since navigated away from.
function waitForScanDone(key, timeoutMs, shouldAbort) {
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs;
    const check = async () => {
      if (typeof shouldAbort === "function" && shouldAbort()) return resolve(false);
      const obj = (await chrome.storage.local.get(key))[key];
      if (obj && obj.status === "done") return resolve(true);
      // Terminal failure states (no token / API error / session expired) - stop
      // waiting instead of hanging until the timeout.
      if (obj && (obj.status === "error" || obj.status === "auth")) return resolve(false);
      if (Date.now() > deadline) return resolve(false);
      setTimeout(check, 500);
    };
    check();
  });
}

// Kick off one auto-walk (screening / eligibility / cases) and wait for it to
// finish. Retries the start message because the tab may be mid-navigation when
// the previous walk hands back control.
async function runScanToCompletion(type, shouldAbort) {
  const map = {
    screening: { msg: "SCREENING_RESCRAPE", key: "uw_screenings", started: scanStarted, status: setScrStatus },
    eligibility: { msg: "ELIGIBILITY_RESCRAPE", key: "uw_eligibility", started: eligScanStarted, status: setEligStatus },
    cases: { msg: "CASE_RESCRAPE", key: "uw_cases", started: caseScanStarted, status: setCaseStatus },
  }[type];
  if (!map) return false;

  const clientId = currentContext && currentContext.client_id;
  const startDeadline = Date.now() + 20000;
  let started = false;
  while (Date.now() < startDeadline && !started) {
    if (typeof shouldAbort === "function" && shouldAbort()) return false;
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && tab.id != null && /app\.uniteus\.io/.test(tab.url || "")) {
      const sentAt = Date.now();
      try {
        await chrome.tabs.sendMessage(tab.id, { type: map.msg, clientId });
      } catch (_) {}
      started = await map.started(sentAt, 2000);
    }
    if (!started) await new Promise((r) => setTimeout(r, 500));
  }
  if (!started) {
    map.status("err", "Couldn't start \u2014 reload the Unite Us tab (F5), then retry");
    return false;
  }
  map.status("warn", "Loading from Unite Us\u2026");
  return waitForScanDone(map.key, SCAN_MAX_MS, shouldAbort);
}

// Grab everything: profile + all three section scans. These are all
// navigation-free API calls now, so they run CONCURRENTLY (they hit different
// endpoints and write to different storage keys). A generation counter lets a
// newer run (e.g. the user switched client mid-scan) cancel an older one at the
// next checkpoint instead of scanning the wrong client.
let scanGeneration = 0;
let fullScanRunning = false;

async function runFullScan() {
  const myGen = ++scanGeneration;
  const targetClient = currentContext && currentContext.client_id;
  if (!targetClient) return;
  // Cancel any pending auto profile-reload so it can't fire mid-walk and derail
  // a scan (the walks navigate the page; a stray deep scrape clicks tabs).
  clearTimeout(autoScanTimer);
  fullScanRunning = true;
  // Bail if a newer scan started or the user navigated to a different client.
  const stale = () =>
    myGen !== scanGeneration ||
    !currentContext ||
    currentContext.client_id !== targetClient;

  setBtnBusy($("rescanBtn"), true);
  setBtnBusy($("rescanBtn2"), true);
  try {
    await deepScrape();
    if (stale()) return;
    // Consent gate: without consent there's nothing to work, so only the Profile
    // is scraped and the other walks are skipped. Read uw_context straight from
    // storage because the in-memory currentContext may not have caught the
    // post-scrape update yet.
    const { uw_context: fresh } = await chrome.storage.local.get("uw_context");
    const cs =
      fresh && fresh.captured && fresh.captured.client && fresh.captured.client.consent_status;
    if (!/accept/i.test(cs || "")) {
      const msg = "No consent \u2014 skipped";
      setScrStatus("warn", msg);
      setEligStatus("warn", msg);
      setCaseStatus("warn", msg);
      return;
    }
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab && /app\.uniteus\.io/.test(tab.url || "")) {
      // Run all three concurrently - they're independent API scans now, so this
      // cuts the total wait to the slowest one instead of their sum.
      await Promise.all([
        runScanToCompletion("screening", stale),
        runScanToCompletion("eligibility", stale),
        runScanToCompletion("cases", stale),
      ]);
    }
  } catch (_) {
    // best-effort; per-tab reload buttons remain available
  } finally {
    // Only the latest run owns the buttons; don't un-busy a run still going.
    if (myGen === scanGeneration) {
      fullScanRunning = false;
      setBtnBusy($("rescanBtn"), false);
      setBtnBusy($("rescanBtn2"), false);
    }
  }
}

// Profile reload button handler.
async function rescan() {
  return runFullScan();
}

// ---------- Screenings tab (Met Council - SCN - PHS) ----------
// Captured by the content-script auto-walk and published to uw_screenings.
let screeningData = null;

function setScrStatus(state, message) {
  const badge = $("scrStatus");
  if (!badge) return;
  badge.className = "badge " + (state || "");
  badge.textContent = message || "";
}

function renderScrMeta() {
  const el = $("scrMeta");
  if (!el) return;
  const d = screeningData;
  if (!d || !d.screenings) {
    el.textContent = "";
    return;
  }
  if (d.note) {
    el.textContent = d.note;
    return;
  }
  const p = d.progress || { done: 0, total: 0 };
  if (d.status === "running") {
    el.textContent = `Scanning ${p.done}/${p.total}\u2026`;
  } else if (d.finishedAt) {
    const dt = new Date(d.finishedAt);
    el.textContent =
      `${d.screenings.length} screening(s) \u00b7 last scanned ` +
      (isNaN(dt.getTime()) ? d.finishedAt : dt.toLocaleString());
  } else {
    el.textContent = `${d.screenings.length} screening(s) found`;
  }
}

// One collapsible screening panel: header = form + status/date; body = meta,
// highlighted Screening Duration, screening-result chips, ordered Q&A.
function renderScreeningAccordion(s, i) {
  const d = s.detail;
  const statusLabel = /complete/i.test(s.status || "") ? "Completed" : s.status || "";
  const statusDate = [statusLabel, s.date].filter(Boolean).join(" ");
  const head =
    `<div class="acc-head"><span class="acc-title">${escapeHtml(
      s.form || `Screening ${i + 1}`
    )}</span>` + `<span class="acc-sub">${escapeHtml(statusDate)}</span></div>`;

  let body;
  if (!d) {
    body = '<p class="muted">Detail not captured yet \u2014 re-scan to fetch its answers.</p>';
  } else {
    // Duration display: prefer the Q&A item answer (already "8 Minutes"),
    // otherwise format the raw number from d.duration.
    const durItem = (d.items || []).find((it) => /screening duration/i.test(it.q || ""));
    let dur = "";
    if (durItem) {
      dur = durItem.a;
    } else if (d.duration) {
      dur = d.duration + (d.duration === "1" ? " Minute" : " Minutes");
    }
    const results = d.results || [];
    const hasQA = d.items && d.items.length > 0;

    let html = `<div class="scr-meta">`;
    html += `<div><span class="sum-k">Submitter</span>${escapeHtml(s.submitter || "\u2014")}</div>`;
    html += `<div><span class="sum-k">Status</span>${escapeHtml(statusDate || "\u2014")}</div>`;
    if (dur)
      html += `<div class="scr-duration"><span class="sum-k">Screening Duration</span><strong>${escapeHtml(
        dur
      )}</strong></div>`;
    html += `</div>`;

    if (results.length) {
      html +=
        `<div class="scr-results"><div class="scr-results-h">Screening Results (${results.length} needs identified)</div>` +
        results.map((r) => `<span class="chip">${escapeHtml(r)}</span>`).join("") +
        `</div>`;
    }

    if (hasQA) {
      html +=
        `<div class="scr-qa-h">Screening Questions</div>` +
        `<table class="qa-table"><tbody>` +
        d.items
          .map((it) => {
            const hl = /screening duration/i.test(it.q || "") ? " hl" : "";
            return `<tr class="qa${hl}"><th>${escapeHtml(it.q)}</th><td>${escapeHtml(it.a)}</td></tr>`;
          })
          .join("") +
        `</tbody></table>`;
    } else {
      html += `<p class="muted">No Q&A captured. Use Inspect tool on the screening detail page to debug.</p>`;
    }
    body = html;
  }
  return `<div class="acc${i === 0 ? " open" : ""}">${head}<div class="acc-body">${body}</div></div>`;
}

// Build an actionable empty-state for a data tab. If a scan already ran for this
// client but captured nothing, guide the user to this tab's own Re-scan (it runs
// on the settled page and is the most reliable). Otherwise show the generic hint.
function emptyTabMessage(d, matchesClient, label) {
  const ran = d && matchesClient && (d.status === "done" || d.finishedAt || d.note);
  if (ran) {
    return (
      '<div class="empty-state">' +
      `<p class="muted"><strong>No ${label} captured.</strong></p>` +
      `<p class="muted">If you can see ${label} on the Unite Us page, click ` +
      "<strong>Re-scan</strong> at the top of this tab to capture them directly " +
      "(the full Profile reload can miss them when the list is still loading).</p>" +
      "</div>"
    );
  }
  return `<p class="muted">No ${label} captured yet. Open a Unite Us facesheet and click Re-scan.</p>`;
}

function renderScreenings() {
  const box = $("cmp-screening");
  if (!box) return;
  renderScrMeta();
  const d = screeningData;
  const matchesClient =
    d && (!currentContext || !currentContext.client_id || d.clientId === currentContext.client_id);

  if (!d || !matchesClient || !d.screenings || !d.screenings.length) {
    box.innerHTML = emptyTabMessage(d, matchesClient, "Met Council screenings");
    return;
  }
  box.innerHTML = d.screenings.map((s, i) => renderScreeningAccordion(s, i)).join("");
  box.querySelectorAll(".acc-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
  });
  updateScrSaveBtn();
}

// Enable the Save button only when there are captured screenings (with details)
// for the current client.
function updateScrSaveBtn() {
  const btn = $("scrSaveBtn");
  if (!btn) return;
  const ok = consentAccepted();
  btn.disabled = !ok || !screeningsSaveable();
  btn.title = !ok
    ? NO_CONSENT_MSG
    : btn.disabled
    ? "Re-scan to capture screenings before saving"
    : "Save screenings to the CRM (client must exist first)";
}

// Poll storage to confirm the content script actually started the scan (it
// writes uw_screenings immediately). Detects an orphaned/old content script.
async function scanStarted(sinceMs, timeout) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const { uw_screenings } = await chrome.storage.local.get("uw_screenings");
    if (
      uw_screenings &&
      uw_screenings.scannedAt &&
      new Date(uw_screenings.scannedAt).getTime() >= sinceMs - 1500
    ) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function scrRescan(ev) {
  if (!consentAccepted()) return setScrStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("scrRescanBtn");
  setBtnBusy(btn, true);
  setScrStatus("warn", "Starting\u2026");
  const sentAt = Date.now();
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id == null || !/app\.uniteus\.io/.test(tab.url || "")) {
      setScrStatus("err", "Open a Unite Us facesheet tab first");
      return;
    }
    const clientId = currentContext && currentContext.client_id;
    // The page navigates during the walk, so the response may never arrive;
    // progress is reported via storage (uw_screenings) instead.
    chrome.tabs
      .sendMessage(tab.id, { type: "SCREENING_RESCRAPE", clientId })
      .catch(() => {});
    // Confirm the content script picked it up; if not, it's the stale-script case.
    if (await scanStarted(sentAt, 3000)) {
      setScrStatus("warn", "Walking screenings\u2026 keep this tab open");
    } else {
      setScrStatus("err", "Couldn't reach the page \u2014 reload the Unite Us tab (F5), then retry");
    }
  } catch (_) {
    setScrStatus("err", "Reload the Unite Us tab (F5), then retry");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Save captured screenings to the CRM ----------
// The backend ScreeningViewSet upserts on enhanced_screen_id (the submission
// UUID we captured). We generate deterministic UUIDv5 ids for the nested
// questions/answers/needs so repeated saves update in place instead of
// creating duplicates. A screening can only be saved once its client exists
// in the CRM (the Screening.subject_id -> Client FK requires it).

// Fixed fallback namespace (random UUID) used when a screening id is missing.
const SCR_NS_FALLBACK = "6f1d3c2a-8b4e-4f7a-9c1d-2e5a7b8c9d0e";

function uuidToBytes(uuid) {
  const hex = String(uuid || "").replace(/[^0-9a-f]/gi, "");
  if (hex.length < 32) return uuidToBytes(SCR_NS_FALLBACK);
  const bytes = new Uint8Array(16);
  for (let i = 0; i < 16; i++) bytes[i] = parseInt(hex.substr(i * 2, 2), 16);
  return bytes;
}

// Deterministic UUIDv5 (SHA-1) namespaced by the screening id.
async function uuidv5(namespaceUuid, name) {
  const nsBytes = uuidToBytes(namespaceUuid);
  const nameBytes = new TextEncoder().encode(name);
  const data = new Uint8Array(nsBytes.length + nameBytes.length);
  data.set(nsBytes, 0);
  data.set(nameBytes, nsBytes.length);
  const hashBuf = await crypto.subtle.digest("SHA-1", data);
  const h = new Uint8Array(hashBuf).slice(0, 16);
  h[6] = (h[6] & 0x0f) | 0x50; // version 5
  h[8] = (h[8] & 0x3f) | 0x80; // RFC 4122 variant
  const hx = [...h].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${hx.slice(0, 8)}-${hx.slice(8, 12)}-${hx.slice(12, 16)}-${hx.slice(16, 20)}-${hx.slice(20)}`;
}

const isDurationQ = (q) => /screening duration/i.test(q || "");

// Are there any captured screenings with detail items ready to save?
function screeningsSaveable() {
  const d = screeningData;
  if (!d || !Array.isArray(d.screenings)) return false;
  if (currentContext && currentContext.client_id && d.clientId !== currentContext.client_id) {
    return false;
  }
  return d.screenings.some(
    (s) => s.detail && Array.isArray(s.detail.items) && s.detail.items.length > 0
  );
}

// Build the array of screening upsert payloads from captured data.
async function buildScreeningPayloads(d, clientId) {
  const payloads = [];
  for (const s of d.screenings) {
    const det = s.detail;
    if (!det || !Array.isArray(det.items) || !det.items.length) continue;
    // enhanced_screen_id: prefer the detail submission UUID, fall back to row id.
    const screenId = det.id || s.id;
    if (!screenId) continue;

    const payload = {
      enhanced_screen_id: screenId,
      subject_id: clientId,
      performing_organization_name: s.org || "",
      screen_source: s.form || "",
      // NEW: Capture screening metadata from list view
      screen_type: s.form || "",  // Form name like "HM #3", "SCN", "PHS"
      screen_status: s.status || "",  // Status from list view
      provider_name: s.submitter || "",  // Submitter name
    };

    // Duration captured as minutes in the UI; store seconds in the model.
    if (det.duration && /^\d+$/.test(String(det.duration))) {
      payload.duration = parseInt(det.duration, 10) * 60;
    }

    // NEW: Parse created date from list view (format like "May 26, 2026")
    if (s.date) {
      const parsedDate = new Date(s.date);
      if (!isNaN(parsedDate.getTime())) {
        payload.screen_created_at = parsedDate.toISOString();
      }
    }

    // Answers (one per non-duration Q&A item), with a deterministic question.
    const answers = [];
    for (const it of det.items) {
      if (isDurationQ(it.q)) continue;
      const q = (it.q || "").trim();
      const a = (it.a || "").trim();
      if (!q) continue;
      const questionId = await uuidv5(screenId, "q|" + q);
      const answerId = await uuidv5(screenId, "a|" + q);
      answers.push({
        answer_id: answerId,
        answer_value: a,
        value_string: a,
        question: { question_id: questionId, question_primary_text: q },
      });
    }
    if (answers.length) payload.answers = answers;

    // Identified needs from the screening results chips.
    const results = Array.isArray(det.results) ? det.results : [];
    const needs = [];
    for (const name of results) {
      const n = (name || "").trim();
      if (!n) continue;
      const needId = await uuidv5(screenId, "n|" + n);
      needs.push({ identified_social_need_id: needId, identified_social_need_name: n });
    }
    if (needs.length) payload.identified_social_needs = needs;

    payloads.push(payload);
  }
  return payloads;
}

async function saveScreenings(ev) {
  if (!consentAccepted()) return setScrStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("scrSaveBtn");
  const ctx = currentContext;
  const d = screeningData;

  if (!ctx || !ctx.client_id) {
    setScrStatus("err", "No client detected");
    return;
  }
  if (!screeningsSaveable()) {
    setScrStatus("err", "Nothing to save \u2014 re-scan first");
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    setScrStatus("err", "No API token configured");
    return;
  }

  setBtnBusy(btn, true);
  setScrStatus("warn", "Checking client\u2026");
  try {
    // Guard: the client must already exist in the CRM (FK requirement).
    const clientRes = await fetch(
      `${cfg.backendUrl}/api/clients/${ctx.client_id}/`,
      { headers: authHeader(cfg) }
    );
    if (clientRes.status === 404) {
      setScrStatus("err", "Client not in CRM \u2014 save it on the Profile tab first");
      setClientImported(false);
      return;
    }
    if (clientRes.status === 401 || clientRes.status === 403) {
      setScrStatus("err", "Auth error");
      return;
    }
    if (!clientRes.ok) {
      setScrStatus("err", `Client check failed (${clientRes.status})`);
      return;
    }
    setClientImported(true);

    // Client exists -> build and upsert the screenings in one batch.
    setScrStatus("warn", "Saving screenings\u2026");
    const payloads = await buildScreeningPayloads(d, ctx.client_id);
    if (!payloads.length) {
      setScrStatus("err", "Nothing to save \u2014 re-scan first");
      return;
    }
    const res = await fetch(`${cfg.backendUrl}/api/screenings/bulk/`, {
      method: "POST",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(payloads),
    });
    if (res.ok || res.status === 207) {
      let body = {};
      try { body = await res.json(); } catch (_) {}
      const ok = body.succeeded != null ? body.succeeded : payloads.length;
      const failed = body.failed || 0;
      if (failed) {
        setScrStatus("warn", `Saved ${ok}, ${failed} failed`);
      } else {
        setScrStatus("ok", `Saved ${ok} screening(s) \u2713`);
      }
      await fetchCrm(cfg, ctx.client_id); // refresh CRM status
    } else if (res.status === 401 || res.status === 403) {
      setScrStatus("err", "Auth error");
    } else {
      let detail = `Error ${res.status}`;
      try { detail = summarizeErrors(await res.json()) || detail; } catch (_) {}
      setScrStatus("err", detail);
    }
  } catch (_) {
    setScrStatus("err", "Network error");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Eligibility tab (Met Council - SCN - PHS) ----------
// Captured by the content-script auto-walk and published to uw_eligibility.
let eligibilityData = null;

function setEligStatus(state, message) {
  const badge = $("eligStatus");
  if (!badge) return;
  badge.className = "badge " + (state || "");
  badge.textContent = message || "";
}

function renderEligMeta() {
  const el = $("eligMeta");
  if (!el) return;
  const d = eligibilityData;
  if (!d || !d.eligibilities) {
    el.textContent = "";
    return;
  }
  if (d.note) {
    el.textContent = d.note;
    return;
  }
  const p = d.progress || { done: 0, total: 0 };
  if (d.status === "running") {
    el.textContent = `Scanning ${p.done}/${p.total}\u2026`;
  } else if (d.finishedAt) {
    const dt = new Date(d.finishedAt);
    el.textContent =
      `${d.eligibilities.length} assessment(s) \u00b7 last scanned ` +
      (isNaN(dt.getTime()) ? d.finishedAt : dt.toLocaleString());
  } else {
    el.textContent = `${d.eligibilities.length} assessment(s) found`;
  }
}

// One collapsible eligibility panel: header = form + status/date; body = meta,
// eligible-program chips, ordered Q&A.
function renderEligibilityAccordion(s, i) {
  const d = s.detail;
  const statusLabel = /complete/i.test(s.status || "") ? "Complete" : s.status || "";
  const statusDate = [statusLabel, s.date].filter(Boolean).join(" ");
  const head =
    `<div class="acc-head"><span class="acc-title">${escapeHtml(
      s.form || `Eligibility ${i + 1}`
    )}</span>` + `<span class="acc-sub">${escapeHtml(statusDate)}</span></div>`;

  let body;
  if (!d) {
    body = '<p class="muted">Detail not captured yet \u2014 re-scan to fetch its answers.</p>';
  } else {
    const results = d.results || [];
    const hasQA = d.items && d.items.length > 0;

    let html = `<div class="scr-meta">`;
    html += `<div><span class="sum-k">Submitter</span>${escapeHtml(s.submitter || "\u2014")}</div>`;
    html += `<div><span class="sum-k">Status</span>${escapeHtml(statusDate || "\u2014")}</div>`;
    html += `<div><span class="sum-k">Organization</span>${escapeHtml(s.org || "\u2014")}</div>`;
    html += `</div>`;

    if (results.length) {
      html +=
        `<div class="scr-results"><div class="scr-results-h">Client May Be Eligible (${results.length})</div>` +
        results.map((r) => `<span class="chip">${escapeHtml(r)}</span>`).join("") +
        `</div>`;
    }

    if (hasQA) {
      html +=
        `<div class="scr-qa-h">Assessment Questions</div>` +
        `<table class="qa-table"><tbody>` +
        d.items
          .map((it) => `<tr class="qa"><th>${escapeHtml(it.q)}</th><td>${escapeHtml(it.a)}</td></tr>`)
          .join("") +
        `</tbody></table>`;
    } else {
      html += `<p class="muted">No Q&A captured.</p>`;
    }
    body = html;
  }
  return `<div class="acc${i === 0 ? " open" : ""}">${head}<div class="acc-body">${body}</div></div>`;
}

function renderEligibility() {
  const box = $("cmp-eligibility");
  if (!box) return;
  renderEligMeta();
  const d = eligibilityData;
  const matchesClient =
    d && (!currentContext || !currentContext.client_id || d.clientId === currentContext.client_id);

  if (!d || !matchesClient || !d.eligibilities || !d.eligibilities.length) {
    box.innerHTML = emptyTabMessage(d, matchesClient, "Met Council eligibility assessments");
    updateEligSaveBtn();
    return;
  }
  box.innerHTML = d.eligibilities.map((s, i) => renderEligibilityAccordion(s, i)).join("");
  box.querySelectorAll(".acc-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
  });
  updateEligSaveBtn();
}

// Enable the Save button only when there are captured assessments (with details).
function updateEligSaveBtn() {
  const btn = $("eligSaveBtn");
  if (!btn) return;
  const ok = consentAccepted();
  btn.disabled = !ok || !eligibilitySaveable();
  btn.title = !ok
    ? NO_CONSENT_MSG
    : btn.disabled
    ? "Re-scan to capture eligibility assessments before saving"
    : "Save eligibility assessments to the CRM (client must exist first)";
}

// Confirm the content script started the eligibility scan (writes uw_eligibility).
async function eligScanStarted(sinceMs, timeout) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const { uw_eligibility } = await chrome.storage.local.get("uw_eligibility");
    if (
      uw_eligibility &&
      uw_eligibility.scannedAt &&
      new Date(uw_eligibility.scannedAt).getTime() >= sinceMs - 1500
    ) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function eligRescan(ev) {
  if (!consentAccepted()) return setEligStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("eligRescanBtn");
  setBtnBusy(btn, true);
  setEligStatus("warn", "Starting\u2026");
  const sentAt = Date.now();
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id == null || !/app\.uniteus\.io/.test(tab.url || "")) {
      setEligStatus("err", "Open a Unite Us facesheet tab first");
      return;
    }
    const clientId = currentContext && currentContext.client_id;
    chrome.tabs
      .sendMessage(tab.id, { type: "ELIGIBILITY_RESCRAPE", clientId })
      .catch(() => {});
    if (await eligScanStarted(sentAt, 3000)) {
      setEligStatus("warn", "Walking assessments\u2026 keep this tab open");
    } else {
      setEligStatus("err", "Couldn't reach the page \u2014 reload the Unite Us tab (F5), then retry");
    }
  } catch (_) {
    setEligStatus("err", "Reload the Unite Us tab (F5), then retry");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Save captured eligibility assessments to the CRM ----------
// Upserts on eligibility_id (the /view/<id> UUID). The Eligibility model has no
// nested answers relation, so captured Q&A is stored inline in `responses` and
// the eligible-program chips go to `eligible_services`. Client must exist first.

function eligibilitySaveable() {
  const d = eligibilityData;
  if (!d || !Array.isArray(d.eligibilities)) return false;
  if (currentContext && currentContext.client_id && d.clientId !== currentContext.client_id) {
    return false;
  }
  return d.eligibilities.some(
    (s) => s.detail && Array.isArray(s.detail.items) && s.detail.items.length > 0
  );
}

// Build eligibility upsert payloads. Q&A is sent as normalized `answers`
// (one Answer + nested Question per item) exactly like screenings, with
// deterministic UUIDv5 ids so repeated saves update in place.
async function buildEligibilityPayloads(d, clientId) {
  const payloads = [];
  for (const s of d.eligibilities) {
    const det = s.detail;
    if (!det || !Array.isArray(det.items) || !det.items.length) continue;
    const eligId = det.id || s.id;
    if (!eligId) continue;

    const answers = [];
    for (const it of det.items) {
      const q = (it.q || "").trim();
      const a = (it.a || "").trim();
      if (!q) continue;
      const questionId = await uuidv5(eligId, "q|" + q);
      const answerId = await uuidv5(eligId, "a|" + q);
      answers.push({
        answer_id: answerId,
        answer_value: a,
        value_string: a,
        question: { question_id: questionId, question_primary_text: q },
      });
    }

    const payload = {
      eligibility_id: eligId,
      subject_id: clientId,
      performing_organization_name: s.org || "",
      screen_source: s.form || "",
      eligible_services: Array.isArray(det.results) ? det.results : [],
    };
    if (answers.length) payload.answers = answers;
    if (/complete/i.test(s.status || "")) payload.eligible_status = "eligible";
    payloads.push(payload);
  }
  return payloads;
}

async function saveEligibility(ev) {
  if (!consentAccepted()) return setEligStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("eligSaveBtn");
  const ctx = currentContext;
  const d = eligibilityData;

  if (!ctx || !ctx.client_id) {
    setEligStatus("err", "No client detected");
    return;
  }
  if (!eligibilitySaveable()) {
    setEligStatus("err", "Nothing to save \u2014 re-scan first");
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    setEligStatus("err", "No API token configured");
    return;
  }

  setBtnBusy(btn, true);
  setEligStatus("warn", "Checking client\u2026");
  try {
    // Guard: the client must already exist in the CRM (FK requirement).
    const clientRes = await fetch(
      `${cfg.backendUrl}/api/clients/${ctx.client_id}/`,
      { headers: authHeader(cfg) }
    );
    if (clientRes.status === 404) {
      setEligStatus("err", "Client not in CRM \u2014 save it on the Profile tab first");
      setClientImported(false);
      return;
    }
    if (clientRes.status === 401 || clientRes.status === 403) {
      setEligStatus("err", "Auth error");
      return;
    }
    if (!clientRes.ok) {
      setEligStatus("err", `Client check failed (${clientRes.status})`);
      return;
    }
    setClientImported(true);

    setEligStatus("warn", "Saving assessments\u2026");
    const payloads = await buildEligibilityPayloads(d, ctx.client_id);
    if (!payloads.length) {
      setEligStatus("err", "Nothing to save \u2014 re-scan first");
      return;
    }
    const res = await fetch(`${cfg.backendUrl}/api/eligibility/bulk/`, {
      method: "POST",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(payloads),
    });
    if (res.ok || res.status === 207) {
      let body = {};
      try { body = await res.json(); } catch (_) {}
      const ok = body.succeeded != null ? body.succeeded : payloads.length;
      const failed = body.failed || 0;
      if (failed) {
        setEligStatus("warn", `Saved ${ok}, ${failed} failed`);
      } else {
        setEligStatus("ok", `Saved ${ok} assessment(s) \u2713`);
      }
      await fetchCrm(cfg, ctx.client_id);
    } else if (res.status === 401 || res.status === 403) {
      setEligStatus("err", "Auth error");
    } else {
      let detail = `Error ${res.status}`;
      try { detail = summarizeErrors(await res.json()) || detail; } catch (_) {}
      setEligStatus("err", detail);
    }
  } catch (_) {
    setEligStatus("err", "Network error");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Cases tab (Met Council - SCN - PHS) ----------
// Captured by the content-script auto-walk and published to uw_cases.
let caseData = null;

// Display order for the captured case fields (key = uppercase page label).
const CASE_FIELD_LABELS = [
  ["PROGRAM", "Program"],
  ["NETWORK", "Network"],
  ["ORGANIZATION", "Organization"],
  ["PRIMARY WORKER", "Primary Worker"],
  ["CASE DESCRIPTION", "Description"],
  ["AUTHORIZATION STATUS", "Authorization Status"],
  ["AUTHORIZED AMOUNT", "Authorized Amount"],
  ["AUTHORIZED SERVICE DELIVERY DATE(S)", "Service Delivery Dates"],
  ["PROGRAM CAP", "Program Cap"],
  ["NOTES", "Notes"],
  ["UNITE US AUTHORIZATION ID", "Authorization ID"],
  ["SOCIAL CARE COVERAGE PLAN", "Coverage Plan"],
  ["SOCIAL CARE COVERAGE STATUS", "Coverage Status"],
];

function setCaseStatus(state, message) {
  const badge = $("caseStatus");
  if (!badge) return;
  badge.className = "badge " + (state || "");
  badge.textContent = message || "";
}

function renderCaseMeta() {
  const el = $("caseMeta");
  if (!el) return;
  const d = caseData;
  if (!d || !d.cases) {
    el.textContent = "";
    return;
  }
  if (d.note) {
    el.textContent = d.note;
    return;
  }
  const p = d.progress || { done: 0, total: 0 };
  if (d.status === "running") {
    el.textContent = `Scanning ${p.done}/${p.total}\u2026`;
  } else if (d.finishedAt) {
    const dt = new Date(d.finishedAt);
    el.textContent =
      `${d.cases.length} case(s) \u00b7 last scanned ` +
      (isNaN(dt.getTime()) ? d.finishedAt : dt.toLocaleString());
  } else {
    el.textContent = `${d.cases.length} case(s) found`;
  }
}

// One collapsible case panel: header = service type + status/date; body = the
// captured field/value pairs.
function renderCaseAccordion(c, i) {
  const d = c.detail;
  const status = (d && d.status) || c.status || "";
  const statusDate = [status, c.date_opened].filter(Boolean).join(" \u00b7 ");
  const title = c.service_type || (d && d.fields && d.fields["SERVICE TYPE"]) || `Case ${i + 1}`;
  const head =
    `<div class="acc-head"><span class="acc-title">${escapeHtml(title)}</span>` +
    `<span class="acc-sub">${escapeHtml(statusDate)}</span></div>`;

  let body;
  if (!d || !d.fields) {
    body = '<p class="muted">Detail not captured yet \u2014 re-scan to fetch its details.</p>';
  } else {
    const f = d.fields;
    let html = `<div class="scr-meta">`;
    html += `<div><span class="sum-k">Service Type</span>${escapeHtml(f["SERVICE TYPE"] || title || "\u2014")}</div>`;
    html += `<div><span class="sum-k">Status</span>${escapeHtml(status || "\u2014")}</div>`;
    html += `<div><span class="sum-k">Date Opened</span>${escapeHtml(f["DATE OPENED"] || c.date_opened || "\u2014")}</div>`;
    html += `</div>`;

    const rows = CASE_FIELD_LABELS.filter(([k]) => f[k]).map(
      ([k, label]) =>
        `<tr class="qa"><th>${escapeHtml(label)}</th><td>${escapeHtml(f[k])}</td></tr>`
    );
    if (rows.length) {
      html +=
        `<div class="scr-qa-h">Case Details</div>` +
        `<table class="qa-table"><tbody>${rows.join("")}</tbody></table>`;
    } else {
      html += `<p class="muted">No case fields captured.</p>`;
    }
    html += renderContractedServices(d.contracted_services);
    body = html;
  }
  return `<div class="acc${i === 0 ? " open" : ""}">${head}<div class="acc-body">${body}</div></div>`;
}

// Captured contracted services for a case. Highlights the fields the workflow
// cares about: service duration, invoice number, and invoice link.
const CONTRACTED_FIELD_LABELS = [
  ["fee_schedule_program_name", "Fee Schedule Program"],
  ["status", "Status"],
  ["unit_type", "Unit Type"],
  ["authorized_units", "Authorized Units"],
  ["authorized_amount", "Authorized Amount"],
  ["service_duration", "Service Duration"],
  ["service_starts_at", "Service Start"],
  ["service_ends_at", "Service End"],
  ["unite_us_authorization_id", "Authorization ID"],
  ["authorization_status", "Authorization Status"],
  ["invoice_number", "Invoice #"],
  ["invoice_status", "Invoice Status"],
  ["invoice_amount", "Invoice Amount"],
];

function renderContractedServices(list) {
  if (!Array.isArray(list) || !list.length) return "";
  let html = `<div class="scr-qa-h">Contracted Services (${list.length})</div>`;
  for (const cs of list) {
    const name = cs.name || cs.fee_schedule_program_name || cs.service_type || "Contracted Service";
    const rows = CONTRACTED_FIELD_LABELS.filter(([k]) => cs[k]).map(
      ([k, label]) => `<tr class="qa"><th>${escapeHtml(label)}</th><td>${escapeHtml(cs[k])}</td></tr>`
    );
    if (cs.invoice_url) {
      rows.push(
        `<tr class="qa"><th>Invoice Link</th><td>` +
        `<a href="${escapeHtml(cs.invoice_url)}" target="_blank" rel="noopener">View invoice</a></td></tr>`
      );
    }
    html +=
      `<div class="cs-block"><div class="cs-name">${escapeHtml(name)}</div>` +
      (rows.length
        ? `<table class="qa-table"><tbody>${rows.join("")}</tbody></table>`
        : `<p class="muted">No details captured.</p>`) +
      `</div>`;
  }
  return html;
}

function renderCases() {
  const box = $("cmp-cases");
  if (!box) return;
  renderCaseMeta();
  const d = caseData;
  const matchesClient =
    d && (!currentContext || !currentContext.client_id || d.clientId === currentContext.client_id);

  if (!d || !matchesClient || !d.cases || !d.cases.length) {
    box.innerHTML = emptyTabMessage(d, matchesClient, "Met Council cases");
    updateCaseSaveBtn();
    return;
  }
  box.innerHTML = d.cases.map((c, i) => renderCaseAccordion(c, i)).join("");
  box.querySelectorAll(".acc-head").forEach((h) => {
    h.addEventListener("click", () => h.parentElement.classList.toggle("open"));
  });
  updateCaseSaveBtn();
}

function updateCaseSaveBtn() {
  const btn = $("caseSaveBtn");
  if (!btn) return;
  const ok = consentAccepted();
  btn.disabled = !ok || !casesSaveable();
  btn.title = !ok
    ? NO_CONSENT_MSG
    : btn.disabled
    ? "Re-scan to capture cases before saving"
    : "Save cases to the CRM (client must exist first)";
}

// Confirm the content script started the case scan (writes uw_cases).
async function caseScanStarted(sinceMs, timeout) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    const { uw_cases } = await chrome.storage.local.get("uw_cases");
    if (
      uw_cases &&
      uw_cases.scannedAt &&
      new Date(uw_cases.scannedAt).getTime() >= sinceMs - 1500
    ) {
      return true;
    }
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

async function caseRescan(ev) {
  if (!consentAccepted()) return setCaseStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("caseRescanBtn");
  setBtnBusy(btn, true);
  setCaseStatus("warn", "Starting\u2026");
  const sentAt = Date.now();
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id == null || !/app\.uniteus\.io/.test(tab.url || "")) {
      setCaseStatus("err", "Open a Unite Us facesheet tab first");
      return;
    }
    const clientId = currentContext && currentContext.client_id;
    chrome.tabs.sendMessage(tab.id, { type: "CASE_RESCRAPE", clientId }).catch(() => {});
    if (await caseScanStarted(sentAt, 3000)) {
      setCaseStatus("warn", "Walking cases\u2026 keep this tab open");
    } else {
      setCaseStatus("err", "Couldn't reach the page \u2014 reload the Unite Us tab (F5), then retry");
    }
  } catch (_) {
    setCaseStatus("err", "Reload the Unite Us tab (F5), then retry");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Save captured cases to the CRM ----------
// Upserts on case_id (the /cases/.../<id> UUID). Client must exist first.

function casesSaveable() {
  const d = caseData;
  if (!d || !Array.isArray(d.cases)) return false;
  if (currentContext && currentContext.client_id && d.clientId !== currentContext.client_id) {
    return false;
  }
  return d.cases.some((c) => c.detail && c.detail.fields && (c.detail.id || c.id));
}

// MM/DD/YYYY -> YYYY-MM-DD (case detail uses US date format).
function parseUSDate(s) {
  const m = String(s || "").trim().match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (!m) return null;
  const [, mm, dd, yy] = m;
  return `${yy}-${mm.padStart(2, "0")}-${dd.padStart(2, "0")}`;
}

const CASE_STATUS_MAP = {
  OPEN: "open",
  CLOSED: "closed",
  MANAGED: "managed",
  DRAFT: "draft",
  CANCELLED: "cancelled",
  "PENDING AUTHORIZATION": "pending_authorization",
  "OFF PLATFORM": "off_platform",
};
const AUTH_STATUS_MAP = {
  ACCEPTED: "approved",
  APPROVED: "approved",
  PENDING: "pending",
  DENIED: "denied",
  EXPIRED: "expired",
  "NOT REQUIRED": "not_required",
};

function buildCasePayloads(d, clientId) {
  const payloads = [];
  for (const c of d.cases) {
    const det = c.detail;
    if (!det || !det.fields) continue;
    const caseId = det.id || c.id;
    if (!caseId) continue;
    const f = det.fields;

    const payload = {
      case_id: caseId,
      client_id: clientId,
      service_type: f["SERVICE TYPE"] || c.service_type || "",
      program_name: f["PROGRAM"] || "",
      network_name: f["NETWORK"] || "",
      provider_name: f["ORGANIZATION"] || c.org || "",
      primary_worker_name: f["PRIMARY WORKER"] || "",
      case_description: f["CASE DESCRIPTION"] || "",
      program_cap: f["PROGRAM CAP"] || "",
      authorization_note: f["NOTES"] || "",
      unite_us_authorization_id: f["UNITE US AUTHORIZATION ID"] || "",
      social_care_coverage_plan: f["SOCIAL CARE COVERAGE PLAN"] || "",
      social_care_coverage_status: f["SOCIAL CARE COVERAGE STATUS"] || "",
      case_status: CASE_STATUS_MAP[(det.status || c.status || "").toUpperCase()] || "open",
    };

    const opened = parseUSDate(f["DATE OPENED"] || c.date_opened);
    if (opened) payload.user_entered_opened_date = opened;
    const closed = parseUSDate(f["DATE CLOSED"]);
    if (closed) payload.user_entered_closed_date = closed;

    const authStatus = AUTH_STATUS_MAP[(f["AUTHORIZATION STATUS"] || "").toUpperCase()];
    if (authStatus) payload.service_authorization_status = authStatus;
    if (f["AUTHORIZATION STATUS"]) payload.service_authorization_status_label = f["AUTHORIZATION STATUS"];

    if (f["AUTHORIZED AMOUNT"]) payload.authorized_amount = f["AUTHORIZED AMOUNT"];

    const sd = f["AUTHORIZED SERVICE DELIVERY DATE(S)"];
    if (sd) {
      const parts = sd.split(/\s*[-\u2013]\s*/);
      const start = parseUSDate(parts[0]);
      const end = parseUSDate(parts[1]);
      if (start) payload.service_authorization_approval_starts_at = start;
      if (end) payload.service_authorization_approval_ends_at = end;
    }

    payloads.push(payload);
  }
  return payloads;
}

// Flatten every captured case's contracted services into upsert payloads for
// /api/contracted-services/bulk/. Keyed on contracted_service_id; case_id must
// reference a case that was just saved.
function buildContractedServicePayloads(d, clientId) {
  const payloads = [];
  for (const c of d.cases) {
    const det = c.detail;
    if (!det || !det.id) continue;
    const list = Array.isArray(det.contracted_services) ? det.contracted_services : [];
    for (const cs of list) {
      if (!cs || !cs.contracted_service_id) continue;
      payloads.push({ ...cs, case_id: det.id });
    }
  }
  return payloads;
}

// Best-effort upsert of contracted services after their parent cases are saved.
// Never throws: a CRM-side failure here must not mask a successful case save.
async function saveContractedServices(cfg, d, clientId) {
  const payloads = buildContractedServicePayloads(d, clientId);
  if (!payloads.length) return "";
  try {
    const res = await fetch(`${cfg.backendUrl}/api/contracted-services/bulk/`, {
      method: "POST",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(payloads),
    });
    if (res.ok || res.status === 207) {
      let body = {};
      try { body = await res.json(); } catch (_) {}
      const ok = body.succeeded != null ? body.succeeded : payloads.length;
      const failed = body.failed || 0;
      return failed ? ` + ${ok} service(s), ${failed} failed` : ` + ${ok} service(s)`;
    }
  } catch (_) {}
  return "";
}

async function saveCases(ev) {
  if (!consentAccepted()) return setCaseStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("caseSaveBtn");
  const ctx = currentContext;
  const d = caseData;

  if (!ctx || !ctx.client_id) {
    setCaseStatus("err", "No client detected");
    return;
  }
  if (!casesSaveable()) {
    setCaseStatus("err", "Nothing to save \u2014 re-scan first");
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    setCaseStatus("err", "No API token configured");
    return;
  }

  setBtnBusy(btn, true);
  setCaseStatus("warn", "Checking client\u2026");
  try {
    const clientRes = await fetch(
      `${cfg.backendUrl}/api/clients/${ctx.client_id}/`,
      { headers: authHeader(cfg) }
    );
    if (clientRes.status === 404) {
      setCaseStatus("err", "Client not in CRM \u2014 save it on the Profile tab first");
      setClientImported(false);
      return;
    }
    if (clientRes.status === 401 || clientRes.status === 403) {
      setCaseStatus("err", "Auth error");
      return;
    }
    if (!clientRes.ok) {
      setCaseStatus("err", `Client check failed (${clientRes.status})`);
      return;
    }
    setClientImported(true);

    setCaseStatus("warn", "Saving cases\u2026");
    const payloads = buildCasePayloads(d, ctx.client_id);
    if (!payloads.length) {
      setCaseStatus("err", "Nothing to save \u2014 re-scan first");
      return;
    }
    const res = await fetch(`${cfg.backendUrl}/api/cases/bulk/`, {
      method: "POST",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(payloads),
    });
    if (res.ok || res.status === 207) {
      let body = {};
      try { body = await res.json(); } catch (_) {}
      const ok = body.succeeded != null ? body.succeeded : payloads.length;
      const failed = body.failed || 0;
      // Contracted services reference a saved case, so save them only after the
      // cases upsert returns. The suffix reports how many services were saved.
      const csNote = await saveContractedServices(cfg, d, ctx.client_id);
      if (failed) {
        setCaseStatus("warn", `Saved ${ok}, ${failed} failed${csNote}`);
      } else {
        setCaseStatus("ok", `Saved ${ok} case(s)${csNote} \u2713`);
      }
      await fetchCrm(cfg, ctx.client_id);
    } else if (res.status === 401 || res.status === 403) {
      setCaseStatus("err", "Auth error");
    } else {
      let detail = `Error ${res.status}`;
      try { detail = summarizeErrors(await res.json()) || detail; } catch (_) {}
      setCaseStatus("err", detail);
    }
  } catch (_) {
    setCaseStatus("err", "Network error");
  } finally {
    setBtnBusy(btn, false);
  }
}

// ---------- Save / upsert captured client to the CRM ----------
// The backend ClientViewSet upserts on client_id, so a POST creates the client
// if missing and updates it (plus nested address / insurances) if it exists.

// Allowed enum values -> lowercase aliases used to match the page's labels.
const ENUMS = {
  gender: [
    ["male", "male"], ["female", "female"], ["nonbinary", "non-binary", "nonbinary"],
    ["transgender", "transgender"], ["declined", "declined"], ["unknown", "unknown"],
    ["other", "other"],
  ],
  marital_status: [
    ["single", "single"], ["married", "married"], ["partnered", "partner"],
    ["separated", "separated"], ["divorced", "divorced"], ["widowed", "widow"],
    ["unknown", "unknown"],
  ],
  consent_status: [
    ["accepted", "accept"], ["pending", "pending"], ["declined", "declin"],
    ["revoked", "revok"], ["expired", "expir"],
  ],
  phone_type: [
    ["mobile", "mobile", "cell"], ["home", "home"], ["work", "work"],
  ],
  // Maps coverage status (incl. social care "Enrolled") -> Insurance.status.
  coverage_status: [
    ["active", "active", "enrolled"],
    ["pending", "pending"],
    ["inactive", "inactive", "disenroll", "not enrolled"],
    ["expired", "expired", "expir"],
  ],
};

function toEnum(val, choices) {
  const v = String(val || "").trim().toLowerCase();
  if (!v) return "";
  for (const [value, ...aliases] of choices) {
    if (v === value || aliases.some((a) => v.includes(a))) return value;
  }
  return ""; // unknown -> omit so the backend keeps its default
}

// "MM/DD/YYYY" or ISO -> "YYYY-MM-DD"; "" / "--" / unparseable -> null.
function toIsoDate(s) {
  s = String(s || "").trim();
  if (!s || s === "--") return null;
  let m = s.match(/^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/);
  if (m) return `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}`;
  m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
  if (m) return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
  return null;
}

function toIsoDateTime(s) {
  const d = toIsoDate(s);
  return d ? `${d}T00:00:00Z` : null;
}

// Build the upsert payload from the captured (page) data only. Empty values are
// omitted so we never overwrite CRM data with blanks.
function buildClientPayload(ctx) {
  const cap = (ctx.captured && ctx.captured.client) || {};
  const addr = (ctx.captured && ctx.captured.address) || {};
  const payload = { client_id: ctx.client_id };
  const set = (k, v) => {
    if (v !== "" && v !== null && v !== undefined) payload[k] = v;
  };

  set("first_name", cap.first_name);
  set("last_name", cap.last_name);
  set("middle_name", cap.middle_name);
  set("suffix", cap.suffix);
  set("date_of_birth", toIsoDate(cap.date_of_birth));
  set("citizenship", cap.citizenship);
  set("race", cap.race);
  set("ethnicity", cap.ethnicity);
  set("sexuality", cap.sexuality);
  set("sexuality_other", cap.sexuality_other);
  set("gender", toEnum(cap.gender, ENUMS.gender));
  set("marital_status", toEnum(cap.marital_status, ENUMS.marital_status));
  set("consent_status", toEnum(cap.consent_status, ENUMS.consent_status));
  set("consented_at", toIsoDateTime(cap.consented_at));
  set("preferred_spoken_language", cap.preferred_spoken_language);
  set("preferred_written_language", cap.preferred_written_language);
  set("client_phone_number", cap.client_phone_number);
  set("phone_type", toEnum(cap.phone_type, ENUMS.phone_type));
  set("client_email_address", cap.client_email_address);
  set("care_coordinator", cap.care_coordinator);
  const income = String(cap.gross_monthly_income || "").replace(/[^0-9.]/g, "");
  set("gross_monthly_income", income);
  const hh = String(cap.household_size || "").replace(/[^0-9]/g, "");
  if (hh) payload.household_size = Number(hh);

  // Primary address (only when we captured something locatable).
  const a = {};
  const aset = (k, v) => { if (v) a[k] = v; };
  aset("address_type", String(addr.address_type || "current").toLowerCase());
  aset("line1", addr.line1);
  aset("line2", addr.line2);
  aset("city", addr.city);
  aset("county", addr.county);
  aset("state", String(addr.state || "").toUpperCase());
  aset("postal_code", addr.postal_code);
  if (a.line1 || a.city || a.postal_code) payload.addresses = [a];

  // Insurance + Social Care Coverage both persist to the CRM Insurance table
  // (the only coverage model). Map capKey -> field. We send EVERY captured
  // record (active and inactive) with an explicit status so the CRM mirrors
  // Unite Us, and -- when the coverage sections were actually on the page --
  // flag the payload as authoritative so the backend deactivates any stored
  // policy that is no longer present in Unite Us.
  const ins = (Array.isArray(ctx.insurance) ? ctx.insurance : [])
    .filter((c) => c.plan_name)
    .map((c) => {
      const o = { plan_name: c.plan_name };
      if (c.member_id) o.external_member_id = c.member_id;
      if (c.group_id) o.external_group_id = c.group_id;
      const en = toIsoDateTime(c.start_date);
      if (en) o.enrolled_at = en;
      const ex = toIsoDateTime(c.end_date);
      if (ex) o.expired_at = ex;
      // active flag is authoritative; fall back to the captured status text.
      o.status =
        c.active === true
          ? "active"
          : c.active === false
          ? "inactive"
          : toEnum(c.status, ENUMS.coverage_status) || "active";
      return o;
    });
  if (ctx.coverage_scraped) {
    payload.insurances = ins; // authoritative list (may be empty)
    payload.reconcile_insurances = true;
  } else if (ins.length) {
    payload.insurances = ins; // non-authoritative: fill only, never deactivate
  }

  return payload;
}

// Flatten a DRF error object into a short readable string.
function summarizeErrors(obj) {
  if (!obj || typeof obj !== "object") return "";
  const parts = [];
  for (const [k, v] of Object.entries(obj)) {
    const msg = Array.isArray(v) ? v.join(" ") : typeof v === "object" ? summarizeErrors(v) : String(v);
    parts.push(k === "detail" ? msg : `${k}: ${msg}`);
  }
  return parts.slice(0, 4).join(" | ");
}

function setSaveStatus(state, message) {
  const badge = $("saveStatus");
  if (!badge) return;
  badge.className = "badge " + (state || "");
  badge.textContent = message || "";
}

async function saveClient(ev) {
  if (!consentAccepted()) return setSaveStatus("warn", NO_CONSENT_MSG);
  const btn = (ev && ev.currentTarget) || $("saveBtn");
  const ctx = currentContext;
  if (!ctx || !ctx.client_id) {
    setSaveStatus("err", "No client detected");
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    setSaveStatus("err", "No API token configured");
    return;
  }
  const payload = buildClientPayload(ctx);
  if (!payload.first_name || !payload.last_name) {
    setSaveStatus("err", "Need first and last name before saving");
    return;
  }

  setBtnBusy(btn, true);
  setSaveStatus("warn", "Saving...");
  try {
    const res = await fetch(`${cfg.backendUrl}/api/clients/`, {
      method: "POST",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      setSaveStatus("ok", "Saved \u2713");
      setClientImported(true);
      await fetchCrm(cfg, ctx.client_id); // refresh CRM column + last-updated
    } else if (res.status === 401 || res.status === 403) {
      setSaveStatus("err", "Auth error");
    } else {
      let detail = `Error ${res.status}`;
      try {
        detail = summarizeErrors(await res.json()) || detail;
      } catch (_) {}
      setSaveStatus("err", detail);
    }
  } catch (_) {
    setSaveStatus("err", "Network error");
  } finally {
    setBtnBusy(btn, false);
  }
}

// Show when this client was last written to the CRM (auto-updated server-side).
function renderProfileMeta() {
  const el = $("profileUpdated");
  if (!el) return;
  const c = crm.client;
  if (c && c.last_synced_at) {
    const dt = new Date(c.last_synced_at);
    el.textContent =
      "Last updated in CRM: " +
      (isNaN(dt.getTime()) ? c.last_synced_at : dt.toLocaleString());
  } else if (currentContext && currentContext.client_id) {
    el.textContent = "Not saved to the CRM yet.";
  } else {
    el.textContent = "";
  }
}

// CRM import status. null = unknown/pending, true = imported (exists in CRM),
// false = not imported.
let importStatus = { client: null };
// Compact record labels for the CRM status table (keyed by type).
let backendRecords = { case: [], screening: [], eligibility: [] };
// Full CRM objects for the field-by-field comparison. client is the full
// client (with nested addresses/insurances) or null when not in the CRM yet.
let crm = { client: null, case: [], screening: [], eligibility: [] };

function resetCrm() {
  backendRecords = { case: [], screening: [], eligibility: [] };
  crm = { client: null, case: [], screening: [], eligibility: [] };
}

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

// Pull the full client (with nested addresses/insurances) plus all cases,
// screenings, and eligibility assessments from the backend. Feeds both the CRM
// status table and the field-by-field comparison. A missing client (404) is
// fine: the comparison then shows the captured column with an empty CRM column.
async function fetchCrm(cfg, clientId) {
  try {
    const headers = authHeader(cfg);
    const [clientRes, casesRes, scrRes, eligRes] = await Promise.all([
      fetch(`${cfg.backendUrl}/api/clients/${clientId}/`, { headers }),
      fetch(`${cfg.backendUrl}/api/cases/?client=${clientId}`, { headers }),
      fetch(`${cfg.backendUrl}/api/screenings/?client=${clientId}`, { headers }),
      fetch(`${cfg.backendUrl}/api/eligibility/?client=${clientId}`, { headers }),
    ]);
    crm = {
      client: clientRes.ok ? await clientRes.json() : null,
      case: casesRes.ok ? asList(await casesRes.json()) : [],
      screening: scrRes.ok ? asList(await scrRes.json()) : [],
      eligibility: eligRes.ok ? asList(await eligRes.json()) : [],
    };
    // Reflect CRM presence in the status table. A 404 here is expected (the
    // client simply isn't imported yet) -> mark "not imported"; only leave the
    // status untouched on transient errors (e.g. 5xx) so we don't show a false ❌.
    if (clientRes.ok) importStatus.client = true;
    else if (clientRes.status === 404) importStatus.client = false;
    backendRecords = {
      case: crm.case.map((c) => ({
        id: String(c.case_id),
        // Use the subtype to match what the Unite Us cases table displays.
        label: c.service_subtype || c.service_type || String(c.case_id),
      })),
      screening: crm.screening.map((s) => ({
        id: String(s.enhanced_screen_id),
        label: s.screen_type || String(s.enhanced_screen_id),
      })),
      eligibility: crm.eligibility.map((e) => ({
        id: String(e.eligibility_id),
        label: e.screen_type || e.eligible_status || String(e.eligibility_id),
      })),
    };
  } catch (_) {
    resetCrm();
  }
  renderCrmStatus(currentContext);
  renderComparison(currentContext);
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
  const { uw_context, uw_screenings, uw_eligibility, uw_cases, uw_session, uw_view } =
    await chrome.storage.local.get([
      "uw_context",
      "uw_screenings",
      "uw_eligibility",
      "uw_cases",
      "uw_session",
      "uw_view",
    ]);
  currentContext = uw_context || null;
  screeningData = uw_screenings || null;
  eligibilityData = uw_eligibility || null;
  caseData = uw_cases || null;
  sessionState = (uw_session && uw_session.state) || "ok";
  viewState = uw_view || { onClient: !!(currentContext && currentContext.client_id) };
  renderContext(currentContext);
  renderCrmStatus(currentContext);
  renderComparison(currentContext);
  renderScreenings();
  renderEligibility();
  renderCases();
  updateHomeOverlay();
  await maybeAutoValidate();
}

// Validate automatically once required info is present (service token = no login).
async function maybeAutoValidate() {
  if (!requiredMet(currentContext)) return;
  const cfg = await getConfig();
  if (cfg.token) validateClient();
}

// ---------- E-Form tab (custom enrollment intake) ----------
// A hand-built form (was an embedded GHL iframe). Fields are prepopulated from
// already-captured data: family size <- the eligibility Q&A, preferred language
// <- the captured client, and the delivery address <- the client's primary
// address. (Eligible For / Referred for live on earlier steps, so they're not
// repeated here.) The Save handler is a stub for now (collects + validates
// only); the submit destination is TBD.

const EFORM_LANGS = ["English", "Mandarin", "Spanish", "Yiddish", "Other"];
const EFORM_CHANNELS = ["Phone", "SMS", "Email"];
const EFORM_TIMES = [
  "Morning (9am - 12pm)",
  "Early Afternoon (12pm - 3pm)",
  "Late afternoon (3pm - 6pm)",
  "Evening (6pm - 8pm)",
];
const EFORM_TRANSFER = [
  "Transfer Successful (Verification agent Answered)",
  "Transfer Failed",
  "No Verification Needed",
];

// Map the form's human labels to the backend's enum codes (Client model).
const EFORM_CHANNEL_CODES = { Phone: "phone", SMS: "text", Email: "email" };
const EFORM_TIME_CODES = {
  "Morning (9am - 12pm)": "morning",
  "Early Afternoon (12pm - 3pm)": "early_afternoon",
  "Late afternoon (3pm - 6pm)": "late_afternoon",
  "Evening (6pm - 8pm)": "evening",
};
const EFORM_TRANSFER_CODES = {
  "Transfer Successful (Verification agent Answered)": "transfer_successful",
  "Transfer Failed": "transfer_failed",
  "No Verification Needed": "no_verification_needed",
};

// Data sources (all guarded against stale data for a different client) -------
const NUM_WORDS = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5,
  six: 6, seven: 7, eight: 8, nine: 9, ten: 10,
};

// Parse a count from an eligibility answer: a digit ("3", "3 members") or a
// spelled-out number ("three"). Null if neither is present.
function parseCountAnswer(a) {
  const s = String(a || "");
  const digit = s.match(/\d+/);
  if (digit) return parseInt(digit[0], 10);
  const word = s.toLowerCase().match(/\b(zero|one|two|three|four|five|six|seven|eight|nine|ten)\b/);
  return word ? NUM_WORDS[word[1]] : null;
}

// The medicaid-enrolled family-member count from the eligibility assessment
// answer to "How many immediate family members in your household are
// medicaid-enrolled ...". Drives "Is this a family?" and "Total Family
// Members". Null if not found. Matching is lenient (wording / answer format
// varies) but still scoped to the medicaid family-members question.
function eformFamilyCount() {
  const d = eligibilityData;
  if (!d || !Array.isArray(d.eligibilities)) return null;
  if (currentContext && currentContext.client_id && d.clientId !== currentContext.client_id) return null;
  for (const s of d.eligibilities) {
    const items = s.detail && Array.isArray(s.detail.items) ? s.detail.items : [];
    for (const it of items) {
      const q = it.q || "";
      if (/family members/i.test(q) && /medicaid/i.test(q)) {
        const n = parseCountAnswer(it.a);
        if (n != null) return n;
      }
    }
  }
  return null;
}

function eformPrimaryAddress() {
  const a = currentContext && currentContext.captured && currentContext.captured.address;
  if (a) return { street: a.line1 || "", city: a.city || "", state: a.state || "", zip: a.postal_code || "" };
  return { street: "", city: "", state: "", zip: "" };
}

function eformPreferredLanguage() {
  const c = currentContext && currentContext.captured && currentContext.captured.client;
  return (c && (c.preferred_spoken_language || c.preferred_written_language)) || "";
}

function eformAgentNote() {
  return agentCode
    ? "Prefilled from your saved Agent Code (set on the home screen)."
    : "No Agent Code saved \u2014 set one from the home screen (Logout to change).";
}

// HTML builders -------------------------------------------------------------
function efText(id, label, opts = {}) {
  const req = opts.req ? '<span class="req">*</span>' : "";
  const type = opts.number ? "number" : "text";
  const extra = opts.number ? ' inputmode="numeric" min="0" step="1"' : "";
  const val = opts.value != null ? ` value="${escapeHtml(opts.value)}"` : "";
  const ph = opts.ph ? ` placeholder="${escapeHtml(opts.ph)}"` : "";
  const note = opts.note ? `<div class="field-note">${escapeHtml(opts.note)}</div>` : "";
  return (
    `<div class="field" data-field="${id}">` +
    `<label class="field-label" for="${id}">${escapeHtml(label)}${req}</label>` +
    `<input type="${type}" id="${id}"${extra}${val}${ph} />${note}</div>`
  );
}

function efOptions(name, label, values, opts = {}) {
  const type = opts.type || "checkbox";
  const inline = opts.inline ? " inline" : "";
  const cols = opts.cols ? ` cols-${opts.cols}` : "";
  const req = opts.req ? '<span class="req">*</span>' : "";
  const selected = new Set((opts.selected || []).map((s) => String(s)));
  const note = opts.note ? `<div class="field-note">${escapeHtml(opts.note)}</div>` : "";
  const rows = values
    .map((v, i) => {
      const checked = selected.has(String(v)) ? " checked" : "";
      return (
        `<label class="opt"><input type="${type}" name="${name}" id="${name}_${i}" ` +
        `value="${escapeHtml(v)}"${checked} /> ${escapeHtml(v)}</label>`
      );
    })
    .join("");
  return (
    `<div class="field" data-field="${name}">` +
    `<label class="field-label">${escapeHtml(label)}${req}</label>` +
    `<div class="opt-group${inline}${cols}">${rows}</div>${note}</div>`
  );
}

// Build (or rebuild) the form for the current client, prepopulating from data.
function buildEform() {
  const form = $("eformForm");
  if (!form) return;
  const cid = currentContext && currentContext.client_id;
  if (!cid) {
    form.innerHTML = '<p class="muted">Open a Unite Us facesheet to load the enrollment form.</p>';
    return;
  }

  const fam = eformFamilyCount();
  const addr = eformPrimaryAddress();
  const lang = eformPreferredLanguage();
  const langSel = EFORM_LANGS.filter((l) => new RegExp(`\\b${l}\\b`, "i").test(lang));
  // Family iff more than one medicaid-enrolled member; default to "No" otherwise
  // (including when the count isn't found).
  const familyYesNo = fam != null && fam > 1 ? "Yes" : "No";

  let html = "";
  // Member ID: hidden; populated from the detected client (TODO: confirm via API).
  html += `<input type="hidden" id="ef_member_id" value="${escapeHtml(cid)}" />`;

  html += efText("ef_lead_source", "Lead Source", { req: true, ph: "e.g. Street Team, Referral" });

  html += efOptions("ef_is_family", "Is this a family?", ["Yes", "No"], {
    type: "radio",
    req: true,
    inline: true,
    selected: familyYesNo ? [familyYesNo] : [],
    note:
      fam != null
        ? `Derived from eligibility: ${fam} medicaid-enrolled family member(s).`
        : "Not found in eligibility \u2014 select manually.",
  });

  html += efText("ef_total_family", "Total Family Members (Incl.)", {
    req: true,
    number: true,
    value: fam != null ? String(fam) : "",
    note: fam != null ? "From the eligibility assessment." : "",
  });

  html += efOptions("ef_attestation", "Attestation Needed?", ["Yes", "No"], {
    type: "radio",
    req: true,
    inline: true,
    selected: ["No"],
  });

  html += efOptions("ef_channel", "Preferred Communication Channel", EFORM_CHANNELS, {
    type: "checkbox",
    req: true,
    inline: true,
    note: "Select at least one.",
  });

  html += efOptions("ef_time", "Preferred Communication Time of Day", EFORM_TIMES, {
    type: "checkbox",
    req: true,
    cols: 2,
  });

  html += efOptions("ef_language", "Preferred Communication Language", EFORM_LANGS, {
    type: "checkbox",
    req: true,
    cols: 2,
    selected: langSel,
    note: lang ? `Captured preference: ${lang}` : "",
  });

  html +=
    `<div class="field" data-field="ef_delivery">` +
    `<div class="subhead">` +
    `<label class="field-label">Delivery Address <span class="req">*</span></label>` +
    `<button type="button" class="link-btn" id="ef_addr_clear">Clear</button></div>` +
    `<div class="addr-grid">` +
    `<input class="full" type="text" id="ef_addr_street" placeholder="Street Address" value="${escapeHtml(addr.street)}" />` +
    `<input type="text" id="ef_addr_city" placeholder="City" value="${escapeHtml(addr.city)}" />` +
    `<input type="text" id="ef_addr_state" placeholder="State" value="${escapeHtml(addr.state)}" />` +
    `<input class="full" type="text" id="ef_addr_zip" placeholder="Zip Code" value="${escapeHtml(addr.zip)}" /></div>` +
    `<div class="field-note">Loaded from the client's primary address. Clear to enter a new delivery address.</div></div>`;

  html += efText("ef_call_duration", "Phone call duration when finished with Eligibility (minutes)", {
    req: true,
    number: true,
    ph: "Whole minutes",
    note: "BEFORE STARTING NAVIGATION.",
  });

  html += efOptions("ef_transfer", "Call Transfer Answered?", EFORM_TRANSFER, {
    type: "radio",
    req: true,
  });

  html += efText("ef_agent_code", "Agent Code", {
    req: true,
    value: agentCode,
    note: eformAgentNote(),
  });

  // Doctor/PCP Information Section
  html += "<hr/><h4>Doctor/PCP Information</h4>";

  html += efText("ef_doctors_name", "Doctor Name", {
    req: false,
    ph: "Dr. Jane Smith",
  });

  html += efText("ef_doctors_street_address", "Doctor Street Address", {
    req: false,
    ph: "123 Medical Plaza, Suite 100",
  });

  html += efText("ef_doctors_phone", "Doctor Phone", {
    req: false,
    ph: "(555) 123-4567",
    type: "tel",
  });

  html += efText("ef_doctors_fax", "Doctor Fax", {
    req: false,
    ph: "(555) 123-4568",
    type: "tel",
  });

  html += efText("ef_doctors_email", "Doctor Email", {
    req: false,
    ph: "doctor@clinic.com",
    type: "email",
  });

  form.innerHTML = html;
  updateEformSaveBtn();
}

// Activate: build once per client (or when emptied); refresh meta line.
function activateEform() {
  const cid = (currentContext && currentContext.client_id) || null;
  const form = $("eformForm");
  if (eformBuiltFor !== cid || !form || !form.querySelector("[data-field]")) {
    buildEform();
    eformBuiltFor = cid;
    eformDirty = false;
  }
  updateEformMeta();
}

// Rebuild from fresh data only if the user hasn't started editing, so a
// background scan completing doesn't wipe their input.
function refreshEformIfPristine() {
  if (eformBuiltFor == null) return;
  if (eformBuiltFor !== ((currentContext && currentContext.client_id) || null)) return;
  if (eformDirty) return;
  buildEform();
}

function eformChecked(name) {
  return [...document.querySelectorAll(`#eformForm input[name="${name}"]:checked`)].map((e) => e.value);
}

function collectEform() {
  const val = (id) => {
    const e = $(id);
    return e ? e.value.trim() : "";
  };
  return {
    member_id: val("ef_member_id"),
    lead_source: val("ef_lead_source"),
    is_family: eformChecked("ef_is_family")[0] || "",
    total_family_members: val("ef_total_family"),
    attestation_needed: eformChecked("ef_attestation")[0] || "",
    communication_channels: eformChecked("ef_channel"),
    communication_times: eformChecked("ef_time"),
    communication_languages: eformChecked("ef_language"),
    delivery_street: val("ef_addr_street"),
    delivery_city: val("ef_addr_city"),
    delivery_state: val("ef_addr_state"),
    delivery_zip: val("ef_addr_zip"),
    call_duration_minutes: val("ef_call_duration"),
    call_transfer: eformChecked("ef_transfer")[0] || "",
    agent_code: val("ef_agent_code"),
    // Doctor/PCP Information
    doctors_name: val("ef_doctors_name"),
    doctors_street_address: val("ef_doctors_street_address"),
    doctors_phone: val("ef_doctors_phone"),
    doctors_fax: val("ef_doctors_fax"),
    doctors_email: val("ef_doctors_email"),
  };
}

// Returns the list of invalid field keys (empty = valid).
function eformValidate(d) {
  const missing = [];
  const need = (ok, field) => {
    if (!ok) missing.push(field);
  };
  need(d.lead_source, "ef_lead_source");
  need(d.is_family, "ef_is_family");
  need(d.total_family_members !== "", "ef_total_family");
  need(d.attestation_needed, "ef_attestation");
  need(d.communication_channels.length, "ef_channel");
  need(d.communication_times.length, "ef_time");
  need(d.communication_languages.length, "ef_language");
  need(d.delivery_street && d.delivery_city && d.delivery_state && d.delivery_zip, "ef_delivery");
  need(d.call_duration_minutes !== "", "ef_call_duration");
  need(d.call_transfer, "ef_transfer");
  need(d.agent_code, "ef_agent_code");
  return missing;
}

function markEformInvalid(missing) {
  document.querySelectorAll("#eformForm .field").forEach((f) => f.classList.remove("invalid"));
  new Set(missing).forEach((id) => {
    const f = document.querySelector(`#eformForm .field[data-field="${id}"]`);
    if (f) f.classList.add("invalid");
  });
}

function setEformStatus(state, message) {
  const b = $("eformStatus");
  if (!b) return;
  b.className = "badge " + (state || "");
  b.textContent = message || "";
}

function updateEformMeta() {
  const el = $("eformMeta");
  if (!el) return;
  const cid = currentContext && currentContext.client_id;
  el.textContent = cid ? "Review & complete the enrollment form" : "";
}

function updateEformSaveBtn() {
  const btn = $("eformSaveBtn");
  if (!btn) return;
  const cid = currentContext && currentContext.client_id;
  const missing = cid ? eformValidate(collectEform()) : ["*"];
  btn.disabled = missing.length > 0;
  btn.title = btn.disabled ? "Complete all required fields to save" : "Save E-Form";
}

// Respond to user edits: enforce integer-only fields, mark dirty, refresh state.
function onEformChange(e) {
  const t = e && e.target;
  if (t && (t.id === "ef_total_family" || t.id === "ef_call_duration")) {
    const clean = t.value.replace(/[^0-9]/g, "");
    if (clean !== t.value) t.value = clean;
  }
  eformDirty = true;
  updateEformSaveBtn();
}

function onEformClick(e) {
  if (e.target && e.target.id === "ef_addr_clear") {
    ["ef_addr_street", "ef_addr_city", "ef_addr_state", "ef_addr_zip"].forEach((id) => {
      const el = $(id);
      if (el) el.value = "";
    });
    eformDirty = true;
    const s = $("ef_addr_street");
    if (s) s.focus();
    updateEformSaveBtn();
  }
}

// Map the collected E-Form values onto the Client model shape for a PATCH.
function buildEformPayload(d) {
  const isYes = (v) => v === "Yes";
  const toInt = (v) => (v === "" ? null : parseInt(v, 10));
  const codes = (arr, map) => arr.map((v) => map[v]).filter(Boolean);

  const payload = {
    client_id: d.member_id,
    lead_source: d.lead_source,
    is_family: isYes(d.is_family),
    total_family_members: toInt(d.total_family_members),
    attestation_needed: isYes(d.attestation_needed),
    communication_channels: codes(d.communication_channels, EFORM_CHANNEL_CODES),
    preferred_communication_times: codes(d.communication_times, EFORM_TIME_CODES),
    preferred_languages: d.communication_languages.slice(),
    call_duration_minutes: toInt(d.call_duration_minutes),
    call_transfer_answered: EFORM_TRANSFER_CODES[d.call_transfer] || "",
    agent_code: d.agent_code,
    // Doctor/PCP Information
    doctors_name: d.doctors_name || "",
    doctors_street_address: d.doctors_street_address || "",
    doctors_phone: d.doctors_phone || "",
    doctors_fax: d.doctors_fax || "",
    doctors_email: d.doctors_email || "",
    crm_source: d.lead_source || "",  // Store lead source as crm_source
  };

  // Delivery address -> nested upsert (address_type "delivery"). Flag whether it
  // differs from the client's captured primary address.
  const primary = eformPrimaryAddress();
  const norm = (s) => String(s || "").trim().toLowerCase();
  const sameAsPrimary =
    norm(primary.street) === norm(d.delivery_street) &&
    norm(primary.city) === norm(d.delivery_city) &&
    norm(primary.state) === norm(d.delivery_state) &&
    norm(primary.zip) === norm(d.delivery_zip);
  payload.different_delivery_address = !sameAsPrimary;
  payload.addresses = [
    {
      address_type: "delivery",
      line1: d.delivery_street,
      city: d.delivery_city,
      state: (d.delivery_state || "").trim().toUpperCase(),
      postal_code: d.delivery_zip,
      is_active: true,
    },
  ];
  return payload;
}

// Save the enrollment form: validates, then PATCHes the existing client. The
// client must already exist in the CRM (the E-Form tab only unlocks after the
// Profile validates the client), so a 404 means "save the Profile first".
async function saveEform(ev) {
  if (!consentAccepted()) return setEformStatus("warn", NO_CONSENT_MSG);
  const cid = currentContext && currentContext.client_id;
  if (!cid) {
    setEformStatus("err", "No client detected");
    return;
  }
  const data = collectEform();
  const missing = eformValidate(data);
  markEformInvalid(missing);
  if (missing.length) {
    setEformStatus("err", `Complete ${missing.length} required field(s)`);
    return;
  }
  const cfg = await getConfig();
  if (!cfg.token) {
    setEformStatus("err", "No API token configured");
    return;
  }

  const btn = (ev && ev.currentTarget) || $("eformSaveBtn");
  setBtnBusy(btn, true);
  setEformStatus("warn", "Saving\u2026");
  try {
    const res = await fetch(`${cfg.backendUrl}/api/clients/${cid}/`, {
      method: "PATCH",
      headers: { ...authHeader(cfg), "Content-Type": "application/json" },
      body: JSON.stringify(buildEformPayload(data)),
    });
    if (res.ok) {
      setEformStatus("ok", "Saved \u2713");
      eformDirty = false;
      await fetchCrm(cfg, cid); // refresh CRM column + last-updated
    } else if (res.status === 404) {
      setEformStatus("err", "Client not in CRM \u2014 save the Profile tab first");
    } else if (res.status === 401 || res.status === 403) {
      setEformStatus("err", "Auth error");
    } else {
      let detail = `Error ${res.status}`;
      try {
        detail = summarizeErrors(await res.json()) || detail;
      } catch (_) {}
      setEformStatus("err", detail);
    }
  } catch (_) {
    setEformStatus("err", "Network error");
  } finally {
    setBtnBusy(btn, false);
  }
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
      fetchCrm(cfg, currentContext.client_id);
    } else if (res.status === 404) {
      setValidation("err", "Client not found");
      setFormsUnlocked(false);
      setClientImported(false);
      // Not in the CRM yet: show captured data against an empty CRM column.
      resetCrm();
      renderComparison(currentContext);
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

// ---------- Logged-in agent badge ----------
// Shows the auto-detected Unite Us user (agentUser) alongside the manually-set
// Agent Code (agentCode), and toggles the logout button. Sources: uw_uu_user
// and uw_agent_code in local storage.
function renderAgentTag() {
  const el = $("agentTag");
  const lo = $("logoutBtn");
  const user = agentUser;
  const hasUser = !!(user && (user.name || user.email));
  if (el) {
    const parts = [];
    if (hasUser) parts.push(user.name || user.email);
    if (agentCode) parts.push(`#${agentCode}`);
    if (!parts.length) {
      el.classList.add("hidden");
      el.textContent = "";
    } else {
      el.textContent = parts.join("  \u00b7  ");
      el.classList.remove("hidden");
      el.classList.toggle("expired", hasUser && user.valid === false);
      const emailPart = hasUser && user.email ? ` <${user.email}>` : "";
      const statusPart = hasUser && user.valid === false ? " \u2014 session expired" : "";
      const userPart = hasUser ? `Logged-in Unite Us user: ${user.name || ""}${emailPart}${statusPart}` : "";
      const codePart = agentCode ? `Agent Code: ${agentCode}` : "";
      el.title = [userPart, codePart].filter(Boolean).join(" \u00b7 ");
    }
  }
  // Logout is available whenever an Agent Code session is active.
  if (lo) lo.classList.toggle("hidden", !agentCode);
}

async function loadAgent() {
  try {
    const { uw_uu_user, uw_agent_code } = await chrome.storage.local.get([
      "uw_uu_user",
      "uw_agent_code",
    ]);
    agentUser = uw_uu_user || null;
    agentCode = uw_agent_code || "";
  } catch (_) {}
  renderAgentTag();
  updateHomeOverlay();
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
  if (name === "eform") activateEform();
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      if (tab.disabled) return;
      activateTab(tab.dataset.tab);
    });
  });
  activateTab("profile"); // open by default
}

// ---------- Wire up ----------
function init() {
  initTabs();
  setFormsUnlocked(false);
  loadContext();
  loadAgent();

  $("validateBtn").addEventListener("click", validateClient);
  $("rescanBtn").addEventListener("click", rescan);
  $("rescanBtn2").addEventListener("click", rescan);
  $("saveBtn").addEventListener("click", saveClient);
  $("scrRescanBtn").addEventListener("click", scrRescan);
  $("scrSaveBtn").addEventListener("click", saveScreenings);
  $("eligRescanBtn").addEventListener("click", eligRescan);
  $("eligSaveBtn").addEventListener("click", saveEligibility);
  $("caseRescanBtn").addEventListener("click", caseRescan);
  $("caseSaveBtn").addEventListener("click", saveCases);
  $("eformSaveBtn").addEventListener("click", saveEform);
  // Delegated on the form so listeners survive rebuilds of its inner markup.
  const eform = $("eformForm");
  if (eform) {
    eform.addEventListener("input", onEformChange);
    eform.addEventListener("change", onEformChange);
    eform.addEventListener("click", onEformClick);
  }
  $("diagnosticBtn").addEventListener("click", runDiagnostic);
  $("copyReportBtn").addEventListener("click", copyReport);
  $("homeRetryBtn").addEventListener("click", retrySession);
  $("agentCodeSaveBtn").addEventListener("click", saveAgentCode);
  $("agentCodeInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveAgentCode();
    }
  });
  $("logoutBtn").addEventListener("click", logout);

  // Live-update when the content script captures a new/changed client.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    // Session expiry / recovery -> toggle the full-page home overlay.
    if (changes.uw_session) {
      sessionState = (changes.uw_session.newValue && changes.uw_session.newValue.state) || "ok";
      updateHomeOverlay();
    }
    // On/off a client page -> show the home overlay when off a client.
    if (changes.uw_view) {
      viewState = changes.uw_view.newValue || { onClient: false };
      updateHomeOverlay();
    }
    // Logged-in Unite Us user detected / session validity changed.
    if (changes.uw_uu_user) {
      agentUser = changes.uw_uu_user.newValue || null;
      renderAgentTag();
    }
    // Agent Code set / cleared (possibly from another panel instance).
    if (changes.uw_agent_code) {
      agentCode = changes.uw_agent_code.newValue || "";
      renderAgentTag();
      updateHomeOverlay();
      refreshEformIfPristine();
    }
    if (area === "local" && changes.uw_context) {
      const prev = currentContext;
      currentContext = changes.uw_context.newValue;
      const clientChanged =
        !prev ||
        prev.client_id !== (currentContext && currentContext.client_id);
      if (clientChanged) {
        importStatus = { client: null };
        resetCrm();
        // Drop the previous client's cases / screenings / eligibility so the
        // tabs don't show stale data (the content script also clears storage).
        screeningData = null;
        eligibilityData = null;
        caseData = null;
        setScrStatus("", "");
        setEligStatus("", "");
        setCaseStatus("", "");
        renderScreenings();
        renderEligibility();
        renderCases();
        // Reset the E-Form so it rebuilds (prepopulated) for the new client.
        eformBuiltFor = null;
        eformDirty = false;
        setEformStatus("", "");
        const ef = $("eformForm");
        if (ef) ef.innerHTML = '<p class="muted">Open a Unite Us facesheet to load the enrollment form.</p>';
        // Auto-load EVERYTHING for the new client via the API: Profile, then
        // (consent permitting) screenings, eligibility and cases. runFullScan
        // is consent-gated internally, so pre-consent only the Profile loads.
        // Debounced + delayed so the facesheet can settle first.
        if (currentContext && currentContext.client_id) {
          clearTimeout(autoScanTimer);
          autoScanTimer = setTimeout(() => {
            if (!fullScanRunning) runFullScan();
          }, 1500);
        }
      }
      renderContext(currentContext);
      renderCrmStatus(currentContext);
      renderComparison(currentContext);
      // Only reset gating/tab when a different client is detected, so a Re-scan
      // of the same client doesn't pull the user off the current tab.
      if (clientChanged) {
        setValidation("", "");
        setFormsUnlocked(false);
        activateTab("profile");
      }
      // A client context arriving means we're on a client page again.
      if (currentContext && currentContext.client_id) viewState.onClient = true;
      updateHomeOverlay();
      maybeAutoValidate();
    }
    // Screening auto-walk progress / results.
    if (area === "local" && changes.uw_screenings) {
      screeningData = changes.uw_screenings.newValue;
      const d = screeningData;
      if (d && d.status === "done") {
        setScrStatus("ok", `Done \u2014 ${d.screenings.length} screening(s)`);
      } else if (d && d.status === "running") {
        const p = d.progress || { done: 0, total: 0 };
        setScrStatus("warn", `Loading ${p.done}/${p.total}\u2026`);
      } else if (d && d.status === "auth") {
        setScrStatus("err", "Session expired");
      } else if (d && d.status === "error") {
        setScrStatus("err", d.note || "Couldn't load from Unite Us");
      } else if (!d) {
        setScrStatus("", "");
      }
      renderScreenings();
      renderComparison(currentContext); // keep the Profile snapshot in sync
    }
    // Eligibility auto-walk progress / results.
    if (area === "local" && changes.uw_eligibility) {
      eligibilityData = changes.uw_eligibility.newValue;
      const d = eligibilityData;
      if (d && d.status === "done") {
        setEligStatus("ok", `Done \u2014 ${d.eligibilities.length} assessment(s)`);
      } else if (d && d.status === "running") {
        const p = d.progress || { done: 0, total: 0 };
        setEligStatus("warn", `Loading ${p.done}/${p.total}\u2026`);
      } else if (d && d.status === "auth") {
        setEligStatus("err", "Session expired");
      } else if (d && d.status === "error") {
        setEligStatus("err", d.note || "Couldn't load from Unite Us");
      } else if (!d) {
        setEligStatus("", "");
      }
      renderEligibility();
      renderComparison(currentContext); // keep the Profile snapshot in sync
      refreshEformIfPristine(); // refresh prefilled "Eligible For" / family size
    }
    // Case auto-walk progress / results.
    if (area === "local" && changes.uw_cases) {
      caseData = changes.uw_cases.newValue;
      const d = caseData;
      if (d && d.status === "done") {
        setCaseStatus("ok", `Done \u2014 ${d.cases.length} case(s)`);
      } else if (d && d.status === "running") {
        const p = d.progress || { done: 0, total: 0 };
        setCaseStatus("warn", `Loading ${p.done}/${p.total}\u2026`);
      } else if (d && d.status === "auth") {
        setCaseStatus("err", "Session expired");
      } else if (d && d.status === "error") {
        setCaseStatus("err", d.note || "Couldn't load from Unite Us");
      } else if (!d) {
        setCaseStatus("", "");
      }
      renderCases();
      renderComparison(currentContext); // keep the Profile snapshot in sync
      refreshEformIfPristine(); // keep the E-Form in sync if untouched
    }
  });
}

document.addEventListener("DOMContentLoaded", init);
