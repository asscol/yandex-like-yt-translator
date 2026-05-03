"""Translation service.

The free `translate.googleapis.com/translate_a/single` endpoint with
`client=gtx` is unauthenticated and works for short text.  We retry with
exponential backoff because Google occasionally returns 429 / 5xx under
sustained load — same code path the Chrome extension uses, just from
the server side so we can batch multiple segments into one call.
"""

from __future__ import annotations

from typing import Iterable

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

_BATCH_SEPARATOR = "\n@@SEG@@\n"


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


async def translate_segments(
    texts: Iterable[str],
    *,
    source: str = "en",
    target: str = "ru",
) -> list[str]:
    """Translate many short strings in one round-trip.

    We join them with a unique separator the translator is unlikely to
    rephrase, then split the response.  This is dramatically faster than
    one-call-per-segment when there are 50+ subtitle lines.
    """
    items = list(texts)
    if not items:
        return []
    joined = _BATCH_SEPARATOR.join(items)
    async with httpx.AsyncClient(timeout=30) as client:
        translated = await translate_text(joined, source=source, target=target, client=client)
    parts = translated.split(_BATCH_SEPARATOR.strip())
    if len(parts) != len(items):
        # Fall back to per-segment translation if the separator was eaten.
        async with httpx.AsyncClient(timeout=30) as client:
            return [
                await translate_text(t, source=source, target=target, client=client)
                for t in items
            ]
    return [p.strip() for p in parts]
