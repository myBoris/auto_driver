"""保存外部 AI 发布的最新感知；不推理，也不发送任何驾驶控制指令。

会话标识阻止重连前的结果进入新画面；可选帧号防止乱序回包覆盖新结果。
新鲜度仅使用中心接收时间，避免训练电脑与终端时钟不同造成误判。
"""

from dataclasses import dataclass
import math
import time
from time import monotonic

from ..domain.errors import APIError


MAX_AGE = 2.0
STATUSES = {"ready", "no_road", "unavailable", "waiting"}
SOURCES = {"model", "road_geometry", "external", "demo"}


def _number(value, low, high):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not low <= value <= high or not math.isfinite(value)):
        raise ValueError("invalid perception number")
    return value


def _points(value, minimum, maximum):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("invalid perception geometry")
    points = []
    for point in value:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("invalid perception point")
        points.append([_number(point[0], 0, 1), _number(point[1], 0, 1)])
    return points


def validate_result(value):
    """只存储已校验的公开字段，不接受无限几何或不明来源的方向值。"""
    if not isinstance(value, dict) or value.get("status", "ready") not in STATUSES:
        raise ValueError("invalid perception status")
    allowed = {"session_id", "frame_sequence", "captured_at", "status", "image", "road", "prediction", "model", "inference_ms"}
    if value.keys() - allowed:
        raise ValueError("unknown perception fields")
    image, model = value.get("image"), value.get("model")
    if not isinstance(image, dict) or not isinstance(model, dict):
        raise ValueError("missing perception metadata")
    dimensions = {}
    for key in ("width", "height"):
        dimension = image.get(key)
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 16384:
            raise ValueError("invalid perception image size")
        dimensions[key] = dimension
    model_info = {}
    for key, limit in (("id", 256), ("label", 160), ("revision", 128)):
        text = model.get(key, model.get("id") if key == "label" else None)
        if text is None and key == "revision":
            continue
        if not isinstance(text, str) or not 1 <= len(text) <= limit:
            raise ValueError("invalid perception model metadata")
        model_info[key] = text
    result = {"status": value.get("status", "ready"), "image": dimensions, "model": model_info,
              "inference_ms": (_number(value["inference_ms"], 0, 60000) if "inference_ms" in value else None),
              "road": None, "prediction": None}
    road, prediction = value.get("road"), value.get("prediction")
    if road is not None:
        if not isinstance(road, dict):
            raise ValueError("invalid road")
        polygon = _points(road.get("polygon"), 3, 256)
        centerline = _points(road.get("centerline"), 2, 128)
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(polygon, polygon[1:] + polygon[:1]))) / 2
        if area < 1e-6 or len({tuple(point) for point in centerline}) < 2:
            raise ValueError("degenerate perception geometry")
        result["road"] = {"polygon": polygon, "centerline": centerline,
                          "confidence": _number(road.get("confidence"), 0, 1)}
    if prediction is not None:
        if not isinstance(prediction, dict) or prediction.get("source", "model") not in SOURCES:
            raise ValueError("invalid prediction source")
        result["prediction"] = {"steering": _number(prediction.get("steering"), -1, 1),
                                "confidence": _number(prediction.get("confidence"), 0, 1),
                                "source": prediction.get("source", "model")}
    if result["status"] == "ready" and road is None and prediction is None:
        raise ValueError("ready result requires a road or prediction")
    if result["status"] == "no_road" and road is not None:
        raise ValueError("no_road result must not contain road geometry")
    if result["status"] in {"waiting", "unavailable"} and (road is not None or prediction is not None):
        raise ValueError("inactive result must not contain geometry or prediction")
    return result


@dataclass
class PublishedResult:
    session_id: str
    received_at: float
    received_mono: float
    frame_sequence: int | None
    captured_at: float | None
    data: dict


class PerceptionService:
    def __init__(self, terminals):
        self.terminals = terminals
        self.results = {}
        self.last_sequences = {}

    def envelope(self, terminal, published=None, *, status=None):
        age = max(0, (monotonic() - published.received_mono) * 1000) if published else None
        data = published.data if published and status is None else {"status": status or "waiting", "road": None, "prediction": None}
        return {**data, "terminal_id": terminal.terminal_id, "session_id": terminal.session_id,
                "received_at": published.received_at if published else None,
                "frame": ({"sequence": published.frame_sequence, "captured_at": published.captured_at,
                           "received_at": None} if published else None),
                "age_ms": age, "ttl_ms": MAX_AGE * 1000}

    def get(self, terminal_id):
        terminal = self.terminals.get(terminal_id)
        published = self.results.get(terminal_id)
        if published is None or published.session_id != terminal.session_id:
            self.results.pop(terminal_id, None)
            return self.envelope(terminal)
        if not terminal.connected or monotonic() - published.received_mono >= MAX_AGE:
            return self.envelope(terminal, published, status="stale")
        return self.envelope(terminal, published)

    def publish(self, terminal_id, value):
        terminal = self.terminals.get(terminal_id)
        if not terminal.connected:
            raise APIError(409, "terminal disconnected")
        session_id = value.get("session_id") if isinstance(value, dict) else None
        if not isinstance(session_id, str) or session_id != terminal.session_id:
            raise APIError(409, "perception session_id does not match the current terminal session")
        data = validate_result(value)
        sequence = value.get("frame_sequence")
        if sequence is not None:
            if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 2**53 - 1:
                raise ValueError("frame_sequence must be a positive safe integer")
            previous = self.last_sequences.get(terminal_id)
            if previous and previous[0] == session_id and sequence < previous[1]:
                raise APIError(409, "perception frame_sequence is older than the latest published frame")
        captured_at = value.get("captured_at")
        if captured_at is not None:
            captured_at = _number(captured_at, 1e-9, 1e20)
        published = PublishedResult(session_id, time.time(), monotonic(), sequence, captured_at, data)
        self.results[terminal_id] = published
        if sequence is not None:
            self.last_sequences[terminal_id] = (session_id, sequence)
        return self.envelope(terminal, published)
