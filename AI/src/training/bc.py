"""车道边界、方向类别与完整驾驶操作的联合监督训练。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import random
import uuid


@dataclass(frozen=True)
class BehaviorCloningConfig:
    """训练和模型参数随检查点保存；验证集始终按完整片段划分。"""

    dataset_dir: Path
    output_dir: Path
    epochs: int = 10
    batch_size: int = 16
    learning_rate: float = 0.0001
    device: str = "auto"
    image_size: int = 128
    patch_size: int = 16
    history_frames: int = 1
    embed_dim: int = 128
    num_heads: int = 4
    depth: int = 3
    mlp_dim: int = 256
    dropout: float = 0.1
    validation_split: float = 0.2
    seed: int = 7
    num_workers: int = 0
    allow_synthetic: bool = False
    allow_policy_labels: bool = False
    allow_auto_lanes: bool = False
    amp: bool = False
    resume: Path | None = None
    lane_weight: float = 2.0
    control_weight: float = 1.0
    direction_weight: float = 0.3
    button_weight: float = 0.2
    presence_weight: float = 0.5

    def __post_init__(self):
        for name in ("epochs", "batch_size", "image_size", "patch_size", "history_frames", "embed_dim", "num_heads", "depth", "mlp_dim"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} 必须为正整数")
        if isinstance(self.num_workers, bool) or not isinstance(self.num_workers, int) or self.num_workers < 0:
            raise ValueError("num_workers 必须为非负整数")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise ValueError("seed 必须为整数")
        for name, low, high in (("learning_rate", 0, 1), ("dropout", 0, 1), ("validation_split", 0, 1)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value < high:
                raise ValueError(f"{name} 超出有效范围")
        if self.learning_rate == 0:
            raise ValueError("learning_rate 必须大于零")
        for name in ("lane_weight", "control_weight", "direction_weight", "button_weight", "presence_weight"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")


def resolve_device(requested="auto"):
    """只在训练/推理入口查询设备；协议和帮助命令不初始化 CUDA。"""
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu") if requested == "auto" else torch.device(requested)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise ValueError("指定了 CUDA，但当前 PyTorch 无可用 CUDA 设备")
    if device.type not in {"cpu", "cuda", "mps"}:
        raise ValueError("device 必须为 auto、cpu、cuda 或 mps")
    return device


def split_episodes(episode_ids, validation_split=0.2, seed=7):
    """返回互不共享 episode 的索引；不足两个片段时不伪造验证集。"""
    episodes = sorted(set(episode_ids))
    if not episodes:
        raise ValueError("没有已标注训练样本；请先 collect-training，再用 annotate 标注")
    if validation_split and len(episodes) < 2:
        raise ValueError("按片段验证至少需要两个 episode；请分别采集两段，或显式设置 --validation-split 0")
    random.Random(seed).shuffle(episodes)
    count = min(len(episodes) - 1, max(1, math.ceil(len(episodes) * validation_split))) if validation_split else 0
    validation = set(episodes[:count])
    training = [i for i, episode in enumerate(episode_ids) if episode not in validation]
    held_out = [i for i, episode in enumerate(episode_ids) if episode in validation]
    return training, held_out


def multitask_loss(outputs, batch, config):
    """仅对存在人工/确认标签的任务计算损失；缺失标签不是零值标签。"""
    import torch
    import torch.nn.functional as F

    zero = outputs["controls"].sum() * 0
    losses = {name: zero for name in ("control", "buttons", "lane", "presence", "direction")}
    control_mask = batch["control_mask"].bool()
    if control_mask.any():
        losses["control"] = F.smooth_l1_loss(outputs["controls"][control_mask], batch["controls"][control_mask])
        losses["buttons"] = F.binary_cross_entropy_with_logits(outputs["button_logits"][control_mask], batch["buttons"][control_mask])
        losses["direction"] = F.cross_entropy(outputs["direction_logits"][control_mask], batch["direction"][control_mask].long())
    lane_mask = batch["lane_mask"].bool()
    if lane_mask.any():
        losses["lane"] = F.smooth_l1_loss(outputs["lane_points"][lane_mask], batch["lane_points"][lane_mask])
    presence_mask = batch["lane_label_mask"].bool()
    if presence_mask.any():
        losses["presence"] = F.binary_cross_entropy_with_logits(outputs["lane_presence_logit"][presence_mask], batch["lane_presence"][presence_mask])
    total = (config.control_weight * losses["control"] + config.button_weight * losses["buttons"]
             + config.direction_weight * losses["direction"] + config.lane_weight * losses["lane"]
             + config.presence_weight * losses["presence"])
    if not torch.isfinite(total):
        raise ValueError("训练损失出现 NaN/Inf，请检查数据及学习率")
    return total, losses


class BehaviorCloningTrainer:
    """可在 CPU/CUDA 上训练，保存可直接供 DrivingPolicy 加载的权重。"""

    def __init__(self, config: BehaviorCloningConfig):
        self.config = config

    def fit(self):
        import torch
        from torch.utils.data import DataLoader, Subset
        from src.data.dataset import DrivingDataset
        from src.data.preprocessing import ObservationPreprocessor
        from src.model import TransformerConfig
        from src.model.driving import DrivingModel, DrivingModelConfig

        config = self.config
        torch.manual_seed(config.seed)
        random.seed(config.seed)
        device = resolve_device(config.device)
        preprocessor = ObservationPreprocessor(config.image_size, config.history_frames)
        encoder = TransformerConfig(image_size=config.image_size, patch_size=config.patch_size, embed_dim=config.embed_dim,
                                    num_heads=config.num_heads, depth=config.depth, mlp_dim=config.mlp_dim,
                                    dropout=config.dropout, max_images=config.history_frames, pooling="mean")
        model_config = DrivingModelConfig(encoder=encoder)
        dataset = DrivingDataset(config.dataset_dir, preprocessor=preprocessor, lane_y=model_config.lane_y,
                                 history_frames=config.history_frames, allow_synthetic=config.allow_synthetic,
                                 allow_policy_labels=config.allow_policy_labels, allow_auto_lanes=config.allow_auto_lanes)
        train_indices, validation_indices = split_episodes(dataset.episode_ids, config.validation_split, config.seed)
        train_samples = [dataset.samples[i] for i in train_indices]
        lane_supervised_y = [y for y in model_config.lane_y if any(
            sample.get("lane") and sample["lane"].get("visible")
            and max(sample["lane"]["left"][0][1], sample["lane"]["right"][0][1]) <= y
            <= min(sample["lane"]["left"][-1][1], sample["lane"]["right"][-1][1])
            for sample in train_samples)]
        trained_tasks = []
        if any(sample.get("action") is not None for sample in train_samples):
            trained_tasks.extend(["control", "direction"])
        if any(sample.get("lane", {}).get("visible") for sample in train_samples if sample.get("lane") is not None):
            trained_tasks.append("lane")
        if not trained_tasks:
            raise ValueError("没有可训练的方向/操作或可见车道标签；请先标注可见车道边界")
        data_source = "synthetic" if any("synthetic" in (sample.get("source"), sample.get("action_source"), sample.get("lane_source")) for sample in dataset.all_samples) else "real"
        # 身份、标签和图片均参与指纹；恢复时不允许无声更换验证集或示范内容。
        fingerprint = hashlib.sha256()
        for sample in dataset.all_samples:
            fingerprint.update(json.dumps(sample, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            fingerprint.update((dataset.root / sample["image"]).read_bytes())
        dataset_fingerprint = fingerprint.hexdigest()
        split = {"training": sorted({dataset.episode_ids[i] for i in train_indices}),
                 "validation": sorted({dataset.episode_ids[i] for i in validation_indices})}
        output = Path(config.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        if not config.resume and any((output / name).exists() for name in ("last.pt", "best.pt", "history.jsonl")):
            raise ValueError("输出目录已包含训练结果；请更换 --output，或用 --resume 继续训练")
        model = DrivingModel(model_config).to(device)
        optimizer = torch.optim.AdamW(model.parameters(), lr=config.learning_rate)
        start_epoch, best_score = 0, math.inf
        model_id = "driving-" + uuid.uuid4().hex[:12]
        if config.resume:
            checkpoint = torch.load(config.resume, map_location="cpu", weights_only=True)
            if (checkpoint.get("schema_version") != 1 or checkpoint.get("model_config") != model_config.to_dict()
                    or checkpoint.get("preprocessing") != preprocessor.to_dict()
                    or checkpoint.get("data_source") != data_source
                    or set(checkpoint.get("trained_tasks", [])) != set(trained_tasks)
                    or checkpoint.get("split") != split
                    or checkpoint.get("dataset_fingerprint") != dataset_fingerprint):
                raise ValueError("恢复检查点的模型、预处理、标签任务或数据来源与当前训练参数不一致")
            model.load_state_dict(checkpoint["model_state"], strict=True)
            optimizer.load_state_dict(checkpoint["optimizer_state"])
            for group in optimizer.param_groups:
                group["lr"] = config.learning_rate
            start_epoch = int(checkpoint["epoch"])
            best_score = float(checkpoint.get("best_score", math.inf))
            model_id = checkpoint["model_id"]
            if "rng_state" in checkpoint:
                torch.set_rng_state(checkpoint["rng_state"])
            if not (output / "best.pt").exists():
                best_score = math.inf
        generator = torch.Generator().manual_seed(config.seed + start_epoch)
        common = {"batch_size": config.batch_size, "num_workers": config.num_workers, "pin_memory": device.type == "cuda"}
        training = DataLoader(Subset(dataset, train_indices), shuffle=True, generator=generator, **common)
        validation = DataLoader(Subset(dataset, validation_indices), shuffle=False, **common) if validation_indices else None
        amp_enabled = bool(config.amp and device.type == "cuda")
        scaler = torch.amp.GradScaler("cuda", enabled=amp_enabled)
        (output / "split.json").write_text(json.dumps(split, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        def epoch_pass(loader, *, train):
            model.train(train)
            totals = {name: 0.0 for name in ("loss", "control", "buttons", "lane", "presence", "direction")}
            denominators = {name: 0 for name in ("control", "buttons", "lane", "presence", "direction")}
            observed, controls, lane_coordinates, direction_correct = 0, 0, 0, 0
            axis_errors = torch.zeros(3, device=device)
            lane_error = 0.0
            with torch.set_grad_enabled(train):
                for batch in loader:
                    batch = {key: value.to(device) for key, value in batch.items() if isinstance(value, torch.Tensor)}
                    if train:
                        optimizer.zero_grad(set_to_none=True)
                    with torch.autocast(device_type=device.type, enabled=amp_enabled):
                        outputs = model(batch["images"], batch["image_mask"])
                        loss, losses = multitask_loss(outputs, batch, config)
                    if train:
                        scaler.scale(loss).backward()
                        scaler.unscale_(optimizer)
                        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0, error_if_nonfinite=True)
                        scaler.step(optimizer)
                        scaler.update()
                    size = batch["images"].shape[0]
                    observed += size
                    totals["loss"] += float(loss.detach()) * size
                    counts = {"control": int(batch["control_mask"].sum()), "buttons": int(batch["control_mask"].sum()),
                              "direction": int(batch["control_mask"].sum()), "lane": int(batch["lane_mask"].sum()),
                              "presence": int(batch["lane_label_mask"].sum())}
                    for name, value in losses.items():
                        totals[name] += float(value.detach()) * counts[name]
                        denominators[name] += counts[name]
                    selected = batch["control_mask"].bool()
                    if selected.any():
                        controls += int(selected.sum())
                        axis_errors += (outputs["controls"][selected] - batch["controls"][selected]).abs().detach().sum(0)
                        direction_correct += int((outputs["direction_logits"][selected].argmax(-1) == batch["direction"][selected]).sum())
                    selected_lane = batch["lane_mask"].bool()
                    if selected_lane.any():
                        error = (outputs["lane_points"][selected_lane] - batch["lane_points"][selected_lane]).abs().detach()
                        lane_error += float(error.sum())
                        lane_coordinates += error.numel()
            result = {key: totals[key] / denominators[key] if denominators[key] else 0.0 for key in denominators}
            result["loss"] = (config.control_weight * result["control"] + config.button_weight * result["buttons"]
                              + config.direction_weight * result["direction"] + config.lane_weight * result["lane"]
                              + config.presence_weight * result["presence"])
            result.update(steering_mae=float(axis_errors[0]) / controls if controls else None,
                          throttle_mae=float(axis_errors[1]) / controls if controls else None,
                          brake_mae=float(axis_errors[2]) / controls if controls else None,
                          direction_accuracy=direction_correct / controls if controls else None,
                          lane_mae=lane_error / lane_coordinates if lane_coordinates else None,
                          samples=observed)
            return result

        last_metrics = None
        for epoch in range(start_epoch + 1, start_epoch + config.epochs + 1):
            train_metrics = epoch_pass(training, train=True)
            validation_metrics = epoch_pass(validation, train=False) if validation is not None else None
            score = (validation_metrics or train_metrics)["loss"]
            improved = score < best_score
            best_score = min(best_score, score)
            last_metrics = {"epoch": epoch, "training": train_metrics, "validation": validation_metrics}
            checkpoint = {"schema_version": 1, "model_id": model_id, "model_config": model_config.to_dict(),
                          "preprocessing": preprocessor.to_dict(), "model_state": model.state_dict(),
                          "optimizer_state": optimizer.state_dict(), "rng_state": torch.get_rng_state(),
                          "epoch": epoch, "best_score": best_score, "metrics": last_metrics,
                          "trained_tasks": trained_tasks, "data_source": data_source, "split": split,
                          "lane_supervised_y": lane_supervised_y,
                          "lane_label_sources": sorted({sample.get("lane_source", "human") for sample in train_samples if sample.get("lane") is not None}),
                          "auto_lane_labels": any(str(sample.get("lane_source", "")).startswith("auto") for sample in train_samples if sample.get("lane") is not None),
                          "dataset_fingerprint": dataset_fingerprint,
                          "training_config": {key: str(value) if isinstance(value, Path) else value for key, value in asdict(config).items()}}
            for name in (["last.pt", "best.pt"] if improved else ["last.pt"]):
                temporary = output / (name + ".pending")
                torch.save(checkpoint, temporary)
                temporary.replace(output / name)
            with (output / "history.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(last_metrics, ensure_ascii=False, allow_nan=False) + "\n")
            print(json.dumps({"epoch": epoch, "train_loss": train_metrics["loss"],
                              "validation_loss": validation_metrics["loss"] if validation_metrics else None}, ensure_ascii=False), flush=True)
        return {"checkpoint": str(output / "best.pt"), "last_checkpoint": str(output / "last.pt"),
                "trained_tasks": trained_tasks, "data_source": data_source,
                "training_samples": len(train_indices), "validation_samples": len(validation_indices), "metrics": last_metrics}
