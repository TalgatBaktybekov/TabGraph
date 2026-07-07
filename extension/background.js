// TabGraph service worker.

const BACKEND_URL = "http://localhost:8000/ingest";
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

  const reason = await blockReason(tab.url ?? "");
  if (reason) {
    console.log(`[TabGraph] skipped (${reason}):`, tab.url);
    return;
  }

  // Readability returns its result through executeScript.
  let extracted;
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      files: ["lib/Readability.js", "content.js"],
    });
    extracted = results?.[0]?.result;
    console.log("[TabGraph] extracted:", extracted?.text.length ?? 0, "chars");
  } catch (err) {
    // Some pages block script injection.
    console.log("[TabGraph] injection failed:", err.message);
    return;
  }
  if (!extracted?.ok) {
    console.log("[TabGraph] extraction failed:", extracted?.error);
    return;
  }
  if (extracted.text.length < MIN_TEXT_LENGTH) {
    console.log(
      `[TabGraph] skipped (only ${extracted.text.length} chars):`,
      tab.url,
    );
    return;
  }

  const payload = {
    url: tab.url,
    title: extracted.title || tab.title || "",
    text: extracted.text,
    timestamp: new Date().toISOString(),
  };
  try {
    const res = await fetch(BACKEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    console.log("[TabGraph] ingest:", res.status, body.status ?? "", tab.url);
  } catch (err) {
    console.warn("[TabGraph] ingest failed (backend down?):", err.message);
  }
}
