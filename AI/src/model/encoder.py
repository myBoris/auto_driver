"""Encoder：单层结构与多层堆叠的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class TransformerEncoderLayer(nn.Module):
    """一层 Encoder：注意力、前馈网络、归一化和残差连接。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 单层也使用相同配置。
        # TODO: 从 attention.py 和 feed_forward.py 引入并创建对应模块。
        # TODO: 定义两个 LayerNorm，以及残差分支需要的 dropout。

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        """输入和输出均为 [B, L, D]，token_mask 为 [B, L]，True 有效。

        采用 Pre-LayerNorm：先归一化，再进入注意力或前馈模块，最后做残差相加。
        不传掩码时全部有效；约定补齐位置输出为零。
        """
        # TODO: 对输入归一化，执行自注意力，再将结果与原输入做残差相加。
        # TODO: 再次归一化，执行前馈网络，再做第二次残差相加。
        # TODO: 将掩码传给注意力，并确保补齐位置最终为零。
        raise NotImplementedError("请实现一层 Transformer Encoder。")


class TransformerEncoder(nn.Module):
    """按 depth 堆叠多层 Encoder；当前仅定义接口。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存层数和每层的配置。
        # TODO: 用 nn.ModuleList 创建 depth 个参数独立的 TransformerEncoderLayer。
        # TODO: 定义所有 Encoder 层之后的最终 LayerNorm。

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        """输入和输出均为 [B, L, D]，token_mask 为 [B, L]，True 有效。

        不传掩码时全部有效；每层接收相同掩码，最终补齐位置输出为零。
        输入应已完成内容向量和位置向量的准备。
        """
        # TODO: 依次通过所有 Encoder 层，并将 token_mask 传给每一层。
        # TODO: 执行最终归一化，清零补齐位置并返回结果。
        raise NotImplementedError("请实现 Transformer Encoder 的多层堆叠。")
