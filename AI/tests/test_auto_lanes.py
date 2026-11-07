"""自动预标注必须有成对视觉支持，并保留伪标签来源、人工标签与动作掩码。"""

import copy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from src.data.auto_lanes import AutoLaneConfig, AutoLaneLabeler, annotate_dataset
from src.data.dataset import DEFAULT_LANE_Y, DrivingDataset, load_samples, write_samples


def road_image(*, left=True, right=True, horizontal=False):
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 360), (55, 63, 67))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 640, 130), fill=(90, 125, 155))
    if horizontal:
        for y in (160, 200, 240):
            draw.line((20, y, 620, y), fill=(245, 245, 230), width=6)
    else:
        if left:
            draw.line((288, 144, 128, 270), fill=(245, 245, 235), width=6)
        if right:
            draw.line((352, 144, 512, 270), fill=(245, 245, 235), width=6)
    stream = BytesIO()
    image.save(stream, format="JPEG", quality=94)
    return stream.getvalue()


class AutoLaneTests(unittest.TestCase):
    def test_clear_paired_markings_produce_reviewable_candidates_with_supported_anchors(self):
        result = AutoLaneLabeler().annotate(road_image())
        self.assertIsNotNone(result)
        self.assertEqual(result["lane_source"], "auto")
        metadata = result["lane_annotation"]
        self.assertEqual(metadata["method"], "opencv-color-hough-v1")
        self.assertFalse(metadata["reviewed"])
        self.assertGreaterEqual(metadata["confidence"], .75)
        lane = result["lane"]
        self.assertTrue(lane["visible"])
        self.assertGreaterEqual(len(lane["left"]), 2)
        for (left, ly), (right, ry) in zip(lane["left"], lane["right"]):
            self.assertEqual(ly, ry)
            self.assertIn(ly, DEFAULT_LANE_Y)
            self.assertTrue(.4 <= ly <= .7)
            self.assertLess(left, right)
        self.assertNotIn("action", result, "road markings cannot generate demonstration controls")

    def test_blank_one_sided_and_horizontal_edges_do_not_become_no_road_labels(self):
        for image in (road_image(left=False, right=False), road_image(right=False),
                      road_image(left=False), road_image(horizontal=True)):
            with self.subTest(image_length=len(image)):
                self.assertIsNone(AutoLaneLabeler().annotate(image))

    def test_roi_threshold_and_jpeg_validation_are_explicit(self):
        self.assertIsNone(AutoLaneLabeler(AutoLaneConfig(min_confidence=1)).annotate(road_image()))
        for kwargs in ({"roi_top": .7, "roi_bottom": .4}, {"roi_top": .51, "roi_bottom": .61},
                       {"min_confidence": True}, {"min_confidence": float("nan")}, {"roi_bottom": 1.1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                AutoLaneConfig(**kwargs)
        for jpeg in (b"", b"not-a-jpeg"):
            with self.assertRaises(ValueError):
                AutoLaneLabeler().annotate(jpeg)


class AutoLaneDatasetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.root.joinpath("frame.jpg").write_bytes(road_image())
        self.record = {"schema_version": 1, "id": "sample-1", "episode_id": "episode-1",
                       "image": "frame.jpg", "sequence": 1, "source": "center",
                       "action_source": "center_confirmed", "action": {"steering": .2, "throttle": .4,
                          "brake": 0, "buttons": [], "duration_ms": 100}}

    def test_automatic_lanes_are_excluded_by_default_without_discarding_control_labels(self):
        automatic = AutoLaneLabeler().annotate(road_image())
        write_samples(self.root, [{**self.record, **automatic}])
        dataset = DrivingDataset(self.root)
        self.assertEqual(dataset.label_counts, {"control": 1, "direction": 1, "lane": 0})
        self.assertTrue(dataset[0]["control_mask"].item())
        self.assertFalse(dataset[0]["lane_label_mask"].item())
        self.assertEqual(dataset.all_samples[0]["lane_source"], "auto")
        self.assertIsNotNone(dataset.all_samples[0]["lane"])
        opted_in = DrivingDataset(self.root, allow_auto_lanes=True)
        self.assertEqual(opted_in.label_counts["lane"], 1)
        self.assertTrue(opted_in[0]["lane_label_mask"].item())
        self.assertGreaterEqual(opted_in[0]["lane_mask"].sum().item(), 2)

    def test_automatic_only_records_wait_for_opt_in_and_reviewed_human_labels_are_trusted(self):
        automatic = AutoLaneLabeler().annotate(road_image())
        record = {**self.record, **automatic}
        record.pop("action")
        write_samples(self.root, [record])
        self.assertEqual(len(DrivingDataset(self.root)), 0)
        self.assertEqual(len(DrivingDataset(self.root, allow_auto_lanes=True)), 1)
        record["lane_source"] = "human"
        record["lane_annotation"]["reviewed"] = True
        write_samples(self.root, [record])
        self.assertEqual(DrivingDataset(self.root).label_counts["lane"], 1)

    def test_auto_metadata_is_required_and_bad_candidate_sources_cannot_be_silently_trusted(self):
        base = {**self.record, **AutoLaneLabeler().annotate(road_image())}
        for changes in ({"lane_annotation": None}, {"lane_source": "unknown"},
                        {"lane_annotation": {"method": "test", "confidence": 1.1, "reviewed": False}},
                        {"lane_annotation": {"method": "test", "confidence": .9, "reviewed": "false"}},
                        {"lane": None}):
            with self.subTest(changes=changes):
                write_samples(self.root, [{**copy.deepcopy(base), **changes}])
                with self.assertRaises(ValueError):
                    load_samples(self.root)

    def test_batch_fills_only_missing_lanes_preserves_actions_and_uses_collection_lock(self):
        rows = []
        auto = AutoLaneLabeler().annotate(road_image())
        for index in range(4):
            row = {**copy.deepcopy(self.record), "id": f"sample-{index + 1}", "sequence": index + 1}
            if index == 1:
                row.update(lane={"visible": False}, lane_source="human")
            if index == 2:
                row.update(**copy.deepcopy(auto))
            if index == 3:
                row["source"] = "synthetic"
                row["episode_id"] = "synthetic-1"
            rows.append(row)
        write_samples(self.root, rows)
        before_image = self.root.joinpath("frame.jpg").read_bytes()
        summary = annotate_dataset(self.root)
        self.assertEqual(summary["scanned"], 1)
        self.assertEqual(summary["annotated"], 1)
        self.assertEqual(summary["skipped_existing"], 3)
        saved = load_samples(self.root)
        self.assertEqual(saved[0]["lane_source"], "auto")
        self.assertEqual(saved[0]["action"], rows[0]["action"])
        self.assertEqual(saved[1:], rows[1:])
        self.assertEqual(self.root.joinpath("frame.jpg").read_bytes(), before_image)
        lock = self.root / ".collection.lock"
        self.assertFalse(lock.exists())
        lock.touch()
        with self.assertRaises(RuntimeError):
            annotate_dataset(self.root)
        self.assertTrue(lock.exists(), "a conflicting process must not delete the owner's lock")

    def test_low_confidence_and_invalid_jpeg_leave_original_manifest_untouched(self):
        self.root.joinpath("frame.jpg").write_bytes(road_image(right=False))
        write_samples(self.root, [self.record])
        before = (self.root / "samples.jsonl").read_bytes()
        summary = annotate_dataset(self.root)
        self.assertEqual(summary["low_confidence"], 1)
        self.assertEqual(summary["annotated"], 0)
        self.assertEqual((self.root / "samples.jsonl").read_bytes(), before)
        self.root.joinpath("frame.jpg").write_bytes(b"broken")
        summary = annotate_dataset(self.root)
        self.assertEqual(summary["errors"], 1)
        self.assertNotIn("lane", load_samples(self.root)[0])


if __name__ == "__main__":
    unittest.main()
