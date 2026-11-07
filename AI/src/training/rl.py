"""由真实游戏反馈驱动的在线 PPO，不需要人工动作标签或预先采集的数据。"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import random
import sys
import threading
import time
import uuid


@dataclass(frozen=True)
class ReinforcementLearningConfig:
    output_dir: Path
    total_steps: int = 10000
    device: str = "auto"
    rollout_steps: int = 128
    batch_size: int = 32
    update_epochs: int = 4
    learning_rate: float = .0003
    gamma: float = .99
    gae_lambda: float = .95
    clip_ratio: float = .2
    entropy_coef: float = .01
    value_coef: float = .5
    max_grad_norm: float = .5
    target_kl: float = .03
    image_size: int = 84
    history_frames: int = 4
    max_steering: float = .7
    max_throttle: float = .35
    max_brake: float = .7
    action_duration_ms: int = 100
    checkpoint_interval: int = 1000
    seed: int = 7
    resume: Path | None = None

    def __post_init__(self):
        for name in ("total_steps", "rollout_steps", "batch_size", "update_epochs", "image_size",
                     "history_frames", "action_duration_ms", "checkpoint_interval"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        if not 64 <= self.image_size <= 256 or self.history_frames > 10:
            raise ValueError("image_size 必须为 64–256，history_frames 最多为 10")
        if not 50 <= self.action_duration_ms <= 1000:
            raise ValueError("action_duration_ms 必须为 50–1000 毫秒，与在线环境的动作窗口一致")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or not 0 <= self.seed < 2**32:
            raise ValueError("seed 必须是 0–2**32-1 的整数")
        for name in ("learning_rate", "gamma", "gae_lambda", "clip_ratio", "entropy_coef", "value_coef",
                     "max_grad_norm", "target_kl", "max_steering", "max_throttle", "max_brake"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数值")
        for name in ("gamma", "gae_lambda", "max_steering", "max_throttle", "max_brake"):
            if not 0 < getattr(self, name) <= 1:
                raise ValueError(f"{name} 必须在 (0,1] 内")
        if not 0 < self.clip_ratio < 1 or not 0 < self.learning_rate < 1:
            raise ValueError("clip_ratio 和 learning_rate 必须在 (0,1) 内")
        if self.entropy_coef < 0 or self.value_coef <= 0 or self.max_grad_norm <= 0 or self.target_kl <= 0:
            raise ValueError("entropy_coef 不能为负，value_coef/max_grad_norm/target_kl 必须为正")
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        if self.resume is not None:
            object.__setattr__(self, "resume", Path(self.resume))


def compute_gae(rewards, values, next_values, terminated, truncated, gamma=.99, gae_lambda=.95):
    """超时截断仍从最终观察 bootstrap，但优势不跨越重置后的 episode。"""
    import torch

    rewards, values, next_values = [torch.as_tensor(item, dtype=torch.float32) for item in
                                   (rewards, values, next_values)]
    terminated, truncated = [torch.as_tensor(item, dtype=torch.bool) for item in (terminated, truncated)]
    if rewards.ndim != 1 or any(item.shape != rewards.shape for item in
                               (values, next_values, terminated, truncated)) or not len(rewards):
        raise ValueError("GAE 需要长度一致的非空一维转移序列")
    if not all(bool(torch.isfinite(item).all()) for item in (rewards, values, next_values)):
        raise ValueError("GAE 转移包含 NaN/Inf")
    advantages = torch.zeros_like(rewards)
    running = torch.tensor(0., device=rewards.device)
    for index in reversed(range(len(rewards))):
        delta = rewards[index] + gamma * next_values[index] * (~terminated[index]) - values[index]
        running = delta + gamma * gae_lambda * (~(terminated[index] | truncated[index])) * running
        advantages[index] = running
    return advantages, advantages + values


class ReinforcementLearningTrainer:
    """有界 rollout、GAE、clipped PPO 与原子恢复检查点；游戏 I/O 保持异步。"""

    def __init__(self, config: ReinforcementLearningConfig):
        self.config = config
        self.stop_event = threading.Event()
        self.model = None
        self.optimizer = None
        self.steps = self.updates = self.episodes = 0
        self.best_score = None
        self.model_id = "ppo-" + uuid.uuid4().hex[:12]
        self.last_metrics = {}
        self._last_saved = 0
        self._initialized = False

    def request_stop(self):
        self.stop_event.set()

    @property
    def action_config(self):
        return {name: getattr(self.config, name) for name in
                ("max_steering", "max_throttle", "max_brake", "action_duration_ms")}

    async def _thread(self, function, *args):
        # 取消时先等线程退出，避免写出一半更新中的权重/优化器状态。
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.stop_event.set()
            try:
                await asyncio.shield(task)
            except Exception:
                pass
            raise

    def _initialize(self):
        import torch
        from src.model.rl import ActorCriticConfig, RLActorCritic
        from .bc import resolve_device

        self.device = resolve_device(self.config.device)
        self.model_config = ActorCriticConfig(image_size=self.config.image_size,
                                              history_frames=self.config.history_frames)
        random.seed(self.config.seed)
        torch.manual_seed(self.config.seed)
        self.model = RLActorCritic(self.model_config).to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.config.learning_rate, eps=1e-5)
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        if self.config.resume is None:
            if (self.config.output_dir / "latest.pt").exists():
                raise ValueError("输出目录已有 PPO 权重；请使用 resume 或新的 output_dir")
            self._initialized = True
            return
        payload = torch.load(self.config.resume, map_location="cpu", weights_only=True)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("algorithm") != "ppo":
            raise ValueError("resume 必须是 PPO 检查点")
        if payload.get("model_config") != self.model_config.to_dict() or payload.get("action_config") != self.action_config:
            raise ValueError("resume 的观察形状或动作界限与本次配置不一致")
        for name in ("steps", "updates", "episodes"):
            value = payload.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"resume 中的 {name} 无效")
            setattr(self, name, value)
        if self.steps < 1:
            raise ValueError("resume 检查点没有真实交互记录")
        self.model.load_state_dict(payload["model_state"], strict=True)
        self.optimizer.load_state_dict(payload["optimizer_state"])
        for group in self.optimizer.param_groups:
            group["lr"] = self.config.learning_rate
        self.best_score = payload.get("best_score")
        if self.best_score is not None and not math.isfinite(self.best_score):
            raise ValueError("resume 中的 best_score 无效")
        self.model_id = payload["model_id"]
        self.last_metrics = payload.get("metrics", {})
        torch.set_rng_state(payload["rng_state"])
        random.setstate(payload["python_rng_state"])
        if self.device.type == "cuda" and payload.get("cuda_rng_state") is not None:
            torch.cuda.set_rng_state_all(payload["cuda_rng_state"])
        self._last_saved = self.steps
        self._initialized = True

    def _sample(self, observation):
        import torch
        from src.model.rl import latent_to_action, observation_tensors

        images, telemetry = observation_tensors(observation, self.model_config, self.device)
        self.model.eval()
        with torch.inference_mode():
            latent, log_probability, value = self.model.sample(images, telemetry)
        if not all(bool(torch.isfinite(item).all()) for item in (latent, log_probability, value)):
            raise ValueError("PPO 采样产生 NaN/Inf")
        action = latent_to_action(latent[0], **self.action_config)
        return {"images": images[0].cpu().clone(), "telemetry": telemetry[0].cpu().clone(),
                "latent": latent[0].cpu().clone(), "log_probability": float(log_probability[0]),
                "value": float(value[0]), "action": action}

    def _value(self, observation):
        import torch
        from src.model.rl import observation_tensors

        images, telemetry = observation_tensors(observation, self.model_config, self.device)
        with torch.inference_mode():
            value = self.model(images, telemetry)[2]
        if not bool(torch.isfinite(value).all()):
            raise ValueError("PPO value 产生 NaN/Inf")
        return float(value[0])

    def _update(self, rollout):
        import torch
        import torch.nn.functional as F

        self.model.train()
        advantages, returns = compute_gae(
            [item["reward"] for item in rollout], [item["value"] for item in rollout],
            [item["next_value"] for item in rollout], [item["terminated"] for item in rollout],
            [item["truncated"] for item in rollout], self.config.gamma, self.config.gae_lambda)
        if len(rollout) > 1:
            advantages = (advantages - advantages.mean()) / (advantages.std(unbiased=False) + 1e-8)
        # 回放在 CPU 上有界保存；每个小批次单独传入显卡。
        images = torch.stack([item["images"] for item in rollout])
        telemetry = torch.stack([item["telemetry"] for item in rollout])
        latent = torch.stack([item["latent"] for item in rollout])
        old_log = torch.tensor([item["log_probability"] for item in rollout])
        metrics = {name: [] for name in ("policy_loss", "value_loss", "entropy", "approx_kl", "clip_fraction", "grad_norm")}
        minibatches = 0
        early_stop = False
        for _ in range(self.config.update_epochs):
            if early_stop or self.stop_event.is_set():
                break
            order = torch.randperm(len(rollout))
            for start in range(0, len(order), self.config.batch_size):
                if self.stop_event.is_set():
                    break
                indices = order[start:start + self.config.batch_size]
                new_log, entropy, values = self.model.evaluate(images[indices].to(self.device),
                    telemetry[indices].to(self.device), latent[indices].to(self.device))
                log_ratio = new_log - old_log[indices].to(self.device)
                ratio = log_ratio.exp()
                advantage = advantages[indices].to(self.device)
                policy_loss = -torch.minimum(ratio * advantage,
                    ratio.clamp(1 - self.config.clip_ratio, 1 + self.config.clip_ratio) * advantage).mean()
                value_loss = .5 * F.mse_loss(values, returns[indices].to(self.device))
                entropy_mean = entropy.mean()
                approximate_kl = ((ratio - 1) - log_ratio).mean().clamp_min(0)
                loss = policy_loss + self.config.value_coef * value_loss - self.config.entropy_coef * entropy_mean
                if not all(bool(torch.isfinite(item).all()) for item in (loss, ratio, approximate_kl)):
                    raise ValueError("PPO loss/ratio 出现 NaN/Inf，已停止更新")
                # 达到 KL 上限时当前小批次不再更新；已有优化器步骤仍被完整保存。
                if float(approximate_kl.detach()) > 1.5 * self.config.target_kl:
                    early_stop = True
                    break
                self.optimizer.zero_grad(set_to_none=True)
                loss.backward()
                grad_norm = torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.config.max_grad_norm,
                                                           error_if_nonfinite=True)
                self.optimizer.step()
                minibatches += 1
                measurements = (policy_loss, value_loss, entropy_mean, approximate_kl,
                                ((ratio - 1).abs() > self.config.clip_ratio).float().mean(), grad_norm)
                for name, measurement in zip(metrics, measurements):
                    metrics[name].append(float(measurement.detach()))
        if minibatches:
            self.updates += 1
        return {**{name: sum(items) / len(items) if items else 0. for name, items in metrics.items()},
                "minibatches": minibatches, "early_stop_kl": early_stop,
                "advantage_mean": float(advantages.mean()), "return_mean": float(returns.mean())}

    def _save(self, status, metrics=None, best=False):
        import torch

        if not self._initialized or self.model is None or not self.steps:
            return
        config = asdict(self.config)
        config["output_dir"] = str(config["output_dir"])
        config["resume"] = str(config["resume"]) if config["resume"] is not None else None
        payload = {"schema_version": 1, "algorithm": "ppo", "model_id": self.model_id,
                   "model_config": self.model_config.to_dict(), "action_config": self.action_config,
                   "training_config": config, "model_state": self.model.state_dict(),
                   "optimizer_state": self.optimizer.state_dict(), "steps": self.steps,
                   "updates": self.updates, "episodes": self.episodes, "best_score": self.best_score,
                   "metrics": self.last_metrics, "rng_state": torch.get_rng_state(),
                   "python_rng_state": random.getstate(), "cuda_rng_state":
                   torch.cuda.get_rng_state_all() if self.device.type == "cuda" else None,
                   "status": status, "saved_at": time.time()}
        names = ["latest.pt"]
        if best:
            names.append("best.pt")
        if self.steps // self.config.checkpoint_interval > self._last_saved // self.config.checkpoint_interval:
            names.append(f"checkpoint-{self.steps:09d}.pt")
        for name in names:
            target = self.config.output_dir / name
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                torch.save(payload, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        self._last_saved = self.steps
        state = {"algorithm": "ppo", "status": status, "steps": self.steps, "updates": self.updates,
                 "episodes": self.episodes, "model_id": self.model_id, "best_training_reward": self.best_score,
                 "metrics": self.last_metrics, "updated_at": payload["saved_at"],
                 "update_behavior": "release_inputs_then_refresh_observation_and_reward_baseline"}
        target = self.config.output_dir / "status.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
        if metrics is not None:
            with (self.config.output_dir / "metrics.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(metrics, ensure_ascii=False, allow_nan=False) + "\n")

    async def train(self, env):
        """env 提供 reset/step/pause/refresh/close；total_steps 是包括恢复记录的累计目标。"""
        status = "error"
        episode_return = 0.
        try:
            for hook in ("reset", "step", "pause", "refresh", "close"):
                if not callable(getattr(env, hook, None)):
                    raise ValueError(f"PPO 环境缺少 {hook} 方法")
            await self._thread(self._initialize)
            if self.steps >= self.config.total_steps:
                status = "complete"
                return self.result(status)
            observation, _ = await env.reset()
            while self.steps < self.config.total_steps and not self.stop_event.is_set():
                rollout = []
                completed_returns = []
                for _ in range(min(self.config.rollout_steps, self.config.total_steps - self.steps)):
                    if self.stop_event.is_set():
                        break
                    sample = await self._thread(self._sample, observation)
                    next_observation, reward, terminated, truncated, info = await env.step(sample.pop("action"))
                    if (isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward)
                            or not isinstance(terminated, bool) or not isinstance(truncated, bool)
                            or not isinstance(info, dict)):
                        raise ValueError("环境必须返回有限 reward、布尔终止标记与 info 字典")
                    # 倒带/瞬移后的画面不是动作的连续后继，不能用于 bootstrap。
                    next_value = (0. if terminated or info.get("bootstrap_allowed") is False
                                  else await self._thread(self._value, next_observation))
                    rollout.append({**sample, "reward": float(reward), "next_value": next_value,
                                    "terminated": terminated, "truncated": truncated})
                    self.steps += 1
                    episode_return += reward
                    observation = next_observation
                    if terminated or truncated:
                        self.episodes += 1
                        completed_returns.append(episode_return)
                        episode_return = 0.
                        if info.get("needs_manual_reset"):
                            raise RuntimeError("环境需要人工复位；已停止控制，请复位后 resume")
                        if (self.steps < self.config.total_steps and len(rollout) < self.config.rollout_steps
                                and not self.stop_event.is_set()):
                            observation, _ = await env.reset()
                if not rollout:
                    break
                # 游戏在梯度计算期间仍继续运行。先松开本会话控制，后续观察重新同步，
                # 不把计算期间的滑行错误归因于下一次动作。
                await env.pause()
                optimization = await self._thread(self._update, rollout)
                score = sum(item["reward"] for item in rollout) / len(rollout)
                improved = self.updates > 0 and (self.best_score is None or score > self.best_score)
                if improved:
                    self.best_score = score
                self.last_metrics = {**optimization, "steps": self.steps, "updates": self.updates,
                    "rollout_steps": len(rollout), "reward_mean": score,
                    "reward_sum": sum(item["reward"] for item in rollout), "episodes": self.episodes,
                    "episode_return_mean": sum(completed_returns) / len(completed_returns) if completed_returns else None,
                    "score_source": "training_rollout_not_independent_evaluation"}
                await self._thread(self._save, "training", self.last_metrics, improved)
                print(json.dumps({"event": "ppo_update", "steps": self.steps, "updates": self.updates,
                    "episodes": self.episodes, "reward_mean": score,
                    "policy_loss": optimization["policy_loss"], "value_loss": optimization["value_loss"],
                    "approx_kl": optimization["approx_kl"]}, allow_nan=False), flush=True)
                if self.steps < self.config.total_steps and not self.stop_event.is_set():
                    # 一个 rollout 恰在 episode 边界结束时仍应执行环境复位逻辑。
                    if rollout[-1]["terminated"] or rollout[-1]["truncated"]:
                        observation, _ = await env.reset()
                    else:
                        observation, _ = await env.refresh()
            status = "complete" if self.steps >= self.config.total_steps else "stopped"
            return self.result(status)
        except asyncio.CancelledError:
            status = "interrupted"
            raise
        finally:
            # 先释放终端，再保存；即使磁盘故障也不能遗留油门/方向输入。
            original_error = sys.exc_info()[1]
            close_error = None
            try:
                if callable(getattr(env, "close", None)):
                    await env.close()
            except BaseException as exc:
                close_error = exc
                status = "error"
            try:
                if self._initialized and self.steps:
                    await self._thread(self._save, status)
            except BaseException:
                if original_error is None and close_error is None:
                    raise
            if close_error is not None and original_error is None:
                raise close_error

    def result(self, status):
        return {"status": status, "algorithm": "ppo", "steps": self.steps, "updates": self.updates,
                "episodes": self.episodes, "model_id": self.model_id,
                "checkpoint": str(self.config.output_dir / "latest.pt"), "metrics": self.last_metrics}
