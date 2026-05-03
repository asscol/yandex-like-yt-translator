"use strict";

const DEFAULTS = {
  rate: 1.05,
  pitch: 1.0,
  volume: 1.0,
  voiceURI: "",
  autoMuteOriginal: true,
  ttsEngine: "browser",
  piperVoice: "ru_RU-ruslan-medium",
};

const els = {
  voice: document.getElementById("voice"),
  rate: document.getElementById("rate"),
  rateValue: document.getElementById("rate-value"),
  pitch: document.getElementById("pitch"),
  pitchValue: document.getElementById("pitch-value"),
  volume: document.getElementById("volume"),
  volumeValue: document.getElementById("volume-value"),
  autoMute: document.getElementById("auto-mute"),
  save: document.getElementById("save"),
  testVoice: document.getElementById("test-voice"),
  engineRadios: document.querySelectorAll('input[name="tts-engine"]'),
  browserVoiceSection: document.getElementById("browser-voice-section"),
  piperStatus: document.getElementById("piper-status"),
};

function fmtPct(v) {
  return Math.round(v * 100) + "%";
}

function fmtRate(v) {
  return parseFloat(v).toFixed(2) + "×";
}

function fmtPitch(v) {
  return parseFloat(v).toFixed(2);
}

function loadVoices() {
  return new Promise((resolve) => {
    let voices = window.speechSynthesis.getVoices();
    if (voices && voices.length) return resolve(voices);
    const handler = () => {
      voices = window.speechSynthesis.getVoices();
      if (voices && voices.length) {
        window.speechSynthesis.removeEventListener("voiceschanged", handler);
        resolve(voices);
      }
    };
    window.speechSynthesis.addEventListener("voiceschanged", handler);
    setTimeout(() => resolve(window.speechSynthesis.getVoices() || []), 1500);
  });
}

function populateVoices(voices, selectedURI) {
  els.voice.innerHTML = "";
  const ru = voices.filter((v) => (v.lang || "").toLowerCase().startsWith("ru"));
  if (ru.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "В системе нет русских голосов";
    opt.disabled = true;
    els.voice.appendChild(opt);
    return;
  }
  // Auto pick.
  const auto = document.createElement("option");
  auto.value = "";
  auto.textContent = "Авто (лучший доступный)";
  els.voice.appendChild(auto);
  for (const v of ru) {
    const opt = document.createElement("option");
    opt.value = v.voiceURI;
    opt.textContent = `${v.name} (${v.lang})`;
    if (v.voiceURI === selectedURI) opt.selected = true;
    els.voice.appendChild(opt);
  }
}

function bindRangeDisplay() {
  els.rate.addEventListener("input", () => {
    els.rateValue.textContent = fmtRate(els.rate.value);
  });
  els.pitch.addEventListener("input", () => {
    els.pitchValue.textContent = fmtPitch(els.pitch.value);
  });
  els.volume.addEventListener("input", () => {
    els.volumeValue.textContent = fmtPct(parseFloat(els.volume.value));
  });
}

function readForm() {
  let engine = "browser";
  for (const r of els.engineRadios) if (r.checked) engine = r.value;
  return {
    rate: parseFloat(els.rate.value),
    pitch: parseFloat(els.pitch.value),
    volume: parseFloat(els.volume.value),
    voiceURI: els.voice.value || "",
    autoMuteOriginal: els.autoMute.checked,
    ttsEngine: engine,
    piperVoice: "ru_RU-ruslan-medium",
  };
}

function writeForm(s) {
  els.rate.value = s.rate;
  els.rateValue.textContent = fmtRate(s.rate);
  els.pitch.value = s.pitch;
  els.pitchValue.textContent = fmtPitch(s.pitch);
  els.volume.value = s.volume;
  els.volumeValue.textContent = fmtPct(s.volume);
  els.autoMute.checked = !!s.autoMuteOriginal;
  const engine = s.ttsEngine === "piper" ? "piper" : "browser";
  for (const r of els.engineRadios) r.checked = (r.value === engine);
  applyEngineUI(engine);
}

function applyEngineUI(engine) {
  if (engine === "piper") {
    els.browserVoiceSection.style.opacity = "0.5";
    els.browserVoiceSection.style.pointerEvents = "none";
    els.piperStatus.textContent =
      "Piper загрузит голос Ruslan (~60 МБ) с huggingface.co при первом включении перевода. После этого работает офлайн.";
  } else {
    els.browserVoiceSection.style.opacity = "";
    els.browserVoiceSection.style.pointerEvents = "";
    els.piperStatus.textContent = "";
  }
}

async function init() {
  bindRangeDisplay();
  const stored = await new Promise((res) =>
    chrome.storage.sync.get(DEFAULTS, (v) => res({ ...DEFAULTS, ...(v || {}) })),
  );
  writeForm(stored);
  const voices = await loadVoices();
  populateVoices(voices, stored.voiceURI);
  for (const r of els.engineRadios) {
    r.addEventListener("change", () => {
      if (r.checked) applyEngineUI(r.value);
    });
  }

  els.save.addEventListener("click", async () => {
    const settings = readForm();
    await new Promise((res) => chrome.storage.sync.set(settings, res));
    // Notify all YouTube tabs about the new settings so they apply live.
    try {
      const tabs = await chrome.tabs.query({ url: ["*://*.youtube.com/*"] });
      for (const tab of tabs) {
        chrome.tabs
          .sendMessage(tab.id, { type: "ru-yt-settings-updated", payload: settings })
          .catch(() => {});
      }
    } catch {}
    flashSaved();
  });

  els.testVoice.addEventListener("click", () => {
    try {
      window.speechSynthesis.cancel();
    } catch {}
    const utt = new SpeechSynthesisUtterance(
      "Проверка голоса. Это пример того, как будет звучать перевод видео.",
    );
    const settings = readForm();
    if (settings.voiceURI) {
      const v = window.speechSynthesis.getVoices().find((x) => x.voiceURI === settings.voiceURI);
      if (v) {
        utt.voice = v;
        utt.lang = v.lang;
      }
    }
    if (!utt.voice) utt.lang = "ru-RU";
    utt.rate = settings.rate;
    utt.pitch = settings.pitch;
    utt.volume = settings.volume;
    window.speechSynthesis.speak(utt);
  });
}

function flashSaved() {
  const old = els.save.textContent;
  els.save.textContent = "Сохранено ✓";
  els.save.disabled = true;
  setTimeout(() => {
    els.save.textContent = old;
    els.save.disabled = false;
  }, 1100);
}

init();
