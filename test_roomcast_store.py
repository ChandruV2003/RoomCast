import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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

    def test_store_connections_enable_wal_and_busy_timeout(self):
        with self.store._connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        self.assertEqual(str(journal_mode).lower(), "wal")
        self.assertGreaterEqual(int(busy_timeout), 30000)
        self.assertEqual(int(foreign_keys), 1)

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

    def test_disabled_schedule_does_not_hold_room_active(self):
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
        self.assertFalse(host["desired_active"])

    def test_recent_schedule_end_waits_for_sustained_silence_before_stopping(self):
        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz).replace(second=0, microsecond=0)
        start = (now - timedelta(minutes=35)).strftime("%H:%M")
        end = (now - timedelta(minutes=10)).strftime("%H:%M")
        day = now.strftime("%a").upper()[:3]
        self.store.replace_host_schedule(
            "hp-pavilion-14m-ba1xx",
            [{"day": day, "start": start, "end": end, "enabled": True}],
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
        self.assertTrue(host["desired_active"])

        old_warning_at = (datetime.now(ZoneInfo("UTC")) - timedelta(minutes=6)).isoformat()
        with self.store._connect() as connection:
            connection.execute(
                "UPDATE source_runtime SET last_error_changed_at = ? WHERE host_slug = ?",
                (old_warning_at, "hp-pavilion-14m-ba1xx"),
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
        events = self.store.list_recent_events(room_slug="meeting-hall", component="listener", limit=4)
        event_types = [event["event_type"] for event in events]
        self.assertIn("listener-joined", event_types)
        self.assertIn("listener-left", event_types)

    def test_sync_meeting_state_closes_active_listeners_when_meeting_ends(self):
        self.store.sync_meeting_state(
            "meeting-hall",
            active=True,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )
        self.store.begin_listener_session(
            "meeting-hall",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="session-1",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )

        self.store.sync_meeting_state(
            "meeting-hall",
            active=False,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="admin",
            actor="test",
        )

        self.assertEqual(self.store.list_listener_sessions("meeting-hall", active_only=True), [])

    def test_close_orphaned_listener_sessions_clears_rows_without_live_meeting(self):
        self.store.begin_listener_session(
            "study-room",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="session-2",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )
        self.store.close_orphaned_listener_sessions()
        self.assertEqual(self.store.list_listener_sessions("study-room", active_only=True), [])

    def test_close_orphaned_listener_sessions_clears_rows_with_live_meeting(self):
        self.store.sync_meeting_state(
            "study-room",
            active=True,
            host_slug="hp-envy-16-ad0xx",
            trigger_mode="admin",
            actor="test",
        )
        self.store.begin_listener_session(
            "study-room",
            channel="web",
            participant_label="Web 127.0.0.1",
            participant_key="session-2",
            ip_address="127.0.0.1",
            user_agent="pytest",
        )

        self.store.close_orphaned_listener_sessions()

        self.assertEqual(self.store.list_listener_sessions("study-room", active_only=True), [])

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

    def test_audio_level_samples_are_available_for_reports_and_summary(self):
        self.store.sync_meeting_state(
            "meeting-hall",
            active=True,
            host_slug="hp-pavilion-14m-ba1xx",
            trigger_mode="watchdog",
            actor="test",
        )
        meeting_id = self.store.get_active_meeting()["id"]

        self.store.record_audio_level_sample(
            "meeting-hall",
            host_slug="hp-pavilion-14m-ba1xx",
            signal_level_db=-28.0,
            signal_peak_db=-8.0,
            signal_level_percent=68.0,
            signal_peak_percent=91.0,
            listener_count=2,
            broadcasting=True,
            is_ingesting=True,
            desired_active=True,
            current_device="CQ 1&2 (CQ18T)",
            stream_transport="hls",
            connection_quality_percent=100.0,
            connection_quality_label="Buffered",
        )

        summary = self.store.audio_level_summary("meeting-hall", window_seconds=300)
        self.assertEqual(summary["sample_count"], 1)
        self.assertEqual(summary["max_signal_level_db"], -28.0)

        report = self.store.get_meeting_report(meeting_id)
        self.assertEqual(len(report["audio_levels"]), 1)
        self.assertEqual(report["audio_levels"][0]["signal_peak_db"], -8.0)

    def test_record_heartbeat_logs_runtime_changes(self):
        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Scarlett Solo 4th Gen",
            devices=["Scarlett Solo 4th Gen"],
            is_ingesting=False,
            last_error="",
            desired_active=False,
            stream_profile="wav_pcm24",
            stream_channels=2,
            sample_rate_hz=48000,
            sample_bits=24,
        )
        self.store.record_heartbeat(
            "hp-pavilion-14m-ba1xx",
            current_device="Scarlett Solo 4th Gen",
            devices=["Scarlett Solo 4th Gen", "Stereo Mix"],
            is_ingesting=True,
            last_error="Sample warning",
            desired_active=False,
            stream_profile="wav_pcm24",
            stream_channels=2,
            sample_rate_hz=48000,
            sample_bits=24,
        )
        events = self.store.list_recent_events(room_slug="meeting-hall", host_slug="hp-pavilion-14m-ba1xx", limit=10)
        event_types = {event["event_type"] for event in events}
        self.assertIn("device-list-changed", event_types)
        self.assertIn("ingest-started", event_types)
        self.assertIn("runtime-error", event_types)


if __name__ == "__main__":
    unittest.main()
