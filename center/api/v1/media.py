"""视频 HTTP 适配器：SDP 经过同源认证，凭据只由中心代理持有。

浏览器先收集完整 ICE 再提交 SDP，无需 PATCH。返回本地 Location，播放器
退出时 DELETE 本地会话，业务服务负责回收对应 MediaMTX 会话。
"""

from aiohttp import web
from ...dependencies import MEDIA
from ...domain.errors import APIError
from ...domain.media import request_hostname
from ..common import read_object


async def video(request):
    return web.json_response(request.app[MEDIA].describe(request.match_info["terminal_id"]))


async def whep(request):
    if request.content_type != "application/sdp":
        raise APIError(415, "video negotiation requires Content-Type: application/sdp")
    answer, local_id, etag = await request.app[MEDIA].create(
        request.match_info["terminal_id"], await request.text(), request_hostname(request.host))
    if request.transport is None or request.transport.is_closing():
        await request.app[MEDIA].abandon(request.match_info["terminal_id"], local_id)
        raise APIError(409, "video negotiation cancelled because the browser disconnected")
    location = f"/api/terminals/{request.match_info['terminal_id']}/video/sessions/{local_id}"
    headers = {"Location": location, "Cache-Control": "no-store"}
    if etag:
        headers["ETag"] = etag
    return web.Response(status=201, text=answer, content_type="application/sdp", headers=headers)


async def delete_session(request):
    await request.app[MEDIA].delete(request.match_info["terminal_id"], request.match_info["session_id"])
    return web.Response(status=204)


async def authenticate_media(request):
    accepted = request.app[MEDIA].authenticate(await read_object(request))
    return web.Response(status=200 if accepted else 401)
