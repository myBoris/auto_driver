"""管理 UDP、媒体连接和后台任务的启动及回收。

媒体清理与心跳看门狗分开运行，媒体请求超时不会阻塞连接心跳检查。
中心停止时先唤醒控制和观察等待者，再关闭终端连接与上游媒体资源。
"""

import asyncio
import time
from ..dependencies import CONFIG, MEDIA, SESSIONS
from .discovery import start_discovery


async def lifecycle(app):
    config = app[CONFIG]
    media = app[MEDIA]
    discovery = None

    async def watchdog():
        while True:
            await asyncio.sleep(min(5, config.heartbeat_timeout / 2))
            for terminal in list(app[SESSIONS].values()):
                if terminal.connected and time.monotonic() - terminal.last_heartbeat > config.heartbeat_timeout:
                    await terminal.disconnect("heartbeat timed out")
                    await terminal.ws.close(code=1001, message=b"heartbeat timeout")

    async def media_cleanup():
        while True:
            await asyncio.sleep(10)
            await media.cleanup()

    await media.gateway.start()
    tasks = [asyncio.create_task(watchdog()), asyncio.create_task(media_cleanup())]
    try:
        if config.enable_discovery:
            discovery = await start_discovery(config)
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if discovery:
            discovery.close()
        await media.cleanup(all_sessions=True)
        await media.gateway.close()


async def shutdown(app):
    for terminal in list(app[SESSIONS].values()):
        if terminal.connected:
            await terminal.disconnect("center shutting down")
            await terminal.ws.close(code=1001, message=b"center shutting down")
