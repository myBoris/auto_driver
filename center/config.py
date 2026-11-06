"""配置兼容入口；实际环境变量解析和约束位于 configuration/settings.py。"""
from .configuration.settings import CenterConfig

__all__ = ["CenterConfig"]
