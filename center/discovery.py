"""UDP 发现兼容入口；网络实现位于 infrastructure/discovery.py。"""
from .infrastructure.discovery import DiscoveryProtocol, start_discovery

__all__ = ["DiscoveryProtocol", "start_discovery"]
