"""集中保存图片 Transformer 配置，并在创建网络之前校验张量约定。"""

from dataclasses import dataclass
import math
from typing import Literal


@dataclass
class TransformerConfig:
    """各模块共用的配置，创建配置对象时不会创建网络层。"""

    image_size: int = 224  # 输入图片的高度和宽度，当前框架约定为正方形。
    patch_size: int = 16  # 每个图片块的边长，需能整除 image_size。
    in_channels: int = 3  # 图片通道数；prepare_images 返回 RGB 三通道。
    embed_dim: int = 256  # 每个 token 的向量长度，需能被 num_heads 整除。
    num_heads: int = 8  # 多头自注意力的头数。
    depth: int = 4  # Transformer Encoder 的层数。
    mlp_dim: int = 1024  # 前馈网络中间层的维度。
    dropout: float = 0.1  # 训练时的随机丢弃概率。
    max_images: int = 10  # 图片位置编码支持的最大图片数；预处理函数当前也限制为 10 张。
    pooling: Literal["mean", "none"] = "mean"  # mean 返回组特征；none 返回逐张图片特征。

    def __post_init__(self) -> None:
        for name in ("image_size", "patch_size", "in_channels", "embed_dim", "num_heads", "depth", "mlp_dim", "max_images"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.image_size % self.patch_size:
            raise ValueError("image_size must be divisible by patch_size")
        if self.embed_dim % self.num_heads:
            raise ValueError("embed_dim must be divisible by num_heads")
        if (isinstance(self.dropout, bool) or not isinstance(self.dropout, (int, float))
                or not 0 <= self.dropout < 1 or not math.isfinite(self.dropout)):
            raise ValueError("dropout must be finite and in [0, 1)")
        if self.pooling not in ("mean", "none"):
            raise ValueError("pooling must be mean or none")
