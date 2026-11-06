"""终端 WebSocket 适配器；消息含义和连接替换交给服务层。

发布地址沿用终端连接中心时的 Host，避免向终端返回 Docker 内部地址。
该消息只经 Bearer 认证后的连接发送，不出现在浏览器 API 中。
"""

import asyncio
import json
import logging
from aiohttp import WSMsgType, web
from ..dependencies import CONFIG, MEDIA, TERMINALS
from ..domain.protocol import PROTOCOL

log = logging.getLogger(__name__)


async def terminal_socket(request):
    config = request.app[CONFIG]
    terminals = request.app[TERMINALS]
    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=config.max_frame_bytes, receive_timeout=25)
    await ws.prepare(request)
    terminal = None
    try:
        async with asyncio.timeout(10):
            hello = await ws.receive_json()
        terminal = await terminals.register(hello, ws)
        await ws.send_json({"type": "welcome", "protocol": PROTOCOL, "session_id": terminal.session_id,
                            "heartbeat_interval": 5,
                            "video": request.app[MEDIA].publisher(request.host, terminal.terminal_id)})
        log.info("Terminal connected: %s", terminal.terminal_id)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await terminals.receive_frame(terminal, message.data)
            elif message.type == WSMsgType.TEXT:
                terminals.receive_message(terminal, json.loads(message.data))
            elif message.type == WSMsgType.ERROR:
                break
    except (ValueError, TypeError, TimeoutError) as exc:
        log.warning("Terminal protocol error: %s", exc)
        await ws.close(code=1008, message=str(exc).encode("utf-8")[:120])
    except ConnectionError:
        pass
    finally:
        if terminal:
            await terminal.disconnect()
            log.info("Terminal disconnected: %s", terminal.terminal_id)
    return ws
