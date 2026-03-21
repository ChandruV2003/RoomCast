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

    def test_restart_ingest_switches_to_new_device_immediately(self):
        agent = self._make_agent()
        agent.test_tone = False
        agent.current_device = "Headset Microphone (Realtek(R) Audio)"
        agent.restart_not_before = 60

        events = []
        agent.stop_ingest = lambda reason=None: events.append(("stop", reason))
        agent.start_ingest = lambda ingest_url, device_name: events.append(("start", ingest_url, device_name))

        agent.restart_ingest(
            "https://example.com/webcall/api/source/ingest/test-host",
            "Analogue 1 + 2 (3- Focusrite USB Audio)",
        )

        self.assertEqual(
            events,
            [
                ("stop", "Switching to preferred input: Analogue 1 + 2 (3- Focusrite USB Audio)"),
                ("start", "https://example.com/webcall/api/source/ingest/test-host", "Analogue 1 + 2 (3- Focusrite USB Audio)"),
            ],
        )
        self.assertEqual(agent.restart_not_before, 0.0)
        self.assertEqual(agent.last_error, "")

    def test_analogue_pair_devices_use_left_channel_mono(self):
        agent = self._make_agent()
        agent.test_tone = False

        command = agent._build_ffmpeg_command(
            "https://example.com/webcall/api/source/ingest/test-host",
            "Analogue 1 + 2 (3- Focusrite USB Audio)",
        )

        self.assertIn("-ac", command)
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-b:a") + 1], "128k")
        self.assertIn("-af", command)
        self.assertEqual(
            command[command.index("-af") + 1],
            "pan=mono|c0=c0,silencedetect=noise=-45dB:d=45",
        )

    def test_microphone_devices_stay_mono(self):
        agent = self._make_agent()
        agent.test_tone = False

        command = agent._build_ffmpeg_command(
            "https://example.com/webcall/api/source/ingest/test-host",
            "Microphone (Scarlett Solo 4th Gen)",
        )

        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-b:a") + 1], "128k")

    def test_stereo_mix_devices_keep_stereo(self):
        agent = self._make_agent()
        agent.test_tone = False

        command = agent._build_ffmpeg_command(
            "https://example.com/webcall/api/source/ingest/test-host",
            "Stereo Mix (Realtek(R) Audio)",
        )

        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertEqual(command[command.index("-b:a") + 1], "192k")

    def test_sq7_devices_switch_to_stereo_when_present(self):
        agent = self._make_agent()
        agent.test_tone = False
        agent.host_slug = "hp-envy-16-ad0xx"

        command = agent._build_ffmpeg_command(
            "https://example.com/webcall/api/source/ingest/test-host",
            "SQ-7 USB Audio",
            "wav_pcm24",
        )

        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertNotIn("pan=mono|c0=c0", command)

    def test_wav_pcm24_profile_uses_raw_pcm_transport(self):
        agent = self._make_agent()
        agent.test_tone = False

        command = agent._build_ffmpeg_command(
            "https://example.com/webcall/api/source/ingest/test-host",
            "Analogue 1 + 2 (3- Focusrite USB Audio)",
            "wav_pcm24",
        )

        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertIn("-c:a", command)
        self.assertEqual(command[command.index("-c:a") + 1], "pcm_s24le")
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f", command.index("-c:a")) + 1], "s24le")
        self.assertIn("-content_type", command)
        self.assertEqual(command[command.index("-content_type") + 1], "application/octet-stream")
        self.assertEqual(command[command.index("-method") + 1], "POST")


if __name__ == "__main__":
    unittest.main()
