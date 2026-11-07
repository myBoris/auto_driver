"""通过 Center 同步采集图像及可对齐的驾驶示范，始终只读。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from src.data.recorder import EpisodeWriter
from src.domain import Action
from src.domain.types import finite_number
from .sessions import ensure_session


def aligned_action(observation, snapshot, session_id):
    """只在同一终端时钟的动作有效区间内标注，不把空闲或未知状态补成零动作。"""
    if (snapshot.get("session_id") != session_id or not snapshot.get("connected")
            or snapshot.get("status") != "active"):
        return None
    applied_at = snapshot.get("applied_at")
    action = snapshot.get("state", {})
    duration = action.get("duration_ms", 0)
    if applied_at is None or duration <= 0:
        return None
    if not applied_at <= observation.captured_at < applied_at + duration / 1000:
        return None
    return Action(**{**action, "buttons": tuple(action["buttons"])}).to_payload()


async def collect_training_samples(client, terminal, output_dir, *, frames=100, timeout=5, interval=0.1, include_controls=True):
    """画面和确认输入写入 samples.jsonl，可直接在 annotate 页面补充车道标签。

    同步的动作表示画面采集时正在执行的输入，并非动作因果结果、奖励或专家能力。
    只记录 Center 已知的网页/API 动作；直接连到游戏的独立设备输入不在此接口中。
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames <= 0:
        raise ValueError("frames 必须为正整数")
    interval = finite_number(interval, "interval", 0, 60)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if (output / ".live.lock").exists():
        raise RuntimeError("此数据集正在实时采集训练，请先停止 train-live")
    lock = output / ".collection.lock"
    try:
        lock_stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError("此数据集正在采集或保存标注，请待当前操作结束") from exc
    terminal_id, session_id = terminal["terminal_id"], terminal.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        lock_stream.close()
        lock.unlink()
        raise ValueError("终端缺少 session_id，请先更新 Center")
    labeled = 0
    try:
        lock_stream.write("Center training collection\n")
        lock_stream.flush()
        if (output / ".live.lock").exists():
            raise RuntimeError("此数据集正在实时采集训练，请先停止 train-live")
        await ensure_session(client, terminal)
        with EpisodeWriter(output, terminal_id, session_id, mode="observations") as writer:
            with (output / "samples.jsonl").open("a", encoding="utf-8", newline="\n") as manifest:
                after = terminal.get("frame_sequence") or None
                for index in range(frames):
                    started = time.monotonic()
                    observation = await client.observe(terminal_id, after=after, timeout=timeout)
                    snapshot = await client.get_control(terminal_id, session_id=session_id) if include_controls else None
                    await ensure_session(client, terminal)
                    relative = writer.write_observation(observation)
                    sample = {"schema_version": 1, "id": f"{writer.episode_id}:{observation.sequence}",
                              "episode_id": writer.episode_id, "image": f"{writer.episode_id}/{relative}",
                              "sequence": observation.sequence, "captured_at": observation.captured_at,
                              "received_at": observation.received_at, "source": "center",
                              "terminal_id": terminal_id, "session_id": session_id}
                    action = aligned_action(observation, snapshot, session_id) if snapshot else None
                    if action is not None:
                        sample.update(action=action, action_source="center_confirmed",
                                      control_revision=snapshot["revision"], command_id=snapshot["command_id"],
                                      control_applied_at=snapshot["applied_at"])
                        labeled += 1
                    manifest.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                    manifest.flush()
                    after = observation.sequence
                    if (index + 1) % 10 == 0 or index + 1 == frames:
                        print(f"Center采集 {index + 1}/{frames} 帧，已对齐 {labeled} 条驾驶操作；车道待标注", flush=True)
                    if index + 1 < frames and interval:
                        await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
    finally:
        lock_stream.close()
        lock.unlink(missing_ok=True)
    return {"frames": frames, "control_labels": labeled, "dataset": str(output)}
