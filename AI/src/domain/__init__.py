"""领域数据接口：只描述动作和观察，不依赖网络、PyTorch 或具体训练算法。"""

from .types import Action, Observation, StepResult

__all__ = ["Action", "Observation", "StepResult"]
