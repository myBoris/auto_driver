"""多头自注意力；无效 token 在投影前清零，避免补齐像素污染有效特征。"""

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from .config import TransformerConfig


def checked_tokens(tokens: Tensor, mask: Tensor | None, embed_dim: int) -> tuple[Tensor, Tensor | None]:
    """统一验证 Encoder 输入；True 有效，每组至少保留一个 token。"""
    if (not isinstance(tokens, Tensor) or tokens.ndim != 3 or tokens.shape[-1] != embed_dim
            or not tokens.is_floating_point() or not tokens.shape[0] or not tokens.shape[1]):
        raise ValueError("tokens must be a nonempty floating tensor [B, L, embed_dim]")
    if mask is not None:
        if (not isinstance(mask, Tensor) or mask.dtype != torch.bool
                or mask.shape != tokens.shape[:2] or mask.device != tokens.device):
            raise ValueError("token_mask must be boolean [B, L] on the tokens device")
        if not bool(mask.any(dim=1).all()):
            raise ValueError("each sample must contain at least one valid token")
        tokens = tokens.masked_fill(~mask.unsqueeze(-1), 0)
    if not bool(torch.isfinite(tokens).all()):
        raise ValueError("valid tokens must be finite")
    return tokens, mask


class MultiHeadSelfAttention(nn.Module):
    """显式 Q/K/V 投影，使用 PyTorch 的缩放点积内核计算各头注意力。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.qkv = nn.Linear(config.embed_dim, config.embed_dim * 3)
        self.projection = nn.Linear(config.embed_dim, config.embed_dim)
        self.dropout = nn.Dropout(config.dropout)

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        """输入/输出 [B, L, D]，token_mask [B, L] 的 True 表示有效位置。"""
        tokens, token_mask = checked_tokens(tokens, token_mask, self.config.embed_dim)
        batch, length, dim = tokens.shape
        heads = self.config.num_heads
        qkv = self.qkv(tokens).reshape(batch, length, 3, heads, dim // heads)
        query, key, value = qkv.permute(2, 0, 3, 1, 4).unbind(0)
        # SDPA 的布尔掩码 True 表示允许读取，与本工程的有效位置约定一致。
        allowed = token_mask[:, None, None, :] if token_mask is not None else None
        attended = F.scaled_dot_product_attention(
            query, key, value, attn_mask=allowed,
            dropout_p=self.config.dropout if self.training else 0.0,
        )
        merged = attended.transpose(1, 2).reshape(batch, length, dim)
        result = self.dropout(self.projection(merged))
        return result.masked_fill(~token_mask.unsqueeze(-1), 0) if token_mask is not None else result
