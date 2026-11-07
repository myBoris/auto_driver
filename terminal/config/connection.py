"""中心连接配置；口令排除在 repr 之外，避免诊断日志意外输出凭据。"""

from dataclasses import dataclass, field
from pathlib import Path

@dataclass(frozen=True)
class TerminalConfig:
    token: str = field(repr=False)
    terminal_id: str
    name: str
    center: str | None = None
    discovery_port: int = 9999
    discovery_address: str = "255.255.255.255"
    stop_file: Path | None = None
    mock: bool = False
    telemetry_enabled: bool = True
    telemetry_host: str = "127.0.0.1"
    telemetry_port: int = 5300
