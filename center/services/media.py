"""协调视频认证、发布地址和浏览器 WHEP 会话。

终端用配对口令发布 RTSP；浏览器只使用中心 Cookie，中心代为向 MediaMTX
提供 reader 凭据。浏览器会话用随机本地 ID 关联上游 Location，并绑定终端
连接代次，终端断开或被替换后由清理任务回收，避免旧会话继续占用资源。
"""

import asyncio
from dataclasses import dataclass
import secrets
import time
import uuid

from ..config import CenterConfig
from ..domain.errors import APIError
from ..domain.media import media_path, publisher_url, rewrite_ice_candidates, terminal_from_media_path
from ..infrastructure.media_gateway import MediaGateway


@dataclass(frozen=True)
class VideoSession:
    terminal_id: str
    terminal_session_id: str
    upstream_url: str
    created_at: float


class MediaService:
    def __init__(self, config: CenterConfig, terminals, gateway: MediaGateway):
        self.config = config
        self.terminals = terminals
        self.gateway = gateway
        self.sessions: dict[str, VideoSession] = {}

    def publisher(self, authority: str, terminal_id: str) -> dict:
        if not self.config.media_enabled:
            return {"enabled": False}
        return {
            "enabled": True, "codec": "H264",
            "publish_url": publisher_url(authority, terminal_id, self.config.token, self.config.media_rtsp_port),
        }

    def describe(self, terminal_id: str) -> dict:
        terminal = self.terminals.get(terminal_id)
        video = terminal.status.get("video", {})
        if not isinstance(video, dict):
            video = {}
        published = video.get("ready") if "ready" in video else video.get("running")
        available = bool(self.config.media_enabled and terminal.connected and published)
        if not self.config.media_enabled:
            reason = "video_disabled"
            message = "中心未启用视频服务"
        elif not terminal.connected:
            reason = "terminal_offline"
            message = "终端已离线，等待自动重连"
        elif video.get("error"):
            reason = "publisher_error"
            message = "终端视频编码或发布暂不可用"
        elif not available:
            reason = "publisher_starting"
            message = "正在等待终端发布 H.264 视频"
        else:
            reason = "ready"
            message = "视频已发布"
        return {
            "enabled": self.config.media_enabled, "available": available, "codec": "H264",
            "whep_url": f"/api/terminals/{terminal_id}/video/whep", "reason": reason, "message": message,
        }

    def authenticate(self, data: dict) -> bool:
        terminal_id = terminal_from_media_path(data.get("path"))
        user = data.get("user")
        password = data.get("password")
        action = data.get("action")
        if (not self.config.media_enabled or terminal_id is None or not isinstance(password, str)
                or not isinstance(user, str) or not isinstance(action, str)):
            return False
        if not secrets.compare_digest(password.encode(), self.config.token.encode()):
            return False
        if (user, action) not in {("publisher", "publish"), ("reader", "read")}:
            return False
        terminal = self.terminals.sessions.get(terminal_id)
        return terminal is not None and terminal.connected

    async def create(self, terminal_id: str, offer: str, hostname: str) -> tuple[str, str, str | None]:
        terminal = self.terminals.get(terminal_id)
        if not terminal.connected:
            raise APIError(409, "terminal disconnected")
        if not self.config.media_enabled:
            raise APIError(503, "video service is disabled")
        if not offer.startswith("v=0") or "m=video" not in offer or len(offer) > 64 * 1024:
            raise APIError(400, "video offer must be a valid SDP document")
        if len(self.sessions) >= 1024:
            raise APIError(503, "too many active video sessions")
        answer, upstream, etag = await self.gateway.create(media_path(terminal_id), offer)
        try:
            answer = rewrite_ice_candidates(
                answer, self.config.media_ice_host or hostname, self.config.media_ice_port,
            )
        except ValueError:
            await self.gateway.delete(upstream)
            raise APIError(502, "media service returned invalid ICE candidates") from None
        if not terminal.connected or self.terminals.sessions.get(terminal_id) is not terminal:
            await self.gateway.delete(upstream)
            raise APIError(409, "terminal connection changed during video negotiation")
        local_id = uuid.uuid4().hex
        self.sessions[local_id] = VideoSession(terminal_id, terminal.session_id, upstream, time.monotonic())
        return answer, local_id, etag

    async def delete(self, terminal_id: str, local_id: str) -> None:
        entry = self.sessions.get(local_id)
        if entry is None or entry.terminal_id != terminal_id:
            raise APIError(404, "video session not found")
        await self.gateway.delete(entry.upstream_url)
        self.sessions.pop(local_id, None)

    async def abandon(self, terminal_id: str, local_id: str) -> None:
        """协商期间浏览器已断开时，立即移除已知会话并尽力回收上游。

        浏览器没有收到 Location，无法再主动 DELETE；此处不能把本地映射
        留到正常会话超时。上游暂不可达时，由媒体服务的连接超时继续兜底。
        """
        entry = self.sessions.get(local_id)
        if entry is None or entry.terminal_id != terminal_id:
            return
        self.sessions.pop(local_id, None)
        try:
            await self.gateway.delete(entry.upstream_url)
        except APIError:
            pass

    async def cleanup(self, *, all_sessions=False) -> None:
        expired = []
        for local_id, entry in list(self.sessions.items()):
            terminal = self.terminals.sessions.get(entry.terminal_id)
            if (all_sessions or terminal is None or not terminal.connected
                    or terminal.session_id != entry.terminal_session_id
                    or time.monotonic() - entry.created_at > 24 * 60 * 60):
                expired.append((local_id, entry))
        if expired:
            await asyncio.gather(*(self.gateway.delete(entry.upstream_url) for _, entry in expired),
                                 return_exceptions=True)
            for local_id, _ in expired:
                self.sessions.pop(local_id, None)
