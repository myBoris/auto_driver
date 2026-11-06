"""图片输入层：切块、展平和线性映射的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class PatchEmbedding(nn.Module):
    """将图片像素转换为 token 向量；当前仅定义接口。"""

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存切块尺寸、通道数和向量维度等配置。
        # TODO: 定义将每块像素映射到 embed_dim 的线性层。

    def forward(self, images: Tensor) -> Tensor:
        """输入 [M, C, H, W]，输出 [M, P, D]。

        M：本次处理的图片总数；C：通道数；H/W：图片高/宽。
        P = (image_size / patch_size) ** 2；D = embed_dim。
        这里只处理图片，不负责区分图片属于哪一组。
        """
        # TODO: 检查图片尺寸及类型，按从上到下、从左到右的顺序切块。
        # TODO: 将每块展平为 C * patch_size * patch_size 个像素数值。
        # TODO: 用线性层将各块映射成长度为 D 的向量并返回。
        raise NotImplementedError("请实现图片切块、展平和线性映射。")
