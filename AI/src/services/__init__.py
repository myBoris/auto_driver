"""业务流程入口：被动采集与策略交互共用中心协议，分别维护控制语义。"""

from .collection import collect_observations
from .rollout import run_rollout
from .sessions import resolve_terminal

__all__ = ["collect_observations", "resolve_terminal", "run_rollout"]
