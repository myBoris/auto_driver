"""采集层公共接口；SDK、共享内存和进程管理各自独立于网络与手柄控制。"""

from .base import CapturedFrame, LatestCapture, MAX_FRAME_BYTES
from .shared import _SharedFrameSlot
from .supervisor import MockCapture, WindowCapture

__all__ = ["CapturedFrame", "LatestCapture", "MAX_FRAME_BYTES", "MockCapture", "WindowCapture", "_SharedFrameSlot"]
