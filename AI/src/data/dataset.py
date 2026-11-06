"""离线数据集契约；张量加载、历史窗口和采样策略由具体训练任务实现。"""

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Any


@dataclass(frozen=True)
class TransitionSample:
    """一条已解析样本的文件引用与元数据，不在此阶段导入 torch 或解码图像。"""

    observation_path: Path
    next_observation_path: Path | None = None
    action: Mapping[str, Any] | None = None
    reward: float | None = None
    terminated: bool = False
    truncated: bool = False
    metadata: Mapping[str, Any] | None = None


class EpisodeDataset(Protocol):
    """TODO：实现记录索引、JPEG 校验、时序窗口、episode 划分及标签筛选。

    实现者需检查 schema_version，禁止把无标签观察自动补成零动作，也不能把
    不同 session_id 下重复出现的序号合并。训练与验证应按 episode 隔离。
    """

    def __len__(self) -> int:
        """返回经过完整性和标签条件筛选后的样本数。"""
        ...

    def __getitem__(self, index: int) -> TransitionSample:
        """返回文件引用及元数据；具体张量变换应由训练/策略共享的实现负责。"""
        ...
