"""中心配置；不依赖游戏电脑上的图形和手柄驱动。"""

from dataclasses import dataclass
import math
import os


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

    def __post_init__(self) -> None:
        if not self.token.strip() or "\n" in self.token or "\r" in self.token:
            raise ValueError("设置 CENTER_TOKEN 或 --token，中心与终端使用相同口令")
        for name in ("port", "discovery_port"):
            if not 1 <= getattr(self, name) <= 65535:
                raise ValueError(f"{name} must be in 1..65535")
        for name in ("command_timeout", "heartbeat_timeout", "max_frame_age"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.advertise_url and not self.advertise_url.startswith(("http://", "https://")):
            raise ValueError("advertise_url must start with http:// or https://")

    @classmethod
    def from_env(cls, **overrides) -> "CenterConfig":
        values = {
            "token": os.environ.get("CENTER_TOKEN", ""),
            "host": os.environ.get("CENTER_HOST", "0.0.0.0"),
            "port": int(os.environ.get("CENTER_PORT", "8000")),
            "discovery_port": int(os.environ.get("DISCOVERY_PORT", "9999")),
            "advertise_url": os.environ.get("CENTER_ADVERTISE_URL", ""),
            "enable_discovery": os.environ.get("CENTER_DISCOVERY", "1").lower() not in {"0", "false", "no"},
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)
