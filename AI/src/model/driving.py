"""共享视觉特征，同时预测车道边界、方向类别和完整驾驶操作。"""

from dataclasses import asdict, dataclass, field
import math

import torch
from torch import Tensor, nn

from src.domain.types import BUTTONS

from .config import TransformerConfig
from .model import ImageTransformer


def _default_encoder() -> TransformerConfig:
    return TransformerConfig(image_size=128, patch_size=16, embed_dim=128,
                             num_heads=4, depth=3, mlp_dim=256, max_images=1)


@dataclass(frozen=True)
class DrivingModelConfig:
    """可完整存入检查点的结构配置；lane_y 从画面近处到远处排列。"""

    encoder: TransformerConfig = field(default_factory=_default_encoder)
    lane_y: tuple[float, ...] = (.95, .85, .75, .65, .55, .45)
    button_names: tuple[str, ...] = tuple(sorted(BUTTONS))

    def __post_init__(self) -> None:
        if not isinstance(self.encoder, TransformerConfig):
            raise ValueError("encoder must be a TransformerConfig")
        if self.encoder.pooling != "mean" or self.encoder.in_channels != 3:
            raise ValueError("driving encoder requires RGB input and mean pooling")
        if (not isinstance(self.lane_y, (tuple, list)) or not 2 <= len(self.lane_y) <= 64
                or any(isinstance(y, bool) or not isinstance(y, (int, float))
                       or not 0 <= y <= 1 or not math.isfinite(y) for y in self.lane_y)):
            raise ValueError("lane_y must contain 2..64 finite normalized image coordinates")
        lane_y = tuple(float(y) for y in self.lane_y)
        if any(near <= far for near, far in zip(lane_y, lane_y[1:])):
            raise ValueError("lane_y must strictly decrease from near to far")
        if (not isinstance(self.button_names, (tuple, list)) or not self.button_names
                or any(not isinstance(name, str) or name not in BUTTONS for name in self.button_names)
                or len(set(self.button_names)) != len(self.button_names)):
            raise ValueError("button_names must contain unique supported Xbox button names")
        object.__setattr__(self, "lane_y", lane_y)
        object.__setattr__(self, "button_names", tuple(self.button_names))

    def to_dict(self) -> dict:
        return {"encoder": asdict(self.encoder), "lane_y": list(self.lane_y),
                "button_names": list(self.button_names)}

    @classmethod
    def from_dict(cls, value: dict) -> "DrivingModelConfig":
        if not isinstance(value, dict) or value.keys() - {"encoder", "lane_y", "button_names"}:
            raise ValueError("invalid driving model configuration")
        options = dict(value)
        if "encoder" in options:
            if not isinstance(options["encoder"], dict):
                raise ValueError("encoder configuration must be an object")
            try:
                options["encoder"] = TransformerConfig(**options["encoder"])
            except TypeError as exc:
                raise ValueError("invalid encoder configuration fields") from exc
        return cls(**options)


class DrivingModel(nn.Module):
    """一个共享 Transformer 加五个任务头；不在网络内执行任何外部驾驶。"""

    def __init__(self, config: DrivingModelConfig | None = None) -> None:
        super().__init__()
        self.config = config if config is not None else DrivingModelConfig()
        if not isinstance(self.config, DrivingModelConfig):
            raise ValueError("config must be a DrivingModelConfig")
        self.backbone = ImageTransformer(self.config.encoder)
        dim = self.config.encoder.embed_dim
        self.control_head = nn.Linear(dim, 3)
        self.button_head = nn.Linear(dim, len(self.config.button_names))
        self.lane_head = nn.Linear(dim, len(self.config.lane_y) * 2)
        self.lane_presence_head = nn.Linear(dim, 1)
        self.direction_head = nn.Linear(dim, 3)

    def forward(self, images: Tensor, image_mask: Tensor | None = None) -> dict[str, Tensor]:
        features = self.backbone(images, image_mask)
        raw_controls = self.control_head(features)
        controls = torch.cat((raw_controls[:, :1].tanh(), raw_controls[:, 1:].sigmoid()), dim=-1)
        raw_lane = self.lane_head(features).reshape(features.shape[0], len(self.config.lane_y), 2)
        # 最小间隔避免两个边界交换或重合；坐标始终处于完整图像的归一化范围。
        min_width = .001
        left = raw_lane[:, :, 0].sigmoid() * (1 - min_width)
        right = left + min_width + (1 - left - min_width) * raw_lane[:, :, 1].sigmoid()
        return {
            "controls": controls,
            "button_logits": self.button_head(features),
            "lane_points": torch.stack((left, right), dim=-1),
            "lane_presence_logit": self.lane_presence_head(features).squeeze(-1),
            "direction_logits": self.direction_head(features),
        }
