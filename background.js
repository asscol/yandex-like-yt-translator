// Service worker. Used as a CORS-safe proxy for translation requests in case
// the content-script direct fetch is blocked by the page's CSP/CORS policy.

const TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single";

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

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "ru-yt-translate") {
    translateText(msg.payload)
      .then((translated) => sendResponse({ ok: true, translated }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true; // keep channel open for async sendResponse
  }
  if (msg && msg.type === "ru-yt-fetch-subs") {
    fetchSubtitles(msg.payload)
      .then((body) => sendResponse({ ok: true, body }))
      .catch((err) => sendResponse({ ok: false, error: String(err) }));
    return true;
  }
  return false;
});
