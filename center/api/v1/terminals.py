"""终端 HTTP 适配器：解析参数后委托业务服务，保留现有 AI API 地址。"""

import asyncio
import time

from aiohttp import web

from ...dependencies import CONFIG, TERMINALS
from ...domain.errors import APIError
from ...domain.protocol import number
from ..common import frame_headers, read_object


def session_for(request):
    return request.app[TERMINALS].get(request.match_info["terminal_id"])


async def terminals(request):
    return web.json_response({"terminals": request.app[TERMINALS].list()})


async def terminal(request):
    return web.json_response(session_for(request).describe())


async def control(request):
    after = None
    if "after" in request.query:
        after = int(request.query["after"])
        if after < 0:
            raise APIError(400, "after must be nonnegative")
    timeout = number(float(request.query.get("timeout", "0")), "timeout", 0, 20)
    value = await session_for(request).get_control(
        after=after, timeout=timeout, session_id=request.query.get("session_id"),
    )
    return web.json_response(value, headers={"Cache-Control": "no-store"})


async def telemetry(request):
    after = None
    if "after" in request.query:
        after = int(request.query["after"])
        if after < 0:
            raise APIError(400, "after must be nonnegative")
    timeout = number(float(request.query.get("timeout", "0")), "timeout", 0, 20)
    value = await session_for(request).get_telemetry(
        after=after, timeout=timeout, session_id=request.query.get("session_id"),
    )
    return web.json_response(value, headers={"Cache-Control": "no-store"})


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
    """保留 MJPEG 诊断接口；浏览器主播放器使用独立的 H.264/WebRTC 通道。

    每次写入只使用最新帧。慢客户端写入有超时，超时直接中断连接，避免
    HTTP 收尾继续排空旧帧；短等待也让停帧时的客户端断开尽快被观察到。
    """
    session = session_for(request)
    image = await session.get_frame(timeout=10)
    response = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-store",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    last_sequence = 0
    while session.connected:
        if request.transport is None or request.transport.is_closing():
            break
        try:
            if image.sequence <= last_sequence:
                image = await session.get_frame(after=last_sequence, timeout=1)
            latest = await session.get_frame()
            if latest.sequence > image.sequence:
                image = latest
            header = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(image.jpeg)}\r\n"
                      f"X-Frame-Sequence: {image.sequence}\r\n"
                      f"X-Frame-Timestamp: {image.captured_at}\r\n\r\n").encode()
            async with asyncio.timeout(1):
                await response.write(header + image.jpeg + b"\r\n")
            last_sequence = image.sequence
        except TimeoutError:
            if request.transport:
                request.transport.abort()
            break
        except ConnectionError:
            break
        except APIError as exc:
            if exc.status == 504 and session.connected and session.frame is not None:
                age = time.time() - session.frame.received_at
                if age < request.app[CONFIG].max_frame_age:
                    continue
            break
    return response


async def actions(request):
    result = await request.app[TERMINALS].action(request.match_info["terminal_id"], await read_object(request),
                                               session_id=request.query.get("session_id"))
    return web.json_response(result)


async def release(request):
    result = await request.app[TERMINALS].release(request.match_info["terminal_id"],
                                                session_id=request.query.get("session_id"))
    return web.json_response(result)


async def step(request):
    result = await request.app[TERMINALS].step(request.match_info["terminal_id"], await read_object(request),
                                             session_id=request.query.get("session_id"))
    return web.json_response(result)
