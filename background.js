// Service worker. Handles two responsibilities:
//   1. CORS-safe proxy for the timedtext + Google Translate endpoints (the
//      content script can't always reach them directly under YouTube's CSP).
//   2. Offscreen-document management for the Piper Ruslan TTS engine, which
//      needs a stable extension-origin context to load ~10 MB of WASM.

const TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single";
const OFFSCREEN_PATH = "offscreen/offscreen.html";

// ------------------------------------------------------------------
// Translation / subtitle proxies
// ------------------------------------------------------------------

async function translateText({ text, source = "en", target = "ru" }) {
  const url =
    `${TRANSLATE_ENDPOINT}?client=gtx&sl=${encodeURIComponent(source)}` +
    `&tl=${encodeURIComponent(target)}&dt=t&q=${encodeURIComponent(text)}`;
  const resp = await fetch(url, { method: "GET" });
  if (!resp.ok) {
    throw new Error(`Translate HTTP ${resp.status}`);
  }
  const data = await resp.json();
  // data[0] is an array of [translatedChunk, originalChunk, ...] tuples.
  const translated = (data[0] || [])
    .map((chunk) => (chunk && chunk[0]) || "")
    .join("");
  return translated;
}

async function fetchSubtitles({ url }) {
  const resp = await fetch(url, { credentials: "include" });
  if (!resp.ok) {
    throw new Error(`Subtitles HTTP ${resp.status}`);
  }
  return await resp.text();
}

// ------------------------------------------------------------------
// Offscreen document lifecycle
// ------------------------------------------------------------------

let offscreenCreating = null;

async function ensureOffscreen() {
  // If we already have one, do nothing.
  const existing = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_PATH)],
  });
  if (existing.length > 0) return;
  if (offscreenCreating) return offscreenCreating;
  offscreenCreating = chrome.offscreen.createDocument({
    url: OFFSCREEN_PATH,
    reasons: ["AUDIO_PLAYBACK"],
    justification:
      "Run Piper WASM TTS and play translated speech in extension origin.",
  });
  try {
    await offscreenCreating;
  } finally {
    offscreenCreating = null;
  }
}

/**
 * Forward a message to the offscreen document. The doc is created on demand.
 */
async function sendToOffscreen(msg) {
  await ensureOffscreen();
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage(
      { ...msg, target: "ru-yt-offscreen" },
      (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
          return;
        }
        if (!resp) return reject(new Error("no response from offscreen"));
        resolve(resp);
      },
    );
  });
}

// ------------------------------------------------------------------
// Message router
// ------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return false;

  // Messages targeted at the offscreen doc are NOT handled here — the
  // offscreen doc has its own listener. We just need to ignore them so
  // we don't double-respond.
  if (msg.target === "ru-yt-offscreen") return false;

  // Forwards from offscreen back to content scripts.
  if (msg.target === "ru-yt-content") {
    chrome.tabs.query({ url: ["*://*.youtube.com/*"] }, (tabs) => {
      for (const tab of tabs) {
        chrome.tabs.sendMessage(tab.id, msg).catch(() => {});
      }
    });
    return false;
  }

  if (msg.type === "ru-yt-translate") {
    translateText(msg.payload)
      .then((translated) => sendResponse({ ok: true, translated }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  if (msg.type === "ru-yt-fetch-subs") {
    fetchSubtitles(msg.payload)
      .then((body) => sendResponse({ ok: true, body }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  if (msg.type === "ru-yt-piper") {
    // payload: { type: "speak"|"cancel"|"preload"|"ping", ... }
    sendToOffscreen(msg.payload || {})
      .then((resp) => sendResponse(resp))
      .catch((err) =>
        sendResponse({ ok: false, error: String(err && err.message ? err.message : err) }),
      );
    return true;
  }
  return false;
});
