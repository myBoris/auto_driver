"""奖励接口占位：奖励的含义和尺度由实际驾驶任务决定。"""

from typing import Protocol

from src.domain import Action, Observation, StepResult


class RewardFunction(Protocol):
    """纯计算接口，不读取网络，也不把动作确认当作车辆已正确行驶。"""

    def __call__(self, observation: Observation, action: Action, result: StepResult) -> float:
        """TODO：根据真实可观测的任务信号计算奖励。"""
        ...


def driving_reward(observation: Observation, action: Action, result: StepResult) -> float:
    """TODO：实现道路保持、速度、碰撞等可验证信号；未实现时明确报错。"""
    raise NotImplementedError("请根据实际任务实现奖励函数；中心未提供奖励或游戏遥测")
