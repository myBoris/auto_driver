"""图片切块、位置编码和 Transformer 组成的可训练视觉特征提取器。"""

import torch
from torch import Tensor, nn

from .config import TransformerConfig
from .encoder import TransformerEncoder
from .patch_embedding import PatchEmbedding
from .position_encoding import PositionEncoding


class ImageTransformer(nn.Module):
    """单组 [N,C,H,W] 或多组 [B,N,C,H,W]；所有输出保留批次维度。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.patch_embedding = PatchEmbedding(config)
        self.position_encoding = PositionEncoding(config)
        self.encoder = TransformerEncoder(config)

    def forward(self, images: Tensor, image_mask: Tensor | None = None) -> Tensor:
        if not isinstance(images, Tensor) or images.ndim not in {4, 5} or not images.is_floating_point():
            raise ValueError("images must be floating [N,C,H,W] or [B,N,C,H,W]")
        single = images.ndim == 4
        if single:
            images = images.unsqueeze(0)
            if isinstance(image_mask, Tensor) and image_mask.ndim == 1:
                image_mask = image_mask.unsqueeze(0)
        batch, count, channels, height, width = images.shape
        if (not batch or not 1 <= count <= self.config.max_images
                or (channels, height, width) != (self.config.in_channels, self.config.image_size, self.config.image_size)):
            raise ValueError("image batch dimensions do not match TransformerConfig")
        if image_mask is None:
            image_mask = torch.ones((batch, count), device=images.device, dtype=torch.bool)
        if (not isinstance(image_mask, Tensor) or image_mask.dtype != torch.bool
                or tuple(image_mask.shape) != (batch, count) or image_mask.device != images.device):
            raise ValueError("image_mask must be boolean [B,N] on the images device")
        if not bool(image_mask.any(dim=1).all()):
            raise ValueError("each sample must contain at least one valid image")
        images = images.masked_fill(~image_mask[:, :, None, None, None], 0)
        if not bool(torch.isfinite(images).all()):
            raise ValueError("valid image pixels must be finite")
        patches = self.patch_embedding(images.reshape(batch * count, channels, height, width))
        patch_count = patches.shape[1]
        tokens = self.position_encoding(patches.reshape(batch, count, patch_count, self.config.embed_dim))
        token_mask = image_mask.unsqueeze(-1).expand(batch, count, patch_count).reshape(batch, count * patch_count)
        encoded = self.encoder(tokens.reshape(batch, count * patch_count, self.config.embed_dim), token_mask)
        features = encoded.reshape(batch, count, patch_count, self.config.embed_dim).mean(dim=2)
        features = features.masked_fill(~image_mask.unsqueeze(-1), 0)
        if self.config.pooling == "none":
            return features
        return features.sum(dim=1) / image_mask.sum(dim=1, keepdim=True).to(features.dtype)
