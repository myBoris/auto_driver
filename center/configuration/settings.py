"""中心配置；不依赖游戏电脑上的图形和手柄驱动。"""

from dataclasses import dataclass
import math
import os
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CenterConfig:
    token: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    discovery_port: int = 9999
    advertise_url: str = ""
    enable_discovery: bool = True
    command_timeout: float = 3.0
    heartbeat_timeout: float = 20.0
    max_frame_age: float = 3.0
    max_frame_bytes: int = 8 * 1024 * 1024
    media_enabled: bool = True
    media_http_url: str = "http://127.0.0.1:8889"
    media_rtsp_port: int = 8554
    media_ice_port: int = 8189
    media_ice_host: str = ""

    def __post_init__(self) -> None:
        if not self.token.strip() or "\n" in self.token or "\r" in self.token:
            raise ValueError("设置 CENTER_TOKEN 或 --token，中心与终端使用相同口令")
        for name in ("port", "discovery_port", "media_rtsp_port", "media_ice_port"):
            if not 1 <= getattr(self, name) <= 65535:
                raise ValueError(f"{name} must be in 1..65535")
        for name in ("command_timeout", "heartbeat_timeout", "max_frame_age"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.advertise_url and not self.advertise_url.startswith(("http://", "https://")):
            raise ValueError("advertise_url must start with http:// or https://")
        media = urlsplit(self.media_http_url)
        if (media.scheme not in {"http", "https"} or not media.hostname or media.username
                or media.password or media.query or media.fragment or media.path not in {"", "/"}):
            raise ValueError("media_http_url must be an HTTP origin without credentials or path")
        try:
            media.port
        except ValueError as exc:
            raise ValueError("media_http_url has an invalid port") from exc
        if self.media_ice_host and any(char.isspace() or char in "/?#@" for char in self.media_ice_host):
            raise ValueError("media_ice_host must contain a hostname or IP address only")

    @classmethod
    def from_env(cls, **overrides) -> "CenterConfig":
        """从进程环境读取配置，再应用非空 CLI 覆盖；.env 由启动入口加载。

        媒体 HTTP 地址是服务器内部地址，不从浏览器请求生成；ICE 主机则默认
        按每次浏览器访问地址动态转换，避免多网卡部署依赖固定通告 IP。
        """
        values = {
            "token": os.environ.get("CENTER_TOKEN", ""),
            "host": os.environ.get("CENTER_HOST", "0.0.0.0"),
            "port": int(os.environ.get("CENTER_PORT", "8000")),
            "discovery_port": int(os.environ.get("DISCOVERY_PORT", "9999")),
            "advertise_url": os.environ.get("CENTER_ADVERTISE_URL", ""),
            "enable_discovery": os.environ.get("CENTER_DISCOVERY", "1").lower() not in {"0", "false", "no"},
            "media_enabled": os.environ.get("CENTER_MEDIA_ENABLED", "1").lower() not in {"0", "false", "no"},
            "media_http_url": os.environ.get("CENTER_MEDIA_HTTP_URL", "http://127.0.0.1:8889"),
            "media_rtsp_port": int(os.environ.get("CENTER_MEDIA_RTSP_PORT", "8554")),
            "media_ice_port": int(os.environ.get("CENTER_MEDIA_ICE_PORT", "8189")),
            "media_ice_host": os.environ.get("CENTER_MEDIA_ICE_HOST", ""),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)
