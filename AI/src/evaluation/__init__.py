"""评估接口与结果容器；不生成未经测量的驾驶成绩。"""

from .metrics import EvaluationResult, evaluate_policy

__all__ = ["EvaluationResult", "evaluate_policy"]
