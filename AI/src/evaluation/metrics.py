"""驾驶评估骨架：先明确任务和计分方法，再填写实际测量结果。"""

from dataclasses import dataclass, field
from typing import Mapping

from src.policies.base import Policy


@dataclass(frozen=True)
class EvaluationResult:
    """缺失结果保持 None 或空映射；不能用零分冒充尚未执行的评估。"""

    episodes: int = 0
    success_rate: float | None = None
    mean_reward: float | None = None
    metrics: Mapping[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


def evaluate_policy(policy: Policy, environment, *, episodes: int = 1) -> EvaluationResult:
    """TODO：定义可复现赛道/初态、终止判定与指标，再运行独立评估。

    应记录任务配置、模型检查点和采集/控制时间；中心动作 ACK 只证明输入已经
    调用，不能用作成功率、驾驶准确率、比赛完成率或碰撞率。
    """
    raise NotImplementedError("请实现任务环境与评估指标，不返回虚构成绩")
