// Background service worker (MV3).
// - Opens the side panel when the toolbar icon is clicked.
// - Enables the side panel only on Unite Us pages.

const UNITEUS_HOST = "app.uniteus.io";

// Open the side panel on action (toolbar icon) click.
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.warn("setPanelBehavior failed", err));

// Enable / disable the side panel based on the active tab's URL.
async function refreshSidePanel(tabId, url) {
  if (!tabId || !url) return;
  let enable = false;
  try {
    enable = new URL(url).hostname === UNITEUS_HOST;
  } catch (_) {
    enable = false;
  }
  try {
    await chrome.sidePanel.setOptions({
      tabId,
      path: "sidepanel/sidepanel.html",
      enabled: enable,
    });
  } catch (err) {
    // setOptions can throw on chrome:// tabs; ignore.
  }
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" || info.url) {
    refreshSidePanel(tabId, tab.url);
  }
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    refreshSidePanel(tabId, tab.url);
  } catch (_) {
    /* tab may be gone */
  }
});

// Relay messages from content scripts to the side panel (and storage already
// updated by the content script). Kept for future use / debugging.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "PING") sendResponse({ ok: true });
});
