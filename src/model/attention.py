"""Transformer 核心：多头自注意力的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class MultiHeadSelfAttention(nn.Module):
    """自行实现 Q/K/V 和注意力计算；当前仅定义接口。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存向量维度、头数和 dropout 等配置。
        # TODO: 定义 Q/K/V 投影层、多头合并后的输出投影层和 dropout。

    def forward(self, tokens: Tensor, token_mask: Tensor | None = None) -> Tensor:
        """输入和输出均为 [B, L, D]。

        L：序列长度；图片任务中 L = N * P。
        token_mask 为 [B, L] 的布尔张量，True 有效，False 为补齐位置。
        不传掩码时默认全部有效；每组至少有一个有效 token。
        约定有效位置不读取补齐位置，补齐位置的最终输出为零。
        """
        # TODO: 计算 Q/K/V，将 D 拆为 num_heads 个头，每头维度为 D / num_heads。
        # TODO: 计算 Q 与 K 的点积，并除以每头维度的平方根。
        # TODO: 屏蔽无效的 key 位置，再对 key 维度计算 softmax 和 dropout。
        # TODO: 用注意力权重对 V 加权求和，拼接各头并执行输出投影。
        # TODO: 清零补齐位置的输出；防止补齐输入中的 NaN 影响有效位置。
        raise NotImplementedError("请实现多头自注意力。")
