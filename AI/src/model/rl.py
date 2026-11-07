"""PPO 图像与真实车辆遥测的 Actor-Critic，不使用人工驾驶动作标签。"""

from dataclasses import asdict, dataclass
import math
from pathlib import Path

import torch
from torch import Tensor, nn
from torch.distributions import Normal

from src.domain import Action


@dataclass(frozen=True)
class ActorCriticConfig:
    image_size: int = 84
    history_frames: int = 4
    telemetry_dim: int = 6
    hidden_dim: int = 256

    def __post_init__(self):
        for name in ("image_size", "history_frames", "telemetry_dim", "hidden_dim"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须为正整数")
        if not 64 <= self.image_size <= 256 or self.history_frames > 10:
            raise ValueError("RL 图像尺寸须为 64–256，历史窗口最多 10 帧")

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("RL 模型配置必须为字典")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError("RL 模型配置字段无效") from exc


def observation_tensors(observation, config: ActorCriticConfig, device="cpu"):
    """只接受有限归一化像素及显式遥测向量，不用零向量冒充缺失遥测。"""
    if not isinstance(observation, dict) or "images" not in observation or "telemetry" not in observation:
        raise ValueError("RL 观察必须提供 images 和真实 telemetry")
    images = torch.as_tensor(observation["images"], dtype=torch.float32)
    telemetry = torch.as_tensor(observation["telemetry"], dtype=torch.float32)
    if tuple(images.shape) == (config.history_frames, 3, config.image_size, config.image_size):
        images = images.reshape(config.history_frames * 3, config.image_size, config.image_size)
    if tuple(images.shape) != (config.history_frames * 3, config.image_size, config.image_size):
        raise ValueError("RL 图片窗口尺寸与模型配置不一致")
    if tuple(telemetry.shape) != (config.telemetry_dim,):
        raise ValueError("RL 真实遥测向量缺失或维度不一致")
    if (not bool(torch.isfinite(images).all()) or not bool(torch.isfinite(telemetry).all())
            or not bool(((images >= 0) & (images <= 1)).all())):
        raise ValueError("RL 观察包含非有限数据，或像素未归一化到 [0,1]")
    return images.unsqueeze(0).to(device), telemetry.unsqueeze(0).to(device)


class RLActorCritic(nn.Module):
    """CNN 提取四帧视觉变化，遥测 MLP 提供真实速度与车辆运动状态。"""

    def __init__(self, config: ActorCriticConfig | None = None):
        super().__init__()
        self.config = config or ActorCriticConfig()
        self.visual = nn.Sequential(
            nn.Conv2d(self.config.history_frames * 3, 32, 8, stride=4), nn.ReLU(),
            nn.Conv2d(32, 64, 4, stride=2), nn.ReLU(),
            nn.Conv2d(64, 64, 3), nn.ReLU(), nn.AdaptiveAvgPool2d((3, 3)), nn.Flatten(),
        )
        self.telemetry_encoder = nn.Sequential(nn.Linear(self.config.telemetry_dim, 32), nn.Tanh())
        self.shared = nn.Sequential(nn.Linear(64 * 3 * 3 + 32, self.config.hidden_dim), nn.Tanh())
        self.actor = nn.Linear(self.config.hidden_dim, 2)
        self.critic = nn.Linear(self.config.hidden_dim, 1)
        self.log_std = nn.Parameter(torch.full((2,), -.7))
        nn.init.orthogonal_(self.actor.weight, gain=.01)
        nn.init.zeros_(self.actor.bias)
        nn.init.orthogonal_(self.critic.weight, gain=1.)
        nn.init.zeros_(self.critic.bias)

    def forward(self, images: Tensor, telemetry: Tensor):
        features = self.shared(torch.cat((self.visual(images), self.telemetry_encoder(telemetry)), dim=-1))
        mean = self.actor(features)
        std = self.log_std.clamp(-5., 1.).exp().expand_as(mean)
        return mean, std, self.critic(features).squeeze(-1)

    def sample(self, images, telemetry, deterministic=False):
        mean, std, value = self(images, telemetry)
        distribution = Normal(mean, std)
        latent = mean if deterministic else distribution.sample()
        return latent, self.log_probability(distribution, latent), value

    @staticmethod
    def log_probability(distribution, latent):
        # 固定 tanh 变换的 Jacobian；动作缩放常数在 PPO 新旧概率比中抵消。
        correction = 2. * (torch.log(torch.tensor(2., device=latent.device)) - latent
                            - torch.nn.functional.softplus(-2. * latent))
        return (distribution.log_prob(latent) - correction).sum(-1)

    def evaluate(self, images, telemetry, latent):
        mean, std, value = self(images, telemetry)
        distribution = Normal(mean, std)
        log_probability = self.log_probability(distribution, latent)
        # 潜空间高斯熵作为探索正则，动作有界由 tanh 映射保证。
        return log_probability, distribution.entropy().sum(-1), value


def latent_to_action(latent, *, max_steering=.7, max_throttle=.35, max_brake=.7, action_duration_ms=100):
    for name, cap in (("max_steering", max_steering), ("max_throttle", max_throttle), ("max_brake", max_brake)):
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not math.isfinite(cap) or not 0 < cap <= 1:
            raise ValueError(f"{name} 必须在 (0,1] 内")
    if (isinstance(action_duration_ms, bool) or not isinstance(action_duration_ms, int)
            or not 50 <= action_duration_ms <= 1000):
        raise ValueError("action_duration_ms 必须是 50–1000 的整数")
    values = torch.as_tensor(latent).reshape(-1)
    if values.numel() != 2 or not bool(torch.isfinite(values).all()):
        raise ValueError("RL 动作必须是两个有限潜变量")
    steering, pedal = values.tanh().detach().cpu().tolist()
    return Action(steering=steering * max_steering, throttle=max(0., pedal) * max_throttle,
                  brake=max(0., -pedal) * max_brake, duration_ms=action_duration_ms)


class RLPolicy:
    """载入独立 PPO 检查点供评估，默认采用均值动作且不更新权重。"""

    def __init__(self, checkpoint: Path, device="auto"):
        from src.training.bc import resolve_device

        self.device = resolve_device(device)
        payload = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if (not isinstance(payload, dict) or payload.get("schema_version") != 1
                or payload.get("algorithm") != "ppo" or isinstance(payload.get("steps"), bool)
                or not isinstance(payload.get("steps"), int) or payload["steps"] < 1
                or isinstance(payload.get("updates"), bool) or not isinstance(payload.get("updates"), int)
                or payload["updates"] < 1):
            raise ValueError("需要完成真实交互和至少一轮优化的 PPO 检查点")
        self.config = ActorCriticConfig.from_dict(payload["model_config"])
        self.model_config = self.config
        self.model = RLActorCritic(self.config)
        self.model.load_state_dict(payload["model_state"], strict=True)
        self.model.to(self.device).eval()
        self.action_config = payload["action_config"]
        if not isinstance(self.action_config, dict) or set(self.action_config) != {
                "max_steering", "max_throttle", "max_brake", "action_duration_ms"}:
            raise ValueError("PPO 动作配置无效")
        latent_to_action([0., 0.], **self.action_config)
        self.model_id = payload["model_id"]
        self.steps = payload["steps"]

    def predict_details(self, observation, deterministic=True):
        images, telemetry = observation_tensors(observation, self.config, self.device)
        with torch.inference_mode():
            latent, log_probability, value = self.model.sample(images, telemetry, deterministic)
        if not all(bool(torch.isfinite(item).all()) for item in (latent, log_probability, value)):
            raise ValueError("PPO 模型输出非有限数据")
        action = latent_to_action(latent[0], **self.action_config)
        return action, {"latent": latent[0].cpu().tolist(), "log_probability": float(log_probability[0]),
                        "value": float(value[0]), "steps": self.steps}

    def predict(self, observation, deterministic=True):
        return self.predict_details(observation, deterministic)[0]
