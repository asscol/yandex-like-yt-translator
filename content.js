// Content script — runs on YouTube watch pages.
// Adds a "Перевести" button to the player controls. On click:
//   1. Reads ytInitialPlayerResponse from the page to find caption tracks.
//   2. Picks an English (or any auto-) track and downloads its JSON3 timed text.
//   3. Translates each segment to Russian via background.js.
//   4. Plays the Russian translation through Web Speech API in sync with the
//      video, muting the original audio while playback is active.

(() => {
  "use strict";

  if (window.__ruYtTranslatorInjected) return;
  window.__ruYtTranslatorInjected = true;

  const STATE_IDLE = "idle";
  const STATE_LOADING = "loading";
  const STATE_ACTIVE = "active";
  const STATE_ERROR = "error";

  const DEFAULT_SETTINGS = {
    rate: 1.05,
    pitch: 1.0,
    volume: 1.0,
    voiceURI: "",
    autoMuteOriginal: true,
    sourceLang: "auto", // "auto" | "en"
    targetLang: "ru",
    ttsEngine: "browser", // "browser" | "backend"
    backendUrl: "",
  };

  /** @type {{
   *   button: HTMLButtonElement|null,
   *   state: string,
   *   videoId: string|null,
   *   segments: Array<{startMs:number,durMs:number,text:string,ru?:string}>,
   *   activeIndex: number,
   *   wasMuted: boolean|null,
   *   timeupdateHandler: ((e:Event)=>void)|null,
   *   navHandler: (()=>void)|null,
   *   speakingUtterance: SpeechSynthesisUtterance|null,
   *   backendAudio: HTMLAudioElement|null,
   *   backendSyncCleanup: (()=>void)|null,
   *   backendJobId: string|null,
   *   backendCancel: AbortController|null,
   * }} */
  const ctx = {
    button: null,
    state: STATE_IDLE,
    videoId: null,
    segments: [],
    activeIndex: -1,
    wasMuted: null,
    timeupdateHandler: null,
    navHandler: null,
    speakingUtterance: null,
    backendAudio: null,
    backendSyncCleanup: null,
    backendJobId: null,
    backendCancel: null,
  };

  let settings = { ...DEFAULT_SETTINGS };

  function loadSettings() {
    return new Promise((resolve) => {
      try {
        chrome.storage.sync.get(DEFAULT_SETTINGS, (stored) => {
          settings = { ...DEFAULT_SETTINGS, ...(stored || {}) };
          resolve(settings);
        });
      } catch {
        resolve(settings);
      }
    });
  }

  // Listen for live setting updates from the popup. Guarded because the
  // chrome.runtime APIs throw "Extension context invalidated" once the
  // extension is reloaded while a content script is still attached.
  function isExtensionContextValid() {
    try { return !!(chrome && chrome.runtime && chrome.runtime.id); }
    catch { return false; }
  }
  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === "ru-yt-settings-updated" && msg.payload) {
        settings = { ...settings, ...msg.payload };
      }
      return false;
    });
  } catch {}

  // ------------------------------------------------------------------
  // UI helpers
  // ------------------------------------------------------------------

  function showToast(text, ms = 2400) {
    const existing = document.querySelector(".ru-yt-translate-toast");
    if (existing) existing.remove();
    const toast = document.createElement("div");
    toast.className = "ru-yt-translate-toast";
    toast.textContent = text;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("is-visible"));
    setTimeout(() => {
      toast.classList.remove("is-visible");
      setTimeout(() => toast.remove(), 350);
    }, ms);
  }

  function setButtonState(state, label) {
    ctx.state = state;
    if (!ctx.button) return;
    ctx.button.dataset.state = state;
    if (label) {
      const labelEl = ctx.button.querySelector(".ru-yt-translate-label");
      if (labelEl) labelEl.textContent = label;
    }
  }

  function ensureButton() {
    // Don't add the button on non-watch pages.
    if (!location.pathname.startsWith("/watch")) {
      removeButton();
      return;
    }

    if (ctx.button && document.contains(ctx.button)) return;

    // Adopt an existing button if one already exists in the DOM (e.g. after
    // a SPA nav re-rendered the actions row but our reference was lost).
    // Without this guard we end up rendering several buttons stacked.
    const existing = document.querySelector(".ru-yt-translate-btn");
    if (existing) {
      // Re-attach the click handler in case it was lost (e.g. element
      // survived but our isolated-world listener was GC'd after reload).
      if (!existing.dataset.ruYtBound) {
        existing.addEventListener("click", onButtonClick);
        existing.dataset.ruYtBound = "1";
      }
      ctx.button = existing;
      return;
    }

    // Mount inside the right side of the player controls if available; fall
    // back to the title actions row.
    const mountSpots = [
      "#actions #actions-inner #menu",
      "#top-level-buttons-computed",
      "ytd-watch-metadata #actions",
      "#above-the-fold #actions",
    ];
    let mount = null;
    for (const sel of mountSpots) {
      mount = document.querySelector(sel);
      if (mount) break;
    }
    if (!mount) return;

    const btn = document.createElement("button");
    btn.className = "ru-yt-translate-btn";
    btn.type = "button";
    btn.dataset.state = STATE_IDLE;
    btn.title = "Перевести и озвучить видео на русском";
    btn.innerHTML =
      '<span class="ru-yt-translate-icon">🎙</span>' +
      '<span class="ru-yt-translate-label">Перевести</span>';
    btn.addEventListener("click", onButtonClick);
    btn.dataset.ruYtBound = "1";
    mount.prepend(btn);
    ctx.button = btn;
  }

  function removeButton() {
    if (ctx.button) {
      ctx.button.remove();
      ctx.button = null;
    }
  }

  // ------------------------------------------------------------------
  // YouTube data extraction
  // ------------------------------------------------------------------

  function getCurrentVideoId() {
    const url = new URL(location.href);
    return url.searchParams.get("v");
  }

  function getVideoElement() {
    return document.querySelector("video.html5-main-video") ||
      document.querySelector("video");
  }

  /**
   * Reads the global `ytInitialPlayerResponse` (or `ytcfg` cached one) from
   * inline <script> tags. We must not access the page's window directly,
   * since content scripts live in an isolated world.
   */
  function readPlayerResponseFromDOM() {
    const scripts = document.querySelectorAll("script");
    const patterns = [
      /var\s+ytInitialPlayerResponse\s*=\s*(\{[\s\S]+?\})\s*;/,
      /window\["ytInitialPlayerResponse"\]\s*=\s*(\{[\s\S]+?\})\s*;/,
      /ytInitialPlayerResponse\s*=\s*(\{[\s\S]+?\})\s*;/,
    ];
    for (const s of scripts) {
      const text = s.textContent || "";
      if (!text.includes("ytInitialPlayerResponse")) continue;
      for (const re of patterns) {
        const m = text.match(re);
        if (m) {
          try {
            return JSON.parse(m[1]);
          } catch {
            // Try to fix unbalanced braces — sometimes the regex over-captures.
            const trimmed = trimToBalancedJson(m[1]);
            if (trimmed) {
              try {
                return JSON.parse(trimmed);
              } catch {}
            }
          }
        }
      }
    }
    return null;
  }

  function trimToBalancedJson(src) {
    let depth = 0;
    let inStr = false;
    let escape = false;
    for (let i = 0; i < src.length; i++) {
      const ch = src[i];
      if (escape) {
        escape = false;
        continue;
      }
      if (ch === "\\") {
        escape = true;
        continue;
      }
      if (ch === '"') {
        inStr = !inStr;
        continue;
      }
      if (inStr) continue;
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (depth === 0) return src.slice(0, i + 1);
      }
    }
    return null;
  }

  /**
   * As a fallback, fetch the watch page HTML and extract player response from
   * it. This is needed when the SPA navigated client-side and the DOM no
   * longer holds the player-response script.
   */
  async function fetchPlayerResponseFromHtml(videoId) {
    const url = `https://www.youtube.com/watch?v=${encodeURIComponent(videoId)}`;
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) throw new Error(`watch HTML HTTP ${resp.status}`);
    const html = await resp.text();
    const patterns = [
      /var\s+ytInitialPlayerResponse\s*=\s*(\{[\s\S]+?\})\s*;\s*(?:var|window|<\/script>)/,
      /ytInitialPlayerResponse\s*=\s*(\{[\s\S]+?\})\s*;/,
    ];
    for (const re of patterns) {
      const m = html.match(re);
      if (m) {
        const trimmed = trimToBalancedJson(m[1]);
        if (trimmed) {
          try {
            return JSON.parse(trimmed);
          } catch {}
        }
      }
    }
    return null;
  }

  function pickCaptionTrack(playerResponse) {
    const tracks =
      playerResponse?.captions?.playerCaptionsTracklistRenderer?.captionTracks;
    if (!Array.isArray(tracks) || tracks.length === 0) return null;

    // Preference order: manual English -> auto English -> any English-like ->
    // first track regardless of language (we'll let the translator handle it).
    const score = (t) => {
      const lang = (t.languageCode || "").toLowerCase();
      const isEn = lang === "en" || lang.startsWith("en-");
      const isAuto = t.kind === "asr";
      if (isEn && !isAuto) return 0;
      if (isEn && isAuto) return 1;
      if (lang.startsWith("en")) return 2;
      return isAuto ? 4 : 3;
    };
    return [...tracks].sort((a, b) => score(a) - score(b))[0];
  }

  async function fetchTrackSegments(track) {
    let url = track.baseUrl;
    if (!url) throw new Error("track has no baseUrl");
    // Force JSON3 format — easiest to parse and includes timing.
    if (!/[?&]fmt=/.test(url)) {
      url += (url.includes("?") ? "&" : "?") + "fmt=json3";
    } else {
      url = url.replace(/([?&])fmt=[^&]*/, "$1fmt=json3");
    }

    // Try direct content-script fetch first (shares page cookies as same-origin
    // to youtube.com), then fall back to the background-worker proxy.
    let body = await fetchSubtitlesDirect(url).catch(() => null);
    if (!body || body.trim().length === 0) {
      body = await fetchViaBackground(url);
    }
    let parsed;
    try {
      parsed = JSON.parse(body);
    } catch (e) {
      const preview = (body || "").slice(0, 80).replace(/\s+/g, " ");
      throw new Error(
        "subtitle response is not JSON3: " + e.message +
        (preview ? ` (got: "${preview}")` : " (empty response)"),
      );
    }
    const events = Array.isArray(parsed.events) ? parsed.events : [];
    /** @type {Array<{startMs:number,durMs:number,text:string}>} */
    const segments = [];
    for (const ev of events) {
      if (!ev || !Array.isArray(ev.segs)) continue;
      const text = ev.segs
        .map((s) => (s && typeof s.utf8 === "string" ? s.utf8 : ""))
        .join("")
        .replace(/\s+/g, " ")
        .trim();
      if (!text) continue;
      const startMs = Math.max(0, ev.tStartMs || 0);
      const durMs = Math.max(200, ev.dDurationMs || 0);
      segments.push({ startMs, durMs, text });
    }
    return segments;
  }

  async function fetchSubtitlesDirect(url) {
    const resp = await fetch(url, { credentials: "include" });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.text();
  }

  async function fetchViaBackground(url) {
    return new Promise((resolve, reject) => {
      if (!isExtensionContextValid()) {
        reject(new Error("extension context invalidated — reload the page"));
        return;
      }
      try {
        chrome.runtime.sendMessage(
          { type: "ru-yt-fetch-subs", payload: { url } },
          (resp) => {
            if (chrome.runtime.lastError) {
              reject(new Error(chrome.runtime.lastError.message));
              return;
            }
            if (!resp) {
              reject(new Error("no response from background"));
              return;
            }
            if (!resp.ok) {
              reject(new Error(resp.error || "background fetch failed"));
              return;
            }
            resolve(resp.body);
          },
        );
      } catch (e) {
        reject(e);
      }
    });
  }

  // ------------------------------------------------------------------
  // Translation
  // ------------------------------------------------------------------

  // Separator used to translate many segments in one request and split back.
  // We pick a token that Google Translate is unlikely to alter, a unique
  // numbered marker.
  const SEP_PREFIX = "\n\n@@SEG";
  const SEP_SUFFIX = "@@\n\n";

  function buildBatch(segments, startIdx, maxChars) {
    const parts = [];
    let totalLen = 0;
    let i = startIdx;
    for (; i < segments.length; i++) {
      const marker = `${SEP_PREFIX}${i}${SEP_SUFFIX}`;
      const piece = marker + segments[i].text;
      if (parts.length > 0 && totalLen + piece.length > maxChars) break;
      parts.push(piece);
      totalLen += piece.length;
    }
    return { joined: parts.join(""), endIdx: i };
  }

  function parseBatch(translatedJoined, startIdx, endIdx) {
    /** @type {Record<number,string>} */
    const out = {};
    // Google may translate "@@SEG12@@" to "@@SEG12@@" verbatim, but sometimes
    // adds spaces: "@@ SEG 12 @@". Make the regex tolerant to those.
    const re = /@@\s*SEG\s*(\d+)\s*@@/g;
    const positions = [];
    let m;
    while ((m = re.exec(translatedJoined)) !== null) {
      positions.push({ idx: parseInt(m[1], 10), start: m.index, end: re.lastIndex });
    }
    for (let i = 0; i < positions.length; i++) {
      const cur = positions[i];
      const next = positions[i + 1];
      const text = translatedJoined.slice(cur.end, next ? next.start : undefined).trim();
      out[cur.idx] = text;
    }
    return out;
  }

  async function translateSegments(segments, onProgress) {
    const MAX_CHARS = 1800; // conservative under Google Translate free limit
    let i = 0;
    let done = 0;
    while (i < segments.length) {
      const { joined, endIdx } = buildBatch(segments, i, MAX_CHARS);
      try {
        const translated = await sendTranslate(joined);
        const map = parseBatch(translated, i, endIdx);
        let allMatched = true;
        for (let k = i; k < endIdx; k++) {
          if (typeof map[k] === "string" && map[k].length > 0) {
            segments[k].ru = map[k];
          } else {
            allMatched = false;
          }
        }
        // If the batched separator approach failed for some segments, fall
        // back to per-segment translation for the affected indices.
        if (!allMatched) {
          for (let k = i; k < endIdx; k++) {
            if (!segments[k].ru) {
              try {
                segments[k].ru = await sendTranslate(segments[k].text);
              } catch (e) {
                segments[k].ru = segments[k].text;
              }
            }
          }
        }
      } catch (e) {
        // Whole batch failed — translate each segment individually.
        for (let k = i; k < endIdx; k++) {
          try {
            segments[k].ru = await sendTranslate(segments[k].text);
          } catch (err) {
            segments[k].ru = segments[k].text;
          }
        }
      }
      done = endIdx;
      i = endIdx;
      if (onProgress) onProgress(done, segments.length);
    }
    return segments;
  }

  function sendTranslate(text) {
    return new Promise((resolve, reject) => {
      if (!isExtensionContextValid()) {
        reject(new Error("extension context invalidated — reload the page"));
        return;
      }
      try {
        chrome.runtime.sendMessage(
          {
            type: "ru-yt-translate",
            payload: {
              text,
              source: settings.sourceLang || "auto",
              target: settings.targetLang || "ru",
            },
          },
          (resp) => {
            if (chrome.runtime.lastError) {
              reject(new Error(chrome.runtime.lastError.message));
              return;
            }
            if (!resp) return reject(new Error("no response"));
            if (!resp.ok) return reject(new Error(resp.error || "translate failed"));
            resolve(resp.translated);
          },
        );
      } catch (e) {
        reject(e);
      }
    });
  }

  // ------------------------------------------------------------------
  // Speech / playback
  // ------------------------------------------------------------------

  function pickRussianVoice() {
    const voices = window.speechSynthesis.getVoices();
    if (settings.voiceURI) {
      const found = voices.find((v) => v.voiceURI === settings.voiceURI);
      if (found) return found;
    }
    const rus = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("ru"));
    if (rus.length > 0) {
      // Prefer non-novelty / "Microsoft" / "Google" voices over local fallbacks.
      const ranked = rus
        .map((v) => {
          const name = (v.name || "").toLowerCase();
          let score = 0;
          if (name.includes("microsoft")) score -= 3;
          if (name.includes("google")) score -= 2;
          if (name.includes("yandex")) score -= 5;
          if (v.localService) score += 1;
          return { v, score };
        })
        .sort((a, b) => a.score - b.score);
      return ranked[0].v;
    }
    return null;
  }

  function speakSegment(seg, video) {
    if (!seg || !seg.ru) return;
    try {
      window.speechSynthesis.cancel();
    } catch {}

    const utt = new SpeechSynthesisUtterance(seg.ru);
    const voice = pickRussianVoice();
    if (voice) {
      utt.voice = voice;
      utt.lang = voice.lang;
    } else {
      utt.lang = "ru-RU";
    }
    utt.volume = clamp(settings.volume, 0, 1);
    utt.pitch = clamp(settings.pitch, 0.5, 2);

    // Auto-adapt rate so a long Russian phrase still fits inside the segment
    // duration. Russian translation is typically ~1.2x longer than English.
    const baseRate = clamp(settings.rate, 0.5, 2.0);
    const charsPerSec = (seg.ru.length / Math.max(0.5, seg.durMs / 1000));
    let rate = baseRate;
    if (charsPerSec > 16) rate = Math.min(2.0, baseRate * 1.25);
    if (charsPerSec > 22) rate = Math.min(2.0, baseRate * 1.5);
    utt.rate = rate;

    ctx.speakingUtterance = utt;
    utt.onend = () => {
      if (ctx.speakingUtterance === utt) ctx.speakingUtterance = null;
    };
    utt.onerror = () => {
      if (ctx.speakingUtterance === utt) ctx.speakingUtterance = null;
    };

    window.speechSynthesis.speak(utt);
  }

  function clamp(v, lo, hi) {
    if (typeof v !== "number" || Number.isNaN(v)) return lo;
    return Math.max(lo, Math.min(hi, v));
  }

  function findSegmentForTime(timeMs) {
    // Linear scan from current activeIndex forward (subtitles are monotonic).
    const segs = ctx.segments;
    let i = Math.max(0, ctx.activeIndex);
    if (i >= segs.length) i = 0;
    // If user sought backwards, scan from start.
    if (i > 0 && segs[i].startMs > timeMs) i = 0;
    for (; i < segs.length; i++) {
      const s = segs[i];
      if (timeMs >= s.startMs && timeMs < s.startMs + s.durMs) return i;
      if (s.startMs > timeMs) return -1;
    }
    return -1;
  }

  function startPlayback() {
    const video = getVideoElement();
    if (!video) {
      showToast("Видео-элемент не найден");
      setButtonState(STATE_ERROR, "Перевести");
      return;
    }
    ctx.wasMuted = video.muted;
    if (settings.autoMuteOriginal) video.muted = true;
    ctx.activeIndex = -1;

    const handler = () => {
      if (video.paused || video.ended) return;
      const timeMs = Math.max(0, Math.floor(video.currentTime * 1000));
      const idx = findSegmentForTime(timeMs);
      if (idx >= 0 && idx !== ctx.activeIndex) {
        ctx.activeIndex = idx;
        speakSegment(ctx.segments[idx], video);
      }
    };
    const pauseHandler = () => {
      try {
        window.speechSynthesis.pause();
      } catch {}
    };
    const playHandler = () => {
      try {
        window.speechSynthesis.resume();
      } catch {}
    };
    const seekHandler = () => {
      try {
        window.speechSynthesis.cancel();
      } catch {}
      ctx.activeIndex = -1;
    };

    ctx.timeupdateHandler = handler;
    video.addEventListener("timeupdate", handler);
    video.addEventListener("pause", pauseHandler);
    video.addEventListener("play", playHandler);
    video.addEventListener("seeking", seekHandler);
    // Stash for later removal.
    ctx._pauseHandler = pauseHandler;
    ctx._playHandler = playHandler;
    ctx._seekHandler = seekHandler;

    setButtonState(STATE_ACTIVE, "Выключить");
  }

  function stopPlayback() {
    try {
      window.speechSynthesis.cancel();
    } catch {}
    const video = getVideoElement();
    if (video && ctx.timeupdateHandler) {
      video.removeEventListener("timeupdate", ctx.timeupdateHandler);
    }
    if (video && ctx._pauseHandler) video.removeEventListener("pause", ctx._pauseHandler);
    if (video && ctx._playHandler) video.removeEventListener("play", ctx._playHandler);
    if (video && ctx._seekHandler) video.removeEventListener("seeking", ctx._seekHandler);
    ctx.timeupdateHandler = null;
    ctx._pauseHandler = null;
    ctx._playHandler = null;
    ctx._seekHandler = null;
    if (ctx._liveObserver) {
      try { ctx._liveObserver.disconnect(); } catch {}
      ctx._liveObserver = null;
    }
    // Backend audio cleanup.
    if (ctx.backendCancel) {
      try { ctx.backendCancel.abort(); } catch {}
      ctx.backendCancel = null;
    }
    if (ctx.backendSyncCleanup) {
      try { ctx.backendSyncCleanup(); } catch {}
      ctx.backendSyncCleanup = null;
    }
    if (ctx.backendAudio) {
      try {
        ctx.backendAudio.pause();
        if (ctx.backendAudio.src && ctx.backendAudio.src.startsWith("blob:")) {
          URL.revokeObjectURL(ctx.backendAudio.src);
        }
        ctx.backendAudio.src = "";
        ctx.backendAudio.remove();
      } catch {}
      ctx.backendAudio = null;
    }
    ctx.backendJobId = null;
    if (video && ctx.wasMuted !== null) video.muted = ctx.wasMuted;
    ctx.wasMuted = null;
    ctx.activeIndex = -1;
  }

  // ------------------------------------------------------------------
  // Live caption mode (fallback when timedtext API is blocked).
  // Reads what YouTube renders inside .ytp-caption-segment in real time,
  // translates each new caption block and speaks it.
  // ------------------------------------------------------------------

  function startLiveCaptionMode() {
    const video = getVideoElement();
    if (!video) {
      showToast("Видео-элемент не найден");
      setButtonState(STATE_ERROR, "Перевести");
      return;
    }
    ctx.wasMuted = video.muted;
    if (settings.autoMuteOriginal) video.muted = true;

    let lastText = "";
    let busy = false;
    let pending = null;

    const handleCaption = async (text) => {
      if (busy) { pending = text; return; }
      busy = true;
      try {
        const ru = await sendTranslate(text);
        if (ctx.state !== STATE_ACTIVE) { busy = false; return; }
        try { window.speechSynthesis.cancel(); } catch {}
        const utt = new SpeechSynthesisUtterance(ru);
        const voice = pickRussianVoice();
        if (voice) { utt.voice = voice; utt.lang = voice.lang; }
        else utt.lang = "ru-RU";
        utt.volume = clamp(settings.volume, 0, 1);
        utt.pitch = clamp(settings.pitch, 0.5, 2);
        utt.rate = clamp(settings.rate, 0.5, 2.0);
        ctx.speakingUtterance = utt;
        window.speechSynthesis.speak(utt);
      } catch (e) {
        console.warn("[RU-YT] live translate failed:", e);
      }
      busy = false;
      if (ctx.state === STATE_ACTIVE && pending && pending !== lastText) {
        const next = pending; pending = null; lastText = next;
        handleCaption(next);
      }
    };

    const scan = () => {
      const segs = document.querySelectorAll(".ytp-caption-segment");
      if (!segs.length) return;
      const text = Array.from(segs)
        .map((s) => (s.textContent || "").trim())
        .filter(Boolean)
        .join(" ")
        .replace(/\s+/g, " ")
        .trim();
      if (!text || text === lastText) return;
      lastText = text;
      handleCaption(text);
    };

    // Scope the observer to the caption container when present — observing
    // the entire <body> fires on every YouTube DOM mutation (heavy on perf).
    const captionRoot =
      document.querySelector(".caption-window") ||
      document.querySelector(".ytp-caption-window-container") ||
      document.querySelector("#movie_player") ||
      document.body;
    const observer = new MutationObserver(scan);
    observer.observe(captionRoot, {
      childList: true, subtree: true, characterData: true,
    });
    ctx._liveObserver = observer;

    // Mirror startPlayback() so TTS follows pause / play / seek of the video.
    const pauseHandler = () => {
      try { window.speechSynthesis.pause(); } catch {}
    };
    const playHandler = () => {
      try { window.speechSynthesis.resume(); } catch {}
    };
    const seekHandler = () => {
      try { window.speechSynthesis.cancel(); } catch {}
      lastText = "";
      pending = null;
    };
    video.addEventListener("pause", pauseHandler);
    video.addEventListener("play", playHandler);
    video.addEventListener("seeking", seekHandler);
    ctx._pauseHandler = pauseHandler;
    ctx._playHandler = playHandler;
    ctx._seekHandler = seekHandler;

    // Initial scan in case captions are already visible.
    scan();

    setButtonState(STATE_ACTIVE, "Выключить");
  }

  // ------------------------------------------------------------------
  // Click handler
  // ------------------------------------------------------------------

  // ------------------------------------------------------------------
  // Backend mode ("живые голоса"): full-pipeline translate + voice clone
  // delegated to a self-hosted FastAPI service (see backend/).
  //
  // Flow:
  //   1. POST /jobs/translate-video {video_url, target_lang}
  //   2. Poll /jobs/{id} until stage=done (with progress tickers)
  //   3. Fetch /jobs/{id}/audio as a blob, attach to a hidden <audio>
  //   4. Mute the original video and play the audio in sync (play/pause/seek
  //      events on <video> are mirrored to <audio>).
  // ------------------------------------------------------------------

  function backendBaseUrl() {
    const raw = (settings.backendUrl || "").trim();
    if (!raw) return "";
    return raw.replace(/\/+$/, "");
  }

  async function backendCreateJob(videoUrl, targetLang, signal) {
    const base = backendBaseUrl();
    if (!base) throw new Error("Бэкенд не настроен. Откройте попап и укажите URL.");
    const resp = await fetch(`${base}/jobs/translate-video`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_url: videoUrl, target_lang: targetLang || "ru" }),
      signal,
    });
    if (!resp.ok) {
      const text = await resp.text().catch(() => "");
      throw new Error(`Бэкенд: ${resp.status} ${text.slice(0, 200)}`);
    }
    return await resp.json();
  }

  async function backendPollUntilDone(jobId, signal, onProgress) {
    const base = backendBaseUrl();
    let lastStage = "";
    while (true) {
      if (signal && signal.aborted) throw new DOMException("aborted", "AbortError");
      const resp = await fetch(`${base}/jobs/${jobId}`, {
        method: "GET",
        cache: "no-store",
        signal,
      });
      if (!resp.ok) throw new Error(`Бэкенд: статус задания ${resp.status}`);
      const data = await resp.json();
      if (data.stage !== lastStage) {
        lastStage = data.stage;
      }
      if (typeof onProgress === "function") {
        onProgress(data);
      }
      if (data.stage === "done") return data;
      if (data.stage === "error") throw new Error(data.error || "Бэкенд вернул ошибку");
      await new Promise((res) => setTimeout(res, 2000));
    }
  }

  async function backendDownloadAudio(jobId, signal) {
    const base = backendBaseUrl();
    const resp = await fetch(`${base}/jobs/${jobId}/audio`, { signal });
    if (!resp.ok) throw new Error(`Бэкенд: не удалось скачать аудио (${resp.status})`);
    return await resp.blob();
  }

  function attachBackendAudioToVideo(audio, video) {
    // Mirror video play/pause/seek/rate onto the audio element.
    const onPlay = () => audio.play().catch(() => {});
    const onPause = () => audio.pause();
    const onSeeking = () => {
      try { audio.currentTime = video.currentTime; } catch {}
    };
    const onRate = () => {
      try { audio.playbackRate = video.playbackRate; } catch {}
    };
    video.addEventListener("play", onPlay);
    video.addEventListener("pause", onPause);
    video.addEventListener("seeking", onSeeking);
    video.addEventListener("ratechange", onRate);

    // Initial sync.
    try {
      audio.currentTime = video.currentTime;
      audio.playbackRate = video.playbackRate;
    } catch {}

    if (!video.paused) {
      audio.play().catch(() => {});
    }

    return () => {
      video.removeEventListener("play", onPlay);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("seeking", onSeeking);
      video.removeEventListener("ratechange", onRate);
    };
  }

  async function startBackendMode() {
    const video = getVideoElement();
    if (!video) throw new Error("Видео-элемент не найден");
    const videoUrl = location.href;

    const ac = new AbortController();
    ctx.backendCancel = ac;

    showToast("Отправляю видео на бэкенд…", 3500);
    const created = await backendCreateJob(videoUrl, settings.targetLang || "ru", ac.signal);
    ctx.backendJobId = created.id;

    showToast(`Бэкенд принял задание (${created.id}). Ждём результат…`, 4000);
    const stageLabel = {
      queued: "В очереди",
      downloading: "Скачивание видео",
      transcribing: "Распознавание речи",
      translating: "Перевод текста",
      extracting_voice: "Извлечение голоса",
      synthesizing: "Синтез речи",
    };
    await backendPollUntilDone(created.id, ac.signal, (data) => {
      if (ctx.button) {
        const labelEl = ctx.button.querySelector(".ru-yt-translate-label");
        if (labelEl) {
          const pct = Math.round((data.overall || 0) * 100);
          const stage = stageLabel[data.stage] || data.stage;
          labelEl.textContent = `${stage}… ${pct}%`;
        }
      }
    });

    showToast("Скачиваю синтезированное аудио…", 2500);
    const blob = await backendDownloadAudio(created.id, ac.signal);
    const url = URL.createObjectURL(blob);

    const audio = document.createElement("audio");
    audio.preload = "auto";
    audio.src = url;
    audio.crossOrigin = "anonymous";
    audio.style.display = "none";
    document.body.appendChild(audio);
    ctx.backendAudio = audio;

    if (ctx.wasMuted === null) ctx.wasMuted = video.muted;
    if (settings.autoMuteOriginal) video.muted = true;
    audio.volume = clamp(settings.volume, 0, 1);

    ctx.backendSyncCleanup = attachBackendAudioToVideo(audio, video);

    setButtonState(STATE_ACTIVE, "Выключить");
    showToast("Бэкенд готов. Озвучка играет синхронно с видео.", 4000);
  }

  async function onButtonClick() {
    if (ctx.state === STATE_ACTIVE) {
      stopPlayback();
      setButtonState(STATE_IDLE, "Перевести");
      showToast("Перевод выключен. Звук оригинала восстановлен.");
      return;
    }
    if (ctx.state === STATE_LOADING) return;

    setButtonState(STATE_LOADING, "Готовлю…");

    try {
      await loadSettings();

      // Backend mode (full-pipeline live voices) takes a different path: it
      // sends the video URL to the FastAPI service which does Whisper +
      // translate + XTTS-v2 voice cloning, and we play the resulting WAV
      // synced to the <video> element.
      if (settings.ttsEngine === "backend") {
        await startBackendMode();
        return;
      }

      showToast("Скачиваю субтитры…");

      const videoId = getCurrentVideoId();
      if (!videoId) throw new Error("Это не страница видео.");
      ctx.videoId = videoId;

      let playerResponse = readPlayerResponseFromDOM();
      if (!playerResponse) {
        playerResponse = await fetchPlayerResponseFromHtml(videoId);
      }
      if (!playerResponse) {
        throw new Error("Не удалось получить данные плеера YouTube.");
      }

      const track = pickCaptionTrack(playerResponse);
      let segments = [];
      if (track) {
        try {
          segments = await fetchTrackSegments(track);
        } catch (e) {
          console.warn("[RU-YT] timedtext fetch failed:", e);
        }
      }

      if (segments.length === 0) {
        // YouTube increasingly blocks timedtext fetches client-side. Fall
        // back to reading captions live from the player DOM — works whenever
        // the user enables CC in YouTube's player.
        showToast(
          "YouTube не отдал JSON-субтитры. Включите CC в плеере — буду озвучивать видимые субтитры в реальном времени.",
          5500,
        );
        startLiveCaptionMode();
        return;
      }

      showToast(`Перевожу ${segments.length} реплик…`);
      await translateSegments(segments, (done, total) => {
        if (ctx.button) {
          const labelEl = ctx.button.querySelector(".ru-yt-translate-label");
          if (labelEl) labelEl.textContent = `Перевод… ${done}/${total}`;
        }
      });

      ctx.segments = segments;
      startPlayback();
      showToast("Перевод включён. Звук оригинала отключён.");
    } catch (err) {
      console.error("[RU-YT] error:", err);
      setButtonState(STATE_ERROR, "Ошибка");
      showToast("Ошибка: " + (err && err.message ? err.message : err), 4500);
      setTimeout(() => {
        if (ctx.state === STATE_ERROR) setButtonState(STATE_IDLE, "Перевести");
      }, 3500);
    }
  }

  // ------------------------------------------------------------------
  // SPA navigation handling
  // ------------------------------------------------------------------

  function onUrlChange() {
    // Stop any in-flight playback and reset.
    stopPlayback();
    setButtonState(STATE_IDLE, "Перевести");
    ctx.segments = [];
    ctx.videoId = getCurrentVideoId();
    ensureButton();
  }

  function setupNavigationObserver() {
    let lastHref = location.href;
    let scheduled = false;
    const check = () => {
      scheduled = false;
      if (location.href !== lastHref) {
        lastHref = location.href;
        onUrlChange();
      } else {
        ensureButton();
      }
    };
    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      // Throttle to one rAF — observing all DOM mutations on YouTube fires
      // hundreds of times per second; we only need to react eventually.
      requestAnimationFrame(check);
    };
    const observer = new MutationObserver(schedule);
    observer.observe(document.documentElement, { childList: true, subtree: true });

    document.addEventListener("yt-navigate-finish", onUrlChange, { capture: true });
    document.addEventListener("yt-page-data-updated", () => ensureButton(), { capture: true });

    setInterval(check, 1500);
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  // Pre-warm voice list (Chromium populates it asynchronously).
  if (window.speechSynthesis) {
    window.speechSynthesis.getVoices();
    window.speechSynthesis.onvoiceschanged = () => {
      window.speechSynthesis.getVoices();
    };
  }

  loadSettings().then(() => {
    ensureButton();
    setupNavigationObserver();
  });
})();
