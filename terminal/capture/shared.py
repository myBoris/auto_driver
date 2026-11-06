"""父子进程有界通道：单张 JPEG 共享内存和可覆盖的状态消息。"""

import queue
import time

from .base import MAX_FRAME_BYTES

class _SharedFrameSlot:
    """共享内存只容纳最新 JPEG；慢消费者读取时直接跳过旧序号，避免延迟累积。"""

    def __init__(self, context) -> None:
        self.buffer = context.RawArray("B", MAX_FRAME_BYTES)
        # 元数据依次为：序号、JPEG 字节数、原始采集时间、宽、高。
        self.metadata = context.RawArray("d", 5)
        self.lock = context.Lock()

    def publish(self, jpeg: bytes, captured_at: float, width: int, height: int) -> None:
        if len(jpeg) + 12 > MAX_FRAME_BYTES:
            raise ValueError("JPEG exceeds 8 MiB; lower --width or --jpeg-quality")
        with self.lock:
            memoryview(self.buffer).cast("B")[:len(jpeg)] = jpeg
            self.metadata[1:] = (len(jpeg), captured_at, width, height)
            self.metadata[0] += 1

    def read_after(self, sequence: int):
        # SDK 崩溃可能遗留未释放锁；父进程限时读取，随后可销毁整个子进程重建。
        if not self.lock.acquire(timeout=0.02):
            return None
        try:
            current = int(self.metadata[0])
            if current <= sequence:
                return None
            length = int(self.metadata[1])
            return (current, memoryview(self.buffer).cast("B")[:length].tobytes(),
                    self.metadata[2], int(self.metadata[3]), int(self.metadata[4]))
        finally:
            self.lock.release()


def _send_capture_status(channel, **message) -> None:
    message["timestamp"] = time.time()
    try:
        channel.put_nowait(message)
    except queue.Full:
        try:
            channel.get_nowait()
        except queue.Empty:
            pass
        try:
            channel.put_nowait(message)
        except queue.Full:
            pass
