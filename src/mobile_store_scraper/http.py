"""Shared async HTTP plumbing: one httpx client, bounded concurrency and polite retries."""
from __future__ import annotations

import asyncio
import random
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .errors import IpBlocked, NetworkError, RateLimited

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
THROTTLE_STATUSES = {429, 503}


def _is_block_page(resp: httpx.Response) -> bool:
    u = resp.url
    return u.path.startswith("/sorry/") and "google." in (u.host or "")


class _HostState:
    __slots__ = ("cooldown_until", "strikes", "next_slot", "lock", "circuit_open_until")

    def __init__(self) -> None:
        self.cooldown_until = 0.0
        self.circuit_open_until = 0.0
        self.strikes = 0
        self.next_slot = 0.0
        self.lock = asyncio.Lock()


class HttpClient:
    """Thin wrapper over ``httpx.AsyncClient`` built for scraping politely at scale.

    * Pace is chosen by the caller and defaults to **as fast as possible**:
      ``concurrency`` caps simultaneous requests (``None`` = unlimited) and ``rate_limit``
      (requests/second per host, ``None`` = unlimited) spaces requests out evenly.
    * **Adaptive throttling**: a 429/503 from a host puts the *whole host* into a cooldown
      that every pending request respects, growing exponentially with consecutive
      throttles (capped at ``max_backoff``) and resetting after successes. This backs off
      far faster than per-request retries and is what keeps all-country sweeps healthy.
    * A redirect to Google's ``/sorry/`` captcha page pauses the host for ``block_cooldown``
      seconds and retries up to ``block_retries`` times before raising ``IpBlocked``
      (``block_retries=0`` fails fast). If the block persists, a circuit breaker makes every
      request to that host fail fast for ``5 * block_cooldown`` without touching the network.
    * 5xx and network errors are retried with exponential backoff + jitter; ``Retry-After``
      is honoured. Any other status is returned untouched (404 is meaningful for stores).
    """

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        *,
        concurrency: Optional[int] = None,
        retries: int = 6,
        backoff: float = 1.0,
        max_backoff: float = 45.0,
        rate_limit: Optional[float] = None,
        block_retries: int = 2,
        block_cooldown: float = 60.0,
        timeout: float = 30.0,
        proxy: Optional[str] = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            proxy=proxy,
            limits=httpx.Limits(max_connections=concurrency or 256, max_keepalive_connections=concurrency or 64),
        )
        self.concurrency = concurrency
        # ``None`` = as fast as possible: no cap beyond the connection pool.
        self.semaphore = asyncio.Semaphore(concurrency if concurrency else 1_000_000)
        self.retries = retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.rate_limit = rate_limit
        self.block_retries = block_retries
        self.block_cooldown = block_cooldown
        self._hosts: dict[str, _HostState] = {}
        self.stats = {"requests": 0, "retries": 0, "throttled": 0, "blocked": 0}

    def _host(self, url: str) -> _HostState:
        host = urlsplit(str(url)).netloc
        st = self._hosts.get(host)
        if st is None:
            st = self._hosts[host] = _HostState()
        return st

    async def _wait_turn(self, st: _HostState) -> None:
        while True:
            now = time.monotonic()
            if st.cooldown_until > now:
                await asyncio.sleep(st.cooldown_until - now)
                continue
            if not self.rate_limit:
                return
            async with st.lock:
                now = time.monotonic()
                slot = max(now, st.next_slot)
                st.next_slot = slot + 1.0 / self.rate_limit
            if slot > now:
                await asyncio.sleep(slot - now)
            return

    def _throttle(self, st: _HostState, resp: Optional[httpx.Response]) -> float:
        st.strikes += 1
        delay = min(self.backoff * (2 ** (st.strikes - 1)), self.max_backoff)
        if resp is not None:
            ra = resp.headers.get("Retry-After", "")
            if ra.isdigit():
                delay = max(delay, min(float(ra), self.max_backoff))
        delay += random.uniform(0, self.backoff)
        st.cooldown_until = max(st.cooldown_until, time.monotonic() + delay)
        return delay

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        st = self._host(url)
        resp: Optional[httpx.Response] = None
        last_exc: Optional[BaseException] = None
        blocks = 0
        attempt = 0
        while True:
            if st.circuit_open_until > time.monotonic():
                raise IpBlocked(f"{method} {url}: skipped - {urlsplit(str(url)).netloc} recently answered with "
                                f"Google's 'unusual traffic' captcha page (circuit open for "
                                f"{st.circuit_open_until - time.monotonic():.0f}s more)")
            await self._wait_turn(st)
            resp = None
            try:
                async with self.semaphore:
                    await self._wait_turn(st)  # cooldown may have started while queued
                    self.stats["requests"] += 1
                    resp = await self.client.request(method, url, **kwargs)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
            if resp is not None and _is_block_page(resp):
                # Google's captcha interstitial. Hammering extends it, so the whole host pauses for
                # ``block_cooldown`` (shared by every pending request) and we try again a few times.
                self.stats["blocked"] += 1
                if blocks >= self.block_retries:
                    # Still blocked after waiting: stop sending anything to this host for a while,
                    # so pending requests fail fast instead of prolonging the block.
                    st.circuit_open_until = time.monotonic() + self.block_cooldown * 5
                    raise IpBlocked(f"{method} {url}: Google answered with its 'unusual traffic' captcha page "
                                    f"({resp.url.host}{resp.url.path}); this IP is temporarily blocked")
                blocks += 1
                st.cooldown_until = max(st.cooldown_until,
                                        time.monotonic() + self.block_cooldown * random.uniform(1.0, 1.1))
                continue
            if resp is not None and resp.status_code not in RETRY_STATUSES:
                if st.strikes:
                    st.strikes = max(0, st.strikes - 1)
                return resp
            if attempt >= self.retries:
                break
            attempt += 1
            self.stats["retries"] += 1
            if resp is not None and resp.status_code in THROTTLE_STATUSES:
                self.stats["throttled"] += 1
                self._throttle(st, resp)
            else:
                await asyncio.sleep(min(self.backoff * (2 ** (attempt - 1)), self.max_backoff) + random.uniform(0, self.backoff))
        if resp is not None:
            if resp.status_code == 429:
                raise RateLimited(f"{method} {url} -> 429 after {self.retries} retries")
            return resp
        assert last_exc is not None
        raise NetworkError(f"{method} {url}: {type(last_exc).__name__}: {last_exc}") from last_exc

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()
