"""PPO 策略独立执行；复用与训练一致的遥测前置检查和环境恢复。"""

import asyncio
import math


async def run_rl_policy(environment, policy, *, steps=300):
    if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
        raise ValueError("steps 必须为正整数")
    completed = episodes = 0
    total_reward = 0.
    try:
        observation, _ = await environment.reset()
        for _ in range(steps):
            action = await asyncio.to_thread(policy.predict, observation, deterministic=True)
            observation, reward, terminated, truncated, info = await environment.step(action)
            if not isinstance(reward, (int, float)) or isinstance(reward, bool) or not math.isfinite(reward):
                raise ValueError("RL 环境返回无效奖励")
            total_reward += float(reward)
            completed += 1
            if terminated or truncated:
                episodes += 1
                if completed < steps:
                    observation, _ = await environment.reset()
        return {"steps": completed, "episodes": episodes, "total_reward": total_reward}
    finally:
        await environment.close()
