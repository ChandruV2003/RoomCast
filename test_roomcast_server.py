import os
import queue
import tempfile
import unittest
from pathlib import Path

from werkzeug.datastructures import MultiDict

os.environ["ROOMCAST_DEFAULT_PIN"] = "7070"

from roomcast_server import INGEST_CHUNK_SIZE, LISTENER_QUEUE_MAXSIZE, RoomStreamHub, create_app


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
                "ROOMCAST_TELEPHONY_SECRET": "telephony-test-secret",
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_ingest_chunk_size_stays_aligned_for_pcm24_frames(self):
        self.assertEqual(INGEST_CHUNK_SIZE % 3, 0)

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
        self.assertEqual(payload["capture_mode"], "mono")
        self.assertEqual(payload["stream_profile"], "mp3")

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

        connect = self.client.post(
            "/telephony/twilio/twilio-test-token/voice",
            data={"Digits": "7070", "From": "+19735551212"},
        )
        self.assertEqual(connect.status_code, 200)
        self.assertIn(b"<Play>", connect.data)
        self.assertIn(b"/telephony/stream/", connect.data)
        self.assertIn(b"%2B19735551212", connect.data)

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
