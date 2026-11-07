"""HTTP 适配器共享的类型化依赖键，避免路由之间相互导入造成循环依赖。"""

from aiohttp import web

from .config import CenterConfig

CONFIG = web.AppKey("config", CenterConfig)
SESSIONS = web.AppKey("sessions", dict)
BROWSER_TOKEN = web.AppKey("browser_token", str)
BROWSER_COOKIE = "auto_driver_dashboard"
TERMINALS = web.AppKey("terminal_service", object)
MEDIA = web.AppKey("media_service", object)
PERCEPTION = web.AppKey("perception_service", object)
