"""中心应用装配入口，不在此处处理控制、训练或媒体协商业务。

请求流程为 API 适配器 → 业务服务 → 领域规则和网络基础设施。保留原有
AppKey 导出，便于已有测试和接入代码迁移，实例状态由每个应用独立持有。
"""

import secrets
from aiohttp import web
from .api.middleware import authenticate, errors
from .api.routes import register_routes
from .config import CenterConfig
from .dependencies import BROWSER_COOKIE, BROWSER_TOKEN, CONFIG, MEDIA, PERCEPTION, SESSIONS, TERMINALS
from .infrastructure.media_gateway import MediaGateway
from .infrastructure.runtime import lifecycle, shutdown
from .services.media import MediaService
from .services.perception import PerceptionService
from .services.terminals import TerminalService


def create_app(config: CenterConfig | None = None) -> web.Application:
    app = web.Application(middlewares=[errors, authenticate], client_max_size=64 * 1024)
    app[CONFIG] = config or CenterConfig.from_env()
    app[SESSIONS] = {}
    app[BROWSER_TOKEN] = secrets.token_urlsafe(32)
    app[TERMINALS] = TerminalService(app[CONFIG], app[SESSIONS])
    app[MEDIA] = MediaService(app[CONFIG], app[TERMINALS], MediaGateway(app[CONFIG]))
    app[PERCEPTION] = PerceptionService(app[TERMINALS])
    register_routes(app)
    app.cleanup_ctx.append(lifecycle)
    app.on_shutdown.append(shutdown)
    return app
