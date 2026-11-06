"""提供监看页面、静态资源、健康检查及自动浏览器会话。"""

from pathlib import Path

from aiohttp import web

from ..dependencies import BROWSER_COOKIE, BROWSER_TOKEN
from ..domain.protocol import PROTOCOL, SERVICE

STATIC = Path(__file__).resolve().parents[1] / "static"


def browser_session(request, response):
    response.set_cookie(BROWSER_COOKIE, request.app[BROWSER_TOKEN], httponly=True,
                        samesite="Strict", secure=request.secure, max_age=43200)
    response.headers["Cache-Control"] = "no-store"
    return response


async def index(request):
    return browser_session(request, web.FileResponse(STATIC / "index.html"))


async def dashboard_session(request):
    return browser_session(request, web.json_response({"ok": True}))


async def health(request):
    return web.json_response({"status": "ok", "service": SERVICE, "protocol": PROTOCOL})
