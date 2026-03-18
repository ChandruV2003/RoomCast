import os
import tempfile
import unittest
from pathlib import Path

os.environ["ROOMCAST_DEFAULT_PIN"] = "7070"

from roomcast_store import RoomCastStore


class RoomCastStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "roomcast.db"
        self.store = RoomCastStore(str(self.db_path))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_seeded_rooms_exist(self):
        rooms = {room["slug"]: room for room in self.store.list_rooms()}
        self.assertIn("meeting-hall", rooms)
        self.assertIn("study-room", rooms)

    def test_default_pin_is_accepted(self):
        self.assertTrue(self.store.verify_room_pin("meeting-hall", "7070"))
        self.assertFalse(self.store.verify_room_pin("meeting-hall", "1967"))

    def test_seeded_host_has_room_mapping_and_secret(self):
        host = self.store.get_host("hp-pavilion-14m-ba1xx", include_secret=True)
        self.assertEqual(host["room_slug"], "meeting-hall")
        self.assertTrue(host["heartbeat_token"])

    def test_replace_host_schedule_rewrites_rows(self):
        self.store.replace_host_schedule("hp-pavilion-14m-ba1xx", "SAT 18:00-20:00\nSUN 10:30-12:00")
        host = self.store.get_host("hp-pavilion-14m-ba1xx")
        self.assertEqual(
            host["schedules"],
            [
                {"day": "SAT", "start": "18:00", "end": "20:00", "enabled": True},
                {"day": "SUN", "start": "10:30", "end": "12:00", "enabled": True},
            ],
        )

    def test_schedule_end_waits_for_silence_warning_before_stopping(self):
        self.store.replace_host_schedule(
            "hp-pavilion-14m-ba1xx",
            [{"day": "SAT", "start": "18:00", "end": "20:00", "enabled": False}],
        )
        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Microphone (Scarlett Solo 4th Gen)",
            devices=["Microphone (Scarlett Solo 4th Gen)"],
            is_ingesting=True,
            last_error="",
            desired_active=False,
        )
        host = self.store.get_host("hp-pavilion-14m-ba1xx")
        self.assertTrue(host["desired_active"])

        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Microphone (Scarlett Solo 4th Gen)",
            devices=["Microphone (Scarlett Solo 4th Gen)"],
            is_ingesting=True,
            last_error="No program audio detected for 15 seconds. Check the room feed if the meeting should be active.",
            desired_active=False,
        )
        host = self.store.get_host("hp-pavilion-14m-ba1xx")
        self.assertFalse(host["desired_active"])

    def test_listener_session_round_trip(self):
        session_id = self.store.begin_listener_session(
            "meeting-hall",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="session-1",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
        active = self.store.list_listener_sessions("meeting-hall", active_only=True)
        self.assertEqual(active[0]["id"], session_id)
        self.store.end_listener_session(session_id)
        active_after = self.store.list_listener_sessions("meeting-hall", active_only=True)
        self.assertEqual(active_after, [])

    def test_meeting_report_collects_listeners_and_incidents(self):
        self.store.sync_meeting_state(
            "meeting-hall",
            active=True,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )
        session_id = self.store.begin_listener_session(
            "meeting-hall",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="session-1",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
        self.store.end_listener_session(session_id)
        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Scarlett Solo 4th Gen",
            devices=["Scarlett Solo 4th Gen"],
            is_ingesting=True,
            last_error="",
            desired_active=True,
        )
        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Scarlett Solo 4th Gen",
            devices=["Scarlett Solo 4th Gen"],
            is_ingesting=True,
            last_error="No program audio detected.",
            desired_active=True,
        )
        meeting_id = self.store.get_active_meeting()["id"]
        self.store.sync_meeting_state(
            "meeting-hall",
            active=False,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )
        report = self.store.get_meeting_report(meeting_id)
        self.assertEqual(report["listener_count"], 1)
        self.assertEqual(report["incident_count"], 1)
        self.assertEqual(report["listeners"][0]["participant_label"], "Web 127.0.0.1")


if __name__ == "__main__":
    unittest.main()
