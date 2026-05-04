"""Lightweight unit tests for the translate service.

These tests use httpx mocks so they don't hit Google in CI.  The pipeline
itself is exercised end-to-end via a manual smoke test on a short
YouTube clip — see README.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import translate


def _patch_get(monkeypatch, body):
    """Replace httpx.AsyncClient.get with a stub that returns `body` as JSON.

    httpx.Response needs the request attached for raise_for_status, so we
    construct it via Request roundtrip.
    """
    async def _fake_get(self, url, params=None, **kwargs):  # noqa: ANN001
        req = httpx.Request("GET", url, params=params)
        resp = httpx.Response(200, json=body, request=req)
        return resp

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)


@pytest.mark.asyncio
async def test_translate_text_strips_chunks(monkeypatch):
    body = [[["Привет мир", "Hello world", None, None, 1]], None, "en"]
    _patch_get(monkeypatch, body)
    out = await translate.translate_text("Hello world")
    assert out == "Привет мир"


@pytest.mark.asyncio
async def test_translate_segments_round_trip(monkeypatch):
    # Bracketed-marker batching: the real gtx endpoint preserves [N] markers
    # so we can split the response back into N items in one round-trip.
    translated = "[1] раз\n[2] два\n[3] три"
    body = [[[translated, "[1] one\n[2] two\n[3] three", None, None, 1]], None, "en"]
    _patch_get(monkeypatch, body)
    out = await translate.translate_segments(["one", "two", "three"])
    assert out == ["раз", "два", "три"]


@pytest.mark.asyncio
async def test_translate_segments_falls_back_per_item_when_markers_lost(monkeypatch):
    # If the translator drops one of the markers, we fall back to per-item
    # translation rather than returning a misaligned batch.
    calls = {"n": 0}

    async def _fake_get(self, url, params=None, **kwargs):  # noqa: ANN001
        calls["n"] += 1
        q = (params or {}).get("q", "")
        if "[1]" in q and "[3]" in q and "\n" in q:
            # Paragraph call where marker [2] got lost.
            translated = "[1] раз [3] три"
        else:
            # Per-item fallback path: echo a known translation.
            translated = {"one": "раз", "two": "два", "three": "три"}.get(q, q)
        req = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json=[[[translated, q, None, None, 1]], None, "en"],
            request=req,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    out = await translate.translate_segments(["one", "two", "three"])
    assert out == ["раз", "два", "три"]
    assert calls["n"] >= 4  # 1 paragraph + 3 per-item fallbacks


@pytest.mark.asyncio
async def test_translate_segments_paragraph_chunking(monkeypatch):
    # When the input exceeds _PARAGRAPH_LINES we split into multiple
    # paragraphs.  Verify each paragraph round-trips independently.
    seen_queries: list[str] = []

    async def _fake_get(self, url, params=None, **kwargs):  # noqa: ANN001
        q = (params or {}).get("q", "")
        seen_queries.append(q)
        # Echo each [N] line in Russian by prefixing "ру:".  Single-item
        # paragraphs skip the marker path and go through translate_text
        # plain — handle that too.
        if "[" in q and "]" in q:
            out_lines = []
            for line in q.splitlines():
                m = translate._MARKER_RE.match(line)
                if m:
                    rest = line[m.end():].strip()
                    out_lines.append(f"[{m.group(1)}] ру:{rest}")
                else:
                    out_lines.append(line)
            translated = "\n".join(out_lines)
        else:
            translated = f"ру:{q}"
        req = httpx.Request("GET", url, params=params)
        return httpx.Response(
            200,
            json=[[[translated, q, None, None, 1]], None, "en"],
            request=req,
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", _fake_get)
    inputs = [f"line{i}" for i in range(1, 14)]
    out = await translate.translate_segments(inputs)
    assert out == [f"ру:line{i}" for i in range(1, 14)]
    # 13 items at 6 per paragraph -> 3 paragraphs.
    assert len(seen_queries) == 3


@pytest.mark.asyncio
async def test_translate_text_empty_returns_empty():
    assert await translate.translate_text("") == ""
    assert await translate.translate_text("   ") == ""
