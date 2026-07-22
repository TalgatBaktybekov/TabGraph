// TabGraph service worker.

const BACKEND = "http://localhost:8000";
const BACKEND_URL = `${BACKEND}/ingest`;
const DEBOUNCE_MS = 5000; 
const MIN_TEXT_LENGTH = 500;

// Default blocklist, stored in chrome.storage.local on install so users can edit it later.
const DEFAULT_BLOCKED_HOSTS = [
  // private stuff addresses
];
const DEFAULT_BLOCKED_KEYWORDS = [];

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(["blockedHosts", "blockedKeywords"]);
  await chrome.storage.local.set({
    blockedHosts: existing.blockedHosts ?? DEFAULT_BLOCKED_HOSTS,
    blockedKeywords: existing.blockedKeywords ?? DEFAULT_BLOCKED_KEYWORDS,
  });
});

async function isCaptureEnabled() {
  const { captureEnabled } = await chrome.storage.local.get({
    captureEnabled: false,
  });
  return captureEnabled;
}

function isLocalHostname(hostname) {
  return (
    hostname === "localhost" ||
    hostname === "::1" ||
    /^127\./.test(hostname) ||
    hostname === "0.0.0.0" ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local")
  );
}

// Returns a human-readable reason if the URL must not be captured, else null.
async function blockReason(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return "unparseable URL";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `non-web scheme (${parsed.protocol})`;
  }
  const host = parsed.hostname.toLowerCase();
  if (isLocalHostname(host)) return "localhost";

  const { blockedHosts, blockedKeywords } = await chrome.storage.local.get({
    blockedHosts: DEFAULT_BLOCKED_HOSTS,
    blockedKeywords: DEFAULT_BLOCKED_KEYWORDS,
  });
  for (const blocked of blockedHosts) {
    if (host === blocked || host.endsWith("." + blocked)) {
      return `blocked host (${blocked})`;
    }
  }
  for (const keyword of blockedKeywords) {
    if (host.includes(keyword)) return `blocked keyword (${keyword})`;
  }
  return null;
}

// Keep one debounce timer per tab.
const pendingTimers = new Map();

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  if (!tab.url || !/^https?:/.test(tab.url)) return;

  console.log("[TabGraph] tab complete, debouncing:", tab.url);
  clearTimeout(pendingTimers.get(tabId));
  pendingTimers.set(
    tabId,
    setTimeout(() => {
      pendingTimers.delete(tabId);
      captureTab(tabId).catch((err) =>
        console.warn("[TabGraph] capture error:", err.message),
      );
    }, DEBOUNCE_MS),
  );
});

chrome.tabs.onRemoved.addListener((tabId) => {
  clearTimeout(pendingTimers.get(tabId));
  pendingTimers.delete(tabId);
});

// Extract page content and POST it to /ingest. Returns the ingest response
// body, or throws with a human-readable reason.
async function extractAndIngest(tab) {
  const reason = await blockReason(tab.url ?? "");
  if (reason) throw new Error(`blocked: ${reason}`);

  // Readability returns its result through executeScript.
  let extracted;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["lib/Readability.js", "content.js"],
    });
    extracted = results?.[0]?.result;
  } catch (err) {
    // Some pages block script injection.
    throw new Error(`injection failed: ${err.message}`);
  }
  if (!extracted?.ok) throw new Error(`extraction failed: ${extracted?.error}`);
  if (extracted.text.length < MIN_TEXT_LENGTH) {
    throw new Error(`page too short (${extracted.text.length} chars)`);
  }

  const payload = {
    url: tab.url,
    title: extracted.title || tab.title || "",
    text: extracted.text,
    html: extracted.html ?? "",
    timestamp: new Date().toISOString(),
  };
  const res = await fetch(BACKEND_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await res.json().catch(() => ({}));
  console.log("[TabGraph] ingest:", res.status, body.status ?? "", tab.url);
  return body;
}

async function captureTab(tabId) {
  if (!(await isCaptureEnabled())) {
    console.log("[TabGraph] capture disabled — not sending");
    return;
  }

  // Re-read the tab in case the URL changed during the debounce window.
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch {
    return; // tab closed
  }

  try {
    await extractAndIngest(tab);
  } catch (err) {
    console.log("[TabGraph] capture skipped:", err.message);
  }
}

// The promote gesture: capture the active tab now (explicit consent — the
// passive-capture toggle is deliberately not checked; the blocklist is),
// then mark it as evidence with an optional project.
async function promoteCurrentTab(project) {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) throw new Error("no active tab");

  const ingested = await extractAndIngest(tab);
  if (ingested.status === "skipped_short") throw new Error(ingested.detail);
  if (!ingested.id) throw new Error(`ingest returned: ${ingested.status}`);

  const res = await fetch(`${BACKEND}/captures/${ingested.id}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project }),
  });
  const body = await res.json().catch(() => ({}));
  if (body.status !== "promoted") {
    throw new Error(`promote returned: ${body.status ?? res.status}`);
  }
  return {
    ok: true,
    detail: project ? `saved as evidence → ${project}` : "saved as evidence",
  };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.type === "promote-current-tab") {
    promoteCurrentTab(msg.project)
      .then(sendResponse)
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true; // keep the message channel open for the async response
  }
});
