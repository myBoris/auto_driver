"""完整图片模型：组装输入层和 Transformer Encoder 的空框架。"""

from torch import Tensor, nn

from .config import TransformerConfig


class ImageTransformer(nn.Module):
    """直接接收 prepare_images 生成的像素张量；当前所有计算均待实现。

    B：样本组数；N：每组图片位置数；P：每张图片的块数；D：embed_dim。
    新模型的数据流不经过已有的 CNN 版 ImageInputModel。
    """

    def __init__(self, config: TransformerConfig) -> None:
        super().__init__()  # 初始化 PyTorch 模块。
        self.config = config  # 保存图片尺寸、Encoder 配置和输出方式。
        # TODO: 引入并创建 PatchEmbedding、PositionEncoding 和 TransformerEncoder。
        # 当前接口只返回特征；分类或回归等任务输出层由训练入口另行组合。

    def forward(self, images: Tensor, image_mask: Tensor | None = None) -> Tensor:
        """输入图片，返回整组或逐张图片的特征。

        images：单组 [N, C, H, W]，或多组 [B, N, C, H, W]。
        image_mask：布尔张量 [B, N]；原始输入为单组四维时也支持 [N]。
        True 表示有效图片，False 表示补齐；不传掩码则全部有效。
        每组至少一张有效图片，N 不超过 max_images，图片尺寸应匹配配置。

        pooling="mean"：返回 [B, D]，仅对有效图片的特征取平均。
        pooling="none"：返回 [B, N, D]，补齐图片的输出为零。
        单组输入也保留 B=1；逐张特征已包含组内其他有效图片的信息。
        """
        # TODO: 校验输入和掩码，为单组输入补上批次维度，统一为 [B, N, C, H, W]。
        # TODO: 屏蔽补齐图片的像素，确保无效位置中的 NaN 不进入后续计算。
        # TODO: 合并 B/N 维度，调用 PatchEmbedding，再恢复成 [B, N, P, D]。
        # TODO: 加入两种位置编码，将所有图片块合并为 [B, N*P, D] 的序列。
        # TODO: 将图片掩码扩展成 [B, N*P] 的 token_mask，并调用 Encoder。
        # TODO: 恢复 [B, N, P, D]，对每张图片的块特征取平均，得到 [B, N, D]。
        # TODO: 清零补齐图片的特征，按 pooling 返回逐张特征或整组有效图片的平均特征。
        raise NotImplementedError("请组装图片输入层、位置编码和 Transformer Encoder。")
