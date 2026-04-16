import hashlib
import hmac
import os
import queue
import sqlite3
import tempfile
import unittest
from pathlib import Path

from werkzeug.datastructures import MultiDict

os.environ["ROOMCAST_DEFAULT_PIN"] = "7070"

from roomcast_server import (
    INGEST_CHUNK_SIZE,
    LISTENER_QUEUE_MAXSIZE,
    RoomStreamHub,
    TelephonyPcmTranscoder,
    create_app,
)


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
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

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

    def test_direct_pin_route_redirects_into_room(self):
        response = self.client.get("/p/7070", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"stream-player", response.data)
        self.assertIn(b"NTC Newark WebCall", response.data)

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

    def test_admin_login_redirects_to_panel(self):
        response = self.client.post(
            "/admin/login",
            data={"password": "test-password"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Control Panel", response.data)
        self.assertIn(b"Room A", response.data)
        self.assertIn(b"Room B", response.data)
        self.assertNotIn(b"Diagnostics", response.data)
        self.assertNotIn(b"Use Schedule", response.data)
        self.assertNotIn(b"Overview", response.data)
        self.assertIn(b"Live Conference", response.data)
        self.assertIn(b"Past Conferences", response.data)
        self.assertNotIn(b"Recent callers", response.data)

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

    def test_listen_requires_pin_authorization(self):
        response = self.client.get("/listen/meeting-hall.mp3")
        self.assertEqual(response.status_code, 403)

    def test_twilio_voice_returns_gather_then_stream_url(self):
        gather = self.client.post("/telephony/twilio/twilio-test-token/voice")
        self.assertEqual(gather.status_code, 200)
        self.assertIn(b"<Gather", gather.data)
        self.assertIn(b'voice="Polly.Joanna-Neural"', gather.data)

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
        gather = self.client.post("/telephony/telnyx/telnyx-test-token/voice")
        self.assertEqual(gather.status_code, 200)
        self.assertIn(b"<Gather", gather.data)
        self.assertIn(b'voice="Polly.Joanna-Neural"', gather.data)

        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"<Connect>", connect.data)
        self.assertIn(b"<Stream", connect.data)
        self.assertIn(b"bidirectionalMode=\"rtp\"", connect.data)
        self.assertIn(b"bidirectionalCodec=\"PCMU\"", connect.data)
        self.assertIn(b"wss://ntcnas.myftp.org/telephony/telnyx/telnyx-test-token/stream/", connect.data)
        self.assertNotIn(b"<Play>", connect.data)

    def test_telnyx_session_segment_returns_buffered_wav(self):
        self.app.config["ROOMCAST_TELNYX_TRANSPORT"] = "segment"
        connect = self.client.post(
            "/telephony/telnyx/telnyx-test-token/voice",
            data={"Digits": "7070", "CallerId": "+19735550000"},
        )
        self.assertEqual(connect.status_code, 200)
        play_url = connect.data.decode().split("<Play>", 1)[1].split("</Play>", 1)[0]
        segment_path = play_url.replace("http://localhost", "").replace("https://localhost", "")
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
