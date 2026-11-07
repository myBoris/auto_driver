"""Center 新鲜画面 → 联合模型 → 预测叠加，以及可选的驾驶执行。"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import logging
from pathlib import Path
import time

from src.data import EpisodeWriter
from src.domain.types import finite_number
from src.integrations.center import CenterClient
from src.policies.driving import DrivingPolicy
from .sessions import ensure_session

log = logging.getLogger(__name__)


async def run_driving(client: CenterClient, terminal: dict, policy: DrivingPolicy, *,
                      frames: int = 100, execute: bool = False, timeout: float = 5,
                      output_dir: Path | None = None) -> int:
    """预览不发送动作或 release；执行时以 step 返回的新观察继续决策。

    POST 失败后不重放；只在尝试过动作的原会话上释放输入。推理耗时用单调时钟
    检查，不拿终端采集时间与 AI 电脑时钟直接相减。输出记录带模型来源和帧号。
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
        raise ValueError("frames 必须为正整数")
    if not isinstance(execute, bool):
        raise ValueError("execute 必须为布尔值")
    timeout = finite_number(timeout, "timeout", .1, 30)
    if not terminal.get("session_id") or terminal.get("connected") is not True:
        raise ValueError("预测需要绑定一个在线终端会话")
    if execute:
        policy.require_executable()
    terminal_id = terminal["terminal_id"]
    policy.reset_state()
    attempted_control = False
    failed = False
    completed = 0
    cursor = terminal.get("frame_sequence")
    cursor = cursor if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor > 0 else None
    with ExitStack() as stack:
        writer = None
        prediction_log = None
        if output_dir is not None:
            writer = stack.enter_context(EpisodeWriter(output_dir, terminal_id, terminal["session_id"],
                                                       mode="rollout" if execute else "observations"))
            prediction_log = stack.enter_context((writer.episode_dir / "predictions.jsonl").open("x", encoding="utf-8"))
        try:
            await ensure_session(client, terminal)
            observation = await client.observe(terminal_id, after=cursor, timeout=timeout)
            for index in range(frames):
                observed_at = time.monotonic()
                await ensure_session(client, terminal)
                result = await asyncio.to_thread(policy.predict_result, observation)
                await ensure_session(client, terminal)
                if execute and time.monotonic() - observed_at > policy.max_inference_seconds:
                    raise RuntimeError("推理与会话检查耗时过长，未向终端发送过期预测动作")
                if execute and result.action is None:
                    raise RuntimeError("模型缺少驾驶操作输出")
                payload = result.to_perception(observation, session_id=terminal["session_id"],
                                               model_id=policy.model_id, source=policy.source)
                await client.publish_perception(terminal_id, payload)
                if prediction_log is not None:
                    record = {"schema_version": 1, "session_id": terminal["session_id"],
                              "sequence": observation.sequence, "model_id": policy.model_id,
                              "data_source": policy.data_source, "execute": execute,
                              "perception": payload,
                              "action": result.action.to_payload() if result.action else None}
                    prediction_log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    prediction_log.flush()
                if execute:
                    # 显示请求也有网络开销，必须在实际操作前再次检查画面是否已过时。
                    await ensure_session(client, terminal)
                    if time.monotonic() - observed_at > policy.max_inference_seconds:
                        raise RuntimeError("预测发布耗时过长，未向终端发送过期预测动作")
                    attempted_control = True
                    step = await client.step(terminal_id, result.action, observation_timeout=timeout)
                    await ensure_session(client, terminal)
                    if step.observation.sequence <= observation.sequence:
                        raise RuntimeError("动作之后没有得到更新的观察帧")
                    if writer is not None:
                        writer.write_transition(observation, result.action, step)
                    observation = step.observation
                else:
                    if writer is not None:
                        writer.write_observation(observation)
                    if index + 1 < frames:
                        observation = await client.observe(terminal_id, after=observation.sequence, timeout=timeout)
                completed += 1
                steering = result.prediction["steering"] if result.prediction else None
                label = "无方向监督" if steering is None else f"转向 {steering:+.2f}"
                print(f"预测 {completed}/{frames}：{label}，道路{'可见' if result.road else '未检出'}，"
                      f"推理 {result.inference_ms:.1f} ms，{'执行驾驶' if execute else '仅显示'}", flush=True)
        except BaseException:
            failed = True
            raise
        finally:
            if attempted_control:
                try:
                    # 连接已经替换时不干扰新会话，旧动作由终端看门狗归零。
                    await ensure_session(client, terminal)
                    await client.release(terminal_id)
                except Exception:
                    if not failed:
                        raise
                    log.warning("驾驶已停止，原会话释放请求未成功；旧动作由终端到期释放")
    return completed
