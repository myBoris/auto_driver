"""被动采集业务：获取新鲜观察帧并写盘，不产生驾驶操作。"""

from pathlib import Path

from src.data import EpisodeWriter
from src.integrations.center import CenterClient
from .sessions import ensure_session


async def collect_observations(
    client: CenterClient, terminal: dict, output_dir: Path, *, frames: int = 100, timeout: float = 5,
) -> int:
    """保存指定数量的不同帧，供无标签预训练或之后的人工标注使用。

    ``after`` 让中心等待比上一次更新的帧，避免重复保存同一画面。采集速率受
    terminal 的 TERMINAL_OBSERVATION_FPS 限制，与 WebRTC 视频帧率无关。
    本流程只读观察，因此结束时也不发送 release，不干扰正在进行的人工驾驶。
    """
    if frames <= 0:
        raise ValueError("frames 必须为正整数")
    terminal_id = terminal["terminal_id"]
    with EpisodeWriter(output_dir, terminal_id, terminal["session_id"], mode="observations") as writer:
        after = None
        for _ in range(frames):
            observation = await client.observe(terminal_id, after=after, timeout=timeout)
            await ensure_session(client, terminal)
            writer.write_observation(observation)
            after = observation.sequence
    return frames
