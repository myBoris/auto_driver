"""真实记录器：JPEG 先落盘，再追加 JSONL，并用唯一 episode 隔离采集会话。

本模块只依赖标准库及领域类型，不读取网络、不控制游戏，也不导入 torch。
动作字段表示策略提出的目标状态，ACK 表示中心/终端执行确认；二者均不是
游戏的真实遥测或人类操作示范。异常中断时保留已完成记录，便于检查和恢复。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any
import uuid

from src.domain import Action, Observation, StepResult


def _json_value(value: Any) -> Any:
    """将确认/动作中的常见容器转成 JSON 值，保留嵌套状态而不使用丢信息的 str()。"""
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"记录中存在不能序列化的值：{type(value).__name__}")


def _action_dict(action: Action) -> dict:
    """沿用领域动作的 API 序列化，避免另行维护一套手柄字段转换规则。"""
    return _json_value(action.to_payload())


class EpisodeWriter:
    """每个实例写入一个新 episode；同一实例不能混入另一终端/会话的观察。

    output_dir 是数据集根目录，实际 episode_dir 自动创建唯一子目录。文件使用
    排他创建；重复观察只复用内容和时间均相同的 JPEG，绝不覆盖已有图像。
    modes：observations 只收集无动作标签图像；rollout 保存策略动作转移。
    """

    def __init__(self, output_dir: Path, terminal_id: str, session_id: str, mode: str = "observations") -> None:
        if mode not in {"observations", "rollout"}:
            raise ValueError("mode 必须为 observations 或 rollout")
        if not isinstance(terminal_id, str) or not terminal_id or not isinstance(session_id, str) or not session_id:
            raise ValueError("terminal_id 和 session_id 必须为非空字符串")
        self.output_dir = Path(output_dir)
        self.terminal_id = terminal_id
        self.session_id = session_id
        self.mode = mode
        self.created_at = time.time()
        self.record_count = 0
        self.closed = False
        self._frames: dict[int, dict] = {}
        self._observation_records: set[int] = set()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self.episode_id = f"episode-{timestamp}-{uuid.uuid4().hex[:12]}"
        self.episode_dir = self.output_dir / self.episode_id
        self.episode_dir.mkdir(exist_ok=False)
        self.frames_dir = self.episode_dir / "frames"
        self.frames_dir.mkdir()
        self.meta_path = self.episode_dir / "meta.json"
        self.records_path = self.episode_dir / "records.jsonl"
        self._metadata = {"schema_version": 1, "episode_id": self.episode_id,
                          "terminal_id": terminal_id, "session_id": session_id, "mode": mode,
                          "created_at": self.created_at, "status": "open", "record_count": 0,
                          "action_labels": "none" if mode == "observations" else "policy_proposed",
                          "reward_available": False}
        self._write_meta(initial=True)
        self._records = self.records_path.open("x", encoding="utf-8", newline="\n")

    def __enter__(self) -> EpisodeWriter:
        self._check_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close(interrupted=exc_type is not None)

    def _check_open(self) -> None:
        if self.closed:
            raise RuntimeError("episode 已关闭，不能继续写入")

    def _write_meta(self, initial=False) -> None:
        text = json.dumps(self._metadata, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if initial:
            with self.meta_path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
        else:
            temporary = self.episode_dir / "meta.pending.json"
            with temporary.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            temporary.replace(self.meta_path)

    def _store_observation(self, observation: Observation) -> dict:
        self._check_open()
        sequence = observation.sequence
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("观察 sequence 必须为非负整数")
        for field in ("terminal_id", "session_id"):
            value = getattr(observation, field, None)
            if value is not None and value != getattr(self, field):
                raise ValueError(f"观察 {field} 与当前 episode 不一致，请新建 episode")
        captured_at = observation.captured_at
        received_at = observation.received_at
        for value in (captured_at, received_at):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("观察时间必须为有限的 UNIX 秒数")
        jpeg = observation.jpeg
        if not isinstance(jpeg, bytes) or not jpeg:
            raise ValueError("观察 jpeg 必须为非空 bytes")
        relative = f"frames/{sequence}.jpg"
        record = {"sequence": sequence, "captured_at": captured_at, "received_at": received_at,
                  "terminal_id": self.terminal_id, "session_id": self.session_id, "path": relative}
        previous = self._frames.get(sequence)
        target = self.episode_dir / relative
        if previous is not None:
            if previous != record or target.read_bytes() != jpeg:
                raise ValueError("相同观察序号对应不同内容或时间，不能覆盖已有帧；请检查会话边界")
            return record
        with target.open("xb") as stream:
            stream.write(jpeg)
        self._frames[sequence] = record
        return record

    def _append(self, record: dict) -> None:
        payload = {"schema_version": 1, "episode_id": self.episode_id,
                   "terminal_id": self.terminal_id, "session_id": self.session_id, **record}
        text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        self._records.write(text + "\n")
        self._records.flush()
        self.record_count += 1

    def write_observation(self, obs: Observation) -> str:
        """保存一张真实 JPEG，并记录其时间；没有动作标签，不自动补零动作。"""
        observation = self._store_observation(obs)
        if obs.sequence not in self._observation_records:
            self._append({"type": "observation", "observation": observation, "labeled": False})
            self._observation_records.add(obs.sequence)
        return observation["path"]

    def write_transition(self, obs: Observation, action: Action, result: StepResult, *,
                         reward: float | None = None, terminated=False, truncated=False) -> None:
        """保存策略提出的动作、执行确认与后续观察；奖励和终止信号必须由任务提供。"""
        self._check_open()
        if self.mode != "rollout":
            raise ValueError("只有 rollout episode 可以写入动作转移")
        if reward is not None and (isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward)):
            raise ValueError("reward 必须为有限数值或 None")
        if not isinstance(terminated, bool) or not isinstance(truncated, bool):
            raise ValueError("terminated 和 truncated 必须为布尔值")
        observation = self._store_observation(obs)
        next_observation = self._store_observation(result.observation)
        ack = _json_value(result.ack)
        command_id = getattr(result, "command_id", None)
        if command_id is None and isinstance(ack, dict):
            command_id = ack.get("command_id")
        self._append({"type": "transition", "observation": observation, "action": _action_dict(action),
                      "action_source": "policy_proposed", "labeled": True,
                      "command_id": command_id, "ack": ack, "next_observation": next_observation,
                      "reward": reward, "terminated": terminated, "truncated": truncated})
        if reward is not None:
            self._metadata["reward_available"] = True

    def close(self, *, interrupted=False) -> None:
        """刷新已完成记录并更新元数据；异常退出保留数据，不把半次操作补成成功转移。"""
        if self.closed:
            return
        self._records.flush()
        self._records.close()
        self.closed = True
        self._metadata.update(status="interrupted" if interrupted else "closed", closed_at=time.time(), record_count=self.record_count)
        self._write_meta()
