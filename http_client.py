from __future__ import annotations

import asyncio

import httpx


_default_client: httpx.AsyncClient | None = None
_proxy_clients: dict[str, httpx.AsyncClient] = {}
_client_lock = asyncio.Lock()


def _build_client(*, proxy: str | None = None) -> httpx.AsyncClient:
    kwargs = {
        "follow_redirects": True,
        "timeout": httpx.Timeout(5.0),
        "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
    }
    if proxy:
        try:
            return httpx.AsyncClient(proxy=proxy, **kwargs)
        except TypeError:
            return httpx.AsyncClient(proxies=proxy, **kwargs)  # type: ignore[arg-type]
    return httpx.AsyncClient(**kwargs)


async def get_http_client(proxy: str | None = None) -> httpx.AsyncClient:
    """Return a shared async client, cached by proxy URL when needed."""
    global _default_client

    if proxy:
        client = _proxy_clients.get(proxy)
        if client is not None and not client.is_closed:
            return client
        async with _client_lock:
            client = _proxy_clients.get(proxy)
            if client is not None and not client.is_closed:
                return client
            client = _build_client(proxy=proxy)
            _proxy_clients[proxy] = client
            return client

    if _default_client is not None and not _default_client.is_closed:
        return _default_client
    async with _client_lock:
        if _default_client is not None and not _default_client.is_closed:
            return _default_client
        _default_client = _build_client()
        return _default_client


async def close_http_clients() -> None:
    global _default_client

    clients = list(_proxy_clients.values())
    if _default_client is not None:
        clients.append(_default_client)
    _default_client = None
    _proxy_clients.clear()

    for client in clients:
        try:
            await client.aclose()
        except Exception:
            pass
