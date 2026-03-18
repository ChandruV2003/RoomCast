import tempfile
import unittest
from pathlib import Path
import sys

from roomcast_agent import RoomCastAgent


class RoomCastAgentTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tempdir.name) / "agent.lock"

    def tearDown(self):
        self.tempdir.cleanup()

    def _make_agent(self):
        agent = RoomCastAgent(
            "https://example.com/webcall",
            "test-host",
            "test-token",
            ffmpeg_path=sys.executable,
            test_tone=True,
        )
        agent.lock_path = self.lock_path
        return agent

    def test_second_agent_instance_is_rejected(self):
        first = self._make_agent()
        second = self._make_agent()

        first.acquire_instance_lock()
        try:
            with self.assertRaises(RuntimeError):
                second.acquire_instance_lock()
        finally:
            first.release_instance_lock()

        second.acquire_instance_lock()
        second.release_instance_lock()

    def test_exact_device_order_beats_pattern_matching(self):
        agent = self._make_agent()
        agent.test_tone = False
        agent.cached_devices = [
            "Microphone Array (Realtek(R) Audio)",
            "Microphone (Scarlett Solo 4th Gen)",
        ]
        chosen = agent.choose_device(["Microphone (Scarlett Solo 4th Gen)"])
        self.assertEqual(chosen, "Microphone (Scarlett Solo 4th Gen)")


if __name__ == "__main__":
    unittest.main()
