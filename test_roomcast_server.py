import hashlib
import hmac
import io
import math
import os
import queue
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from werkzeug.datastructures import MultiDict

os.environ["ROOMCAST_DEFAULT_PIN"] = "7070"

from roomcast_server import (
    INGEST_CHUNK_SIZE,
    LISTENER_QUEUE_MAXSIZE,
    PcmStreamTranscoder,
    RoomStreamHub,
    TelephonyPcmTranscoder,
    _pcm16_multitone_frame,
    _pcm16_sine_frame,
    _rtp_packet,
    _telnyx_rtp_clock_rate,
    _telnyx_rtp_payload_type,
    create_app,
)


def _pcm16_rms(payload: bytes) -> float:
    usable = len(payload) - (len(payload) % 2)
    if usable <= 0:
        return 0.0
    total = 0.0
    count = 0
    for offset in range(0, usable, 2):
        sample = int.from_bytes(payload[offset:offset + 2], "little", signed=True)
        total += float(sample) * float(sample)
        count += 1
    if count <= 0:
        return 0.0
    return math.sqrt(total / count)


class RoomCastServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "roomcast.db"
        self.app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(self.db_path),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_ADMIN_PASSWORD": "test-password",
                "ROOMCAST_TWILIO_WEBHOOK_TOKEN": "twilio-test-token",
                "ROOMCAST_TELNYX_WEBHOOK_TOKEN": "telnyx-test-token",
                "ROOMCAST_TELEPHONY_SECRET": "telephony-test-secret",
                "ROOMCAST_TELEPHONY_SEGMENT_SECONDS": 0.1,
                "ROOMCAST_TELEPHONY_SEGMENT_TIMEOUT_SECONDS": 0.1,
                "ROOMCAST_TELEPHONY_PROMPT_DIR": str(Path(self.tempdir.name) / "prompts"),
                "ROOMCAST_DIAGNOSTIC_AUDIO_DIR": str(Path(self.tempdir.name) / "diagnostic-audio"),
                "ROOMCAST_HLS_ENABLED": False,
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def _disable_all_hosts(self):
        for host in self.app.roomcast_store.list_hosts():
            self.app.roomcast_store.update_host_controls(
                host["slug"],
                enabled=False,
                manual_mode="force_off",
                notes="",
                device_order=[],
            )
            self.app.roomcast_store.replace_host_schedule(host["slug"], [])

    def test_ingest_chunk_size_stays_aligned_for_pcm24_frames(self):
        self.assertEqual(INGEST_CHUNK_SIZE % 3, 0)

    def test_telephony_transcoder_downmixes_and_resamples_pcm24(self):
        transcoder = TelephonyPcmTranscoder(
            source_channels=2,
            source_rate_hz=48000,
            bits_per_sample=24,
            output_rate_hz=8000,
        )
        left = (600000).to_bytes(3, "little", signed=True)
        right = (300000).to_bytes(3, "little", signed=True)
        stereo_payload = (left + right) * 480
        converted = transcoder.transcode(stereo_payload)
        self.assertGreater(len(converted), 0)
        self.assertEqual(len(converted) % 2, 0)
        self.assertNotEqual(converted, b"\x00" * len(converted))

    def test_telephony_transcoder_applies_makeup_gain_to_low_level_signal(self):
        transcoder = TelephonyPcmTranscoder(
            source_channels=1,
            source_rate_hz=16000,
            bits_per_sample=16,
            output_rate_hz=16000,
            phone_dsp_enabled=True,
        )
        low_level = (int(600)).to_bytes(2, "little", signed=True) * 1600
        converted = transcoder.transcode(low_level)
        self.assertGreater(len(converted), 0)
        self.assertGreater(_pcm16_rms(converted), _pcm16_rms(low_level))

    def test_telephony_transcoder_leaves_level_unchanged_when_phone_dsp_disabled(self):
        transcoder = TelephonyPcmTranscoder(
            source_channels=1,
            source_rate_hz=16000,
            bits_per_sample=16,
            output_rate_hz=16000,
            phone_dsp_enabled=False,
        )
        low_level = (int(600)).to_bytes(2, "little", signed=True) * 1600
        converted = transcoder.transcode(low_level)
        self.assertGreater(len(converted), 0)
        self.assertAlmostEqual(_pcm16_rms(converted), _pcm16_rms(low_level), delta=1.0)

    def test_telephony_transcoder_applies_fixed_phone_gain_without_dsp(self):
        transcoder = TelephonyPcmTranscoder(
            source_channels=1,
            source_rate_hz=16000,
            bits_per_sample=16,
            output_rate_hz=16000,
            phone_dsp_enabled=False,
            phone_gain_db=12.0,
        )
        low_level = (int(600)).to_bytes(2, "little", signed=True) * 1600
        converted = transcoder.transcode(low_level)
        self.assertGreater(len(converted), 0)
        self.assertGreater(_pcm16_rms(converted), _pcm16_rms(low_level) * 3)

    def test_telephony_transcoder_defaults_to_left_channel_for_phone_mono(self):
        left = int(1200).to_bytes(2, "little", signed=True)
        right = int(-1200).to_bytes(2, "little", signed=True)
        out_of_phase_stereo = (left + right) * 480

        left_transcoder = TelephonyPcmTranscoder(
            source_channels=2,
            source_rate_hz=48000,
            bits_per_sample=16,
            output_rate_hz=48000,
        )
        mix_transcoder = TelephonyPcmTranscoder(
            source_channels=2,
            source_rate_hz=48000,
            bits_per_sample=16,
            output_rate_hz=48000,
            mono_mix="mix",
        )

        self.assertGreater(_pcm16_rms(left_transcoder.transcode(out_of_phase_stereo)), 0)
        self.assertEqual(_pcm16_rms(mix_transcoder.transcode(out_of_phase_stereo)), 0)

    def test_browser_transcoder_downconverts_pcm24_to_pcm16_stereo(self):
        transcoder = PcmStreamTranscoder(
            source_channels=2,
            source_rate_hz=96000,
            bits_per_sample=24,
            output_channels=2,
            output_rate_hz=48000,
            output_bits_per_sample=16,
        )
        left = (600000).to_bytes(3, "little", signed=True)
        right = (300000).to_bytes(3, "little", signed=True)
        stereo_payload = (left + right) * 960
        converted = transcoder.transcode(stereo_payload)
        self.assertGreater(len(converted), 0)
        self.assertEqual(len(converted) % 4, 0)
        self.assertNotEqual(converted, b"\x00" * len(converted))

    def test_browser_transcoder_applies_configured_gain_with_peak_limit(self):
        transcoder = PcmStreamTranscoder(
            source_channels=2,
            source_rate_hz=48000,
            bits_per_sample=16,
            output_channels=2,
            output_rate_hz=48000,
            output_bits_per_sample=16,
            gain_db=12.0,
        )
        low_level_stereo = (
            int(600).to_bytes(2, "little", signed=True)
            + int(300).to_bytes(2, "little", signed=True)
        ) * 480
        converted = transcoder.transcode(low_level_stereo)
        self.assertGreater(_pcm16_rms(converted), _pcm16_rms(low_level_stereo) * 3)

        loud_stereo = (
            int(32000).to_bytes(2, "little", signed=True)
            + int(32000).to_bytes(2, "little", signed=True)
        ) * 480
        limited = transcoder.transcode(loud_stereo)
        peak = max(
            abs(int.from_bytes(limited[offset:offset + 2], "little", signed=True))
            for offset in range(0, len(limited), 2)
        )
        self.assertLessEqual(peak, 30000)

    def test_telnyx_rtp_packet_wraps_codec_payload(self):
        payload = b"encoded"
        packet = _rtp_packet(
            payload,
            payload_type=_telnyx_rtp_payload_type("OPUS"),
            sequence_number=0x1234,
            timestamp=0x01020304,
            ssrc=0xAABBCCDD,
        )

        self.assertEqual(packet[:2], b"\x80\x6f")
        self.assertEqual(packet[2:4], b"\x12\x34")
        self.assertEqual(packet[4:8], b"\x01\x02\x03\x04")
        self.assertEqual(packet[8:12], b"\xaa\xbb\xcc\xdd")
        self.assertEqual(packet[12:], payload)
        self.assertEqual(_telnyx_rtp_clock_rate("OPUS", 16000), 48000)
        self.assertEqual(_telnyx_rtp_clock_rate("PCMU", 8000), 8000)

    def test_pcm16_sine_frame_generates_bounded_tone(self):
        frame, phase = _pcm16_sine_frame(16000, 320, 440, 0.18, 0.0)

        self.assertEqual(len(frame), 640)
        self.assertGreater(_pcm16_rms(frame), 0)
        self.assertLessEqual(max(abs(int.from_bytes(frame[i : i + 2], "little", signed=True)) for i in range(0, len(frame), 2)), 5900)
        self.assertGreaterEqual(phase, 0)

    def test_pcm16_multitone_frame_generates_bounded_signal(self):
        frame, phases = _pcm16_multitone_frame(16000, 320, [220, 440, 880, 1760], 0.18, [])

        self.assertEqual(len(frame), 640)
        self.assertEqual(len(phases), 4)
        self.assertGreater(_pcm16_rms(frame), 0)
        self.assertLessEqual(max(abs(int.from_bytes(frame[i : i + 2], "little", signed=True)) for i in range(0, len(frame), 2)), 11800)

    def test_join_flow_redirects_to_room_page(self):
        response = self.client.post(
            "/join",
            data={"room_slug": "meeting-hall", "join_pin": "7070"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NTC Newark WebCall", response.data)
        self.assertIn(b"stream-player", response.data)

    def test_public_page_hides_admin_link(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NTC Newark WebCall", response.data)
        self.assertIn(b"Enter PIN", response.data)
        self.assertIn(b"Dial-In Alternative", response.data)
        self.assertIn(b"+1 862 872 7904", response.data)
        self.assertNotIn(b'href="tel:', response.data)
        self.assertIn(b"Use the phone line if needed.", response.data)
        self.assertNotIn(b",,7070%23", response.data)
        self.assertNotIn(b"PIN 7070", response.data)
        self.assertNotIn(b"Volunteer / Admin", response.data)

    def test_join_flow_accepts_pin_without_room_slug(self):
        response = self.client.post(
            "/join",
            data={"join_pin": "7070"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"stream-player", response.data)
        self.assertIn(b"NTC Newark WebCall", response.data)
        self.assertNotIn(b"Enter PIN", response.data)
        self.assertIn(b"/listen/live.wav", response.data)
        self.assertIn(b"Playback buffer", response.data)
        self.assertIn(b"Dial-In Alternative", response.data)
        self.assertIn(b"+1 862 872 7904", response.data)
        self.assertIn(b'href="tel:+18628727904,,7070%23"', response.data)
        self.assertIn(b"Use the phone line if needed.", response.data)
        self.assertNotIn(b"createMediaElementSource", response.data)

    def test_direct_pin_route_redirects_into_room(self):
        response = self.client.get("/p/7070", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"stream-player", response.data)
        self.assertIn(b"NTC Newark WebCall", response.data)
        self.assertIn(b"/listen/live.wav", response.data)

    def test_live_page_uses_hls_for_ios_when_available(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "ios-roomcast.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_HLS_ENABLED": True,
                "ROOMCAST_HLS_FFMPEG_PATH": "/usr/bin/ffmpeg",
            }
        )
        client = app.test_client()
        app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        response = client.get(
            "/p/7070",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/123.0.0.0 Mobile/15E148 Safari/604.1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/listen/live.m3u8?client=", response.data)
        self.assertIn(b"Buffering audio...", response.data)
        self.assertNotIn(b"Reconnecting", response.data)

    def test_live_page_defaults_to_hls_when_available_on_desktop(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "desktop-hls-roomcast.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_HLS_ENABLED": True,
                "ROOMCAST_HLS_FFMPEG_PATH": "/usr/bin/ffmpeg",
            }
        )
        client = app.test_client()
        app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        response = client.get(
            "/p/7070",
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0) AppleWebKit/537.36 Chrome/123 Safari/537.36"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/listen/live.m3u8?client=", response.data)
        self.assertIn(b"Buffering audio...", response.data)
        self.assertNotIn(b"Reconnecting", response.data)

    def test_live_page_can_force_direct_wav_for_debugging(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "direct-debug-roomcast.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_HLS_ENABLED": True,
                "ROOMCAST_HLS_FFMPEG_PATH": "/usr/bin/ffmpeg",
            }
        )
        client = app.test_client()
        app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        client.get("/p/7070", follow_redirects=True)

        response = client.get("/live?transport=direct")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/listen/live.wav", response.data)
        self.assertNotIn(b"/listen/live.m3u8?client=", response.data)
        self.assertIn(b"Connecting audio...", response.data)

    def test_room_page_uses_hls_for_ios_when_available(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "ios-roomcast-room.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_HLS_ENABLED": True,
                "ROOMCAST_HLS_FFMPEG_PATH": "/usr/bin/ffmpeg",
            }
        )
        client = app.test_client()
        app.roomcast_store.update_host_controls(
            "hp-pavilion-14m-ba1xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        response = client.post(
            "/join",
            data={"room_slug": "meeting-hall", "join_pin": "7070"},
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/listen/meeting-hall.m3u8?client=", response.data)
        self.assertIn(b"Buffering audio...", response.data)

    def test_shared_pin_prefers_room_that_is_forced_on(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_off",
            notes="",
            device_order=[],
        )
        self.app.roomcast_store.update_host_controls(
            "hp-pavilion-14m-ba1xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        response = self.client.get("/p/7070", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        status = self.client.get("/api/live/status")
        self.assertEqual(status.status_code, 200)
        payload = status.get_json()
        self.assertEqual(payload["slug"], "meeting-hall")

    def test_telnyx_pin_connect_prefers_room_that_is_forced_on(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_off",
            notes="",
            device_order=[],
        )
        self.app.roomcast_store.update_host_controls(
            "hp-pavilion-14m-ba1xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b'room_slug" value="meeting-hall"', connect.data)

    def test_listener_page_avoids_eager_stream_reload_loops(self):
        response = self.client.get("/p/7070", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"queueReconnect(", response.data)
        self.assertNotIn(b"paintMeter(0, 0);\n      rebuildStreamUrl();", response.data)
        self.assertNotIn(b"if (roomActive && audio.paused) {\n            refreshAudio();", response.data)

    def test_live_status_requires_pin_authorization(self):
        response = self.client.get("/api/live/status")
        self.assertEqual(response.status_code, 403)

    def test_direct_pin_authorizes_live_status(self):
        self.client.get("/p/7070", follow_redirects=True)
        response = self.client.get("/api/live/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("broadcasting", payload)

    def test_live_status_reports_signal_telemetry_from_ingest(self):
        self.client.get("/p/7070", follow_redirects=True)
        self.app.stream_hub.start_broadcast(
            "meeting-hall",
            "hp-pavilion-14m-ba1xx",
            stream_channels=2,
            sample_rate_hz=96000,
            bits_per_sample=24,
        )
        sample = (400000).to_bytes(3, "little", signed=True)
        self.app.stream_hub.publish("meeting-hall", (sample + sample) * 256)

        response = self.client.get("/api/live/status")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertGreater(payload["signal_level_percent"], 0)
        self.assertGreater(payload["signal_peak_percent"], 0)
        self.assertIsNotNone(payload["signal_peak_db"])
        self.assertIn("connection_quality_percent", payload)
        self.assertIn("hls", payload)

    def test_healthz_reports_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["host_count"], 2)

    def test_admin_login_redirects_to_panel(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Control Panel", response.data)
        self.assertIn(b"Main Sanctuary", response.data)
        self.assertIn(b"Room B", response.data)
        self.assertNotIn(b"Diagnostics", response.data)
        self.assertNotIn(b"Use Schedule", response.data)
        self.assertNotIn(b"Overview", response.data)
        self.assertIn(b"Live Conference", response.data)
        self.assertIn(b"Past Conferences", response.data)
        self.assertIn(b"Public stream quality", response.data)
        self.assertIn(b'id="admin-connection-label"', response.data)
        self.assertNotIn(b"Recent callers", response.data)
        self.assertNotIn(b"Tap to arm", response.data)
        self.assertNotIn(b"createMediaElementSource", response.data)
        self.assertNotIn(b"nextMonitorPanel.replaceWith(currentMonitorPanel)", response.data)

    def test_admin_panel_defaults_monitor_to_active_host_signal(self):
        self.app.stream_hub.start_broadcast(
            "meeting-hall",
            "hp-pavilion-14m-ba1xx",
            stream_channels=2,
            sample_rate_hz=96000,
            bits_per_sample=24,
        )
        sample = (450000).to_bytes(3, "little", signed=True)
        self.app.stream_hub.publish("meeting-hall", (sample + sample) * 256)

        response = self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )

        html = response.data.decode("utf-8")
        match = re.search(r'data-signal-level-percent="([^"]+)"', html)
        self.assertIsNotNone(match)
        self.assertGreater(float(match.group(1)), 0.0)

    def test_admin_schedule_update_persists(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        response = self.client.post(
            "/admin/hosts/hp-pavilion-14m-ba1xx",
            data=MultiDict([
                ("enabled", "on"),
                ("manual_mode", "auto"),
                ("notes", "Updated"),
                ("device_order", "Microphone (Scarlett Solo 4th Gen)"),
                ("device_order", "Microphone Array (Realtek(R) Audio)"),
                ("device_order", ""),
                ("schedule_enabled", "1"),
                ("schedule_day", "SAT"),
                ("schedule_start", "18:00"),
                ("schedule_end", "21:00"),
                ("schedule_enabled", "0"),
                ("schedule_day", "MON"),
                ("schedule_start", "19:00"),
                ("schedule_end", "21:00"),
            ]),
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx")
        self.assertEqual(len(host["schedules"]), 2)
        self.assertEqual(
            host["device_order"],
            ["Microphone (Scarlett Solo 4th Gen)", "Microphone Array (Realtek(R) Audio)"],
        )
        schedules_by_day = {row["day"]: row for row in host["schedules"]}
        self.assertTrue(schedules_by_day["SAT"]["enabled"])
        self.assertFalse(schedules_by_day["MON"]["enabled"])
        self.assertIn(b"Updated hp-pavilion-14m-ba1xx", response.data)

    def test_admin_settings_shows_agent_swap_setup_commands(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)

        response = self.client.get("/admin/settings")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Agent swap", response.data)
        self.assertIn(b"install_roomcast_agent_task.ps1", response.data)
        self.assertIn(b"install_roomcast_agent_launchd.sh", response.data)
        self.assertIn(host["slug"].encode("utf-8"), response.data)
        self.assertIn(host["heartbeat_token"].encode("utf-8"), response.data)
        self.assertIn(b"https://ntcnas.myftp.org/webcall", response.data)
        self.assertIn(b"data-schedule-toggle", response.data)
        self.assertIn(b"data-schedule-enabled", response.data)
        self.assertNotIn(b"Phone Voice", response.data)
        self.assertNotIn(b"Call-In Greeting Phrases", response.data)
        self.assertNotIn(b"Open Audio Diagnostics", response.data)
        self.assertNotIn(b"Manage recorded phone phrases", response.data)

    def test_admin_runtime_endpoint_reports_live_devices(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        self.client.post(
            "/api/source/heartbeat",
            json={
                "host_slug": host["slug"],
                "token": host["heartbeat_token"],
                "devices": ["Scarlett Solo 4th Gen", "Stereo Mix"],
                "current_device": "Scarlett Solo 4th Gen",
                "is_ingesting": False,
                "last_error": "",
            },
        )

        response = self.client.get("/api/admin/runtime")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        host_payload = next(item for item in payload["hosts"] if item["room_slug"] == "meeting-hall")
        self.assertTrue(host_payload["source_online"])
        self.assertEqual(host_payload["current_device"], "Scarlett Solo 4th Gen")
        self.assertIn("Stereo Mix", host_payload["known_devices"])

    def test_admin_runtime_masks_stale_host_devices(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        self.client.post(
            "/api/source/heartbeat",
            json={
                "host_slug": host["slug"],
                "token": host["heartbeat_token"],
                "devices": ["Scarlett Solo 4th Gen"],
                "current_device": "Scarlett Solo 4th Gen",
                "is_ingesting": True,
                "last_error": "",
            },
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE source_runtime SET last_seen_at = ? WHERE host_slug = ?",
                ("2020-01-01T00:00:00+00:00", "hp-pavilion-14m-ba1xx"),
            )

        response = self.client.get("/api/admin/runtime")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        host_payload = next(item for item in payload["hosts"] if item["room_slug"] == "meeting-hall")
        self.assertFalse(host_payload["source_online"])
        self.assertEqual(host_payload["current_device"], "")
        self.assertEqual(host_payload["known_devices"], [])

    def test_room_status_listener_count_excludes_monitor_stream(self):
        self.client.get("/p/7070", follow_redirects=True)
        self.app.stream_hub.start_broadcast("meeting-hall", "hp-pavilion-14m-ba1xx")
        listener_id, _, _ = self.app.stream_hub.open_listener("meeting-hall")
        try:
            response = self.client.get("/api/rooms/meeting-hall/status")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["listener_count"], 0)
        finally:
            self.app.stream_hub.close_listener("meeting-hall", listener_id)

    def test_source_heartbeat_returns_ingest_url(self):
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        response = self.client.post(
            "/api/source/heartbeat",
            json={
                "host_slug": host["slug"],
                "token": host["heartbeat_token"],
                "devices": ["Scarlett Solo 4th Gen"],
                "current_device": "Scarlett Solo 4th Gen",
                "is_ingesting": False,
                "last_error": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("/api/source/ingest/hp-pavilion-14m-ba1xx", payload["ingest_url"])
        self.assertIn("device_order", payload)
        self.assertEqual(payload["capture_mode"], "stereo")
        self.assertEqual(payload["capture_sample_rate_hz"], 96000)
        self.assertEqual(payload["stream_profile"], self.app.config["ROOMCAST_STREAM_PROFILE"])

    def test_source_event_endpoint_records_agent_event(self):
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        response = self.client.post(
            "/api/source/event",
            json={
                "host_slug": host["slug"],
                "token": host["heartbeat_token"],
                "event_type": "agent-started",
                "level": "info",
                "message": "Agent started.",
                "details": {"device_count": 2},
            },
        )
        self.assertEqual(response.status_code, 200)
        events = self.app.roomcast_store.list_recent_events(
            host_slug="hp-pavilion-14m-ba1xx",
            component="agent",
            limit=5,
        )
        self.assertEqual(events[0]["event_type"], "agent-started")
        self.assertEqual(events[0]["details"]["device_count"], 2)

    def test_source_event_endpoint_ignores_reserved_detail_keys(self):
        host = self.app.roomcast_store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        response = self.client.post(
            "/api/source/event",
            json={
                "host_slug": host["slug"],
                "token": host["heartbeat_token"],
                "event_type": "device-list-updated",
                "level": "info",
                "message": "Device list changed.",
                "details": {"room_slug": "wrong-room", "host_slug": "wrong-host", "devices": ["CQ 1&2 (CQ)"]},
            },
        )
        self.assertEqual(response.status_code, 200)
        events = self.app.roomcast_store.list_recent_events(
            host_slug="hp-pavilion-14m-ba1xx",
            component="agent",
            limit=5,
        )
        self.assertEqual(events[0]["event_type"], "device-list-updated")
        self.assertEqual(events[0]["room_slug"], "meeting-hall")
        self.assertEqual(events[0]["host_slug"], "hp-pavilion-14m-ba1xx")
        self.assertNotIn("room_slug", events[0]["details"])
        self.assertEqual(events[0]["details"]["devices"], ["CQ 1&2 (CQ)"])

    def test_listen_live_uses_wav_mimetype_when_configured(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "wav-roomcast.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_STREAM_PROFILE": "wav_pcm24",
            }
        )
        client = app.test_client()
        client.get("/p/7070", follow_redirects=True)
        response = client.get("/listen/live.mp3")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        response_wav = client.get("/listen/live.wav")
        self.assertEqual(response_wav.status_code, 200)
        self.assertEqual(response_wav.mimetype, "audio/wav")

    def test_room_specific_wav_stream_uses_stereo_header_when_capture_mode_is_stereo(self):
        self.app.config["ROOMCAST_STREAM_PROFILE"] = "wav_pcm24"
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.client.post(
            "/admin/hosts/hp-pavilion-14m-ba1xx",
            data=MultiDict([
                ("enabled", "on"),
                ("manual_mode", "force_on"),
                ("notes", ""),
                ("device_order", ""),
            ]),
            follow_redirects=True,
        )
        self.client.post(
            "/join",
            data={"room_slug": "meeting-hall", "join_pin": "7070"},
            follow_redirects=True,
        )
        self.app.stream_hub.start_broadcast(
            "meeting-hall",
            "hp-pavilion-14m-ba1xx",
            stream_channels=2,
            sample_rate_hz=96000,
            bits_per_sample=24,
        )
        self.app.stream_hub.publish("meeting-hall", b"\x00" * (INGEST_CHUNK_SIZE * 2))

        response = self.client.get("/listen/meeting-hall.wav", buffered=False)
        header = next(response.response)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertEqual(header[:4], b"RIFF")
        self.assertEqual(int.from_bytes(header[22:24], "little"), 2)
        self.assertEqual(int.from_bytes(header[24:28], "little"), 48000)
        self.assertEqual(int.from_bytes(header[34:36], "little"), 16)

    def test_wav_stream_closes_listener_when_client_disconnects(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.client.post(
            "/admin/hosts/hp-pavilion-14m-ba1xx",
            data=MultiDict([
                ("enabled", "on"),
                ("manual_mode", "force_on"),
                ("notes", ""),
                ("device_order", ""),
            ]),
            follow_redirects=True,
        )
        self.client.post(
            "/join",
            data={"room_slug": "meeting-hall", "join_pin": "7070"},
            follow_redirects=True,
        )
        self.app.stream_hub.start_broadcast("meeting-hall", "hp-pavilion-14m-ba1xx")
        self.app.stream_hub.publish("meeting-hall", b"\x00" * 96000)

        response = self.client.get("/listen/meeting-hall.wav", buffered=False)
        next(response.response)
        next(response.response)
        self.assertEqual(self.app.stream_hub.status("meeting-hall")["listener_count"], 1)

        response.response.close()

        self.assertEqual(self.app.stream_hub.status("meeting-hall")["listener_count"], 0)

    def test_listen_requires_pin_authorization(self):
        response = self.client.get("/listen/meeting-hall.mp3")
        self.assertEqual(response.status_code, 403)

    def test_twilio_voice_returns_gather_then_stream_url(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        gather = self.client.post("/telephony/twilio/twilio-test-token/voice")
        self.assertEqual(gather.status_code, 200)
        self.assertIn(b"<Gather", gather.data)
        self.assertIn(b'voice="Telnyx.NaturalHD.astra"', gather.data)

        connect = self.client.post(
            "/telephony/twilio/twilio-test-token/voice",
            data={"Digits": "7070", "From": "+19735551212"},
        )
        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"<Play>", connect.data)
        self.assertIn(b"/telephony/session/", connect.data)
        self.assertIn(b"<Redirect", connect.data)
        self.assertNotIn(b"Connecting you now", connect.data)

    def test_telnyx_voice_returns_gather_then_websocket_stream_xml(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_CODEC"] = "G722"
        self.app.config["ROOMCAST_TELNYX_STREAM_SAMPLE_RATE"] = 8000
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        gather = self.client.post("/telephony/telnyx/telnyx-test-token/voice")
        self.assertEqual(gather.status_code, 200)
        self.assertIn(b"<Gather", gather.data)
        self.assertIn(b'voice="Telnyx.NaturalHD.astra"', gather.data)

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"<Connect>", connect.data)
        self.assertIn(b"<Stream", connect.data)
        self.assertIn(b"bidirectionalMode=\"rtp\"", connect.data)
        self.assertIn(b"bidirectionalCodec=\"G722\"", connect.data)
        self.assertIn(b"bidirectionalSamplingRate=\"8000\"", connect.data)
        self.assertIn(b"enableReconnect=\"true\"", connect.data)
        self.assertIn(b"wss://ntcnas.myftp.org/webcall/telephony/telnyx/telnyx-test-token/stream/", connect.data)
        self.assertNotIn(b"<Play>", connect.data)
        self.assertIn(b"<Redirect", connect.data)
        self.assertIn(b"/telephony/telnyx/telnyx-test-token/continue/", connect.data)

    def test_telnyx_voice_can_answer_twilio_validation_call(self):
        self.app.config["ROOMCAST_TWILIO_VERIFY_CODE"] = "123456"
        response = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"CallerId": "+14157234000"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<Play digits="ww123456"', response.data)
        self.assertNotIn(b"<Gather", response.data)

    def test_telnyx_sms_webhook_logs_inbound_payload(self):
        log_path = Path(self.tempdir.name) / "telnyx-sms.jsonl"
        self.app.config["ROOMCAST_TELNYX_SMS_LOG_PATH"] = str(log_path)

        response = self.client.post(
            "/telephony/telnyx/telnyx-test-token/sms",
            json={
                "data": {
                    "event_type": "message.received",
                    "payload": {
                        "from": {"phone_number": "+14157234000"},
                        "to": [{"phone_number": "+18628727904"}],
                        "text": "Your verification code is 123456",
                    },
                }
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(log_path.exists())
        self.assertIn("123456", log_path.read_text())

    def test_telnyx_sms_webhook_rejects_wrong_token(self):
        response = self.client.post("/telephony/telnyx/wrong-token/sms", json={"data": {}})

        self.assertEqual(response.status_code, 404)

    def test_telnyx_stream_reconnect_can_be_disabled_for_diagnostics(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_RECONNECT_ENABLED"] = "0"
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"enableReconnect=\"false\"", connect.data)

    def test_telnyx_continue_uses_websocket_stream_transport(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        match = re.search(rb"/telephony/telnyx/telnyx-test-token/continue/([A-Za-z0-9_-]+)", connect.data)
        self.assertIsNotNone(match)

        continued = self.client.post(f"/telephony/telnyx/telnyx-test-token/continue/{match.group(1).decode('ascii')}")

        self.assertEqual(continued.status_code, 200)
        self.assertIn(b"<Connect>", continued.data)
        self.assertIn(b"<Stream", continued.data)
        self.assertNotIn(b"<Play>", continued.data)

    def test_telnyx_voice_can_advertise_opus_stream_codec(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_CODEC"] = "OPUS"
        self.app.config["ROOMCAST_TELNYX_STREAM_SAMPLE_RATE"] = 16000
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"bidirectionalCodec=\"OPUS\"", connect.data)
        self.assertIn(b"bidirectionalSamplingRate=\"16000\"", connect.data)

    def test_telnyx_opus_stream_sample_rate_is_capped_to_supported_rtp_rate(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_CODEC"] = "OPUS"
        self.app.config["ROOMCAST_TELNYX_STREAM_SAMPLE_RATE"] = 24000
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"bidirectionalCodec=\"OPUS\"", connect.data)
        self.assertIn(b"bidirectionalSamplingRate=\"16000\"", connect.data)

    def test_telnyx_voice_can_advertise_l16_stream_codec(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_CODEC"] = "L16"
        self.app.config["ROOMCAST_TELNYX_STREAM_SAMPLE_RATE"] = 16000
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"bidirectionalCodec=\"L16\"", connect.data)
        self.assertIn(b"bidirectionalSamplingRate=\"16000\"", connect.data)

    def test_telnyx_unsupported_stream_codec_falls_back_to_g722(self):
        self.app.config["ROOMCAST_TELNYX_STREAM_CODEC"] = "AMR-WB"
        self.app.config["ROOMCAST_TELNYX_STREAM_SAMPLE_RATE"] = 16000
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )

        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"bidirectionalCodec=\"G722\"", connect.data)
        self.assertIn(b"bidirectionalSamplingRate=\"8000\"", connect.data)

    def test_uploaded_prompt_replaces_telnyx_say_with_play(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        wav_payload = (
            b"RIFF"
            + (36 + 320).to_bytes(4, "little")
            + b"WAVEfmt "
            + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + (16000).to_bytes(4, "little")
            + (32000).to_bytes(4, "little")
            + (2).to_bytes(2, "little")
            + (16).to_bytes(2, "little")
            + b"data"
            + (320).to_bytes(4, "little")
            + (b"\x00\x00" * 160)
        )
        self.client.post("/admin/login", data={"password": "test-password"})
        upload = self.client.post(
            "/admin/prompts/welcome_pin",
            data={"audio": (io.BytesIO(wav_payload), "welcome.wav")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(upload.status_code, 200)

        gather = self.client.post("/telephony/telnyx/telnyx-test-token/voice")

        self.assertEqual(gather.status_code, 200)
        self.assertIn(b"<Gather", gather.data)
        self.assertIn(b"<Play>https://ntcnas.myftp.org/webcall/telephony/prompts/welcome_pin.wav", gather.data)
        self.assertNotIn(b"Please enter your four digit pin", gather.data)

    def test_admin_diagnostic_audio_upload_saves_sample(self):
        wav_payload = (
            b"RIFF"
            + (36 + 320).to_bytes(4, "little")
            + b"WAVEfmt "
            + (16).to_bytes(4, "little")
            + (1).to_bytes(2, "little")
            + (1).to_bytes(2, "little")
            + (48000).to_bytes(4, "little")
            + (96000).to_bytes(4, "little")
            + (2).to_bytes(2, "little")
            + (16).to_bytes(2, "little")
            + b"data"
            + (320).to_bytes(4, "little")
            + (b"\x00\x00" * 160)
        )
        self.client.post("/admin/login", data={"password": "test-password"})
        upload = self.client.post(
            "/admin/diagnostics/audio",
            data={"audio": (io.BytesIO(wav_payload), "phone-static.wav"), "note": "phone static sample"},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(upload.status_code, 200)
        self.assertIn(b"phone static sample", upload.data)
        samples = list((Path(self.tempdir.name) / "diagnostic-audio").glob("hearing-*.wav"))
        self.assertEqual(len(samples), 1)
        metadata = samples[0].with_suffix(".json").read_text(encoding="utf-8")
        self.assertIn("phone static sample", metadata)

        playback = self.client.get(f"/admin/diagnostics/audio/{samples[0].name}")
        self.assertEqual(playback.status_code, 200)
        self.assertEqual(playback.mimetype, "audio/wav")

    def test_telnyx_continue_uses_technical_difficulty_message_after_stream_failure(self):
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        session_match = re.search(rb'name="session_id" value="([^"]+)"', connect.data)
        self.assertIsNotNone(session_match)
        session_id = session_match.group(1).decode("utf-8")
        self.app.telephony_sessions[session_id].stream_ended = True

        response = self.client.post(f"/telephony/telnyx/telnyx-test-token/continue/{session_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"technical difficulty", response.data)

        follow_up = self.client.post(f"/telephony/telnyx/telnyx-test-token/continue/{session_id}")
        self.assertEqual(follow_up.status_code, 200)
        self.assertIn(b"technical difficulty", follow_up.data)

    def test_telnyx_session_segment_returns_buffered_wav(self):
        self.app.config["ROOMCAST_TELNYX_TRANSPORT"] = "segment"
        self.app.roomcast_store.update_host_controls(
            "hp-envy-16-ad0xx",
            enabled=True,
            manual_mode="force_on",
            notes="",
            device_order=[],
        )
        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        play_url = connect.data.decode().split("<Play>", 1)[1].split("</Play>", 1)[0]
        split_url = urlsplit(play_url)
        segment_path = split_url.path
        if segment_path.startswith("/webcall/"):
            segment_path = segment_path[len("/webcall") :]
        if split_url.query:
            segment_path = f"{segment_path}?{split_url.query}"
        response = self.client.get(segment_path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertTrue(response.data.startswith(b"RIFF"))

    def test_telnyx_stream_status_accepts_callback(self):
        response = self.client.post(
            "/telephony/telnyx/telnyx-test-token/stream-status",
            json={"event_type": "stream-started", "stream_id": "abc123"},
        )
        self.assertEqual(response.status_code, 204)

    def test_telnyx_voice_valid_pin_without_active_room_returns_goodbye(self):
        self._disable_all_hosts()
        response = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no conference is active right now", response.data)
        self.assertIn(b"<Hangup", response.data)

    def test_twilio_voice_valid_pin_without_active_room_returns_goodbye(self):
        self._disable_all_hosts()
        response = self.client.post(
            "/telephony/twilio/twilio-test-token/voice",
            data={"Digits": "7070", "From": "+19735551212"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no conference is active right now", response.data)
        self.assertIn(b"<Hangup", response.data)

    def test_telnyx_voice_without_active_meeting_skips_pin_prompt(self):
        self._disable_all_hosts()
        response = self.client.post("/telephony/telnyx/telnyx-test-token/voice")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no scheduled meeting active right now", response.data)
        self.assertNotIn(b"<Gather", response.data)
        self.assertIn(b"<Hangup", response.data)

    def test_twilio_voice_without_active_meeting_skips_pin_prompt(self):
        self._disable_all_hosts()
        response = self.client.post("/telephony/twilio/twilio-test-token/voice")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"no scheduled meeting active right now", response.data)
        self.assertNotIn(b"<Gather", response.data)
        self.assertIn(b"<Hangup", response.data)

    def test_telephony_stream_uses_phone_wav_profile(self):
        app = create_app(
            {
                "TESTING": True,
                "ROOMCAST_DB_PATH": str(Path(self.tempdir.name) / "phone-roomcast.db"),
                "SECRET_KEY": "test-secret",
                "SESSION_COOKIE_SECURE": False,
                "ROOMCAST_STREAM_PROFILE": "wav_pcm24",
                "ROOMCAST_TELEPHONY_SECRET": "telephony-test-secret",
            }
        )
        client = app.test_client()
        room_slug = "meeting-hall"
        exp = "9999999999"
        sig = hmac.new(
            b"telephony-test-secret",
            f"{room_slug}:{exp}:Phone caller:phone".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        response = client.get(
            f"/telephony/stream/{room_slug}.wav?exp={exp}&sig={sig}&label=Phone%20caller&channel=phone",
            buffered=False,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "audio/wav")
        self.assertEqual(next(response.response)[:4], b"RIFF")

    def test_admin_report_download_returns_listener_rows(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.app.roomcast_store.sync_meeting_state(
            "meeting-hall",
            active=True,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )
        session_id = self.app.roomcast_store.begin_listener_session(
            "meeting-hall",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="listener-1",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
        self.app.roomcast_store.end_listener_session(session_id)
        meeting_id = self.app.roomcast_store.get_active_meeting()["id"]
        self.app.roomcast_store.sync_meeting_state(
            "meeting-hall",
            active=False,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )

        response = self.client.get(f"/admin/reports/{meeting_id}.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(b"Meeting,Tarry Meeting Hall", response.data)
        self.assertIn(b"Web 127.0.0.1", response.data)

    def test_admin_stop_call_closes_active_listener_sessions(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.app.roomcast_store.sync_meeting_state(
            "meeting-hall",
            active=True,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )
        self.app.roomcast_store.begin_listener_session(
            "meeting-hall",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="listener-1",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )

        response = self.client.post(
            "/admin/call/stop",
            data={"room_slug": "meeting-hall"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.app.roomcast_store.list_listener_sessions("meeting-hall", active_only=True),
            [],
        )

    def test_admin_stop_call_terminates_live_room_stream(self):
        self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.app.stream_hub.start_broadcast("meeting-hall", "hp-pavilion-14m-ba1xx")

        response = self.client.post(
            "/admin/call/stop",
            data={"room_slug": "meeting-hall"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.app.stream_hub.status("meeting-hall")["broadcasting"])

    def test_stream_hub_uses_small_listener_queue(self):
        hub = RoomStreamHub()
        room = hub._get_room("meeting-hall")
        queue_obj = queue.Queue(maxsize=LISTENER_QUEUE_MAXSIZE)
        room.listeners[1] = queue_obj
        self.assertEqual(queue_obj.maxsize, LISTENER_QUEUE_MAXSIZE)

    def test_stream_hub_drops_backlog_when_listener_falls_behind(self):
        hub = RoomStreamHub()
        room = hub._get_room("meeting-hall")
        listener = queue.Queue(maxsize=LISTENER_QUEUE_MAXSIZE)
        room.listeners[1] = listener

        for index in range(LISTENER_QUEUE_MAXSIZE + 3):
            hub.publish("meeting-hall", str(index).encode("utf-8"))

        queued = []
        while not listener.empty():
            queued.append(listener.get_nowait())

        self.assertEqual(
            queued,
            [
                str(LISTENER_QUEUE_MAXSIZE).encode("utf-8"),
                str(LISTENER_QUEUE_MAXSIZE + 1).encode("utf-8"),
                str(LISTENER_QUEUE_MAXSIZE + 2).encode("utf-8"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
