"""用道路标线的颜色和透视结构生成待复核候选；不生成驾驶操作或车道真值。"""

from dataclasses import asdict, dataclass
import math
import os
from pathlib import Path

from .dataset import DEFAULT_LANE_Y, image_path, load_samples, validate_lane, write_samples


@dataclass(frozen=True)
class AutoLaneConfig:
    """ROI 采用原图归一化高度，驾驶舱视角可缩小底边以避开车盖。"""

    min_confidence: float = .75
    roi_top: float = .4
    roi_bottom: float = .7

    def __post_init__(self):
        for name in ("min_confidence", "roi_top", "roi_bottom"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"{name} 必须是 0–1 范围的有限数值")
        if self.roi_bottom - self.roi_top < .1:
            raise ValueError("车道 ROI 高度至少为原图的 10%")
        if sum(self.roi_top <= y <= self.roi_bottom for y in DEFAULT_LANE_Y) < 2:
            raise ValueError("车道 ROI 至少需要覆盖两个训练锚点")


class AutoLaneLabeler:
    """只接受成对、收敛、受图像边缘支持的标线；遮挡和纹理不足时返回 None。"""

    def __init__(self, config: AutoLaneConfig | None = None):
        self.config = config or AutoLaneConfig()
        if not isinstance(self.config, AutoLaneConfig):
            raise TypeError("config 必须是 AutoLaneConfig")

    def annotate(self, jpeg: bytes) -> dict | None:
        """confidence 是几何/边缘一致性分数，不是模型概率或人工准确率。"""
        try:
            import cv2
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("自动车道预标注需要安装 opencv-python 和 numpy") from exc

        if not isinstance(jpeg, bytes) or not jpeg:
            raise ValueError("车道预标注需要非空 JPEG bytes")
        image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None or min(image.shape[:2]) < 64:
            raise ValueError("无法解码车道预标注图片，或图片小于 64 像素")
        original_height, original_width = image.shape[:2]
        if original_width > 960:
            image = cv2.resize(image, (960, round(original_height * 960 / original_width)), interpolation=cv2.INTER_AREA)
        height, width = image.shape[:2]
        top, bottom = self.config.roi_top, self.config.roi_bottom
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        white = cv2.inRange(hsv, np.array((0, 0, 145)), np.array((179, 85, 255)))
        yellow = cv2.inRange(hsv, np.array((12, 70, 120)), np.array((42, 255, 255)))
        markings = cv2.bitwise_or(white, yellow)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edge = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 45, 120)
        edge = cv2.bitwise_and(edge, cv2.dilate(markings, np.ones((5, 5), np.uint8)))
        edge[:math.floor(top * height)] = 0
        edge[math.ceil(bottom * height):] = 0
        # 画面最外缘常为仪表、车窗和 HUD，不能作为车道标线支持。
        edge[:, :round(width * .03)] = 0
        edge[:, round(width * .97):] = 0
        segments = cv2.HoughLinesP(edge, 1, np.pi / 180, threshold=max(12, round(width * .025)),
                                 minLineLength=max(16, round(height * .055)), maxLineGap=max(8, round(height * .035)))
        if segments is None:
            return None
        candidates = {"left": [], "right": []}
        for x1, y1, x2, y2 in segments.reshape(-1, 4):
            x1, y1, x2, y2 = x1 / width, y1 / height, x2 / width, y2 / height
            if abs(y2 - y1) < .04:
                continue
            slope = (x2 - x1) / (y2 - y1)
            intercept = x1 - slope * y1
            if not .12 <= abs(slope) <= 2.5:
                continue
            near = slope * bottom + intercept
            far = slope * top + intercept
            if not -.03 <= far <= 1.03 or not .03 <= near <= .97:
                continue
            side = "left" if slope < 0 and .03 < near < .55 else "right" if slope > 0 and .45 < near < .97 else None
            if side:
                candidates[side].append((slope, intercept, ((x1, y1), (x2, y2))))
        left = self._fit(candidates["left"], top, bottom, np)
        right = self._fit(candidates["right"], top, bottom, np)
        if left is None or right is None:
            return None
        la, lb, left_y0, left_y1, left_quality = left
        ra, rb, right_y0, right_y1, right_quality = right
        supported_top = max(top, left_y0, right_y0)
        supported_bottom = min(bottom, left_y1, right_y1)
        # 不将少量可见线段外推到整幅图，训练也只监督真正受支持的锚点。
        anchors = sorted(y for y in DEFAULT_LANE_Y if supported_top <= y <= supported_bottom)
        if len(anchors) < 2:
            return None
        lane_left = [[round(la * y + lb, 6), y] for y in anchors]
        lane_right = [[round(ra * y + rb, 6), y] for y in anchors]
        near_width = (ra - la) * supported_bottom + rb - lb
        far_width = (ra - la) * supported_top + rb - lb
        near_center = ((ra + la) * supported_bottom + rb + lb) / 2
        if (not .12 <= near_width <= .92 or not .01 <= far_width < near_width * .93
                or not .25 <= near_center <= .75):
            return None
        vanishing_y = (lb - rb) / (ra - la)
        vanishing_x = la * vanishing_y + lb
        if not .1 <= vanishing_y <= supported_top or not .12 <= vanishing_x <= .88:
            return None
        support_left = self._edge_support(edge, la, lb, supported_top, supported_bottom, width, height, np)
        support_right = self._edge_support(edge, ra, rb, supported_top, supported_bottom, width, height, np)
        support = min(support_left, support_right)
        if support < .45:
            return None
        coverage = min(1., (supported_bottom - supported_top) / (bottom - top))
        fit_quality = min(left_quality, right_quality)
        confidence = min(.99, .4 * support + .35 * coverage + .25 * fit_quality)
        if confidence < self.config.min_confidence:
            return None
        try:
            lane = validate_lane({"visible": True, "left": lane_left, "right": lane_right})
        except ValueError:
            return None
        return {"lane": lane, "lane_source": "auto", "lane_annotation": {
            "method": "opencv-color-hough-v1", "confidence": round(confidence, 6), "reviewed": False,
            "roi_top": top, "roi_bottom": bottom, "min_confidence": self.config.min_confidence,
            "image_width": original_width, "image_height": original_height,
            "edge_support": round(support, 6), "vertical_coverage": round(coverage, 6)}}

    @staticmethod
    def _fit(candidates, top, bottom, np):
        if not candidates:
            return None
        best, best_score = None, 0.
        probes = (top, (top + bottom) / 2, bottom)
        for slope, intercept, _ in candidates:
            # RANSAC 风格的线段共识，减轻车辆轮廓、护栏和另一车道的干扰。
            inliers = [candidate for candidate in candidates
                       if max(abs((candidate[0] - slope) * y + candidate[1] - intercept) for y in probes) < .035]
            score = sum(abs(points[1][1] - points[0][1]) for _, _, points in inliers)
            if score > best_score:
                best, best_score = inliers, score
        points = np.array([point for _, _, pair in best for point in pair], dtype=np.float64)
        ys, xs = points[:, 1], points[:, 0]
        if float(ys.max() - ys.min()) < .1:
            return None
        slope, intercept = np.linalg.lstsq(np.stack((ys, np.ones_like(ys)), axis=1), xs, rcond=None)[0]
        residual = float(np.mean(np.abs(xs - (slope * ys + intercept))))
        quality = max(0., 1 - residual / .025)
        return float(slope), float(intercept), float(ys.min()), float(ys.max()), quality

    @staticmethod
    def _edge_support(edge, slope, intercept, top, bottom, width, height, np):
        hits = 0
        samples = 64
        for y in np.linspace(top, bottom, samples):
            x = round((slope * y + intercept) * width)
            row = round(y * height)
            radius = max(3, round(width * .006))
            if edge[max(0, row - 2):min(height, row + 3), max(0, x - radius):min(width, x + radius + 1)].any():
                hits += 1
        return hits / samples


def annotate_dataset(dataset_dir: Path, config: AutoLaneConfig | None = None, *, limit: int | None = None) -> dict:
    """为未标注图像追加自动候选；已有人工、合成和自动标签都保持原样。"""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit 必须是正整数或 None")
    root = Path(dataset_dir).resolve()
    try:
        descriptor = os.open(root / ".collection.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("数据正在采集或标注，请等待后再批量预标注") from exc
    try:
        os.close(descriptor)
        records = load_samples(root)
        labeler = AutoLaneLabeler(config)
        summary = {"total": len(records), "scanned": 0, "annotated": 0, "skipped_existing": 0,
                   "low_confidence": 0, "errors": 0, "method": "opencv-color-hough-v1"}
        for record in records:
            if record.get("lane") is not None or record.get("source") == "synthetic":
                summary["skipped_existing"] += 1
                continue
            if limit is not None and summary["scanned"] >= limit:
                break
            summary["scanned"] += 1
            try:
                annotation = labeler.annotate(image_path(root, record["image"]).read_bytes())
            except ValueError:
                summary["errors"] += 1
                continue
            if annotation is None:
                summary["low_confidence"] += 1
                continue
            record.update(annotation)
            summary["annotated"] += 1
        if summary["annotated"]:
            write_samples(root, records)
        return summary
    finally:
        (root / ".collection.lock").unlink(missing_ok=True)
