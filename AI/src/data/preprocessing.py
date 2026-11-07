"""训练和推理共用 RGB 缩放；不裁剪画面，保持归一化车道坐标的含义。"""

from collections.abc import Sequence
from io import BytesIO
from pathlib import Path

from src.domain import Observation


class ObservationPreprocessor:
    """构造和导入保持轻量；第一次处理图片时才加载 Pillow、NumPy 和 torch。"""

    def __init__(self, image_size: int = 128, history_frames: int = 1) -> None:
        for name, value, maximum in (("image_size", image_size, 4096), ("history_frames", history_frames, 10)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} 必须是 1–{maximum} 的整数")
        self.image_size = image_size
        self.history_frames = history_frames

    def to_dict(self) -> dict:
        return {"image_size": self.image_size, "history_frames": self.history_frames}

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or value.keys() - {"image_size", "history_frames"}:
            raise ValueError("预处理配置字段无效")
        return cls(**value)

    def _decode(self, sources):
        import numpy as np
        from PIL import Image
        import torch

        tensors = []
        for source in sources:
            try:
                with Image.open(source) as image:
                    # 游戏截图的坐标以原始像素为准，不做 EXIF 自动旋转或裁剪。
                    rgb = image.convert("RGB").resize((self.image_size, self.image_size), Image.Resampling.BILINEAR)
                    pixels = np.array(rgb, dtype=np.float32, copy=True) / 255.0
            except (OSError, ValueError) as exc:
                raise ValueError("无法解码训练观察图片") from exc
            tensors.append(torch.from_numpy(pixels).permute(2, 0, 1).contiguous())
        return torch.stack(tensors)

    def _check_count(self, sequence):
        if isinstance(sequence, (str, bytes, Path)) or not isinstance(sequence, Sequence):
            raise TypeError("图片窗口必须是序列")
        if not 1 <= len(sequence) <= self.history_frames:
            raise ValueError("图片窗口不能为空，且不能超过 history_frames")

    def transform(self, observations: Sequence[Observation]):
        """输出 [N, 3, H, W] float32；历史补齐和掩码由调用方负责。"""
        self._check_count(observations)
        for index, observation in enumerate(observations):
            if not isinstance(observation, Observation):
                raise TypeError("窗口必须包含 Observation")
            if index and (observation.sequence <= observations[index - 1].sequence
                          or observation.captured_at < observations[index - 1].captured_at):
                raise ValueError("观察窗口必须按帧号和采集时间递增")
        return self._decode([BytesIO(observation.jpeg) for observation in observations])

    def transform_paths(self, paths: Sequence[Path]):
        """离线图片与在线 JPEG 使用同一像素变换。"""
        self._check_count(paths)
        return self._decode([Path(path) for path in paths])
