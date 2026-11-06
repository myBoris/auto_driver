"""行为克隆训练骨架：需要带动作标签的数据集与具体模型实现。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BehaviorCloningConfig:
    """训练参数容器；这些默认值不是已经验证的最优参数。"""

    dataset_dir: Path
    output_dir: Path
    epochs: int = 10
    batch_size: int = 32
    learning_rate: float = 0.0001
    device: str = "auto"


class BehaviorCloningTrainer:
    """仅定义训练接入位置；无动作标签的被动观察不能直接用于行为克隆。"""

    def __init__(self, config: BehaviorCloningConfig) -> None:
        self.config = config

    def fit(self) -> None:
        """TODO：按 episode 划分数据、构建模型、损失、优化器、验证和检查点。

        首先检查标签来源，避免把 NeutralPolicy 的零动作轨迹当成人类示范。
        同一轨迹相邻帧不应随机拆到训练集与验证集，否则会产生数据泄漏。
        torch 及模型应在此处或具体实现模块内导入，保持 CLI 帮助可轻量运行。
        """
        raise NotImplementedError("请实现行为克隆训练流程")
