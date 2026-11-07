"""Center 示范采集与后台增量训练；本服务只读取终端，不执行驾驶。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

from src.data.dataset import load_samples, write_samples
from src.data.recorder import EpisodeWriter
from src.domain.types import finite_number
from .sessions import ensure_session
from .training_collection import aligned_action


async def _commit_samples(root, pending, *, timeout):
    """短暂持有写锁，合并期间人工修正过的记录，再原子发布完整清单。"""
    if not pending:
        return
    lock = root / ".collection.lock"
    started = time.monotonic()
    while True:
        try:
            stream = lock.open("x", encoding="utf-8")
            break
        except FileExistsError:
            if time.monotonic() - started >= timeout:
                raise RuntimeError("训练数据正在被其他进程保存；已完成图片保留在 episode 中")
            await asyncio.sleep(.05)
    try:
        stream.write("live training manifest update\n")
        stream.close()
        samples = load_samples(root) if (root / "samples.jsonl").exists() else []
        write_samples(root, samples + pending)
        pending.clear()
    finally:
        stream.close()
        lock.unlink(missing_ok=True)


async def run_live_training(client, terminal, dataset_dir, trainer, *, frames=None,
                            chunk_frames=16, interval=.1, timeout=5, auto_labeler=None):
    """持续采集教师输入，每批唤醒一次后台训练；慢训练时合并新数据更新。

    一次启动对应一个独立 episode，分批写盘不会把相邻帧分到训练与验证两侧。
    JPEG 不覆盖，JSONL 原子替换，因此训练线程能读取一致快照。人工标注可在
    两次提交之间保存。取消时等待当前有界训练更新结束，再释放采集锁。
    """
    for name, value in (("chunk_frames", chunk_frames), ("frames", frames)):
        if value is None and name == "frames":
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} 必须为正整数")
    interval = finite_number(interval, "interval", 0, 60)
    timeout = finite_number(timeout, "timeout", .001, 60)
    root = Path(dataset_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    terminal_id, session_id = terminal["terminal_id"], terminal.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("终端缺少 session_id，请先更新 Center")
    lock = root / ".live.lock"
    try:
        lock_stream = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError("此数据集已有实时采集训练进程") from exc
    pending = []
    training_task = None
    training_revision = 0
    revision = 0
    count = controls = lanes = 0
    summary = {"status": "waiting", "reason": "尚未收到新数据"}
    output_lock = output_stream = None

    async def finish_training():
        nonlocal training_task, summary
        if training_task is not None:
            # asyncio.to_thread 的线程不能被 Task.cancel() 中断；等待本次有界更新。
            task = training_task
            try:
                summary = await asyncio.shield(task)
            except asyncio.CancelledError:
                request_stop = getattr(trainer, "request_stop", None)
                if callable(request_stop):
                    request_stop()
                summary = await task
                raise
            finally:
                training_task = None
            print(json.dumps({"training": summary}, ensure_ascii=False), flush=True)

    def start_training():
        nonlocal training_task, training_revision
        training_revision = revision
        training_task = asyncio.create_task(asyncio.to_thread(trainer.update, root))

    async def commit():
        nonlocal revision
        await _commit_samples(root, pending, timeout=timeout)
        revision += 1
        print(f"实时采集 {count} 帧，已对齐操作 {controls}，自动车道 {lanes}；后台分批训练", flush=True)
        if training_task is not None and training_task.done():
            await finish_training()
        if training_task is None:
            start_training()

    try:
        lock_stream.write("Center live supervised training\n")
        lock_stream.flush()
        if (root / ".collection.lock").exists():
            raise RuntimeError("请等待当前采集或标注保存结束，再开始实时训练")
        trainer_output = getattr(trainer, "output", None)
        if trainer_output is not None:
            output_lock = Path(trainer_output) / ".live-training.lock"
            try:
                output_stream = output_lock.open("x", encoding="utf-8")
            except FileExistsError as exc:
                output_lock = None
                raise RuntimeError("此模型输出目录已有实时训练进程，请更换 --output") from exc
        await ensure_session(client, terminal)
        with EpisodeWriter(root, terminal_id, session_id, mode="observations") as writer:
            try:
                after = terminal.get("frame_sequence") or None
                while frames is None or count < frames:
                    if training_task is not None and training_task.done():
                        await finish_training()
                    started = time.monotonic()
                    observation = await client.observe(terminal_id, after=after, timeout=timeout)
                    snapshot = await client.get_control(terminal_id, session_id=session_id)
                    await ensure_session(client, terminal)
                    relative = writer.write_observation(observation)
                    sample = {"schema_version": 1, "id": f"{writer.episode_id}:{observation.sequence}",
                              "episode_id": writer.episode_id, "split_group": writer.episode_id,
                              "image": f"{writer.episode_id}/{relative}", "sequence": observation.sequence,
                              "captured_at": observation.captured_at, "received_at": observation.received_at,
                              "source": "center", "terminal_id": terminal_id, "session_id": session_id}
                    action = aligned_action(observation, snapshot, session_id)
                    if action is not None:
                        sample.update(action=action, action_source="center_confirmed",
                                      control_revision=snapshot["revision"], command_id=snapshot["command_id"],
                                      control_applied_at=snapshot["applied_at"])
                        controls += 1
                    if auto_labeler is not None:
                        candidate = await asyncio.to_thread(auto_labeler.annotate, observation.jpeg)
                        if candidate is not None:
                            sample.update(candidate)
                            lanes += 1
                    pending.append(sample)
                    count += 1
                    after = observation.sequence
                    if len(pending) >= chunk_frames or count == frames:
                        await commit()
                    if (frames is None or count < frames) and interval:
                        await asyncio.sleep(max(0, interval - (time.monotonic() - started)))
            except BaseException:
                request_stop = getattr(trainer, "request_stop", None)
                if callable(request_stop):
                    request_stop()
                # 取消/断线时保留已完成观察，保存失败不掩盖原始采集异常。
                try:
                    await _commit_samples(root, pending, timeout=timeout)
                except Exception as exc:
                    print(f"部分观察未写入 samples.jsonl，JPEG 保留在 episode 中：{exc}", flush=True)
                raise
            else:
                await _commit_samples(root, pending, timeout=timeout)
        await finish_training()
        if revision > training_revision:
            start_training()
            await finish_training()
        return {"frames": count, "control_labels": controls, "auto_lane_labels": lanes,
                "dataset": str(root), "training": summary}
    finally:
        try:
            await finish_training()
        finally:
            if output_stream is not None:
                output_stream.close()
            if output_lock is not None:
                output_lock.unlink(missing_ok=True)
            lock_stream.close()
            lock.unlink(missing_ok=True)
