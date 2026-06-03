// Content script for https://scnlp.metcouncil.org/agentforms/*
// Runs in every frame (including the side panel iframe). Finds the
// "Enrollment Platform Member ID" field and fills it with the client_id
// captured from the Unite Us URL.

const TARGET_LABEL = "enrollment platform member id";
let filledValue = null;

function normalize(s) {
  return (s || "").toLowerCase().replace(/\s+/g, " ").trim();
}

// React/Angular friendly value setter that triggers change detection.
function setNativeValue(el, value) {
  const proto = el instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (setter) setter.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur", { bubbles: true }));
}

function matchesTarget(text) {
  return normalize(text).includes(TARGET_LABEL);
}

function findFieldByLabel() {
  // 1) <label for="..."> or <label><input></label>
  for (const label of document.querySelectorAll("label")) {
    if (!matchesTarget(label.textContent)) continue;
    const forId = label.getAttribute("for");
    if (forId) {
      const el = document.getElementById(forId);
      if (el) return el;
    }
    const nested = label.querySelector("input, textarea");
    if (nested) return nested;
    // input as a following sibling / within the same field container
    const container = label.closest("div, fieldset, .form-group, .field") || label.parentElement;
    if (container) {
      const el = container.querySelector("input, textarea");
      if (el) return el;
    }
  }
  return null;
}

function findFieldByAttributes() {
  const candidates = document.querySelectorAll("input, textarea");
  for (const el of candidates) {
    const haystack = [
      el.getAttribute("aria-label"),
      el.getAttribute("placeholder"),
      el.getAttribute("name"),
      el.getAttribute("id"),
      el.getAttribute("title"),
    ]
      .filter(Boolean)
      .join(" ");
    if (matchesTarget(haystack)) return el;
    // also check an aria-labelledby reference
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const ref = document.getElementById(labelledBy);
      if (ref && matchesTarget(ref.textContent)) return el;
    }
  }
  return null;
}

function findMemberIdField() {
  return findFieldByLabel() || findFieldByAttributes();
}

function tryFill(clientId) {
  if (!clientId) return false;
  const field = findMemberIdField();
  if (!field) return false;
  if (field.value === clientId) {
    filledValue = clientId;
    return true;
  }
  setNativeValue(field, clientId);
  filledValue = clientId;
  return true;
}

async function getClientId() {
  const { uw_context } = await chrome.storage.local.get("uw_context");
  return uw_context && uw_context.client_id ? uw_context.client_id : null;
}

async function run() {
  const clientId = await getClientId();
  if (!clientId) return;
  if (filledValue === clientId && tryFill(clientId)) return;
  tryFill(clientId);
}

// Forms are often rendered dynamically; observe and retry for a while.
function watch() {
  const observer = new MutationObserver(() => run());
  if (document.body) {
    observer.observe(document.body, { childList: true, subtree: true });
  }
  // Stop aggressive observing after 30s to avoid overhead.
  setTimeout(() => observer.disconnect(), 30000);
}

// Re-fill if the captured client changes while the form is open.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.uw_context) {
    filledValue = null;
    run();
  }
});

run();
watch();
