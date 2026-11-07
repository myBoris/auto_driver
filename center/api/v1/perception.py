"""只读道路感知结果；请求不会发送任何终端控制指令。"""

from aiohttp import web

from ...dependencies import PERCEPTION
from ..common import read_object


async def perception(request):
    result = request.app[PERCEPTION].get(request.match_info["terminal_id"])
    return web.json_response(result, headers={"Cache-Control": "no-store"})


async def publish(request):
    result = request.app[PERCEPTION].publish(request.match_info["terminal_id"], await read_object(request))
    return web.json_response(result, headers={"Cache-Control": "no-store"})
