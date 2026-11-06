"""集中保存图片 Transformer 的配置；参数校验留待实现。"""

from dataclasses import dataclass
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

    # TODO: 按需要添加配置校验，例如尺寸整除、维度整除和 dropout 范围。
