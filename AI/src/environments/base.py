"""定义环境与训练算法之间的边界，不把 Center 接口误作完整的仿真环境。"""

from typing import Protocol

from src.domain import Action, Observation, StepResult


class DrivingEnvironment(Protocol):
    """供强化学习实现接入的异步环境接口。

    observe 对应 CenterClient.observe；step 对应 CenterClient.step。Center 的
    StepResult 只有图像及执行确认，奖励、terminated、truncated 应由任务层另外
    计算，不能自动补成 0 / False 后当作真实比赛结果。中断时应调用 release。
    """

    async def observe(self) -> Observation:
        """获取当前会话的图像；自行维护终端绑定及历史窗口。"""
        ...

    async def step(self, action: Action) -> StepResult:
        """经 Center 执行动作并获取保持时段之后采集的后续观察。"""
        ...

    async def reset(self) -> Observation:
        """TODO：重置比赛、等待场景就绪并清空历史；Center 没有现成 reset API。"""
        ...

    async def release(self) -> None:
        """释放全部手柄状态；环境退出和训练异常时都应执行。"""
        ...
