"""Download one remote image without letting the URL reach private networks."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import typing

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend

USER_AGENT = "Autocut/0.1 (+image-cropper)"
READ_CHUNK = 64 * 1024


class FetchError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _parse_url(url: str) -> httpx.URL:
    if not isinstance(url, str) or len(url.strip()) > 2048:
        raise FetchError("invalid_url", "image_url must be a valid http or https URL.")
    try:
        parsed = httpx.URL(url.strip())
    except (httpx.InvalidURL, ValueError) as exc:
        raise FetchError("invalid_url", "image_url must be a valid http or https URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise FetchError("invalid_url", "image_url must be a valid http or https URL.")
    if parsed.userinfo:
        raise FetchError("invalid_url", "image_url must not contain credentials.")
    try:
        parsed = parsed.copy_with(port=parsed.port or (443 if parsed.scheme == "https" else 80))
    except (httpx.InvalidURL, ValueError) as exc:
        raise FetchError("invalid_url", "image_url must be a valid http or https URL.") from exc
    return parsed


def _is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def _resolve_public(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise FetchError("url_unreachable", "The image URL host could not be resolved.") from exc
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if not _is_public(address):
            raise FetchError("url_not_allowed", "The image URL must point to a public address.")
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise FetchError("url_unreachable", "The image URL host could not be resolved.")
    return addresses


class _PinnedBackend(AutoBackend):
    """Replace the hostname with an already-validated IP so DNS cannot be re-answered."""

    def __init__(self, host: str, addresses: list[str]) -> None:
        self.host = host.lower()
        self.addresses = addresses
        self._index = 0

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = self.addresses[self._index % len(self.addresses)]
        self._index += 1
        return await super().connect_tcp(
            address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


async def fetch_image_url(url: str, *, max_bytes: int, timeout: float) -> bytes:
    """Download one image over http(s), bounded by size and limited to public hosts."""
    parsed = _parse_url(url)
    if not 0.1 <= timeout <= 120:
        raise FetchError("invalid_config", "IMAGE_FETCH_TIMEOUT must be between 0.1 and 120 seconds.", 500)
    addresses = await asyncio.to_thread(_resolve_public, parsed.host, parsed.port)
    ssl_context = ssl.create_default_context()
    pool = httpcore.AsyncConnectionPool(
        ssl_context=ssl_context,
        max_connections=1,
        max_keepalive_connections=0,
        http1=True,
        http2=False,
        retries=0,
        network_backend=_PinnedBackend(parsed.host, addresses),
    )
    transport = httpx.AsyncHTTPTransport(verify=ssl_context, trust_env=False, retries=0)
    transport._pool = pool
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(timeout),
        headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
    )
    try:
        async with client.stream("GET", url.strip()) as response:
            status = response.status_code
            if status in {401, 403}:
                raise FetchError("url_forbidden", "The image URL refused access.")
            if status in {404, 410}:
                raise FetchError("url_not_found", "The image URL does not exist.")
            if not 200 <= status < 300:
                raise FetchError("url_fetch_failed", f"The image URL responded with HTTP {status}.", 502)
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > max_bytes:
                raise FetchError("upload_too_large", "Remote image exceeds the configured file-size limit.", 413)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes(READ_CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise FetchError("upload_too_large", "Remote image exceeds the configured file-size limit.", 413)
                chunks.append(chunk)
            return b"".join(chunks)
    except FetchError:
        raise
    except httpx.TimeoutException as exc:
        raise FetchError("url_timeout", "The image URL timed out.", 504) from exc
    except (httpx.HTTPError, OSError, ssl.SSLError) as exc:
        raise FetchError("url_fetch_failed", "The image URL could not be downloaded.", 502) from exc
    finally:
        await client.aclose()
