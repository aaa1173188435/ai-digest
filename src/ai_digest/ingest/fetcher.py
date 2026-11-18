"""HTTP fetching with a bounded concurrency, retry budget, and conditional GET.

Three behaviours worth calling out, because each one is a real failure we hit:

* **Retries only where retrying helps.** 5xx / timeouts / connection resets get
  exponential backoff with jitter. A 404 will never become a 200, so it fails
  fast instead of burning three attempts.
* **Conditional GET.** We replay the stored ``ETag`` / ``Last-Modified``. Feeds
  that answer 304 cost us no bandwidth and no parsing time.
* **A semaphore.** Eight feeds is nothing, but the same class is used for page
  fetches, and an unbounded gather is how you get rate-limited into oblivion.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

ACCEPT_FEED = (
    "application/atom+xml, application/rss+xml, application/xml;q=0.9, "
    "text/xml;q=0.9, */*;q=0.8"
)


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    content: bytes | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False
    error: str | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None and (self.not_modified or self.content is not None)


class Fetcher:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        concurrency: int = 6,
        user_agent: str = "ai-digest/0.1",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._retries = max(1, retries)
        self._base_headers = {"User-Agent": user_agent, "Accept": ACCEPT_FEED}
        self._semaphore = asyncio.Semaphore(concurrency)
        # Injected in tests so the whole pipeline runs without a socket.
        self._transport = transport

    async def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        async with self._semaphore:
            return await self._fetch_with_retry(url, etag, last_modified)

    async def fetch_many(self, urls: list[str]) -> list[FetchResult]:
        return list(await asyncio.gather(*(self.fetch(u) for u in urls)))

    async def _fetch_with_retry(
        self, url: str, etag: str | None, last_modified: str | None
    ) -> FetchResult:
        headers = dict(self._base_headers)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error: str | None = None
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers=headers,
            transport=self._transport,
        ) as client:
            for attempt in range(1, self._retries + 1):
                try:
                    response = await client.get(url)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < self._retries:
                        await self._sleep(attempt)
                        continue
                    break

                if response.status_code == httpx.codes.NOT_MODIFIED:
                    return FetchResult(
                        url=url,
                        status=304,
                        etag=etag,
                        last_modified=last_modified,
                        not_modified=True,
                        attempts=attempt,
                    )

                if 200 <= response.status_code < 300:
                    return FetchResult(
                        url=url,
                        status=response.status_code,
                        content=response.content,
                        etag=response.headers.get("etag"),
                        last_modified=response.headers.get("last-modified"),
                        attempts=attempt,
                    )

                last_error = f"HTTP {response.status_code}"
                retryable = (
                    response.status_code >= 500
                    or response.status_code == httpx.codes.TOO_MANY_REQUESTS
                )
                if retryable and attempt < self._retries:
                    await self._sleep(attempt)
                    continue
                break

        return FetchResult(url=url, error=last_error, attempts=self._retries)

    @staticmethod
    async def _sleep(attempt: int) -> None:
        # Exponential backoff, capped, plus jitter so parallel retries de-sync.
        await asyncio.sleep(min(2 ** (attempt - 1), 8) + random.uniform(0, 0.5))
