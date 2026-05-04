// Offscreen document for Piper TTS.
//
// Why offscreen?
//   * Loading Piper requires bundling ONNX Runtime Web (~10 MB of WASM) and a
//     piper-phonemize WASM module. We must run all of that in a context that
//     the extension fully controls — i.e. the extension's own origin, not
//     youtube.com — so OPFS storage, web-workers, and arbitrary fetch are
//     available.
//   * The MV3 service worker can't run WASM modules with threads / Worker
//     spawning reliably. An offscreen document with reason="AUDIO_PLAYBACK"
//     stays alive and has a real DOM, which is exactly what we need.
//
// API (chrome.runtime messages, sender = service worker / content script):
//   { target: "ru-yt-offscreen", type: "ping" }
//       → { ok: true, ready: <bool> }
//   { target: "ru-yt-offscreen", type: "speak", id, text, voiceId,
//                                rate, pitch, volume }
//       → { ok: true, id }   (resolves when audio playback finishes; errors
//                              are sent as { ok:false, id, error })
//   { target: "ru-yt-offscreen", type: "cancel" }
//       → { ok: true }   (stops current playback and drops the queue)
//   { target: "ru-yt-offscreen", type: "preload", voiceId }
//       → { ok: true }   (downloads the voice model into OPFS)

// Configure vits-web BEFORE importing it. The library reads these at module
// load time and captures them in closures it can't otherwise see.
const extBase = (path) => chrome.runtime.getURL(path);
globalThis._RU_YT_ORT_BASE = extBase("vendor/onnxruntime-web/");
globalThis._RU_YT_PIPER_WASM_BASE = extBase("vendor/piper-wasm/piper_phonemize");
globalThis._RU_YT_ORT_MODULE_URL = extBase(
  "vendor/onnxruntime-web/ort.wasm.min.js",
);

const tts = await import(extBase("vendor/vits-web/vits-web.js"));

// ------------------------------------------------------------------
// State
// ------------------------------------------------------------------

const state = {
  /** @type {HTMLAudioElement|null} */
  audio: null,
  /** Currently-playing speak id, or null. */
  currentId: null,
  /** Pending speak id → { resolve, reject } awaiting playback completion. */
  pending: new Map(),
  /** Cached predictions to avoid re-synthesizing identical short phrases. */
  cache: new Map(),
  cacheLimit: 32,
};

// Make sure subsequent `tts.predict()` calls fetch the voice from HF the first
// time and reuse the OPFS-cached copy after that.
const RUSLAN_VOICE_ID = "ru_RU-ruslan-medium";

async function ensureVoice(voiceId) {
  // tts.stored() returns voiceIds present in OPFS.
  try {
    const have = await tts.stored();
    if (have.includes(voiceId)) return;
  } catch {}
  // Download (also writes to OPFS).
  try {
    await tts.download(voiceId, ({ loaded, total }) => {
      if (total > 0) {
        broadcastDownloadProgress(voiceId, loaded, total);
      }
    });
  } catch (e) {
    console.warn("[RU-YT offscreen] download failed:", e);
    throw e;
  }
}

function broadcastDownloadProgress(voiceId, loaded, total) {
  try {
    chrome.runtime.sendMessage({
      target: "ru-yt-content",
      type: "piper-progress",
      voiceId,
      loaded,
      total,
    });
  } catch {}
}

// ------------------------------------------------------------------
// Synthesis + playback
// ------------------------------------------------------------------

/**
 * Synthesize `text` to a WAV blob using Piper and the chosen voice.
 * Caches up to N most recent (text+voice) tuples so identical short caption
 * lines don't trigger another inference.
 */
async function synthesize(text, voiceId) {
  const key = `${voiceId}|${text}`;
  const cached = state.cache.get(key);
  if (cached) {
    state.cache.delete(key);
    state.cache.set(key, cached); // refresh LRU order
    return cached;
  }
  const wav = await tts.predict({ text, voiceId });
  // LRU eviction.
  if (state.cache.size >= state.cacheLimit) {
    const oldestKey = state.cache.keys().next().value;
    state.cache.delete(oldestKey);
  }
  state.cache.set(key, wav);
  return wav;
}

function stopAudio() {
  if (state.audio) {
    try {
      state.audio.pause();
      state.audio.src = "";
    } catch {}
    state.audio = null;
  }
  state.currentId = null;
}

async function playWav(wav, opts) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(wav);
    const a = new Audio(url);
    a.volume = clamp(opts.volume ?? 1, 0, 1);
    if (typeof opts.rate === "number") a.playbackRate = clamp(opts.rate, 0.5, 2);
    state.audio = a;
    const cleanup = () => {
      try { URL.revokeObjectURL(url); } catch {}
      if (state.audio === a) state.audio = null;
    };
    a.addEventListener("ended", () => { cleanup(); resolve(); });
    a.addEventListener("error", () => { cleanup(); reject(new Error("audio error")); });
    a.play().catch((e) => { cleanup(); reject(e); });
  });
}

function clamp(v, lo, hi) {
  if (typeof v !== "number" || Number.isNaN(v)) return lo;
  return Math.max(lo, Math.min(hi, v));
}

// ------------------------------------------------------------------
// Message handler
// ------------------------------------------------------------------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.target !== "ru-yt-offscreen") return false;
  handle(msg).then(sendResponse).catch((e) =>
    sendResponse({ ok: false, error: String(e && e.message ? e.message : e) }),
  );
  return true; // keep async sendResponse open
});

async function handle(msg) {
  switch (msg.type) {
    case "ping":
      return { ok: true, ready: true };

    case "preload": {
      await ensureVoice(msg.voiceId || RUSLAN_VOICE_ID);
      return { ok: true };
    }

    case "cancel": {
      stopAudio();
      // Reject anything currently awaiting playback so the caller's queue
      // can move on without waiting for a phantom audio-end event.
      for (const [, p] of state.pending) p.reject(new Error("cancelled"));
      state.pending.clear();
      return { ok: true };
    }

    case "pause": {
      try { state.audio?.pause(); } catch {}
      return { ok: true };
    }

    case "resume": {
      try { state.audio?.play(); } catch {}
      return { ok: true };
    }

    case "speak": {
      const voiceId = msg.voiceId || RUSLAN_VOICE_ID;
      const text = String(msg.text || "").trim();
      if (!text) return { ok: true, id: msg.id, skipped: true };
      await ensureVoice(voiceId);
      const wav = await synthesize(text, voiceId);
      state.currentId = msg.id;
      await playWav(wav, {
        rate: msg.rate,
        volume: msg.volume,
      });
      // If a `cancel` arrived during playback, currentId is null/something else.
      // Either way we're done with this speak.
      state.currentId = null;
      return { ok: true, id: msg.id };
    }
  }
  return { ok: false, error: `unknown message type: ${msg.type}` };
}

// Tell the service worker we're alive.
try {
  chrome.runtime.sendMessage({ target: "ru-yt-background", type: "offscreen-ready" });
} catch {}
