"""位置编码层：提供图片位置和图片块位置的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class PositionEncoding(nn.Module):
    """给图片 token 加入位置信息；当前仅定义接口。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存图片数量上限、块数计算所需尺寸和向量维度。
        # TODO: 定义图片内部各块的位置向量，以及组内各张图片的位置向量。

    def forward(self, tokens: Tensor) -> Tensor:
        """输入和输出均为 [B, N, P, D]。

        B：组数；N：每组图片位置数；P：每张图片的块数；D：向量长度。
        约定使用可学习的位置向量，因此图片在组内的顺序有意义。
        掩码由完整模型传给 Encoder，本层只负责加入位置信息。
        """
        # TODO: 按 P 取出图片块位置向量，按 N 取出组内图片位置向量。
        # TODO: 将两种位置向量广播并加到输入 token 上，保持形状不变。
        raise NotImplementedError("请实现图片块位置编码和组内图片位置编码。")
