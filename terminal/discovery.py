"""旧局域网发现导入兼容入口；实际协议逻辑位于 network.discovery。"""

from .network.discovery import DiscoveryProtocol, SERVICE, discover_center, validate_center_url

__all__ = ["DiscoveryProtocol", "SERVICE", "discover_center", "validate_center_url"]
