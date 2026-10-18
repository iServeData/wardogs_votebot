"""Minimal, defensive client for the WARDOGS dedicated-server HTTP RCON API (/v1).

Reference: community docs of the WDRCON API (warcon-app/warcon docs/wardogs-api.md,
wardogs.tech/rcon-reference). Every request carries `Authorization: Bearer <password>`.

Hardening:
- strict timeouts, response size cap, JSON-only parsing
- client-side request budget (stays far below the server's 600 req/min/IP)
- honours 429 Retry-After and backs off
- path parameters are validated + percent-encoded (no path injection)
- the password is never logged
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
import time
from collections import deque
from typing import Any
from urllib.parse import quote

import aiohttp

log = logging.getLogger("votebot.rcon")

MAX_RESPONSE_BYTES = 1_000_000
MAX_CONFIG_BYTES = 65_536  # server body cap
_ID_RE = re.compile(r"^[0-9A-Za-z_.:\-]{1,64}$")


class RconError(Exception):
    def __init__(self, status: int, code: str, message: str, payload: Any = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.payload = payload


class RconUnavailable(RconError):
    pass


class RconClient:
    def __init__(self, base_url: str, password: str, timeout_s: int = 8,
                 max_per_min: int = 120, ca_file: str | None = None):
        self._base = base_url.rstrip("/")
        self._auth = f"Bearer {password}"
        self._timeout = aiohttp.ClientTimeout(total=timeout_s, connect=min(5, timeout_s))
        self._max_per_min = max_per_min
        self._sent: deque[float] = deque()
        self._hold_until = 0.0
        self._lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._ssl: ssl.SSLContext | bool = True
        if ca_file:
            ctx = ssl.create_default_context(cafile=ca_file)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            self._ssl = ctx
        self.routes: set[str] = set()

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"Accept": "application/json", "User-Agent": "wardogs-votebot/1.0"},
                connector=aiohttp.TCPConnector(limit=4, ttl_dns_cache=300),
                trust_env=False,  # ignore proxy env vars
            )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    def seg(value: str) -> str:
        """Validate + encode a single path segment (steam id, map id...)."""
        if not _ID_RE.match(value or ""):
            raise ValueError("invalid identifier")
        return quote(value, safe="")

    async def _budget(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._hold_until:
                raise RconUnavailable(429, "rate_limited", f"holding for {self._hold_until - now:.0f}s")
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            if len(self._sent) >= self._max_per_min:
                raise RconUnavailable(429, "local_budget", "local RCON request budget exhausted")
            self._sent.append(now)

    async def request(self, method: str, path: str, body: Any | None = None, *, text: str | None = None,
                      extra_headers: dict[str, str] | None = None, want_headers: bool = False,
                      want_raw: bool = False) -> Any:
        await self.start()
        await self._budget()
        assert self._session is not None
        headers = {"Authorization": self._auth}
        if extra_headers:
            headers.update(extra_headers)
        data: str | bytes | None = None
        if text is not None:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            data = text.encode("utf-8")
            if len(data) > MAX_CONFIG_BYTES:
                raise RconError(413, "too_large", f"config document is {len(data)} bytes; server limit is {MAX_CONFIG_BYTES}")
        elif body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":"))
        url = self._base + path
        try:
            async with self._session.request(method, url, data=data, headers=headers,
                                             ssl=self._ssl, allow_redirects=False) as resp:
                # Read the WHOLE body (content.read(n) may return only the first chunk,
                # which cut the 4+ KB config reply short and made it unparseable).
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(16384):
                    buf.extend(chunk)
                    if len(buf) > MAX_RESPONSE_BYTES:
                        raise RconError(resp.status, "too_large", "response exceeded size cap")
                raw = bytes(buf)
                if resp.status == 429:
                    ra = resp.headers.get("Retry-After", "5")
                    try:
                        wait = min(60.0, max(1.0, float(ra)))
                    except ValueError:
                        wait = 5.0
                    self._hold_until = time.monotonic() + wait
                    raise RconUnavailable(429, "rate_limited", f"server asked to wait {wait:.0f}s")
                payload: Any = None
                if raw:
                    try:
                        payload = json.loads(raw.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        payload = None
                if resp.status >= 400:
                    err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
                    code = str(err.get("code", "http_error"))[:64]
                    msg = str(err.get("message", resp.reason or ""))[:300]
                    if resp.status in (401, 403):
                        log.error("RCON rejected credentials (%s). Check RCON_PASSWORD / allow-list.", resp.status)
                    raise RconError(resp.status, code, msg, payload)
                if want_raw:
                    return payload, raw.decode("utf-8", errors="replace"), dict(resp.headers)
                if want_headers:
                    return payload, dict(resp.headers)
                return payload
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RconUnavailable(0, "unreachable", type(e).__name__) from None

    # ---- typed helpers -------------------------------------------------
    async def capabilities(self) -> dict:
        data = await self.request("GET", "/v1/capabilities") or {}
        self.routes = set(data.get("routes") or [])
        return data

    async def status(self) -> dict:
        return await self.request("GET", "/v1/status") or {}

    async def server_id(self) -> str | None:
        """The server's WARDOGS join code (stable even when the server is renamed)."""
        try:
            data = await self.request("GET", "/v1/server-id") or {}
        except RconError as e:
            if e.status == 404:
                return None
            raise
        v = data.get("serverId") if isinstance(data, dict) else None
        return str(v)[:64] if v else None

    async def health(self) -> dict:
        return await self.request("GET", "/v1/health") or {}

    async def players(self) -> list[dict]:
        data = await self.request("GET", "/v1/players") or {}
        players = data.get("players") or []
        return [p for p in players if isinstance(p, dict) and p.get("steamId")]

    async def catalog_maps(self) -> list[dict]:
        return (await self.request("GET", "/v1/catalog/maps") or {}).get("maps") or []

    async def catalog_lightings(self) -> list[dict]:
        return (await self.request("GET", "/v1/catalog/lightings") or {}).get("lightings") or []

    async def catalog_experiences(self) -> list[dict]:
        return (await self.request("GET", "/v1/catalog/experiences") or {}).get("experiences") or []

    async def map_experiences(self, map_id: str) -> list[str]:
        d = await self.request("GET", f"/v1/catalog/maps/{self.seg(map_id)}/experiences") or {}
        return [str(x) for x in d.get("experiences") or []]

    async def map_alternators(self, map_id: str) -> list[dict]:
        d = await self.request("GET", f"/v1/catalog/maps/{self.seg(map_id)}/alternators") or {}
        return d.get("alternators") or []

    async def rotation(self) -> dict:
        return await self.request("GET", "/v1/rotation") or {}

    async def whisper(self, steam_id: str, message: str) -> None:
        await self.request("POST", f"/v1/players/{self.seg(steam_id)}/message", {"message": message[:256]})

    async def broadcast(self, message: str) -> None:
        await self.request("POST", "/v1/broadcast", {"message": message[:256]})

    # ---- config document (the only rotation write path on live builds) -----------
    # NOTE: this bot deliberately has NO call to POST /v1/match/map. On the live
    # server that route travelled to the map within ~1 minute of each vote.
    async def get_config(self) -> dict:
        """Read the whole config document + revision.

        Handles the shapes seen across builds: JSON {revision, text, ...}, the text under
        another key, or a raw text/plain body with the revision only in the ETag header.
        Update 0.1.2 switched RCON to 'Standard' response detail by default, so if the text
        is missing the bot retries once asking for full detail."""
        last_shape = ""
        for path in ("/v1/config", "/v1/config?detail=full"):
            payload, raw, headers = await self.request("GET", path, want_raw=True)
            etag = (headers.get("ETag") or headers.get("Etag") or "").replace("W/", "").strip().strip('"')
            ctype = headers.get("Content-Type", "")
            text, rev, writable = None, etag or None, None
            if isinstance(payload, dict):
                inner = payload.get("config") if isinstance(payload.get("config"), dict) else payload
                for k in ("text", "document", "ini", "content", "body"):
                    if isinstance(inner.get(k), str) and inner.get(k).strip():
                        text = inner[k]
                        break
                rev = str(inner.get("revision") or payload.get("revision") or rev or "") or None
                writable = inner.get("writable", payload.get("writable"))
                last_shape = f"JSON keys={sorted(payload.keys())[:12]}"
            elif raw.strip().startswith("[") or "text/plain" in ctype:
                text = raw  # raw ini document
                last_shape = f"text body ({len(raw)} chars, Content-Type={ctype or 'none'})"
            else:
                last_shape = f"unparseable body ({len(raw)} chars, Content-Type={ctype or 'none'})"
            if text and rev:
                return {"text": text, "revision": rev, "writable": writable}
            last_shape += f", revision={'yes' if rev else 'no'}, ETag={'yes' if etag else 'no'}, text={'yes' if text else 'no'}"
            log.warning("GET %s gave no usable document: %s", path, last_shape)
        raise RconError(0, "bad_config", f"GET /v1/config returned no document text or revision ({last_shape})")

    async def validate_config(self, text: str) -> dict:
        return await self.request("POST", "/v1/config/validate", text=text) or {}

    async def put_config(self, text: str, revision: str) -> dict:
        """Write the whole document with optimistic locking. Never uses force=true."""
        rev = revision.replace('"', "")
        return await self.request("PUT", "/v1/config", text=text, extra_headers={"If-Match": f'"{rev}"'}) or {}
