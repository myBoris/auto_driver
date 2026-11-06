"""通过本地假中心验证采集与 rollout 时序，避免用实现镜像替代业务行为。"""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from src.domain import Action
from src.integrations.center import CenterClient
from src.services import collect_observations, resolve_terminal, run_rollout

from fake_center import FakeCenter, TOKEN


class RecordingPolicy:
    def __init__(self):
        self.observed_sequences = []

    def predict(self, observation):
        self.observed_sequences.append(observation.sequence)
        return Action(throttle=0.2, duration_ms=100)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await FakeCenter().start()
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)

    async def asyncTearDown(self):
        await self.server.close()

    def records(self):
        return [json.loads(line) for path in Path(self.output.name).rglob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines() if line]

    async def test_passive_collection_reads_fresh_sequences_without_control_posts(self):
        self.server.auto_frame_sequence = True
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            count = await collect_observations(client, terminal, Path(self.output.name), frames=3)
        self.assertEqual(count, 3)
        self.assertEqual(len(self.records()), 3)
        frames = [request for request in self.server.requests if request["path"].endswith("/frame")]
        self.assertEqual(len(frames), 3)
        self.assertNotIn("after", frames[0]["query"])
        self.assertEqual(frames[1]["query"]["after"], "2")
        self.assertEqual(frames[2]["query"]["after"], "3")
        self.assertFalse(any(request["method"] == "POST" for request in self.server.requests))

    async def test_rollout_starts_with_observation_then_uses_each_next_observation(self):
        policy = RecordingPolicy()
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client, "game-pc")
            count = await run_rollout(client, terminal, policy, Path(self.output.name), steps=2)
        self.assertEqual(count, 2)
        self.assertEqual(policy.observed_sequences, [1, 2])
        paths = [request["path"].rsplit("/", 1)[-1] for request in self.server.requests]
        self.assertLess(paths.index("frame"), paths.index("step"))
        self.assertEqual(paths.count("step"), 2)
        self.assertEqual(paths[-1], "release")
        self.assertEqual(len(self.records()), 2)
        for request in self.server.requests:
            if request["path"].endswith("/step"):
                self.assertEqual(request["body"]["action"]["throttle"], 0.2)
                self.assertEqual(request["body"]["action"]["duration_ms"], 100)

    async def test_policy_error_preserved_even_if_release_fails(self):
        class BrokenPolicy:
            def predict(self, observation):
                raise LookupError("policy failed")

        self.server.response_status["release"] = 502
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            with self.assertRaisesRegex(LookupError, "policy failed"):
                await run_rollout(client, terminal, BrokenPolicy(), Path(self.output.name), steps=2)
        control = [request["path"].rsplit("/", 1)[-1] for request in self.server.requests
                   if request["method"] == "POST"]
        self.assertEqual(control, ["release"])

    async def test_cancelled_step_releases_input_before_cancellation_returns(self):
        self.server.step_gate = asyncio.Event()
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            task = asyncio.create_task(run_rollout(
                client, terminal, RecordingPolicy(), Path(self.output.name), steps=2,
            ))
            await asyncio.wait_for(self.server.step_started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
        controls = [request["path"].rsplit("/", 1)[-1] for request in self.server.requests
                    if request["method"] == "POST"]
        self.assertEqual(controls, ["step", "release"])
        self.assertEqual(self.records(), [])

    async def test_ambiguous_terminal_selection_never_sends_action(self):
        self.server.response_overrides["terminals"] = {"terminals": [
            {"terminal_id": "one", "session_id": "s1", "connected": True},
            {"terminal_id": "two", "session_id": "s2", "connected": True},
        ]}
        async with CenterClient(self.server.url, TOKEN) as client:
            with self.assertRaises((ValueError, RuntimeError)):
                await resolve_terminal(client)
        self.assertFalse(any(request["method"] == "POST" for request in self.server.requests))

    async def test_reconnected_terminal_ends_rollout_without_recording_cross_session_transition(self):
        def replace_connection(server):
            server.response_overrides["game-pc"] = {
                "terminal_id": "game-pc", "session_id": "replacement-session", "connected": True,
            }

        self.server.after_step = replace_connection
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            with self.assertRaisesRegex(RuntimeError, "连接"):
                await run_rollout(client, terminal, RecordingPolicy(), Path(self.output.name), steps=2)
        controls = [request["path"].rsplit("/", 1)[-1] for request in self.server.requests
                    if request["method"] == "POST"]
        self.assertEqual(controls, ["step", "release"])
        self.assertEqual(self.records(), [])
        metadata, = list(Path(self.output.name).rglob("meta.json"))
        self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["status"], "interrupted")

    async def test_passive_collection_rejects_changed_session_without_control_posts(self):
        async with CenterClient(self.server.url, TOKEN) as client:
            terminal = await resolve_terminal(client)
            self.server.response_overrides["game-pc"] = {
                "terminal_id": "game-pc", "session_id": "replacement-session", "connected": True,
            }
            with self.assertRaisesRegex(RuntimeError, "连接"):
                await collect_observations(client, terminal, Path(self.output.name), frames=2)
        self.assertFalse(any(request["method"] == "POST" for request in self.server.requests))
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
