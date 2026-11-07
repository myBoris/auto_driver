"""真实单张道路预标注 HTTP 边界；不连接 Center，不执行驾驶操作。"""

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image, ImageDraw

from src.data.annotations import create_annotation_app
from src.data.dataset import load_samples, write_samples


def road_jpeg():
    image = Image.new("RGB", (640, 400), (40, 80, 110))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 100, 640, 400), fill=(35, 70, 25))
    draw.polygon([(90, 320), (300, 108), (340, 108), (550, 320)], fill=(55, 55, 55))
    draw.line([(90, 320), (300, 108)], fill=(245, 245, 235), width=6)
    draw.line([(340, 108), (550, 320)], fill=(245, 245, 235), width=6)
    output = BytesIO()
    image.save(output, format="JPEG", quality=95)
    return output.getvalue()


class AutoAnnotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "road.jpg").write_bytes(road_jpeg())
        self.sample = {"schema_version": 1, "id": "captured-1", "episode_id": "session-1",
                       "sequence": 1, "image": "road.jpg", "source": "center",
                       "terminal_id": "game-pc", "session_id": "connection-1",
                       "action": {"steering": -.2, "throttle": .3, "brake": 0,
                                  "buttons": [], "duration_ms": 200}, "action_source": "center_confirmed"}
        write_samples(self.root, [self.sample])
        self.client = TestClient(TestServer(create_annotation_app(self.root)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_auto_candidate_has_provenance_confidence_and_unreviewed_state_without_changing_action_or_image(self):
        before_image = (self.root / "road.jpg").read_bytes()
        response = await self.client.post("/api/samples/0/auto-lane", json={})
        self.assertEqual(response.status, 200, await response.text())
        body = await response.json()
        self.assertTrue(body["detected"])
        saved = load_samples(self.root)[0]
        self.assertEqual(saved["lane_source"], "auto")
        self.assertTrue(saved["lane"]["visible"])
        self.assertFalse(saved["lane_annotation"]["reviewed"])
        self.assertGreaterEqual(saved["lane_annotation"]["confidence"], .75)
        self.assertEqual(saved["lane_annotation"]["method"], "opencv-color-hough-v1")
        self.assertEqual(saved["lane_annotation"]["roi_top"], .4)
        self.assertEqual(saved["lane_annotation"]["roi_bottom"], .7)
        self.assertEqual(saved["action"], self.sample["action"])
        self.assertEqual(saved["action_source"], "center_confirmed")
        self.assertEqual((self.root / "road.jpg").read_bytes(), before_image)
        self.assertFalse((self.root / ".collection.lock").exists())
        self.assertFalse(list(self.root.glob("samples-*.tmp")))

    async def test_weak_or_missing_candidate_keeps_original_unlabeled_sample_instead_of_inventing_no_road(self):
        image = Image.new("RGB", (640, 400), (60, 60, 60))
        image.save(self.root / "road.jpg")
        before_manifest = (self.root / "samples.jsonl").read_bytes()
        response = await self.client.post("/api/samples/0/auto-lane", json={})
        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body["detected"])
        self.assertIn("未检出", body["message"])
        self.assertNotIn("lane", body["sample"])
        self.assertEqual((self.root / "samples.jsonl").read_bytes(), before_manifest)

    async def test_manual_review_becomes_human_provenance_and_auto_cannot_overwrite_it(self):
        auto = await self.client.post("/api/samples/0/auto-lane", json={})
        candidate = (await auto.json())["sample"]
        lane = candidate["lane"]
        lane["left"][0][0] += .01
        saved = await self.client.post("/api/samples/0", json={"lane": lane, "action": candidate["action"]})
        self.assertEqual(saved.status, 200, await saved.text())
        reviewed = await saved.json()
        self.assertEqual(reviewed["lane_source"], "human")
        self.assertTrue(reviewed["lane_annotation"]["reviewed"])
        self.assertEqual(reviewed["lane_annotation"]["confidence"], candidate["lane_annotation"]["confidence"])
        before = (self.root / "samples.jsonl").read_bytes()
        response = await self.client.post("/api/samples/0/auto-lane", json={})
        self.assertEqual(response.status, 409)
        self.assertEqual((self.root / "samples.jsonl").read_bytes(), before)

    async def test_existing_human_and_no_road_annotations_are_protected(self):
        for lane in ({"visible": False}, {"visible": True, "left": [[.3, .45], [.2, .65]],
                                          "right": [[.7, .45], [.8, .65]]}):
            with self.subTest(lane=lane):
                write_samples(self.root, [{**self.sample, "lane": lane, "lane_source": "human"}])
                before = (self.root / "samples.jsonl").read_bytes()
                response = await self.client.post("/api/samples/0/auto-lane", json={})
                self.assertEqual(response.status, 409)
                self.assertEqual((self.root / "samples.jsonl").read_bytes(), before)

    async def test_roi_and_minimum_confidence_are_validated_and_change_the_detection(self):
        for config in ({"roi_top": .8, "roi_bottom": .7}, {"roi_top": -.1},
                       {"min_confidence": 1.1}, {"min_confidence": True}, {"overwrite": True},
                       {"roi_top": .45, "roi_bottom": .49}):
            with self.subTest(config=config):
                response = await self.client.post("/api/samples/0/auto-lane", json=config)
                self.assertEqual(response.status, 400, await response.text())
        response = await self.client.post("/api/samples/0/auto-lane", json={"min_confidence": 1})
        self.assertFalse((await response.json())["detected"])
        self.assertNotIn("lane", load_samples(self.root)[0])
        response = await self.client.post("/api/samples/0/auto-lane", json={"roi_top": .4, "roi_bottom": .7, "min_confidence": .75})
        self.assertTrue((await response.json())["detected"])

    async def test_foreign_origin_collection_lock_and_invalid_index_never_modify_data(self):
        before = (self.root / "samples.jsonl").read_bytes()
        response = await self.client.post("/api/samples/0/auto-lane", json={}, headers={"Origin": "http://foreign.invalid"})
        self.assertEqual(response.status, 403)
        lock = self.root / ".collection.lock"
        lock.write_text("another collector")
        response = await self.client.post("/api/samples/0/auto-lane", json={})
        self.assertEqual(response.status, 409)
        self.assertEqual(lock.read_text(), "another collector")
        lock.unlink()
        response = await self.client.post("/api/samples/999/auto-lane", json={})
        self.assertEqual(response.status, 404)
        self.assertFalse(lock.exists())
        self.assertEqual((self.root / "samples.jsonl").read_bytes(), before)

    async def test_page_explains_proposals_and_exposes_configuration_without_a_driving_control(self):
        response = await self.client.get("/")
        page = await response.text()
        for text in ("自动预标注当前图片", "未复核", "auto-confidence", "auto-top", "auto-bottom", "/auto-lane"):
            self.assertIn(text, page)
        self.assertNotIn("/api/terminals", page)
        self.assertNotIn("/actions", page)


if __name__ == "__main__":
    unittest.main()
