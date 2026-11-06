"""中立策略只用于验证观察、动作确认和数据记录链路，不表示已训练驾驶能力。"""

from src.domain import Action, Observation


class NeutralPolicy:
    """始终返回零油门、零刹车、居中转向和空按键，不主动驱动车辆。"""

    def __init__(self, duration_ms: int = 100) -> None:
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 0 <= duration_ms <= 5000:
            raise ValueError("duration_ms 必须为 0～5000 的整数")
        self.duration_ms = duration_ms

    def predict(self, observation: Observation) -> Action:
        """观察参数保留在统一接口中；联调策略不读取图像内容。"""
        return Action(steering=0.0, throttle=0.0, brake=0.0, buttons=(), duration_ms=self.duration_ms)
