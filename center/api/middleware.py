"""统一错误响应和认证边界。

终端 WebSocket 与训练脚本只认 Bearer 配对口令；浏览器 API 可使用中心发放
的 HttpOnly Cookie。Cookie 控制请求校验 Origin，WHEP SDP POST 也经过此处，
不会因请求体不是 JSON 而绕过认证。内部媒体回调由媒体服务自行核验凭据。
"""

import secrets
from urllib.parse import urlsplit

from aiohttp import web

from ..dependencies import BROWSER_COOKIE, BROWSER_TOKEN, CONFIG
from ..domain.errors import APIError


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except APIError as exc:
        return web.json_response({"error": exc.message}, status=exc.status)
    except (ValueError, TypeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException as exc:
        if request.path.startswith(("/api/", "/ws/", "/internal/")):
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
