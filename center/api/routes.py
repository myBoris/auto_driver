"""集中注册地址，版本目录拆分不改变现有训练 API 的 URL。"""

from aiohttp import web
from . import web as pages
from .terminal_socket import terminal_socket
from .v1 import media, perception, terminals


def register_routes(app):
    app.add_routes([
        web.get("/", pages.index), web.get("/health", pages.health),
        web.get("/dashboard-session", pages.dashboard_session),
        web.get("/api/terminals", terminals.terminals),
        web.get("/api/terminals/{terminal_id}", terminals.terminal),
        web.get("/api/terminals/{terminal_id}/control", terminals.control),
        web.get("/api/terminals/{terminal_id}/telemetry", terminals.telemetry),
        web.get("/api/terminals/{terminal_id}/frame", terminals.frame),
        web.get("/api/terminals/{terminal_id}/stream", terminals.stream),
        web.get("/api/terminals/{terminal_id}/perception", perception.perception),
        web.post("/api/terminals/{terminal_id}/perception", perception.publish),
        web.post("/api/terminals/{terminal_id}/actions", terminals.actions),
        web.post("/api/terminals/{terminal_id}/release", terminals.release),
        web.post("/api/terminals/{terminal_id}/step", terminals.step),
        web.get("/api/terminals/{terminal_id}/video", media.video),
        web.post("/api/terminals/{terminal_id}/video/whep", media.whep),
        web.delete("/api/terminals/{terminal_id}/video/sessions/{session_id}", media.delete_session),
        web.post("/internal/media/auth", media.authenticate_media),
        web.get("/ws/terminal", terminal_socket),
        web.static("/static/", pages.STATIC, show_index=False, follow_symlinks=False),
    ])
