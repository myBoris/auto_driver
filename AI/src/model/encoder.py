"""Pre-LayerNorm Transformer Encoder 与多层堆叠。"""

from torch import Tensor, nn

from .attention import MultiHeadSelfAttention, checked_tokens
from .config import TransformerConfig
from .feed_forward import FeedForward


class TransformerEncoderLayer(nn.Module):
    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.attention = MultiHeadSelfAttention(config)
        self.feed_forward = FeedForward(config)
        self.attention_norm = nn.LayerNorm(config.embed_dim)
        self.feed_forward_norm = nn.LayerNorm(config.embed_dim)

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        tokens, token_mask = checked_tokens(tokens, token_mask, self.config.embed_dim)
        tokens = tokens + self.attention(self.attention_norm(tokens), token_mask)
        tokens = tokens + self.feed_forward(self.feed_forward_norm(tokens))
        return tokens.masked_fill(~token_mask.unsqueeze(-1), 0) if token_mask is not None else tokens


class TransformerEncoder(nn.Module):
    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.layers = nn.ModuleList([TransformerEncoderLayer(config) for _ in range(config.depth)])
        self.norm = nn.LayerNorm(config.embed_dim)

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        tokens, token_mask = checked_tokens(tokens, token_mask, self.config.embed_dim)
        for layer in self.layers:
            tokens = layer(tokens, token_mask)
        tokens = self.norm(tokens)
        return tokens.masked_fill(~token_mask.unsqueeze(-1), 0) if token_mask is not None else tokens
