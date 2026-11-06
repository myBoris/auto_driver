"""图像观察模型，显式区分终端采集时间与中心接收时间。"""

from dataclasses import dataclass

@dataclass(frozen=True)
class Frame:
    sequence: int
    captured_at: float
    received_at: float
    jpeg: bytes
