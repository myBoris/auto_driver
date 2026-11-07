"""加载联合训练检查点，输出道路几何、转向预测与完整手柄动作。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import io
from pathlib import Path
import time
from typing import Any

from src.domain import Action, Observation
from src.domain.types import finite_number


@dataclass(frozen=True)
class DrivingPrediction:
    """同一次推理的显示结果与操作；无操作监督的模型不会生成驾驶动作。"""

    action: Action | None
    road: dict[str, Any] | None
    prediction: dict[str, Any] | None
    image_width: int
    image_height: int
    inference_ms: float

    def to_perception(self, observation: Observation, *, session_id: str,
                      model_id: str, source: str = "model") -> dict[str, Any]:
        """使用原始画面坐标，叠加通道不会替代动作接口。"""
        prediction = {**self.prediction, "source": source} if self.prediction else None
        return {
            "session_id": session_id, "frame_sequence": observation.sequence,
            "captured_at": observation.captured_at,
            "status": "ready" if self.road is not None or prediction is not None else "no_road",
            "image": {"width": self.image_width, "height": self.image_height},
            "road": self.road, "prediction": prediction,
            "model": {"id": f"demo-{model_id}" if source == "demo" else model_id,
                      "label": "合成样本训练验证" if source == "demo" else model_id},
            "inference_ms": self.inference_ms,
        }


class DrivingPolicy:
    """训练与推理共用预处理；检查点必须明确包含受监督训练的任务。

    torch 与模型在实例化时才导入，查看 CLI 帮助或只采集数据不申请 GPU。
    合成数据训练的检查点只支持预测预览，不能作为真实终端驾驶策略。
    """

    def __init__(self, checkpoint: Path | None = None, device: str = "auto", *,
                 duration_ms: int = 100, lane_threshold: float = .5,
                 button_threshold: float = .5, max_throttle: float = 1,
                 max_inference_seconds: float = 1.0) -> None:
        if checkpoint is None:
            raise ValueError("请提供 train-bc 生成的 --checkpoint 文件")
        Action(duration_ms=duration_ms)
        if duration_ms == 0:
            raise ValueError("驾驶模型的 duration_ms 必须大于 0")
        self.duration_ms = duration_ms
        self.lane_threshold = finite_number(lane_threshold, "lane_threshold", 0, 1)
        self.button_threshold = finite_number(button_threshold, "button_threshold", 0, 1)
        self.max_throttle = finite_number(max_throttle, "max_throttle", 0, 1)
        self.max_inference_seconds = finite_number(max_inference_seconds, "max_inference_seconds", .01, 30)

        import torch
        from src.data.preprocessing import ObservationPreprocessor
        from src.model.driving import DrivingModel, DrivingModelConfig

        self._torch = torch
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            self.device = torch.device(device)
        except (ValueError, RuntimeError) as exc:
            raise ValueError("device 必须为 auto、cpu 或可用的 PyTorch 设备") from exc
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("当前 PyTorch 环境无法使用 CUDA")
        self.checkpoint = Path(checkpoint)
        payload = torch.load(self.checkpoint, map_location="cpu", weights_only=True)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("不支持的驾驶检查点格式")
        if not {"model_config", "preprocessing", "model_state"}.issubset(payload):
            raise ValueError("检查点缺少模型权重、结构或预处理配置")
        if not isinstance(payload["model_state"], dict):
            raise ValueError("检查点的 model_state 必须为 PyTorch 权重字典")
        tasks = payload.get("trained_tasks")
        if (not isinstance(tasks, (list, tuple)) or not tasks
                or any(task not in {"lane", "control", "direction"} for task in tasks)):
            raise ValueError("检查点必须声明已经训练的 lane/control/direction 任务")
        if "direction" in tasks and "control" not in tasks:
            raise ValueError("方向预测检查点缺少转向监督任务")
        if payload.get("data_source") not in {"real", "synthetic"}:
            raise ValueError("检查点缺少真实或合成训练数据来源")
        if isinstance(payload.get("epoch"), bool) or not isinstance(payload.get("epoch"), int) or payload["epoch"] < 1:
            raise ValueError("检查点尚未完成有效训练轮次")
        self.model_id = payload.get("model_id")
        if not isinstance(self.model_id, str) or not self.model_id or len(self.model_id) > 128:
            raise ValueError("检查点的 model_id 必须是 1～128 字符")
        self.trained_tasks = frozenset(tasks)
        self.data_source = payload["data_source"]
        self.source = "demo" if self.data_source == "synthetic" else "model"
        self.config = DrivingModelConfig.from_dict(payload["model_config"])
        supervised_y = payload.get("lane_supervised_y", list(self.config.lane_y))
        if (not isinstance(supervised_y, (list, tuple))
                or any(isinstance(y, bool) or not isinstance(y, (int, float))
                       or y not in self.config.lane_y for y in supervised_y)
                or len(set(supervised_y)) != len(supervised_y)
                or ("lane" in self.trained_tasks and len(supervised_y) < 2)):
            raise ValueError("检查点车道监督锚点必须是模型 lane_y 的子集，且车道任务至少需要两个锚点")
        # 未获得任何车道标注的远近位置不能由随机输出补成可用道路几何。
        self.lane_indices = tuple(index for index, y in enumerate(self.config.lane_y) if y in supervised_y)
        self.preprocessor = ObservationPreprocessor.from_dict(payload["preprocessing"])
        if (self.preprocessor.image_size != self.config.encoder.image_size
                or self.preprocessor.history_frames != self.config.encoder.max_images):
            raise ValueError("检查点的图像预处理与模型尺寸或历史窗口不一致")
        self.model = DrivingModel(self.config)
        self.model.load_state_dict(payload["model_state"], strict=True)
        self.model.to(self.device).eval()
        self._history: deque[Observation] = deque(maxlen=self.preprocessor.history_frames)

    def require_executable(self) -> None:
        """显示预览与驾驶使用相同权重，但驾驶需要真实操作监督。"""
        if "control" not in self.trained_tasks:
            raise ValueError("该检查点没有驾驶操作标签训练，只能预测显示")
        if self.data_source != "real":
            raise ValueError("合成样本检查点只能预览；驾驶需要真实 Center 数据和操作标签训练")

    def reset_state(self) -> None:
        """开始新片段时显式清空历史，不拼接不同会话的画面。"""
        self._history.clear()

    def preprocess(self, observation: Observation):
        """返回 [1,N,3,H,W] 像素及 [1,N] 掩码；前部补零不构造假历史帧。"""
        if not isinstance(observation, Observation):
            raise TypeError("推理需要 Center Observation")
        if self._history:
            previous = self._history[-1]
            if observation.sequence <= previous.sequence or observation.captured_at < previous.captured_at:
                raise ValueError("观察序号或时间回退，请重置策略并开始新的会话片段")
        self._history.append(observation)
        torch = self._torch
        images = self.preprocessor.transform(list(self._history))
        count = len(self._history)
        wanted = self.preprocessor.history_frames
        if images.ndim != 4 or images.shape[0] != count:
            raise ValueError("预处理必须返回仅包含有效历史帧的 [N,3,H,W] 张量")
        padding = wanted - count
        if padding:
            images = torch.cat((torch.zeros((padding, *images.shape[1:]), dtype=images.dtype), images), dim=0)
        mask = torch.tensor([False] * padding + [True] * count, dtype=torch.bool)
        return images.unsqueeze(0).to(self.device), mask.unsqueeze(0).to(self.device)

    def predict_result(self, observation: Observation) -> DrivingPrediction:
        """只返回模型实算结果，不发网络请求；置信度来自已训练任务的输出概率。"""
        started = time.perf_counter()
        torch = self._torch
        from PIL import Image

        with Image.open(io.BytesIO(observation.jpeg)) as image:
            width, height = image.size
        images, mask = self.preprocess(observation)
        with torch.inference_mode():
            output = self.model(images, image_mask=mask)
            for value in output.values():
                if not torch.isfinite(value).all().item():
                    raise RuntimeError("模型输出包含非有限数值，未生成驾驶操作")
            controls = output["controls"][0].detach().cpu().tolist()
            button_probs = torch.sigmoid(output["button_logits"][0]).detach().cpu().tolist()
            lanes = output["lane_points"][0].detach().cpu().tolist()
            presence = float(torch.sigmoid(output["lane_presence_logit"].reshape(-1)[0]).cpu())
            direction_probs = torch.softmax(output["direction_logits"][0], dim=-1).detach().cpu().tolist()

        action = None
        prediction = None
        if "control" in self.trained_tasks:
            action = Action(steering=controls[0], throttle=min(controls[1], self.max_throttle),
                            brake=controls[2], duration_ms=self.duration_ms,
                            buttons=tuple(name for name, probability in zip(self.config.button_names, button_probs)
                                          if probability >= self.button_threshold))
            if "direction" in self.trained_tasks:
                category = 0 if action.steering < -.08 else 2 if action.steering > .08 else 1
                prediction = {"steering": action.steering, "confidence": direction_probs[category]}
        road = None
        if "lane" in self.trained_tasks and presence >= self.lane_threshold:
            left = [[float(lanes[index][0]), float(self.config.lane_y[index])] for index in self.lane_indices]
            right = [[float(lanes[index][1]), float(self.config.lane_y[index])] for index in self.lane_indices]
            road = {"polygon": left + right[::-1],
                    "centerline": [[(a[0] + b[0]) / 2, a[1]] for a, b in zip(left, right)],
                    "confidence": presence}
        return DrivingPrediction(action, road, prediction, width, height,
                                 (time.perf_counter() - started) * 1000)

    def predict(self, observation: Observation) -> Action:
        """兼容 rollout Policy 协议，仅支持真实数据训练过的驾驶任务。"""
        self.require_executable()
        result = self.predict_result(observation)
        if result.action is None:
            raise RuntimeError("模型未返回驾驶操作")
        return result.action
