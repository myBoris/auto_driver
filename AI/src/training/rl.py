"""强化学习训练骨架：奖励、终止条件和游戏重置协议均需按任务实现。"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReinforcementLearningConfig:
    """强化学习参数容器；中心 step API 只提供动作确认与后续观察。"""

    output_dir: Path
    total_steps: int = 10000
    checkpoint_interval: int = 1000
    seed: int = 0
    device: str = "auto"


class ReinforcementLearningTrainer:
    """算法、经验缓冲区和环境适配由使用者编写，不伪造现成训练结果。"""

    def __init__(self, config: ReinforcementLearningConfig) -> None:
        self.config = config

    def train(self) -> None:
        """TODO：连接环境、定义奖励、采样转移、更新策略并执行周期评估。

        中心不提供车速、道路偏移、碰撞遥测、比赛重置或奖励函数；需要通过
        图像/额外遥测实现这些信号。异常退出必须调用环境释放接口停止手柄。
        """
        raise NotImplementedError("请实现强化学习算法与环境训练循环")
