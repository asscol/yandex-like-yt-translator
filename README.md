# YouTube Russian Voice Translator (Chrome Extension)

Расширение для Chrome, которое переводит видео с YouTube на русский язык
**голосом** — как делает Яндекс.Браузер. MVP без своего бэкенда: работает
только на видео с субтитрами (включая автоматические).

## Как это работает

```
YouTube watch page
    │
    │ 1. content.js достаёт ytInitialPlayerResponse
    ▼
Caption track URL (JSON3)
    │
    │ 2. background.js скачивает и парсит субтитры
    ▼
[{startMs, durMs, text}, ...]
    │
    │ 3. Google Translate (бесплатный gtx endpoint) → русский
    ▼
[{startMs, durMs, text, ru}, ...]
    │
    │ 4. <video> заглушается, текущий сегмент озвучивается
    │    через Web Speech API синхронно по timeupdate
    ▼
Русская озвучка поверх видео
```

Никаких API-ключей, никаких серверов — всё крутится в браузере.

## Установка (для разработки)

1. Открой `chrome://extensions/`.
2. Включи **«Режим разработчика»** в правом верхнем углу.
3. Нажми **«Загрузить распакованное»** и выбери папку этого репозитория.
4. Открой любое видео на `youtube.com/watch?v=...`.
5. В строке действий рядом с лайками появится кнопка **«🎙 Перевести»** —
   нажми её. После загрузки оригинал заглушится и пойдёт русская озвучка.

## Настройки

Кликни по иконке расширения в панели Chrome → попап с настройками:

- **Движок голоса** —
  - **Браузер** (по умолчанию): Web Speech API, использует системный TTS
    (Microsoft / Google / и т. п.). Быстро, без скачивания, но качество
    зависит от ОС.
  - **Piper Ruslan**: нейросетевой TTS, голос
    [`ru_RU-ruslan-medium`](https://huggingface.co/rhasspy/piper-voices)
    из проекта [Piper](https://github.com/rhasspy/piper). При первом
    включении расширение скачает модель (~60 МБ) с HuggingFace и
    закеширует её в OPFS. Последующие сессии работают офлайн.
- **Русский голос (браузер)** — выбор системного TTS-голоса (актуален
  только для движка «Браузер»).
- **Скорость** — 0.5×–2×. Для режима с заранее известными таймингами
  субтитров расширение дополнительно ускоряет голос, если русский
  перевод длиннее английского оригинала и не помещается в длительность
  субтитра.
- **Громкость** и **тон** (тон применяется только к браузерному движку).
- **Заглушать оригинал автоматически** — если выключено, оригинальное
  аудио продолжит играть параллельно с переводом.

## Piper-движок: как это работает

```
content.js (на youtube.com)
   │ 1. собирает стабилизированный текст субтитра
   ▼
background.js (service worker)
   │ 2. ru-yt-piper → ensureOffscreen()
   ▼
offscreen/offscreen.html (chrome-extension origin)
   │ 3. vits-web (ONNX Runtime Web + Piper WASM)
   │    - модель кешируется в OPFS (одна загрузка ~60 МБ)
   │    - phonemize → инференс → WAV
   ▼
<audio> внутри offscreen-документа → колонки
```

Все WASM-артефакты лежат локально в `vendor/`:

- `vendor/onnxruntime-web/` — ESM-сборка ONNX Runtime Web 1.18 + SIMD-WASM.
- `vendor/piper-wasm/` — собранный espeak-ng + piper_phonemize.
- `vendor/vits-web/` — обёртка `tts.predict({text, voiceId})` поверх ORT.

Модель Ruslan скачивается с
`https://huggingface.co/diffusionstudio/piper-voices/resolve/main/ru/ru_RU/ruslan/medium/ru_RU-ruslan-medium.onnx`
(потребуется доступ к HuggingFace при первом включении).

## Ограничения MVP

- Работает только на видео, у которых есть субтитры. Если субтитры
  отключены пользователем YouTube, расширение покажет ошибку.
- В live-режиме (когда YouTube не отдаёт `timedtext` JSON) расширение
  читает субтитры прямо из DOM плеера — это работает, но первые
  несколько слов могут быть пропущены, пока копится стабильная фраза
  (350 мс дебаунса).
- Браузерный TTS на Linux/macOS звучит «робот»; используй Piper для
  нормального качества.
- «Живые голоса» (клонирование оригинального спикера) **пока не
  поддерживаются**. Это требует тяжёлой модели (XTTS / Coqui /
  ElevenLabs). Запланировано на v0.3.

## Архитектура

```
manifest.json                   # MV3, host_permissions + offscreen reasons
content.js                      # инжектится на youtube.com, основная логика
content.css                     # стили кнопки и тоста
background.js                   # service worker — прокси fetch + offscreen
popup.html / .js / .css         # настройки движка, голоса, скорости
offscreen/offscreen.{html,js}   # Piper TTS на extension-origin (vits-web)
vendor/onnxruntime-web/         # вендорим ESM ORT + WASM (SIMD)
vendor/piper-wasm/              # piper_phonemize.wasm + .data (espeak-ng)
vendor/vits-web/                # пропатченный tts.predict()
icons/                          # 16/32/48/128 PNG (scripts/make_icons.py)
_locales/ru/                    # перевод имени и описания расширения
```

## План v0.2 («живые голоса»)

1. Бэкенд (FastAPI) с Whisper для транскрипции аудио (для видео без
   субтитров) и Silero / XTTS для русского TTS более высокого качества.
2. Клонирование тембра оригинального спикера через XTTS-v2 (5–10 секунд
   референса).
3. Опциональная диаризация (распознавание разных голосов) для разделения
   собеседников в подкастах/интервью.

## Лицензия

MIT.
