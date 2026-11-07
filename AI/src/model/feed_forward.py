"""逐 token 的两层前馈网络。"""

from torch import Tensor, nn

from .config import TransformerConfig


class FeedForward(nn.Module):
    """各位置共用 D → mlp_dim → D 的 GELU 网络。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()
        self.config = config
        self.layers = nn.Sequential(
            nn.Linear(config.embed_dim, config.mlp_dim), nn.GELU(),
            nn.Dropout(config.dropout), nn.Linear(config.mlp_dim, config.embed_dim),
            nn.Dropout(config.dropout),
        )

    def forward(self, tokens: Tensor) -> Tensor:
        return self.layers(tokens)
