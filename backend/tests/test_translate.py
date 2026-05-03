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
    # We can only realistically mock the joined response.  The real Google
    # endpoint preserves @@SEG@@ separators most of the time.
    sep = "\n@@SEG@@\n"
    joined = sep.join(["раз", "два", "три"])
    body = [[[joined, "one\n@@SEG@@\ntwo\n@@SEG@@\nthree", None, None, 1]], None, "en"]
    _patch_get(monkeypatch, body)
    out = await translate.translate_segments(["one", "two", "three"])
    assert out == ["раз", "два", "три"]


@pytest.mark.asyncio
async def test_translate_text_empty_returns_empty():
    assert await translate.translate_text("") == ""
    assert await translate.translate_text("   ") == ""
