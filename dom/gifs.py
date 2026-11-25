"""Reaction GIFs: Dom names a short search query, this module finds one on KLIPY.

KLIPY's content_filter is set to its strictest level and the search returns mp4 files, which Telegram
fetches by URL as animations. Every failure means "no GIF"; nothing here may fail a turn.
"""

from __future__ import annotations

import asyncio
import logging
from random import Random

import httpx

log = logging.getLogger(__name__)
SEARCH_URL = "https://api.klipy.com/api/v1/{key}/gifs/search"
TIMEOUT_SECONDS = 5
RESULTS = 10  # KLIPY allows 8 to 50 per page
MAX_BYTES = 5_000_000  # Telegram fetches the file by URL; reaction GIFs are far smaller
SIZES = ("md", "sm")  # KLIPY's medium rendition is about 300 px wide; the small one is the fallback
MEDIA_HOST = "klipy.com"
GIF_NOTE = (
    'Optional action "gif": a short English search query (two to four words, no names or private details) '
    "for a reaction GIF that is sent after the bubbles."
)


def _mp4(item: object) -> str | None:
    """The URL of the first rendition of a KLIPY result that is a small mp4 hosted by KLIPY, else None."""
    if not isinstance(item, dict) or item.get("type") != "gif" or not isinstance(item.get("file"), dict):
        return None
    for size in SIZES:
        rendition = item["file"].get(size)
        mp4 = rendition.get("mp4") if isinstance(rendition, dict) else None
        if not isinstance(mp4, dict):
            continue
        url, bytes_ = mp4.get("url"), mp4.get("size")
        if not isinstance(url, str) or not isinstance(bytes_, int) or isinstance(bytes_, bool) or bytes_ > MAX_BYTES:
            continue
        parsed = httpx.URL(url)
        if parsed.scheme == "https" and (parsed.host == MEDIA_HOST or parsed.host.endswith("." + MEDIA_HOST)):
            return url
    return None


class GifSearch:
    def __init__(self, api_key: str, http: httpx.AsyncClient, rng: Random | None = None):
        self._key, self._http, self._rng = api_key, http, rng or Random()

    async def _search(self, query: str) -> object:
        async with asyncio.timeout(TIMEOUT_SECONDS):
            response = await self._http.get(
                SEARCH_URL.format(key=self._key),
                params={
                    "q": query, "per_page": RESULTS, "content_filter": "high", "format_filter": "mp4",
                    "customer_id": "dom",  # one fixed id: nothing about the chat or its people goes to KLIPY
                },
                timeout=TIMEOUT_SECONDS,
            )
        response.raise_for_status()
        return response.json()

    async def probe(self) -> str | None:
        """None when KLIPY accepts the key, else a short reason that never holds the key (the request URL does)."""
        try:
            await self._search("hello")
        except httpx.HTTPStatusError as error:
            return "KLIPY refused the key." if error.response.status_code in (401, 403) else f"KLIPY answered HTTP {error.response.status_code}."
        except (httpx.HTTPError, TimeoutError):
            return "Could not reach KLIPY."
        except ValueError:
            return "KLIPY's answer was not in the expected format."
        return None

    async def find(self, query: str) -> str | None:
        try:
            data = await self._search(query)
        except (httpx.HTTPError, ValueError, TimeoutError) as error:  # the request URL holds the key: log the type only
            log.warning("gif search failed (%s)", type(error).__name__)
            return None
        items = data.get("data") if isinstance(data, dict) else None
        items = items.get("data") if isinstance(items, dict) else None
        urls = [url for item in items if (url := _mp4(item))] if isinstance(items, list) else []
        return self._rng.choice(urls) if urls else None
