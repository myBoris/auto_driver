"""可学习的图片内块位置与时间窗口内图片位置编码。"""

import torch
from torch import Tensor, nn

from .config import TransformerConfig


class PositionEncoding(nn.Module):
    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        patches = (config.image_size // config.patch_size) ** 2
        self.patch_positions = nn.Parameter(torch.empty(1, 1, patches, config.embed_dim))
        self.image_positions = nn.Parameter(torch.empty(1, config.max_images, 1, config.embed_dim))
        nn.init.trunc_normal_(self.patch_positions, std=.02)
        nn.init.trunc_normal_(self.image_positions, std=.02)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, tokens: Tensor) -> Tensor:
        """输入/输出 [B, N, P, D]，N 的顺序代表图片的先后次序。"""
        if (not isinstance(tokens, Tensor) or tokens.ndim != 4 or not tokens.shape[0]
                or not 1 <= tokens.shape[1] <= self.config.max_images
                or tuple(tokens.shape[2:]) != tuple(self.patch_positions.shape[2:])):
            raise ValueError("position tokens must be [B, N, P, D] matching the model configuration")
        return self.dropout(tokens + self.patch_positions + self.image_positions[:, :tokens.shape[1]])
