#!/usr/bin/env python3
"""
OpenHands sandbox gateway.

Single public port (8000). Routes each request to the right sandbox's random
host port by session_api_key (HTTP header X-Session-API-Key / WS query param).

Map built by docker-inspecting all oh-agent-server-* containers:
  OH_SESSION_API_KEYS_<n>  ->  the 8000/tcp HostPort (random).

Fixes vs naive version:
  - /health (keyless) is forwarded to the MOST RECENTLY created sandbox, so the
    app's readiness probe actually checks the sandbox instead of getting a fake 200.
  - upstream forwards are retried, so a brand-new sandbox's agent-server (still
    booting) doesn't cause a 502.
  - CORS preflight (OPTIONS, keyless) answered permissively instead of 502.
"""
import asyncio
import threading
import time

import docker
from aiohttp import ClientSession, WSMsgType, web

GATEWAY_PORT = 8000
SANDBOX_NAME_PREFIX = "oh-agent-server-"
KEY_ENV_PREFIX = "OH_SESSION_API_KEYS_"
AGENT_SERVER_CONTAINER_PORT = "8000/tcp"
UPSTREAM_RETRIES = 30
UPSTREAM_RETRY_DELAY = 1.0

_MAP = {}
_LATEST_PORT = None  # most recently created sandbox's host port (for keyless /health)
_LOCK = threading.Lock()


def refresh_map():
    """Inspect all sandboxes; rebuild {session_api_key: host_port} + track newest."""
    try:
        client = docker.from_env()
        new_map = {}
        latest_port = None
        latest_created = ""
        for c in client.containers.list(filters={"name": SANDBOX_NAME_PREFIX}):
            env = c.attrs.get("Config", {}).get("Env", []) or []
            keys = [
                e.split("=", 1)[1]
                for e in env
                if e.startswith(KEY_ENV_PREFIX) and "=" in e
            ]
            bindings = c.attrs.get("NetworkSettings", {}).get("Ports", {}).get(
                AGENT_SERVER_CONTAINER_PORT
            )
            host_port = int(bindings[0]["HostPort"]) if bindings else None
            if host_port:
                for k in keys:
                    new_map[k] = host_port
                created = c.attrs.get("Created", "") or ""
                if created > latest_created:  # ISO strings compare chronologically
                    latest_created = created
                    latest_port = host_port
        with _LOCK:
            _MAP.clear()
            _MAP.update(new_map)
            global _LATEST_PORT
            _LATEST_PORT = latest_port
        summary = ", ".join(f"{k[:8]}:{p}" for k, p in new_map.items()) or "(empty)"
        print(f"[map] {len(new_map)} key(s): {summary} latest={latest_port}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[map] refresh error: {e}", flush=True)


def _map_refresher():
    while True:
        refresh_map()
        time.sleep(3)


def _get_port(key):
    with _LOCK:
        return _MAP.get(key)


def _get_latest_port():
    with _LOCK:
        return _LATEST_PORT


def _extract_key(request):
    k = request.headers.get("X-Session-API-Key")
    if k:
        return k
    return request.query.get("session_api_key")


def _resolve(key):
    """Return host port for key, refreshing once if unknown."""
    port = _get_port(key)
    if not port:
        refresh_map()
        port = _get_port(key)
    return port


async def _forward_once(request, port):
    """Forward one HTTP request to localhost:port. Raises on upstream failure."""
    target = f"http://127.0.0.1:{port}{request.path_qs}"
    async with ClientSession() as s:
        body = await request.read()
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in ("host", "content-length")
        }
        async with s.request(
            request.method, target, headers=headers, data=body, allow_redirects=False
        ) as r:
            resp_body = await r.read()
            resp = web.Response(body=resp_body, status=r.status)
            for k, v in r.headers.items():
                if k.lower() not in (
                    "content-encoding",
                    "transfer-encoding",
                    "content-length",
                    "connection",
                ):
                    resp.headers[k] = v
            return resp


def _cors_preflight(request):
    resp = web.Response(status=204)
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    return resp


async def _forward_with_retry(request, port, tag="http"):
    """Forward + retry while a new sandbox's agent-server may still be booting."""
    last_err = None
    for attempt in range(UPSTREAM_RETRIES):
        try:
            return await _forward_once(request, port)
        except Exception as e:  # noqa: BLE001
            last_err = e
            if attempt < UPSTREAM_RETRIES - 1:
                await asyncio.sleep(UPSTREAM_RETRY_DELAY)
    print(f"[{tag}] upstream error after {UPSTREAM_RETRIES} tries: {last_err}", flush=True)
    return web.Response(status=502, text=f"upstream not ready: {last_err}")


async def handle_http(request):
    key = _extract_key(request)
    print(
        f"[http] {request.method} {request.path_qs[:70]} key={(key or 'None')[:12]}",
        flush=True,
    )

    # CORS preflight (browsers send OPTIONS without a key)
    if request.method == "OPTIONS":
        return _cors_preflight(request)

    # Keyless health checks: forward to the most recently created sandbox
    # (that's the one the app is waiting to become ready). Real check, not fake 200.
    if not key and request.path in ("/health", "/alive"):
        port = _get_latest_port()
        if not port:
            refresh_map()
            port = _get_latest_port()
        if not port:
            return web.Response(status=502, text="no sandbox for health check")
        return await _forward_with_retry(request, port, tag="health")

    port = _resolve(key)
    if not port:
        return web.Response(
            status=502, text=f"no sandbox for key {(key or '(none)')[:10]}"
        )
    return await _forward_with_retry(request, port)


async def handle_ws(request):
    key = _extract_key(request)
    port = _resolve(key)
    if not port:
        return web.Response(status=502, text="no sandbox for key")
    target = f"http://127.0.0.1:{port}{request.path_qs}"
    # heartbeat keeps idle conns alive and detects dead ones instead of letting
    # them rot into spurious reconnects.
    ws_in = web.WebSocketResponse(heartbeat=30)
    await ws_in.prepare(request)
    try:
        async with ClientSession() as s:
            async with s.ws_connect(target, heartbeat=30) as ws_out:
                async def pump(src, dst):
                    # On any end-of-stream (normal close, CLOSING, ERROR, or the
                    # async-for simply finishing), close the *other* side too so
                    # its pump stops cleanly. Guard on dst.closed so we never
                    # write to a closing transport ("Cannot write to closing
                    # transport"), which is what surfaced as the client-side
                    # "Failed to connect to server" toast.
                    try:
                        async for msg in src:
                            if msg.type == WSMsgType.TEXT:
                                if not dst.closed:
                                    await dst.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                if not dst.closed:
                                    await dst.send_bytes(msg.data)
                            elif msg.type in (
                                WSMsgType.CLOSE,
                                WSMsgType.CLOSING,
                                WSMsgType.ERROR,
                            ):
                                break
                    finally:
                        if not dst.closed:
                            await dst.close()

                # FIRST_COMPLETED: as soon as one direction ends, the pump's
                # finally closes the peer; cancel the now-doomed sibling instead
                # of letting it raise on a dead transport.
                a = asyncio.create_task(pump(ws_in, ws_out))
                b = asyncio.create_task(pump(ws_out, ws_in))
                _, pending = await asyncio.wait(
                    {a, b}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
    except Exception as e:  # noqa: BLE001
        print(f"[ws] error: {e}", flush=True)
    return ws_in


async def handle_request(request):
    """Dispatch WS upgrades vs plain HTTP."""
    if request.path.startswith("/sockets"):
        return await handle_ws(request)
    return await handle_http(request)


app = web.Application()
app.router.add_route("*", "/{tail:.*}", handle_request)


def main():
    threading.Thread(target=_map_refresher, daemon=True).start()
    print(f"[gateway] listening on :{GATEWAY_PORT}", flush=True)
    web.run_app(app, host="0.0.0.0", port=GATEWAY_PORT)


if __name__ == "__main__":
    main()
