"""生成带明确来源标记的离线合成道路，仅用于验证代码，不代表游戏驾驶能力。"""

import math
from pathlib import Path
import random

from .dataset import DEFAULT_LANE_Y, write_samples


def generate_synthetic_dataset(output_dir: Path, episodes=4, frames_per_episode=16, seed=7):
    """新目录中生成可复现 JPEG 和完整标签；不会覆盖已有数据集。"""
    from PIL import Image, ImageDraw

    for name, value in (("episodes", episodes), ("frames_per_episode", frames_per_episode)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} 必须是正整数")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("合成数据输出目录必须为空，避免覆盖已有采集数据")
    rng = random.Random(seed)
    samples = []
    width, height = 320, 192
    for episode in range(episodes):
        episode_id = f"synthetic-{episode + 1:04d}"
        directory = root / episode_id
        directory.mkdir()
        phase = rng.uniform(-math.pi, math.pi)
        for frame in range(frames_per_episode):
            steering = math.sin(phase + frame * .35) * .75
            throttle = .25 + .55 * (1 - abs(steering))
            brake = max(0., abs(steering) - .55)
            left, right = [], []
            for y in DEFAULT_LANE_Y:
                depth = (.95 - y) / .5
                center = .5 + steering * .15 * depth * depth
                half_width = .32 - .26 * depth
                left.append([round(center - half_width, 6), y])
                right.append([round(center + half_width, 6), y])
            image = Image.new("RGB", (width, height), (60 + episode * 7 % 40, 110, 150))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, height * .44, width, height), fill=(40, 85 + episode * 5 % 30, 35))
            polygon = [(int(x * width), int(y * height)) for x, y in left + right[::-1]]
            draw.polygon(polygon, fill=(65, 65, 68))
            for points in (left, right):
                draw.line([(int(x * width), int(y * height)) for x, y in points], fill=(235, 235, 215), width=3)
            # 小图标让油门、刹车和按钮也能建立可学习的视觉关系。
            draw.rectangle((8, 7, 8 + int(90 * throttle), 14), fill=(40, 210, 90))
            draw.rectangle((8, 19, 8 + int(90 * brake), 26), fill=(220, 65, 40))
            buttons = ["LB"] if frame % 8 == 0 else ["B"] if frame % 8 == 1 else ["X"] if frame % 8 == 2 else []
            if buttons:
                draw.text((width - 36, 7), buttons[0], fill=(255, 220, 110))
            relative = f"{episode_id}/{frame + 1:06d}.jpg"
            image.save(root / relative, format="JPEG", quality=92)
            samples.append({"schema_version": 1, "id": f"{episode_id}-{frame + 1}",
                            "episode_id": episode_id, "image": relative, "sequence": frame + 1,
                            "source": "synthetic", "action_source": "synthetic", "lane_source": "synthetic",
                            "action": {"steering": steering, "throttle": throttle, "brake": brake,
                                       "buttons": buttons, "duration_ms": 100},
                            "lane": {"visible": True, "left": left, "right": right}})
    write_samples(root, samples)
    return len(samples)
