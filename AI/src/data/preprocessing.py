"""训练与在线推理共享的预处理接口，图像计算由你实现。"""

from collections.abc import Sequence
from typing import Any

from src.domain import Observation


class ObservationPreprocessor:
    """保存输入约定；不在导入或构造时读取图片、加载模型或申请 GPU。

    原有 ImageTransformer 接收 [B, N, 3, H, W]。建议先把一个时间窗口转换为
    [N, 3, H, W]，再由 DataLoader 或推理入口补批次维度。归一化参数、历史窗口
    长度和采样间隔应与模型检查点一起保存，训练和推理必须一致。
    """

    def __init__(self, image_size: int = 224, history_frames: int = 1) -> None:
        self.image_size = image_size
        self.history_frames = history_frames

    def transform(self, observations: Sequence[Observation]) -> Any:
        """TODO：校验时间顺序、JPEG 解码、RGB 转换、裁剪缩放及张量归一化。

        不足窗口长度时的补齐规则也需自行定义，且不能跨越 episode 或连接会话。
        建议在具体实现内部导入 torch/PIL，保持协议和采集命令轻量可用。
        """
        raise NotImplementedError("请实现训练与推理共用的观察图像预处理")
