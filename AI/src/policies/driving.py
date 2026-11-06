"""驾驶模型策略骨架；权重加载、图像预处理和动作映射需要自行实现。"""

from pathlib import Path

from src.domain import Action, Observation


class DrivingPolicy:
    """保存推理配置，不在模块导入阶段加载 torch 或申请 GPU。

    原有 src.model.ImageTransformer 仍是模型开发入口。其前向过程和任务输出
    需要按模型目录说明补齐后，才能实现这里的驾驶预测。
    """

    def __init__(self, checkpoint: Path | None = None, device: str = "auto") -> None:
        self.checkpoint = checkpoint
        self.device = device

    def preprocess(self, observation: Observation):
        """TODO：JPEG 解码、RGB 转换、缩放、归一化与历史帧组装。

        训练与推理必须共享相同预处理；同时保留观察的采集时间，避免把过时画面
        当作当前状态。可参考 src.model.input.prepare_images 的张量约定。
        """
        raise NotImplementedError("请实现驾驶策略的图像预处理")

    def predict(self, observation: Observation) -> Action:
        """TODO：加载检查点、执行推理，再将模型输出映射到完整有效动作。

        输出范围由领域 Action 约束。这里不猜测模型输出维度，也不返回随机动作，
        以免未完成的策略被误认为已经具备驾驶能力。
        """
        raise NotImplementedError("请实现驾驶策略的模型推理和动作映射")
