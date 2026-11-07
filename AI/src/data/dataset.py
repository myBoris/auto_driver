"""读取可追溯的样本清单，按 episode 组装历史，并对缺失标签使用独立掩码。"""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
from typing import Any, Protocol

from src.domain import Action
from src.domain.types import BUTTONS, finite_number
from .preprocessing import ObservationPreprocessor

DEFAULT_LANE_Y = (.95, .85, .75, .65, .55, .45)
BUTTON_NAMES = tuple(sorted(BUTTONS))


@dataclass(frozen=True)
class TransitionSample:
    """保留旧的文件引用接口，供不需要张量的数据工具使用。"""

    observation_path: Path
    next_observation_path: Path | None = None
    action: Mapping[str, Any] | None = None
    reward: float | None = None
    terminated: bool = False
    truncated: bool = False
    metadata: Mapping[str, Any] | None = None


class EpisodeDataset(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, index: int) -> TransitionSample: ...


def image_path(root: Path, relative: str) -> Path:
    """清单只能引用数据集内的真实文件，不跟随逃逸到外部的路径或符号链接。"""
    if not isinstance(relative, str) or not relative:
        raise ValueError("样本 image 必须是非空相对路径")
    root = Path(root).resolve()
    path = Path(relative)
    if path.is_absolute():
        raise ValueError("样本 image 必须是数据集内的相对路径")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"图片不存在或路径越出数据集：{relative}")
    return resolved


def validate_action(value: dict) -> dict:
    if not isinstance(value, dict) or not {"steering", "throttle", "brake", "buttons"} <= value.keys():
        raise ValueError("动作标签必须明确提供 steering、throttle、brake 和 buttons")
    if value.keys() - {"steering", "throttle", "brake", "buttons", "duration_ms"}:
        raise ValueError("动作标签包含未知字段")
    if not isinstance(value["buttons"], (list, tuple)):
        raise ValueError("动作 buttons 必须是列表")
    return Action(**{**value, "buttons": tuple(value["buttons"])}).to_payload()


def _boundary(value, name):
    if not isinstance(value, list) or not 2 <= len(value) <= 256:
        raise ValueError(f"车道 {name} 至少需要两个点")
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("车道坐标必须是 [x, y]")
        points.append([finite_number(point[0], "x", 0, 1), finite_number(point[1], "y", 0, 1)])
    points.sort(key=lambda point: point[1])
    if any(first[1] == second[1] for first, second in zip(points, points[1:])):
        raise ValueError("同侧边界不能在相同 y 处标注多个点")
    return points


def _interpolate(points, y):
    if not points[0][1] <= y <= points[-1][1]:
        return None
    for first, second in zip(points, points[1:]):
        if first[1] <= y <= second[1]:
            fraction = (y - first[1]) / (second[1] - first[1])
            return first[0] + fraction * (second[0] - first[0])
    return None


def validate_lane(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("visible"), bool):
        raise ValueError("车道标签必须提供 visible 布尔值")
    if value.keys() - {"visible", "left", "right"}:
        raise ValueError("车道标签包含未知字段")
    if not value["visible"]:
        if value.get("left") or value.get("right"):
            raise ValueError("无车道标签不能同时包含边界点")
        return {"visible": False}
    left = _boundary(value.get("left"), "left")
    right = _boundary(value.get("right"), "right")
    low, high = max(left[0][1], right[0][1]), min(left[-1][1], right[-1][1])
    if low >= high:
        raise ValueError("左右边界需要共同的 y 范围")
    anchors = {low, high, *(point[1] for point in left + right if low <= point[1] <= high)}
    if any(_interpolate(left, y) >= _interpolate(right, y) for y in anchors):
        raise ValueError("车道左右边界不能交叉或重合")
    return {"visible": True, "left": left, "right": right}


def load_samples(root: Path) -> list[dict]:
    """加载完整 JSONL；坏记录明确失败，避免静默制造部分训练集。"""
    root = Path(root).resolve()
    manifest = root / "samples.jsonl"
    if not manifest.is_file():
        raise ValueError(f"没有样本清单：{manifest}；请先从 Center 采集数据")
    records, identifiers, sequences, sessions, sources = [], set(), set(), {}, {}
    for number, line in enumerate(manifest.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
            if not isinstance(sample, dict) or type(sample.get("schema_version")) is not int or sample["schema_version"] != 1:
                raise ValueError("schema_version 必须为 1")
            for name in ("id", "episode_id"):
                if not isinstance(sample.get(name), str) or not sample[name]:
                    raise ValueError(f"{name} 必须是非空字符串")
            if sample["id"] in identifiers:
                raise ValueError("样本 id 重复")
            sequence = sample.get("sequence")
            if type(sequence) is not int or sequence < 1:
                raise ValueError("sequence 必须是正整数")
            identity = (sample["episode_id"], sequence)
            if identity in sequences:
                raise ValueError("同一 episode 的帧号重复")
            session = (sample.get("terminal_id"), sample.get("session_id"))
            if sample["episode_id"] in sessions and sessions[sample["episode_id"]] != session:
                raise ValueError("同一 episode 不能混入另一终端或连接会话")
            if sample.get("source", "center") not in {"center", "human", "synthetic"}:
                raise ValueError("未知样本来源")
            if (sample["episode_id"] in sources
                    and sources[sample["episode_id"]] != (sample.get("source") == "synthetic")):
                raise ValueError("同一 episode 不能混合真实和合成图像")
            image_path(root, sample.get("image"))
            for name in ("captured_at", "received_at"):
                if name in sample:
                    finite_number(sample[name], name, .000001, 1e12)
            if sample.get("action") is not None:
                sample["action"] = validate_action(sample["action"])
                if sample.get("action_source") not in {"center_confirmed", "human", "synthetic", "policy_proposed"}:
                    raise ValueError("动作标签必须注明 action_source")
            if sample.get("lane") is not None:
                sample["lane"] = validate_lane(sample["lane"])
            lane_source = sample.get("lane_source")
            if lane_source not in {None, "human", "synthetic", "auto"}:
                raise ValueError("未知车道标签来源")
            annotation = sample.get("lane_annotation")
            if lane_source == "auto" or annotation is not None:
                if not isinstance(annotation, dict):
                    raise ValueError("自动车道标签必须保留 lane_annotation 元数据")
                if not isinstance(annotation.get("method"), str) or not annotation["method"]:
                    raise ValueError("车道标注 method 必须是非空字符串")
                finite_number(annotation.get("confidence"), "车道候选 confidence", 0, 1)
                if not isinstance(annotation.get("reviewed"), bool):
                    raise ValueError("车道标注 reviewed 必须是布尔值")
                if lane_source == "auto" and sample.get("lane") is None:
                    raise ValueError("自动车道元数据必须对应实际候选边界")
            identifiers.add(sample["id"])
            sequences.add(identity)
            sessions[sample["episode_id"]] = session
            sources[sample["episode_id"]] = sample.get("source") == "synthetic"
            records.append(sample)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"samples.jsonl 第 {number} 行：{exc}") from exc
    return records


def write_samples(root: Path, samples: list[dict]) -> None:
    """标注保存使用同目录临时文件和原子替换；绝不改动原图片。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n" for sample in samples)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=root, prefix="samples-", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(text)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(root / "samples.jsonl")
    finally:
        temporary.unlink(missing_ok=True)


class DrivingDataset:
    """DataLoader 可直接使用；不导入 torch，直到实际取出一份张量样本。"""

    def __init__(self, dataset_dir: Path, preprocessor=None, lane_y=DEFAULT_LANE_Y,
                 history_frames: int = 1, allow_policy_labels=False, allow_synthetic=False,
                 allow_auto_lanes=False):
        self.root = Path(dataset_dir).resolve()
        self.preprocessor = preprocessor or ObservationPreprocessor(history_frames=history_frames)
        self.history_frames = self.preprocessor.history_frames
        self.lane_y = tuple(finite_number(y, "lane_y", 0, 1) for y in lane_y)
        if len(self.lane_y) < 2 or len(set(self.lane_y)) != len(self.lane_y):
            raise ValueError("lane_y 至少有两个不重复的锚点")
        self.all_samples = load_samples(self.root)
        self.samples, self.episode_ids, self._contexts = [], [], []
        self.label_counts = {"control": 0, "lane": 0, "direction": 0}
        histories = {}
        for sample in sorted(self.all_samples, key=lambda item: (item["episode_id"], item["sequence"])):
            history = histories.setdefault(sample["episode_id"], [])
            if history and sample.get("captured_at", 0) < history[-1].get("captured_at", 0):
                raise ValueError("同一 episode 的采集时间不能倒退")
            history.append(sample)
            if sample.get("source") == "synthetic" and not allow_synthetic:
                continue
            action_allowed = sample.get("action") is not None and (
                sample["action_source"] != "policy_proposed" or allow_policy_labels)
            if sample.get("action_source") == "synthetic" and not allow_synthetic:
                action_allowed = False
            annotation = sample.get("lane_annotation") or {}
            auto_lane = sample.get("lane_source") == "auto" or annotation.get("reviewed") is False
            lane = sample.get("lane") if allow_auto_lanes or not auto_lane else None
            if not action_allowed and lane is None:
                continue
            if lane and lane["visible"]:
                valid_anchors = sum(_interpolate(lane["left"], y) is not None
                                    and _interpolate(lane["right"], y) is not None for y in self.lane_y)
                if valid_anchors < 2:
                    raise ValueError(f"样本 {sample['id']} 的车道范围至少需要覆盖两个 lane_y 锚点")
            training_sample = {**sample, "action": sample.get("action") if action_allowed else None, "lane": lane}
            self.samples.append(training_sample)
            self.episode_ids.append(sample["episode_id"])
            self._contexts.append(tuple(history[-self.history_frames:]))
            self.label_counts["control"] += int(action_allowed)
            self.label_counts["direction"] += int(action_allowed)
            self.label_counts["lane"] += int(lane is not None)

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, index):
        import torch

        sample, context = self.samples[index], self._contexts[index]
        images = self.preprocessor.transform_paths([image_path(self.root, item["image"]) for item in context])
        padding = self.history_frames - images.shape[0]
        image_mask = torch.tensor([False] * padding + [True] * images.shape[0], dtype=torch.bool)
        if padding:
            images = torch.cat([torch.zeros((padding, *images.shape[1:]), dtype=images.dtype), images])
        action = sample.get("action")
        controls = [action[axis] for axis in ("steering", "throttle", "brake")] if action else [0., 0., 0.]
        buttons = [float(button in action["buttons"]) if action else 0. for button in BUTTON_NAMES]
        lane = sample.get("lane")
        lane_points, lane_mask = [], []
        for y in self.lane_y:
            left = _interpolate(lane["left"], y) if lane and lane["visible"] else None
            right = _interpolate(lane["right"], y) if lane and lane["visible"] else None
            valid = left is not None and right is not None
            lane_points.append([left, right] if valid else [0., 0.])
            lane_mask.append(valid)
        steering = controls[0]
        return {"images": images, "image_mask": image_mask,
                "controls": torch.tensor(controls, dtype=torch.float32),
                "control_mask": torch.tensor(action is not None),
                "buttons": torch.tensor(buttons, dtype=torch.float32),
                "lane_points": torch.tensor(lane_points, dtype=torch.float32),
                "lane_mask": torch.tensor(lane_mask, dtype=torch.bool),
                "lane_presence": torch.tensor(float(bool(lane and lane["visible"]))),
                "lane_label_mask": torch.tensor(lane is not None),
                "direction": torch.tensor(0 if steering < -.08 else 2 if steering > .08 else 1, dtype=torch.long)}
