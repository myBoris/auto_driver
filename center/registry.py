"""会话兼容入口，保留已有导入；会话协调由 services/terminal_session.py 负责。"""
from .domain.errors import APIError
from .domain.models import Frame
from .services.terminal_session import TerminalSession

__all__ = ["APIError", "Frame", "TerminalSession"]
