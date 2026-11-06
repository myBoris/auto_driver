"""独立 AI 配置：默认读取工程自身 .env，进程环境覆盖文件值。

不修改全局环境，便于测试和同一进程加载不同配置。仅解析数据和范围，
不初始化显卡、模型或网络；缺少口令可以查看脚手架，连接时客户端再明确拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from ..domain.types import finite_number

AI_ROOT = Path(__file__).resolve().parents[2]


def validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("CENTER_URL must be an HTTP or HTTPS URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("CENTER_URL contains an invalid host or port") from exc
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(char.isspace() for char in value) or port == 0):
        raise ValueError("CENTER_URL must be http(s)://host[:port] without credentials, path, query, or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    """动作持续时间为毫秒，观察等待时间为秒，data_dir 相对 AI 工程根目录解析。"""

    base_url: str = "http://127.0.0.1:8000"
    token: str = field(default="", repr=False)
    terminal_id: str | None = None
    observation_timeout: float = 5.0
    action_duration_ms: int = 100
    data_dir: Path = Path("data")

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", validate_base_url(self.base_url))
        if not isinstance(self.token, str) or "\r" in self.token or "\n" in self.token:
            raise ValueError("CENTER_TOKEN must be a single-line string")
        if self.terminal_id is not None:
            if not isinstance(self.terminal_id, str) or not self.terminal_id.strip():
                raise ValueError("AI_TERMINAL_ID must be a non-empty terminal identifier")
            if len(self.terminal_id) > 64:
                raise ValueError("AI_TERMINAL_ID must be at most 64 characters")
        object.__setattr__(self, "observation_timeout", finite_number(self.observation_timeout, "observation_timeout", 0.1, 30))
        if isinstance(self.action_duration_ms, bool) or not isinstance(self.action_duration_ms, int) or not 0 <= self.action_duration_ms <= 5000:
            raise ValueError("AI_ACTION_DURATION_MS must be an integer between 0 and 5000")
        data_dir = Path(self.data_dir).expanduser()
        object.__setattr__(self, "data_dir", data_dir if data_dir.is_absolute() else AI_ROOT / data_dir)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """只在显式调用时读取 .env；路径按模块位置定位，不依赖终端当前目录。"""
        values = {key: value for key, value in dotenv_values(env_file if env_file is not None else AI_ROOT / ".env").items()
                  if value is not None}
        values.update(os.environ)
        try:
            timeout = float(values.get("AI_OBSERVATION_TIMEOUT", "5"))
            duration = int(values.get("AI_ACTION_DURATION_MS", "100"))
        except (TypeError, ValueError) as exc:
            raise ValueError("AI_OBSERVATION_TIMEOUT must be numeric and AI_ACTION_DURATION_MS must be an integer") from exc
        return cls(base_url=values.get("CENTER_URL", "http://127.0.0.1:8000"),
                   token=values.get("CENTER_TOKEN", ""), terminal_id=values.get("AI_TERMINAL_ID") or None,
                   observation_timeout=timeout, action_duration_ms=duration,
                   data_dir=Path(values.get("AI_DATA_DIR", "data")))
