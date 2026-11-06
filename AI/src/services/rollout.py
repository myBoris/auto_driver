"""策略交互业务：组织观察、决策、动作确认及后续观察，不实现学习算法。"""

import asyncio
import logging
from pathlib import Path

from src.data import EpisodeWriter
from src.integrations.center import CenterClient
from src.policies import Policy
from .sessions import ensure_session

log = logging.getLogger(__name__)


async def run_rollout(
    client: CenterClient, terminal: dict, policy: Policy, output_dir: Path, *, steps: int = 10,
    timeout: float = 5,
) -> int:
    """保存 ``(当前观察, 请求动作, ACK, 后续观察)``，供推理验证或后续强化学习使用。

    策略只提供目标手柄状态，Center 的 step 负责执行确认和观察因果顺序：后续图像
    必须在动作执行时间加保持时长之后采集。它不是游戏逐物理帧推进，也不包含奖励、
    碰撞、比赛重置或 episode 结束判断；这些任务规则应由你在训练和评估模块接入。
    """
    if steps <= 0:
        raise ValueError("steps 必须为正整数")
    terminal_id = terminal["terminal_id"]
    with EpisodeWriter(output_dir, terminal_id, terminal["session_id"], mode="rollout") as writer:
        failed = False
        try:
            observation = await client.observe(terminal_id, timeout=timeout)
            await ensure_session(client, terminal)
            for _ in range(steps):
                # 同步模型推理放在线程执行，避免占住负责 HTTP 超时和取消的事件循环。
                action = await asyncio.to_thread(policy.predict, observation)
                action.to_payload()
                await ensure_session(client, terminal)
                result = await client.step(terminal_id, action, observation_timeout=timeout)
                await ensure_session(client, terminal)
                writer.write_transition(observation, action, result)
                observation = result.observation
        except BaseException:
            # 包括 Ctrl+C 转换的任务取消；清理失败不能覆盖真正的实验失败原因。
            failed = True
            raise
        finally:
            try:
                await client.release(terminal_id)
            except Exception:
                if not failed:
                    raise
                log.warning("实验已中断，中心释放请求未成功；终端动作仍受自身到期计时器约束")
    return steps
