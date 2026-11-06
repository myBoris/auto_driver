"""HTTP/WS 服务：终端上行、浏览器监看、AI 操作与观察。"""

import asyncio
import base64
import contextlib
import json
import logging
import math
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .config import CenterConfig
from .discovery import start_discovery
from .protocol import PROTOCOL, SERVICE, TERMINAL_ID, normalize_action, number, unpack_frame
from .registry import APIError, TerminalSession

log = logging.getLogger(__name__)
CONFIG = web.AppKey("config", CenterConfig)
SESSIONS = web.AppKey("sessions", dict)
BROWSER_TOKEN = web.AppKey("browser_token", str)
BROWSER_COOKIE = "auto_driver_dashboard"


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except APIError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException as exc:
        if request.path.startswith(("/api/", "/ws/")):
            return web.json_response({"error": exc.reason}, status=exc.status)
        raise


@web.middleware
async def authenticate(request, handler):
    if request.path.startswith(("/api/", "/ws/")):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {request.app[CONFIG].token}"
        if secrets.compare_digest(supplied.encode(), expected.encode()):
            return await handler(request)
        cookie = request.cookies.get(BROWSER_COOKIE, "")
        if (not supplied and request.path.startswith("/api/")
                and secrets.compare_digest(cookie.encode(), request.app[BROWSER_TOKEN].encode())):
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                allowed = {f"{request.scheme}://{request.host}"}
                public = urlsplit(request.app[CONFIG].advertise_url)
                if public.netloc:
                    allowed.add(f"{public.scheme}://{public.netloc}")
                origin = request.headers.get("Origin")
                if origin and origin not in allowed:
                    raise APIError(403, "browser control requires the center's own page")
            return await handler(request)
        return web.json_response({"error": "unauthorized"}, status=401,
                                 headers={"WWW-Authenticate": "Bearer"})
    return await handler(request)


async def read_object(request) -> dict:
    try:
        data = await request.json()
    except (ValueError, UnicodeError) as exc:
        raise APIError(400, "request body must be valid JSON") from exc
    if not isinstance(data, dict):
        raise APIError(400, "request body must be an object")
    return data


def session_for(request) -> TerminalSession:
    session = request.app[SESSIONS].get(request.match_info["terminal_id"])
    if session is None:
        raise APIError(404, "terminal not found")
    return session


async def index(request):
    response = web.FileResponse(Path(__file__).parent / "static" / "index.html")
    return browser_session(request, response)


def browser_session(request, response):
    response.set_cookie(BROWSER_COOKIE, request.app[BROWSER_TOKEN], httponly=True,
                        samesite="Strict", secure=request.secure, max_age=43200)
    response.headers["Cache-Control"] = "no-store"
    return response


async def dashboard_session(request):
    return browser_session(request, web.json_response({"ok": True}))


async def health(request):
    return web.json_response({"status": "ok", "service": SERVICE, "protocol": PROTOCOL})


async def terminals(request):
    return web.json_response({"terminals": [s.describe() for s in request.app[SESSIONS].values()]})


async def terminal(request):
    return web.json_response(session_for(request).describe())


def frame_headers(frame):
    return {"Cache-Control": "no-store", "X-Frame-Sequence": str(frame.sequence),
            "X-Frame-Timestamp": str(frame.captured_at), "X-Frame-Received-At": str(frame.received_at)}


async def frame(request):
    after = None
    if "after" in request.query:
        after = int(request.query["after"])
        if after < 0:
            raise APIError(400, "after must be nonnegative")
    timeout = number(float(request.query.get("timeout", "0")), "timeout", 0, 30)
    image = await session_for(request).get_frame(after=after, timeout=timeout)
    return web.Response(body=image.jpeg, content_type="image/jpeg", headers=frame_headers(image))


async def stream(request):
    session = session_for(request)
    image = await session.get_frame(timeout=10)
    response = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store",
    })
    await response.prepare(request)
    try:
        while session.connected:
            header = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(image.jpeg)}\r\n"
                      f"X-Frame-Sequence: {image.sequence}\r\n\r\n").encode()
            async with asyncio.timeout(5):
                await response.write(header + image.jpeg + b"\r\n")
            image = await session.get_frame(after=image.sequence, timeout=20)
    except (ConnectionError, TimeoutError, APIError):
        pass
    return response


async def actions(request):
    session = session_for(request)
    action = normalize_action(await read_object(request))
    epoch = session.control_epoch
    async with session.operation_lock:
        session.check_operation(epoch)
        result = await session.command(action)
    return web.json_response(result)


async def release(request):
    # 急停不排在训练 step 的等待画面锁之后。
    session = session_for(request)
    await session.cancel_operations()
    result = await session.command(normalize_action({"duration_ms": 0}))
    return web.json_response(result)


async def step(request):
    data = await read_object(request)
    if data.keys() - {"action", "observation_timeout"}:
        raise APIError(400, "step accepts action and observation_timeout only")
    if "action" not in data:
        raise APIError(400, "step requires action")
    action = normalize_action(data["action"])
    timeout = number(data.get("observation_timeout", 5), "observation_timeout", 0.1, 30)
    session = session_for(request)
    epoch = session.control_epoch
    async with session.operation_lock:
        session.check_operation(epoch)
        result = await session.command(action)
        applied_at = result["ack"]["applied_at"]
        image = await session.get_frame(
            captured_after=applied_at + action["duration_ms"] / 1000,
            timeout=timeout,
            control_epoch=epoch,
        )
        result["observation"] = {
            "sequence": image.sequence, "captured_at": image.captured_at,
            "received_at": image.received_at, "content_type": "image/jpeg",
            "jpeg_base64": base64.b64encode(image.jpeg).decode("ascii"),
        }
    return web.json_response(result)


async def terminal_socket(request):
    config = request.app[CONFIG]
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=config.max_frame_bytes, receive_timeout=25)
    await ws.prepare(request)
    session = None
    try:
        async with asyncio.timeout(10):
            hello = await ws.receive_json()
        if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("protocol") != PROTOCOL:
            raise ValueError("expected hello with protocol=1")
        terminal_id = hello.get("terminal_id", "")
        if not isinstance(terminal_id, str) or not TERMINAL_ID.fullmatch(terminal_id):
            raise ValueError("terminal_id must contain 1..64 ASCII letters, digits, _, . or -")
        name = hello.get("name", terminal_id)
        capture = hello.get("capture", {})
        status = hello.get("status", {})
        capabilities = hello.get("capabilities", [])
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise ValueError("name must contain 1..128 characters")
        if not isinstance(capture, dict) or not isinstance(status, dict) or not isinstance(capabilities, list):
            raise ValueError("invalid terminal metadata")
        previous = request.app[SESSIONS].get(terminal_id)
        session = TerminalSession(terminal_id, name, ws, config, capture=capture,
                                  status=status, capabilities=capabilities,
                                  sequence=previous.sequence if previous else 0)
        request.app[SESSIONS][terminal_id] = session
        if previous and previous.connected:
            await previous.disconnect("terminal session replaced")
            await previous.ws.close(code=1000, message=b"session replaced")
        await ws.send_json({"type": "welcome", "protocol": PROTOCOL,
                            "session_id": session.session_id, "heartbeat_interval": 5})
        log.info("Terminal connected: %s", terminal_id)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                captured_at, jpeg = unpack_frame(message.data)
                await session.receive_frame(captured_at, jpeg)
            elif message.type == WSMsgType.TEXT:
                data = json.loads(message.data)
                if not isinstance(data, dict):
                    raise ValueError("terminal message must be an object")
                session.last_seen = time.time()
                if data.get("type") in {"heartbeat", "status"}:
                    session.last_heartbeat = time.monotonic()
                    if isinstance(data.get("status"), dict):
                        session.status = data["status"]
                    if isinstance(data.get("capture"), dict):
                        session.capture = data["capture"]
                elif data.get("type") == "ack":
                    command_id = data.get("command_id")
                    future = session.pending.get(command_id) if isinstance(command_id, str) else None
                    if future is not None and not future.done():
                        if not isinstance(data.get("ok"), bool):
                            raise ValueError("ack.ok must be boolean")
                        if data["ok"]:
                            applied_at = data.get("applied_at")
                            if (isinstance(applied_at, bool) or not isinstance(applied_at, (int, float))
                                    or not math.isfinite(applied_at) or applied_at <= 0):
                                raise ValueError("ack.applied_at must be a positive finite timestamp")
                        future.set_result(data)
            elif message.type == WSMsgType.ERROR:
                break
    except (ValueError, TypeError, TimeoutError) as exc:
        log.warning("Terminal protocol error: %s", exc)
        await ws.close(code=1008, message=str(exc).encode("utf-8")[:120])
    except ConnectionError:
        pass
    finally:
        if session:
            await session.disconnect()
            log.info("Terminal disconnected: %s", session.terminal_id)
    return ws


async def lifecycle(app):
    config = app[CONFIG]
    discovery = None

    async def watchdog():
        while True:
            await asyncio.sleep(min(5, config.heartbeat_timeout / 2))
            for session in list(app[SESSIONS].values()):
                if session.connected and time.monotonic() - session.last_heartbeat > config.heartbeat_timeout:
                    await session.disconnect("heartbeat timed out")
                    await session.ws.close(code=1001, message=b"heartbeat timeout")

    task = asyncio.create_task(watchdog())
    try:
        if config.enable_discovery:
            discovery = await start_discovery(config)
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if discovery:
            discovery.close()


async def shutdown(app):
    for session in list(app[SESSIONS].values()):
        if session.connected:
            await session.disconnect("center shutting down")
            await session.ws.close(code=1001, message=b"center shutting down")


def create_app(config: CenterConfig | None = None) -> web.Application:
    app = web.Application(middlewares=[errors, authenticate], client_max_size=64 * 1024)
    app[CONFIG] = config or CenterConfig.from_env()
    app[SESSIONS] = {}
    app[BROWSER_TOKEN] = secrets.token_urlsafe(32)
    app.add_routes([
        web.get("/", index), web.get("/health", health),
        web.get("/dashboard-session", dashboard_session),
        web.get("/api/terminals", terminals), web.get("/api/terminals/{terminal_id}", terminal),
        web.get("/api/terminals/{terminal_id}/frame", frame),
        web.get("/api/terminals/{terminal_id}/stream", stream),
        web.post("/api/terminals/{terminal_id}/actions", actions),
        web.post("/api/terminals/{terminal_id}/release", release),
        web.post("/api/terminals/{terminal_id}/step", step),
        web.get("/ws/terminal", terminal_socket),
    ])
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    return app
