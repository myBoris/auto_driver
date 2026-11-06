"""旧网络服务导入兼容入口；实际连接逻辑位于 network 包。"""

from .config.connection import TerminalConfig
from .network.client import TerminalClient

__all__ = ["TerminalClient", "TerminalConfig"]
