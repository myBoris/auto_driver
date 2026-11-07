"""有界增量监督训练：模型与优化器跨更新保留，验证片段从不进入回放。"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import random
import threading
import uuid

from .bc import BehaviorCloningConfig, multitask_loss, resolve_device, split_episodes


@dataclass(frozen=True)
class OnlineTrainingConfig:
    """每次 update 最多执行固定步数，避免采集事件持续占用训练线程。"""

    base: BehaviorCloningConfig
    min_samples: int = 16
    steps_per_update: int = 10
    replay_size: int = 512
    warm_start: Path | None = None

    def __post_init__(self):
        if not isinstance(self.base, BehaviorCloningConfig):
            raise ValueError("base 必须为 BehaviorCloningConfig")
        if self.base.resume is not None:
            raise ValueError("增量训练使用 warm_start；离线 --resume 保持严格数据指纹校验")
        for name in ("min_samples", "steps_per_update", "replay_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")


def _atomic_json(path, value):
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _sample_signature(sample):
    return hashlib.sha256(json.dumps(sample, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _training_signature(dataset, index):
    """标签与实际历史窗口共同决定样本；补帧/移除历史时原样本也须重新训练。"""
    fields = ("id", "image", "sequence", "captured_at", "source", "terminal_id", "session_id")
    history = [{key: frame.get(key) for key in fields} for frame in dataset._contexts[index]]
    return _sample_signature({"sample": dataset.samples[index], "history": history})


def _group(sample):
    """同次连续采集可跨多个 chunk，但必须共享 split_group 防止相邻帧泄漏。"""
    value = sample.get("split_group", sample["episode_id"])
    if not isinstance(value, str) or not value:
        raise ValueError("split_group 必须为非空字符串")
    return value


class _Metrics:
    def __init__(self):
        self.sums = {key: 0. for key in ("control", "buttons", "direction", "lane", "presence")}
        self.counts = {key: 0 for key in self.sums}
        self.axis_error = [0., 0., 0.]
        self.controls = self.correct = self.lane_values = self.samples = 0
        self.lane_error = 0.

    def add(self, outputs, batch, losses):
        counts = {"control": int(batch["control_mask"].sum()), "buttons": int(batch["control_mask"].sum()),
                  "direction": int(batch["control_mask"].sum()), "lane": int(batch["lane_mask"].sum()),
                  "presence": int(batch["lane_label_mask"].sum())}
        for key in self.sums:
            self.sums[key] += float(losses[key].detach()) * counts[key]
            self.counts[key] += counts[key]
        self.samples += len(batch["images"])
        selected = batch["control_mask"].bool()
        if selected.any():
            self.controls += int(selected.sum())
            errors = (outputs["controls"][selected] - batch["controls"][selected]).abs().detach().sum(0).cpu().tolist()
            self.axis_error = [previous + value for previous, value in zip(self.axis_error, errors)]
            self.correct += int((outputs["direction_logits"][selected].argmax(-1) == batch["direction"][selected]).sum())
        selected = batch["lane_mask"].bool()
        if selected.any():
            error = (outputs["lane_points"][selected] - batch["lane_points"][selected]).abs().detach()
            self.lane_error += float(error.sum())
            self.lane_values += error.numel()

    def result(self, config):
        values = {key: self.sums[key] / self.counts[key] if self.counts[key] else 0. for key in self.sums}
        values["loss"] = (values["control"] * config.control_weight + values["buttons"] * config.button_weight
                          + values["direction"] * config.direction_weight + values["lane"] * config.lane_weight
                          + values["presence"] * config.presence_weight)
        values.update({f"{axis}_mae": error / self.controls if self.controls else None
                       for axis, error in zip(("steering", "throttle", "brake"), self.axis_error)})
        values.update(direction_accuracy=self.correct / self.controls if self.controls else None,
                      lane_mae=self.lane_error / self.lane_values if self.lane_values else None,
                      samples=self.samples)
        return values


class OnlineTrainer:
    """只读取原子发布的清单和不可变 JPEG；调用方可用 asyncio.to_thread 运行。

    验证 split 在首次分配后固定，后续按 split_group 独立分配；新增数据不改变
    旧分组。回放保存有界 CPU 张量，不保留整个历史 Dataset 对象或读取验证帧。
    """

    def __init__(self, config: OnlineTrainingConfig):
        if not isinstance(config, OnlineTrainingConfig):
            raise ValueError("config 必须为 OnlineTrainingConfig")
        self.config = config
        self.output = Path(config.base.output_dir)
        self.output.mkdir(parents=True, exist_ok=True)
        if config.warm_start is None and (self.output / "latest.pt").exists():
            raise ValueError("在线输出已有 latest.pt；请显式 warm_start 或使用新目录")
        self.model = self.optimizer = self.scaler = None
        self.preprocessor = self.model_config = self.device = None
        self.version = self.optimizer_steps = self.epoch = 0
        self.model_id = "online-driving-" + uuid.uuid4().hex[:12]
        self._groups = {}
        self._seen = {}
        self._replay = OrderedDict()
        self._tasks = set()
        self._lane_y = set()
        self._lane_sources = set()
        self._data_source = None
        self._warm_payload = None
        self._rng = random.Random(config.base.seed)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def request_stop(self):
        """线程安全停止请求；当前梯度步结束后保存已完成的更新再退出。"""
        self._stop.set()

    def _status(self, status, **fields):
        result = {"status": status, "version": self.version, "optimizer_steps": self.optimizer_steps,
                  "replay_samples": len(self._replay), **fields}
        _atomic_json(self.output / "status.json", result)
        return result

    def _partition(self, samples):
        groups = sorted({_group(sample) for sample in samples})
        fraction = self.config.base.validation_split
        if not self._groups and fraction and len(groups) < 2:
            return None
        if not self._groups and groups:
            training, validation = split_episodes(groups, fraction, self.config.base.seed)
            self._groups.update({groups[index]: "training" for index in training})
            self._groups.update({groups[index]: "validation" for index in validation})
        for group in groups:
            if group not in self._groups:
                number = int(hashlib.sha256(f"{self.config.base.seed}\0{group}".encode()).hexdigest()[:16], 16) / 2 ** 64
                self._groups[group] = "validation" if number < fraction else "training"
        return [index for index, sample in enumerate(samples) if self._groups[_group(sample)] == "training"], [
            index for index, sample in enumerate(samples) if self._groups[_group(sample)] == "validation"]

    def _initialize(self, data_source):
        import torch
        from src.data.preprocessing import ObservationPreprocessor
        from src.model import DrivingModel, DrivingModelConfig, TransformerConfig

        base = self.config.base
        torch.manual_seed(base.seed)
        self.device = resolve_device(base.device)
        self.preprocessor = ObservationPreprocessor(base.image_size, base.history_frames)
        self.model_config = DrivingModelConfig(encoder=TransformerConfig(
            image_size=base.image_size, patch_size=base.patch_size, embed_dim=base.embed_dim,
            num_heads=base.num_heads, depth=base.depth, mlp_dim=base.mlp_dim,
            dropout=base.dropout, max_images=base.history_frames,
        ))
        self.model = DrivingModel(self.model_config).to(self.device)
        self.optimizer = torch.optim.AdamW(self.model.parameters(), lr=base.learning_rate)
        self.scaler = torch.amp.GradScaler("cuda", enabled=bool(base.amp and self.device.type == "cuda"))
        if self.config.warm_start is not None:
            checkpoint = self._warm_payload or torch.load(self.config.warm_start, map_location="cpu", weights_only=True)
            if (not isinstance(checkpoint, dict) or checkpoint.get("schema_version") != 1
                    or checkpoint.get("model_config") != self.model_config.to_dict()
                    or checkpoint.get("preprocessing") != self.preprocessor.to_dict()
                    or checkpoint.get("data_source") != data_source):
                raise ValueError("warm_start 的模型、预处理或真实/合成数据来源不匹配")
            tasks = checkpoint.get("trained_tasks")
            if (not isinstance(tasks, list) or not tasks or any(task not in {"control", "direction", "lane"} for task in tasks)
                    or not isinstance(checkpoint.get("epoch"), int) or checkpoint["epoch"] < 1):
                raise ValueError("warm_start 必须来自已训练的驾驶检查点")
            self.model.load_state_dict(checkpoint["model_state"], strict=True)
            if "optimizer_state" in checkpoint:
                self.optimizer.load_state_dict(checkpoint["optimizer_state"])
                for group in self.optimizer.param_groups:
                    group["lr"] = base.learning_rate
            self.version = int(checkpoint.get("online_version", 0))
            self.optimizer_steps = int(checkpoint.get("optimizer_steps", 0))
            self.epoch = checkpoint["epoch"]
            self.model_id = checkpoint["model_id"]
            self._tasks.update(tasks)
            self._lane_y.update(checkpoint.get("lane_supervised_y", []))
            self._lane_sources.update(checkpoint.get("lane_label_sources", []))
            if "scaler_state" in checkpoint:
                self.scaler.load_state_dict(checkpoint["scaler_state"])
            if "rng_state" in checkpoint:
                torch.set_rng_state(checkpoint["rng_state"])
            if "sampling_rng_state" in checkpoint:
                self._rng.setstate(checkpoint["sampling_rng_state"])
            # 同一数据根上的连续在线 warm_start 保留组归属；新的独立数据集可扩展。
            for group, partition in checkpoint.get("online_group_split", {}).items():
                if partition in {"training", "validation"}:
                    previous = self._groups.get(group)
                    if previous is not None and previous != partition:
                        raise ValueError("warm_start 与当前数据片段的验证归属冲突")
                    self._groups[group] = partition
        self._data_source = data_source

    def update(self, dataset_dir):
        """对最新清单执行一次有界更新；无标签或不足数据时返回 waiting。"""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("同一个 OnlineTrainer 不能并发 update")
        try:
            return self._update(dataset_dir)
        finally:
            self._lock.release()

    def _update(self, dataset_dir):
        import torch
        from torch.utils.data import default_collate
        from src.data.dataset import DrivingDataset
        from src.data.preprocessing import ObservationPreprocessor

        if self._stop.is_set():
            return self._status("stopped")
        root = Path(dataset_dir)
        if not (root / "samples.jsonl").is_file():
            return self._status("waiting", reason="no_manifest", training_samples=0)
        base = self.config.base
        dataset = DrivingDataset(root, preprocessor=ObservationPreprocessor(base.image_size, base.history_frames),
                                 allow_synthetic=base.allow_synthetic, allow_policy_labels=base.allow_policy_labels,
                                 allow_auto_lanes=base.allow_auto_lanes)
        present = {sample["id"] for sample in dataset.all_samples}
        for identifier in list(self._seen):
            if identifier not in present:
                del self._seen[identifier]
        if not len(dataset):
            self._replay.clear()
            self._seen.clear()
            return self._status("waiting", reason="no_labels", training_samples=0)
        if self.config.warm_start is not None and self.model is None:
            if self._warm_payload is None:
                self._warm_payload = torch.load(self.config.warm_start, map_location="cpu", weights_only=True)
            if not isinstance(self._warm_payload, dict):
                raise ValueError("warm_start 检查点必须是字典")
            for group, partition in self._warm_payload.get("online_group_split", {}).items():
                if partition not in {"training", "validation"}:
                    raise ValueError("warm_start 中的训练/验证片段归属无效")
                self._groups[group] = partition
        partitions = self._partition(dataset.samples)
        if partitions is None:
            return self._status("waiting", reason="need_independent_validation_episode", training_samples=len(dataset))
        training, validation = partitions
        eligible = {dataset.samples[index]["id"]: index for index in training}
        signatures = {identifier: _training_signature(dataset, index) for identifier, index in eligible.items()}
        for identifier in list(self._replay):
            if identifier not in eligible or self._replay[identifier]["signature"] != signatures[identifier]:
                del self._replay[identifier]
        available = len(set(eligible) | set(self._replay))
        if available < self.config.min_samples:
            return self._status("waiting", reason="min_samples", training_samples=available,
                                required_samples=self.config.min_samples, validation_samples=len(validation))
        fresh = [identifier for identifier in eligible if self._seen.get(identifier) != signatures[identifier]]
        if not fresh:
            return self._status("unchanged", training_samples=available, validation_samples=len(validation))
        useful = [identifier for identifier in fresh if dataset.samples[eligible[identifier]].get("action") is not None
                  or bool((dataset.samples[eligible[identifier]].get("lane") or {}).get("visible"))]
        warm_tasks = set(self._warm_payload.get("trained_tasks", [])) if self._warm_payload is not None else set()
        if not useful and not self._tasks and not warm_tasks:
            return self._status("waiting", reason="no_trainable_tasks", training_samples=available)
        data_source = "synthetic" if any("synthetic" in (sample.get("source"), sample.get("action_source"), sample.get("lane_source"))
                                         for sample in dataset.all_samples) else "real"
        if self.model is None:
            self._initialize(data_source)
        elif data_source != self._data_source:
            raise ValueError("同一在线训练器不能混用真实与合成数据来源")
        if self._stop.is_set():
            return self._status("stopped")
        amp = bool(base.amp and self.device.type == "cuda")
        metrics = _Metrics()
        used = {}
        steps = 0
        self.model.train()
        for _ in range(self.config.steps_per_update):
            if self._stop.is_set():
                break
            old = [identifier for identifier in self._replay if identifier not in fresh]
            new_count = max(1, base.batch_size // 2) if old else base.batch_size
            identifiers = self._rng.sample(fresh, min(new_count, len(fresh)))
            if not self._tasks and useful and not any(identifier in useful for identifier in identifiers):
                identifiers[0] = useful[0]
            if old:
                identifiers += self._rng.sample(old, min(base.batch_size - len(identifiers), len(old)))
            if len(identifiers) < base.batch_size:
                identifiers += self._rng.choices(fresh, k=base.batch_size - len(identifiers))
            records = []
            for identifier in identifiers:
                if identifier in eligible:
                    index = eligible[identifier]
                    record = dataset[index]
                    sample = dataset.samples[index]
                    used[identifier] = {"signature": signatures[identifier], "record": record,
                                        "sample": sample, "group": _group(sample)}
                else:
                    record = self._replay[identifier]["record"]
                records.append(record)
            batch = {key: value.to(self.device) for key, value in default_collate(records).items()}
            self.optimizer.zero_grad(set_to_none=True)
            with torch.autocast(device_type=self.device.type, enabled=amp):
                outputs = self.model(batch["images"], batch["image_mask"])
                loss, components = multitask_loss(outputs, batch, base)
            self.scaler.scale(loss).backward()
            self.scaler.unscale_(self.optimizer)
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1., error_if_nonfinite=True)
            self.scaler.step(self.optimizer)
            self.scaler.update()
            metrics.add(outputs, batch, components)
            steps += 1
            self.optimizer_steps += 1
        if not steps:
            return self._status("stopped" if self._stop.is_set() else "waiting")
        for identifier, cached in used.items():
            self._seen[identifier] = cached["signature"]
            self._replay[identifier] = cached
            self._replay.move_to_end(identifier)
            sample = cached["sample"]
            if sample.get("action") is not None:
                self._tasks.update(("control", "direction"))
            lane = sample.get("lane")
            if lane is not None:
                self._lane_sources.add(sample.get("lane_source", "human"))
            if lane and lane.get("visible"):
                self._tasks.add("lane")
                low = max(lane["left"][0][1], lane["right"][0][1])
                high = min(lane["left"][-1][1], lane["right"][-1][1])
                self._lane_y.update(y for y in self.model_config.lane_y if low <= y <= high)
        while len(self._replay) > self.config.replay_size:
            self._replay.popitem(last=False)
        held_out = _Metrics()
        self.model.eval()
        # 验证计算同样有界；同组所有历史帧均未进入训练与回放。
        validation_indices = sorted(validation, key=lambda index: dataset.samples[index]["id"])[:self.config.replay_size]
        with torch.inference_mode():
            for start in range(0, len(validation_indices), base.batch_size):
                if self._stop.is_set():
                    break
                records = [dataset[index] for index in validation_indices[start:start + base.batch_size]]
                batch = {key: value.to(self.device) for key, value in default_collate(records).items()}
                with torch.autocast(device_type=self.device.type, enabled=amp):
                    outputs = self.model(batch["images"], batch["image_mask"])
                    _, components = multitask_loss(outputs, batch, base)
                held_out.add(outputs, batch, components)
        versions = self.output / "versions"
        versions.mkdir(exist_ok=True)
        # 崩溃可能只写完独立版本而尚未替换 latest；跳过已有版本，保持历史不可变。
        self.version += 1
        while (versions / f"v{self.version:06d}.pt").exists():
            self.version += 1
        self.epoch += 1
        split = {partition: sorted(group for group, value in self._groups.items() if value == partition)
                 for partition in ("training", "validation")}
        values = {"training": metrics.result(base), "validation": held_out.result(base) if held_out.samples else None}
        checkpoint = {"schema_version": 1, "model_id": self.model_id, "model_config": self.model_config.to_dict(),
                      "preprocessing": self.preprocessor.to_dict(), "model_state": self.model.state_dict(),
                      "optimizer_state": self.optimizer.state_dict(), "scaler_state": self.scaler.state_dict(),
                      "rng_state": torch.get_rng_state(), "sampling_rng_state": self._rng.getstate(),
                      "epoch": self.epoch, "online_version": self.version,
                      "optimizer_steps": self.optimizer_steps, "training_mode": "online", "trained_tasks": sorted(self._tasks),
                      "data_source": self._data_source, "lane_supervised_y": sorted(self._lane_y, reverse=True),
                      "lane_label_sources": sorted(self._lane_sources),
                      "auto_lane_labels": any(str(source).startswith("auto") for source in self._lane_sources),
                      "metrics": values, "split": split, "online_group_split": dict(self._groups),
                      "training_config": {key: str(value) if isinstance(value, Path) else value for key, value in asdict(base).items()}}
        version_path = versions / f"v{self.version:06d}.pt"
        for path in (version_path, self.output / "latest.pt"):
            temporary = path.with_name(path.name + ".pending")
            torch.save(checkpoint, temporary)
            temporary.replace(path)
        result = self._status("updated", steps=steps, new_samples=len(used), training_samples=available,
                              validation_samples=len(validation), metrics=values, checkpoint=str(self.output / "latest.pt"),
                              version_checkpoint=str(version_path), trained_tasks=sorted(self._tasks),
                              data_source=self._data_source, split=split, stopped=self._stop.is_set())
        with (self.output / "history.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False) + "\n")
        return result
