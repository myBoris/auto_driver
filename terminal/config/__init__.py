"""独立终端配置公共接口：配置对象、参数解析和输入校验。"""

from .connection import TerminalConfig
from .settings import build_parser, parse_args

__all__ = ["TerminalConfig", "build_parser", "parse_args"]
