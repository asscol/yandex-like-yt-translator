"""Translation service.

The free `translate.googleapis.com/translate_a/single` endpoint with
`client=gtx` is unauthenticated and works for short text.  We retry with
exponential backoff because Google occasionally returns 429 / 5xx under
sustained load — same code path the Chrome extension uses, just from
the server side so we can batch multiple segments into one call.

Quality strategy (no API keys):
    1. **Bracketed numeric markers** — `[1]`, `[2]`… are preserved by
       Google Translate across many language pairs.  Far more robust
       than "@@SEG@@" which can get rephrased / dropped in Russian.
    2. **Paragraph batching** — we join ~6 short subtitle lines into one
       paragraph before sending; Google has more context to disambiguate
       homographs and produce idiomatic Russian.
    3. **Char-budget chunking** — keep each request under 1.5 KB so the
       URL fits the gtx endpoint's limit and we don't get truncated.
    4. **Graceful per-line fallback** — if the marker round-trip fails
       for one paragraph, we re-translate just that paragraph (one line
       at a time as last resort) without losing the whole batch.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger(__name__)

# How many original lines we glue into one paragraph before sending.
# YouTube auto-CC lines are ~5-10 words each; 6 ≈ a full sentence-or-two
# of context = noticeably better translation than line-by-line.
_PARAGRAPH_LINES = 6
# Soft byte budget for one request (gtx accepts much more, but short
# requests retry faster on transient 429 / 5xx).
_PARAGRAPH_CHAR_BUDGET = 1500

# Marker that survives translation: Google preserves [1], [2], etc.
# verbatim when wrapped on their own line.  We use trailing "." after
# the bracket so the translator treats it as a sentence end and doesn't
# stitch it to the next line.
_MARKER_RE = re.compile(r"\[(\d{1,4})\]")


def _wrap(idx: int, text: str) -> str:
    return f"[{idx}] {text}"


def _split_markers(translated: str, expected: int) -> list[str] | None:
    """Split a marker-tagged translated paragraph back into per-line list.

    Returns None if not all `expected` markers survived (caller falls
    back to a finer-grained retranslation for that paragraph)."""
    # We split on the marker boundaries, keeping the marker so we can
    # tell which line each fragment belongs to.  Some translations move
    # punctuation around the marker; tolerate that.
    pieces: dict[int, list[str]] = {}
    last_idx: int | None = None
    last_pos = 0
    for m in _MARKER_RE.finditer(translated):
        if last_idx is not None:
            pieces.setdefault(last_idx, []).append(translated[last_pos : m.start()])
        last_idx = int(m.group(1))
        last_pos = m.end()
    if last_idx is not None:
        pieces.setdefault(last_idx, []).append(translated[last_pos:])
    out: list[str] = []
    for i in range(1, expected + 1):
        chunk = "".join(pieces.get(i, [])).strip()
        # Strip leading punctuation Google often glues to the marker.
        chunk = chunk.lstrip(".,:;-—–·•) ").strip()
        if not chunk:
            return None
        out.append(chunk)
    return out


@retry(
    reraise=True,
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
    retry=retry_if_exception_type((httpx.HTTPError, ValueError)),
)
async def translate_text(
    text: str,
    *,
    source: str = "en",
    target: str = "ru",
    client: httpx.AsyncClient | None = None,
) -> str:
    """Translate a single string."""
    if not text.strip():
        return ""
    params = {
        "client": "gtx",
        "sl": source,
        "tl": target,
        "dt": "t",
        "q": text,
    }
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=30)
    try:
        resp = await client.get(settings.translate_endpoint, params=params)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not isinstance(data[0], list):
            raise ValueError(f"unexpected translate response: {data!r}")
        return "".join(chunk[0] or "" for chunk in data[0] if chunk)
    finally:
        if owns_client:
            await client.aclose()


async def _translate_paragraph(
    items: list[str],
    *,
    source: str,
    target: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """Translate one paragraph of N items as a single round-trip.

    Falls back to per-item translation if marker round-trip fails."""
    if not items:
        return []
    if len(items) == 1:
        return [await translate_text(items[0], source=source, target=target, client=client)]

    tagged = "\n".join(_wrap(i + 1, items[i]) for i in range(len(items)))
    translated = await translate_text(tagged, source=source, target=target, client=client)
    parts = _split_markers(translated, expected=len(items))
    if parts is not None:
        return parts

    # Markers didn't round-trip cleanly.  Translate each item individually
    # rather than dropping the whole paragraph.
    logger.info("marker round-trip failed for %d items; per-item fallback", len(items))
    out: list[str] = []
    for t in items:
        out.append(await translate_text(t, source=source, target=target, client=client))
    return out


def _chunk_for_paragraphs(items: list[str]) -> list[list[str]]:
    """Group items into paragraphs respecting both line-count and char budget."""
    paragraphs: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for t in items:
        # Each item adds ~6 chars of marker overhead ("[NN] ") + newline.
        cost = len(t) + 8
        if current and (
            len(current) >= _PARAGRAPH_LINES
            or current_chars + cost > _PARAGRAPH_CHAR_BUDGET
        ):
            paragraphs.append(current)
            current = []
            current_chars = 0
        current.append(t)
        current_chars += cost
    if current:
        paragraphs.append(current)
    return paragraphs


async def translate_segments(
    texts: Iterable[str],
    *,
    source: str = "en",
    target: str = "ru",
) -> list[str]:
    """Translate many short strings with paragraph-level context.

    Strategy: bucket the items into ~6-line paragraphs (≤1.5 KB each),
    send each paragraph as one bracketed-marker block, and reassemble.
    Far better Russian than line-by-line because the translator sees
    a full sentence-or-two of context."""
    items = list(texts)
    if not items:
        return []

    paragraphs = _chunk_for_paragraphs(items)
    out: list[str] = []
    async with httpx.AsyncClient(timeout=30) as client:
        for para in paragraphs:
            out.extend(
                await _translate_paragraph(
                    para, source=source, target=target, client=client
                )
            )
    if len(out) != len(items):
        # Defensive: shouldn't happen because the per-item fallback always
        # yields N outputs for N inputs.  Pad with empties so the caller
        # doesn't crash on zip(..., strict=True).
        logger.warning("translate_segments size mismatch %d vs %d", len(out), len(items))
        while len(out) < len(items):
            out.append("")
        out = out[: len(items)]
    return out
