"""Shared async HTTP client for codal.ir (search API and letter pages).

Every tool goes through `fetch`, which serializes requests with a minimum gap (codal.ir answers 429 plus a
captcha after a burst of requests and then blocks the IP for an hour), caches answers, and turns HTTP failures
into `ToolError` messages the model can act on.
"""

from __future__ import annotations

import asyncio
import copy
import json as json_lib
import os
import random
import time
from typing import Any

import httpx
from curl_cffi.const import CurlOpt
from httpx_curl_cffi import AsyncCurlTransport
from mcp.server.mcpserver.exceptions import ToolError

SEARCH = "https://search.codal.ir/api/search"
WWW = "https://www.codal.ir"

HEADERS = {
    # User-Agent, sec-ch-ua and TLS come from the impersonated Chrome profile (see _get_client).
    "Accept-Language": "fa-IR,fa;q=0.9,en;q=0.8",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "priority": "u=1, i",
}
# A letter page is fetched the way Chrome opens a typed URL: a navigation with no Referer or Origin.
PAGE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
    "application/signed-exchange;v=b3;q=0.7",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "priority": "u=0, i",
}

MIN_INTERVAL = 2.0  # seconds between two requests to codal.ir; all requests are serialized
JITTER = (0.1, 0.5)  # extra seconds added to each gap
BLOCK_COOLDOWN = 60  # seconds to stop calling codal.ir after a 429 or captcha (shared servers recover fast)
MAX_RETRIES = 1  # timeouts, network errors and 5xx only; 429 and 403 are never retried
RETRY_DELAY = 2.0  # seconds before the retry
CACHE_TTL = 120  # seconds; default for searches
CACHE_MAX = 64
MAX_BODY = 5_000_000  # bytes; larger responses are refused (a big monthly report page is about 0.9 MB)
AXIOS = "application/json, text/plain, */*"  # AngularJS $http default, as codal's own search page sends it
# Host -> (site the browser calls it from, sec-fetch-site, Accept); doubles as the host allowlist.
HOSTS = {
    "search.codal.ir": ("https://www.codal.ir", "same-site", AXIOS),
    "www.codal.ir": ("https://www.codal.ir", "none", None),  # read as pages, see PAGE_HEADERS
}

_transport: httpx.AsyncBaseTransport | None = None
_client: httpx.AsyncClient | None = None
_lock: asyncio.Lock | None = None
_last = 0.0  # monotonic time the last request was sent
_blocked_until = 0.0
_proxy_index = 0
_retired: set[asyncio.Task] = set()
_cache: dict[tuple, tuple[float, float, Any]] = {}  # key -> (stored at, ttl, body)


class _XhrTransport(AsyncCurlTransport):
    """The Chrome profile sends navigation-only headers; a None value makes curl drop them on XHR calls."""

    def _create_request_params(self, req: httpx.Request) -> dict[str, Any]:
        # The parent drops "accept: */*" as an httpx default; curl would then send Chrome's page Accept instead.
        accept = req.headers.get("accept")
        p = super()._create_request_params(req)
        extra = [(b"cache-control", None)]  # Chrome sends max-age=0 only on a reload
        if req.headers.get("sec-fetch-mode") != "navigate":
            extra += [(b"upgrade-insecure-requests", None), (b"sec-fetch-user", None)]
        p["headers"] = [*p["headers"], *extra, *([(b"accept", b"*/*")] if accept == "*/*" else [])]
        return p


class ApiError(ToolError):
    """A failed upstream call. `status` is the HTTP status, or None for network errors."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Swap the transport (tests use httpx.MockTransport). Drops the current client, cache and block state."""
    global _transport, _client, _lock, _proxy_index, _last, _blocked_until
    _transport, _client, _lock, _proxy_index, _last, _blocked_until = transport, None, None, 0, 0.0, 0.0
    _cache.clear()


def _proxies() -> list[str]:
    """CODAL_MCP_PROXY: one proxy URL, or a comma-separated list used one at a time."""
    return [p.strip() for p in os.environ.get("CODAL_MCP_PROXY", "").split(",") if p.strip()]


def _next_proxy(client: httpx.AsyncClient) -> bool:
    """Sticky pool: move to the next proxy after a block or connect error; the next call rebuilds the client.

    Returns True when there was another proxy to move to. The old client is closed late because other requests
    may still use it.
    """
    global _client, _proxy_index
    if len(_proxies()) < 2 or client is not _client:
        return False
    _client, _proxy_index = None, _proxy_index + 1
    t = asyncio.create_task(_close_later(client))
    _retired.add(t)
    t.add_done_callback(_retired.discard)
    return True


async def _close_later(client: httpx.AsyncClient) -> None:
    await asyncio.sleep(40)  # longer than the 30 s request timeout
    await client.aclose()


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # System proxy settings are ignored; the proxy only comes from CODAL_MCP_PROXY. curl_cffi presents
        # Chrome's TLS/HTTP2 fingerprint and default headers. The proxy goes to the transport.
        proxies = _proxies()
        _client = httpx.AsyncClient(
            transport=_transport
            or _XhrTransport(
                impersonate="chrome",
                default_headers=True,
                curl_options={
                    CurlOpt.NOPROXY: "*"
                },  # httpx-curl-cffi drops its own NOPROXY unless this dict is non-empty
                proxy=proxies[_proxy_index % len(proxies)] if proxies else None,
                max_connections=4,
            ),
            headers=HEADERS,
            timeout=httpx.Timeout(30, connect=8),
            limits=httpx.Limits(max_connections=4),
            follow_redirects=False,
            trust_env=False,
        )
    return _client


def _blocked_message(seconds: int) -> str:
    return (
        f"codal.ir is rate limiting this connection (HTTP 429 / captcha). Do not retry now: wait about {seconds} "
        "seconds and then try again with fewer calls. Earlier results are still valid."
    )


async def fetch(url: str, params: dict[str, Any] | None = None, text: bool = False, ttl: float = CACHE_TTL) -> Any:
    """GET an endpoint and return the parsed JSON body, or the decoded body of a page when `text`.

    Answers are cached for `ttl` seconds. A 429 or captcha page raises at once and pauses all calls for
    BLOCK_COOLDOWN seconds; timeouts, network errors and 5xx are retried once.
    """
    global _blocked_until
    query = {k: v for k, v in (params or {}).items() if v is not None}
    host = httpx.URL(url).host
    if host not in HOSTS:
        raise ApiError(f"Refusing to call unexpected host {host}.")
    key = (url, tuple(sorted(query.items())), text)
    if (hit := _cache.get(key)) and time.monotonic() - hit[0] < hit[1]:
        return copy.deepcopy(hit[2])
    site, fetch_site, accept = HOSTS[host]
    if text:
        headers = dict(PAGE_HEADERS)
    else:
        headers = {"Referer": f"{site}/", "Accept": accept, "sec-fetch-site": fetch_site, "Origin": site}
    for attempt in range(MAX_RETRIES + 1):
        last = attempt == MAX_RETRIES
        try:
            r, content, client = await _request(url, query, headers, host)
        except ApiError as e:
            if last or e.status is not None:
                raise
            await asyncio.sleep(RETRY_DELAY)
            continue
        if r.status_code >= 500 and not last:
            await asyncio.sleep(RETRY_DELAY)
            continue
        break

    blocked = r.status_code == 429 or (text and b"Captcha.aspx/GetCaptchaImage" in content)
    if blocked or r.status_code == 403:
        rotated = _next_proxy(client)  # blocked: the next call goes through the next proxy, if there is one
        if blocked:
            if not rotated:
                _blocked_until = time.monotonic() + BLOCK_COOLDOWN
            raise ApiError(_blocked_message(BLOCK_COOLDOWN), 429)
    if r.status_code >= 400:
        raise ApiError(_status_message(r, host), r.status_code)
    if text:
        body = content.decode("utf-8", "replace")
    else:
        try:
            body = json_lib.loads(content)
        except ValueError as e:
            raise ApiError(f"{host} returned a non-JSON response (HTTP {r.status_code}).", r.status_code) from e
    if len(_cache) >= CACHE_MAX:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.monotonic(), ttl, copy.deepcopy(body))
    return body


async def _request(url, query, headers, host) -> tuple[httpx.Response, bytes, httpx.AsyncClient]:
    global _lock, _last
    if (left := _blocked_until - time.monotonic()) > 0:
        raise ApiError(_blocked_message(max(1, round(left))), 429)
    if _lock is None:
        _lock = asyncio.Lock()
    async with _lock:  # one request at a time, at least MIN_INTERVAL apart
        if (wait := _last + MIN_INTERVAL + random.uniform(*JITTER) - time.monotonic()) > 0:
            await asyncio.sleep(wait)
        client = _get_client()
        try:
            async with client.stream("GET", url, params=query, headers=headers) as r:
                content = await _read_capped(r, host)
        except httpx.TimeoutException as e:
            raise ApiError(f"{host} did not answer in time. Try again in a moment.") from e
        except httpx.RequestError as e:
            if isinstance(e, httpx.ConnectError | httpx.ProxyError):
                _next_proxy(client)
            raise ApiError(
                f"Could not reach {host} ({type(e).__name__}). codal.ir often answers only connections from Iran: "
                "check the internet connection, or set CODAL_MCP_PROXY to an Iranian HTTP proxy."
            ) from e
        finally:
            _last = time.monotonic()
    return r, content, client


async def _read_capped(r: httpx.Response, host: str) -> bytes:
    """Read the body, refusing anything over MAX_BODY so one huge answer cannot flood memory or context."""
    buf = bytearray()
    async for chunk in r.aiter_bytes():
        buf += chunk
        if len(buf) > MAX_BODY:
            raise ApiError(f"{host} sent a response over {MAX_BODY // 1_000_000} MB; refused.", r.status_code)
    return bytes(buf)


def _status_message(r: httpx.Response, host: str) -> str:
    code = r.status_code
    if code == 403:
        return f"{host} blocked the request (HTTP 403). Try another network or set CODAL_MCP_PROXY."
    if code >= 500:
        return f"{host} had a server error (HTTP {code}). Try again later."
    return f"{host} rejected the request (HTTP {code})."
