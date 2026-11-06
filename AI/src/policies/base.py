"""同步策略契约：一个新鲜观察产生一份完整的手柄目标状态。"""

from typing import Protocol

from src.domain import Action, Observation


class Policy(Protocol):
    """推理不直接调用中心，也不负责释放；服务层执行动作并处理停止与失败。

    采用同步接口，允许服务层用 asyncio.to_thread 隔离 CPU/GPU 推理，避免
    阻塞异步网络心跳。时序模型如需历史帧，应在具体策略中维护自己的窗口。
    """

    def predict(self, observation: Observation) -> Action:
        """根据观察返回完整动作；未填写的手柄状态不能继承上一帧的输入。"""
        ...
