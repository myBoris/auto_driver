"""前馈网络：逐个 token 加工特征的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class FeedForward(nn.Module):
    """各 token 共用同一个前馈网络；当前仅定义接口。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存输入输出维度、中间层维度和 dropout。
        # TODO: 定义 D → mlp_dim → D 的两层线性网络、激活函数和 dropout。

    def forward(self, tokens: Tensor) -> Tensor:
        """输入和输出均为 [B, L, D]，每个位置分别计算，不交换位置信息。"""
        # TODO: 依次执行第一层线性变换、激活、dropout 和第二层线性变换。
        raise NotImplementedError("请实现逐个 token 的前馈网络。")
