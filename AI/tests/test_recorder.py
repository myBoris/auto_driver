"""验证落盘数据能够追溯帧、动作和确认，且不会覆盖已有样本。"""

import json
from pathlib import Path
import tempfile
import unittest

from src.data import EpisodeWriter
from src.domain import Action, Observation, StepResult

from fake_center import JPEG


def observation(sequence=1, *, captured_at=None, jpeg=JPEG):
    timestamp = float(sequence) if captured_at is None else captured_at
    return Observation(jpeg=jpeg, sequence=sequence, captured_at=timestamp, received_at=timestamp + 0.01)


class EpisodeWriterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def records(self, writer):
        return [json.loads(line) for line in writer.records_path.read_text(encoding="utf-8").splitlines()]

    def test_passive_episode_saves_real_bytes_metadata_and_no_invented_action_labels(self):
        obs = observation()
        with EpisodeWriter(self.root, "game-pc", "session-1") as writer:
            relative = writer.write_observation(obs)
            self.assertEqual(writer.write_observation(obs), relative)
            self.assertEqual((writer.episode_dir / relative).read_bytes(), JPEG)
        metadata = json.loads(writer.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "closed")
        self.assertEqual(metadata["record_count"], 1)
        self.assertEqual(metadata["action_labels"], "none")
        self.assertFalse(metadata["reward_available"])
        record, = self.records(writer)
        self.assertEqual(record["terminal_id"], "game-pc")
        self.assertEqual(record["session_id"], "session-1")
        self.assertEqual(record["observation"]["sequence"], 1)
        self.assertEqual(record["observation"]["captured_at"], obs.captured_at)
        self.assertFalse(record["labeled"])
        self.assertNotIn("action", record)

    def test_transition_references_present_frames_and_preserves_ack_reward_provenance(self):
        first, next_obs = observation(1), observation(2)
        action = Action(steering=-0.4, throttle=0.2, buttons=("A",), duration_ms=100)
        ack = {"type": "ack", "ok": True, "command_id": "command-1", "applied_at": 1.1,
               "state": action.to_payload()}
        result = StepResult(next_obs, "command-1", ack)
        with EpisodeWriter(self.root, "game-pc", "session-1", mode="rollout") as writer:
            writer.write_transition(first, action, result, reward=1.5, terminated=True)
        record, = self.records(writer)
        self.assertEqual(record["action"], action.to_payload())
        self.assertEqual(record["ack"], ack)
        self.assertEqual(record["command_id"], result.command_id)
        self.assertEqual(record["reward"], 1.5)
        self.assertTrue(record["terminated"])
        self.assertFalse(record["truncated"])
        self.assertEqual(record["action_source"], "policy_proposed")
        for field, obs in (("observation", first), ("next_observation", next_obs)):
            relative = record[field]["path"]
            target = writer.episode_dir / relative
            self.assertTrue(target.resolve().is_relative_to(writer.episode_dir.resolve()))
            self.assertEqual(target.read_bytes(), obs.jpeg)
            self.assertEqual(record[field]["sequence"], obs.sequence)
        metadata = json.loads(writer.meta_path.read_text(encoding="utf-8"))
        self.assertTrue(metadata["reward_available"])
        self.assertEqual(metadata["action_labels"], "policy_proposed")

    def test_sequence_collision_is_rejected_without_overwriting_existing_frame(self):
        first = observation()
        with EpisodeWriter(self.root, "game-pc", "session-1") as writer:
            relative = writer.write_observation(first)
            for conflicting in (observation(captured_at=5), observation(jpeg=b"\xff\xd8different\xff\xd9")):
                with self.assertRaisesRegex(ValueError, "序号"):
                    writer.write_observation(conflicting)
            self.assertEqual((writer.episode_dir / relative).read_bytes(), first.jpeg)
        self.assertEqual(len(self.records(writer)), 1)

    def test_separate_runs_never_reuse_episode_or_overwrite_old_frames(self):
        with EpisodeWriter(self.root, "game-pc", "old-session") as old:
            old.write_observation(observation())
        with EpisodeWriter(self.root, "game-pc", "new-session") as new:
            new.write_observation(observation())
        self.assertNotEqual(old.episode_dir, new.episode_dir)
        self.assertEqual((old.frames_dir / "1.jpg").read_bytes(), JPEG)
        self.assertEqual((new.frames_dir / "1.jpg").read_bytes(), JPEG)

    def test_exception_keeps_completed_data_and_marks_episode_interrupted(self):
        with self.assertRaisesRegex(RuntimeError, "sampling stopped"):
            with EpisodeWriter(self.root, "game-pc", "session-1") as writer:
                writer.write_observation(observation())
                raise RuntimeError("sampling stopped")
        metadata = json.loads(writer.meta_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["status"], "interrupted")
        self.assertEqual(metadata["record_count"], 1)
        self.assertEqual(len(self.records(writer)), 1)
        with self.assertRaises(RuntimeError):
            writer.write_observation(observation(2))


if __name__ == "__main__":
    unittest.main()
