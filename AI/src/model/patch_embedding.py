"""用不重叠卷积实现图片切块与每块像素的可学习线性映射。"""

from torch import Tensor, nn

from .config import TransformerConfig


class PatchEmbedding(nn.Module):
    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.projection = nn.Conv2d(config.in_channels, config.embed_dim,
                                    kernel_size=config.patch_size, stride=config.patch_size)

    def forward(self, images: Tensor) -> Tensor:
        """将 [M, C, H, W] 转换为按行排列的 [M, P, D]。"""
        if (not isinstance(images, Tensor) or images.ndim != 4 or not images.shape[0]
                or not images.is_floating_point()
                or tuple(images.shape[1:]) != (self.config.in_channels, self.config.image_size, self.config.image_size)):
            raise ValueError("images must be a nonempty floating tensor [M, C, image_size, image_size]")
        return self.projection(images).flatten(2).transpose(1, 2)
