"""独立终端的薄入口；参数解析与运行编排由对应功能包实现。"""

from .config import build_parser
from .services.runtime import main

__all__ = ["build_parser", "main"]

if __name__ == "__main__":
    main()
