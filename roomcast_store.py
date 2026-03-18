"""SQLite-backed configuration and runtime state for RoomCast."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from host_defaults import get_default_hosts
from schedule_engine import is_schedule_active, next_schedule_change, normalize_schedule_rows, parse_schedule_text


DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_PIN = os.getenv("ROOMCAST_DEFAULT_PIN", "7070")

ROOM_SEEDS = {
    "hp-pavilion-14m-ba1xx": {
        "slug": "meeting-hall",
        "label": "Tarry Meeting Hall",
        "description": "Primary Saturday meeting audio room.",
        "enabled": True,
    },
    "hp-envy-16-ad0xx": {
        "slug": "study-room",
        "label": "Bible Study Room",
        "description": "Sunday and Wednesday audio room.",
        "enabled": True,
    },
    "leonovo-laptop-mv23gfqd": {
        "slug": "diagnostics",
        "label": "Diagnostics",
        "description": "Spare room for testing and fallback validation.",
        "enabled": False,
    },
}

LEGACY_AUDIO_DEFAULTS = {
    "hp-pavilion-14m-ba1xx": ("Scarlett", "Microphone"),
    "hp-envy-16-ad0xx": ("Scarlett", "Microphone"),
    "leonovo-laptop-mv23gfqd": ("Microphone", ""),
}

HOST_PRIORITY = {
    "hp-envy-16-ad0xx": 20,
    "hp-pavilion-14m-ba1xx": 10,
    "leonovo-laptop-mv23gfqd": 0,
}

SILENCE_WARNING_PREFIX = "No program audio detected for "


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_pin(pin: str) -> str:
    return hashlib.sha256(pin.encode("utf-8")).hexdigest()


def _bool_from_int(value) -> bool:
    return bool(int(value)) if value is not None else False


def _json_list(value: str | None):
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        parsed = []
    if not isinstance(parsed, list):
        return []
    deduped = []
    for item in parsed:
        text = str(item or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _dedupe_list(values):
    deduped = []
    for item in values or []:
        text = str(item or "").strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _is_silence_warning(message: str | None) -> bool:
    return (message or "").strip().startswith(SILENCE_WARNING_PREFIX)


class RoomCastStore:
    """Persist rooms, source hosts, schedules, and source heartbeat state."""

    def __init__(self, db_path: str | None = None):
        default_path = Path(__file__).resolve().parent / "data" / "roomcast.db"
        configured_path = Path(db_path or os.getenv("ROOMCAST_DB_PATH", default_path))
        self.db_path = configured_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_defaults()

    def _connect(self):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self):
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rooms (
                    slug TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    pin_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hosts (
                    slug TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    room_slug TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    manual_mode TEXT NOT NULL DEFAULT 'auto',
                    notes TEXT NOT NULL DEFAULT '',
                    timezone TEXT NOT NULL DEFAULT 'America/New_York',
                    preferred_audio_pattern TEXT NOT NULL DEFAULT 'Scarlett',
                    fallback_audio_pattern TEXT NOT NULL DEFAULT 'Microphone',
                    device_order_json TEXT NOT NULL DEFAULT '[]',
                    heartbeat_token TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(room_slug) REFERENCES rooms(slug) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_slug TEXT NOT NULL,
                    day TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(host_slug) REFERENCES hosts(slug) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS source_runtime (
                    host_slug TEXT PRIMARY KEY,
                    current_device TEXT NOT NULL DEFAULT '',
                    device_list_json TEXT NOT NULL DEFAULT '[]',
                    is_ingesting INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    desired_active INTEGER NOT NULL DEFAULT 0,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY(host_slug) REFERENCES hosts(slug) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS listener_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_slug TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    participant_label TEXT NOT NULL,
                    participant_key TEXT NOT NULL,
                    ip_address TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    joined_at TEXT NOT NULL,
                    left_at TEXT,
                    FOREIGN KEY(room_slug) REFERENCES rooms(slug) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_listener_sessions_room_joined
                ON listener_sessions(room_slug, joined_at DESC);

                CREATE TABLE IF NOT EXISTS meeting_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_slug TEXT NOT NULL,
                    host_slug TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    trigger_mode TEXT NOT NULL DEFAULT 'system',
                    started_by TEXT NOT NULL DEFAULT '',
                    ended_by TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(room_slug) REFERENCES rooms(slug) ON DELETE CASCADE,
                    FOREIGN KEY(host_slug) REFERENCES hosts(slug) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_meeting_sessions_room_started
                ON meeting_sessions(room_slug, started_at DESC);

                CREATE TABLE IF NOT EXISTS meeting_incidents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_slug TEXT NOT NULL,
                    host_slug TEXT,
                    occurred_at TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warn',
                    message TEXT NOT NULL,
                    FOREIGN KEY(room_slug) REFERENCES rooms(slug) ON DELETE CASCADE,
                    FOREIGN KEY(host_slug) REFERENCES hosts(slug) ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_meeting_incidents_room_time
                ON meeting_incidents(room_slug, occurred_at DESC);
                """
            )
            host_columns = {row["name"] for row in connection.execute("PRAGMA table_info(hosts)").fetchall()}
            if "device_order_json" not in host_columns:
                connection.execute(
                    "ALTER TABLE hosts ADD COLUMN device_order_json TEXT NOT NULL DEFAULT '[]'"
                )

            schedule_columns = {row["name"] for row in connection.execute("PRAGMA table_info(schedules)").fetchall()}
            if "enabled" not in schedule_columns:
                connection.execute(
                    "ALTER TABLE schedules ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
                )

    def _seed_defaults(self):
        with self._connect() as connection:
            existing_hosts = connection.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]
            existing_rooms = connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0]
            if existing_hosts or existing_rooms:
                self._sync_seed_metadata(connection)
                return

            timestamp = _utc_now()
            pin_hash = _hash_pin(DEFAULT_PIN)
            inserted_rooms = set()

            for host in get_default_hosts():
                room_seed = ROOM_SEEDS.get(
                    host["slug"],
                    {
                        "slug": host["slug"],
                        "label": host["label"],
                        "description": host["room"],
                        "enabled": host["enabled"],
                    },
                )
                room_slug = room_seed["slug"]
                if room_slug not in inserted_rooms:
                    connection.execute(
                        """
                        INSERT INTO rooms (slug, label, description, enabled, pin_hash, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            room_slug,
                            room_seed["label"],
                            room_seed["description"],
                            1 if room_seed["enabled"] else 0,
                            pin_hash,
                            timestamp,
                        ),
                    )
                    inserted_rooms.add(room_slug)

                preferred_audio, fallback_audio = LEGACY_AUDIO_DEFAULTS.get(host["slug"], ("Scarlett", "Microphone"))
                connection.execute(
                    """
                    INSERT INTO hosts (
                        slug,
                        label,
                        room_slug,
                        enabled,
                        manual_mode,
                        notes,
                        timezone,
                        preferred_audio_pattern,
                        fallback_audio_pattern,
                        device_order_json,
                        heartbeat_token,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        host["slug"],
                        host["label"],
                        room_slug,
                        1 if host["enabled"] else 0,
                        host["manual_mode"],
                        host["notes"],
                        host["timezone"],
                        preferred_audio,
                        fallback_audio,
                        json.dumps([]),
                        secrets.token_urlsafe(24),
                        timestamp,
                    ),
                )

                for schedule in normalize_schedule_rows(host["schedules"]):
                    connection.execute(
                        """
                        INSERT INTO schedules (host_slug, day, start_time, end_time, enabled)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            host["slug"],
                            schedule["day"],
                            schedule["start"],
                            schedule["end"],
                            1 if schedule["enabled"] else 0,
                        ),
                    )
            self._sync_seed_metadata(connection)

    def _sync_seed_metadata(self, connection):
        for host in get_default_hosts():
            room_seed = ROOM_SEEDS.get(
                host["slug"],
                {
                    "slug": host["slug"],
                    "label": host["label"],
                    "description": host["room"],
                    "enabled": host["enabled"],
                },
            )
            connection.execute(
                """
                UPDATE rooms
                SET label = ?, description = ?, enabled = ?, pin_hash = ?
                WHERE slug = ?
                """,
                (
                    room_seed["label"],
                    room_seed["description"],
                    1 if room_seed["enabled"] else 0,
                    _hash_pin(DEFAULT_PIN),
                    room_seed["slug"],
                ),
            )
            connection.execute(
                """
                UPDATE hosts
                SET label = ?, room_slug = ?
                WHERE slug = ?
                """,
                (
                    host["label"],
                    room_seed["slug"],
                    host["slug"],
                ),
            )

    def _schedules_for_host(self, connection, slug: str):
        rows = connection.execute(
            """
            SELECT day, start_time, end_time, enabled
            FROM schedules
            WHERE host_slug = ?
            ORDER BY
                CASE day
                    WHEN 'MON' THEN 0
                    WHEN 'TUE' THEN 1
                    WHEN 'WED' THEN 2
                    WHEN 'THU' THEN 3
                    WHEN 'FRI' THEN 4
                    WHEN 'SAT' THEN 5
                    WHEN 'SUN' THEN 6
                END,
                start_time,
                end_time
            """,
            (slug,),
        ).fetchall()
        return [
            {
                "day": row["day"],
                "start": row["start_time"],
                "end": row["end_time"],
                "enabled": _bool_from_int(row["enabled"]),
            }
            for row in rows
        ]

    def _runtime_for_host(self, connection, slug: str):
        row = connection.execute(
            """
            SELECT current_device, device_list_json, is_ingesting, last_error, desired_active, last_seen_at
            FROM source_runtime
            WHERE host_slug = ?
            """,
            (slug,),
        ).fetchone()
        if not row:
            return None

        return {
            "current_device": row["current_device"],
            "devices": _json_list(row["device_list_json"]),
            "is_ingesting": _bool_from_int(row["is_ingesting"]),
            "last_error": row["last_error"],
            "desired_active": _bool_from_int(row["desired_active"]),
            "last_seen_at": row["last_seen_at"],
        }

    def _enrich_host(self, connection, row: sqlite3.Row, include_secret: bool = False):
        schedules = self._schedules_for_host(connection, row["slug"])
        runtime = self._runtime_for_host(connection, row["slug"])
        schedule_active = is_schedule_active(schedules, timezone=row["timezone"])
        desired_active = False
        if row["manual_mode"] == "force_off":
            desired_active = False
        elif _bool_from_int(row["enabled"]):
            if row["manual_mode"] == "force_on":
                desired_active = True
            elif schedule_active:
                desired_active = True
            elif runtime and runtime["is_ingesting"] and not _is_silence_warning(runtime["last_error"]):
                desired_active = True

        next_change = next_schedule_change(schedules, timezone=row["timezone"])
        payload = {
            "slug": row["slug"],
            "label": row["label"],
            "room_slug": row["room_slug"],
            "room_label": row["room_label"],
            "room_description": row["room_description"],
            "room_enabled": _bool_from_int(row["room_enabled"]),
            "enabled": _bool_from_int(row["enabled"]),
            "manual_mode": row["manual_mode"],
            "notes": row["notes"],
            "timezone": row["timezone"],
            "preferred_audio_pattern": row["preferred_audio_pattern"],
            "fallback_audio_pattern": row["fallback_audio_pattern"],
            "device_order": _json_list(row["device_order_json"]),
            "priority": HOST_PRIORITY.get(row["slug"], 0),
            "schedules": schedules,
            "schedule_active": schedule_active,
            "desired_active": desired_active,
            "next_change": next_change.isoformat() if next_change else None,
            "runtime": runtime,
        }
        if include_secret:
            payload["heartbeat_token"] = row["heartbeat_token"]
        return payload

    def list_rooms(self):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT slug, label, description, enabled, updated_at
                FROM rooms
                ORDER BY label
                """
            ).fetchall()
            return [
                {
                    "slug": row["slug"],
                    "label": row["label"],
                    "description": row["description"],
                    "enabled": _bool_from_int(row["enabled"]),
                    "updated_at": row["updated_at"],
                }
                for row in rows
            ]

    def get_room(self, slug: str, *, include_secret: bool = False):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT slug, label, description, enabled, pin_hash, updated_at
                FROM rooms
                WHERE slug = ?
                """,
                (slug,),
            ).fetchone()
            if not row:
                return None

            payload = {
                "slug": row["slug"],
                "label": row["label"],
                "description": row["description"],
                "enabled": _bool_from_int(row["enabled"]),
                "updated_at": row["updated_at"],
            }
            if include_secret:
                payload["pin_hash"] = row["pin_hash"]
            return payload

    def verify_room_pin(self, slug: str, pin: str) -> bool:
        room = self.get_room(slug, include_secret=True)
        if not room:
            return False
        provided_hash = _hash_pin(pin.strip())
        return hmac.compare_digest(room["pin_hash"], provided_hash)

    def list_hosts(self, *, include_secret: bool = False):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    hosts.slug,
                    hosts.label,
                    hosts.room_slug,
                    hosts.enabled,
                    hosts.manual_mode,
                    hosts.notes,
                    hosts.timezone,
                    hosts.preferred_audio_pattern,
                    hosts.fallback_audio_pattern,
                    hosts.device_order_json,
                    hosts.heartbeat_token,
                    rooms.label AS room_label,
                    rooms.description AS room_description,
                    rooms.enabled AS room_enabled
                FROM hosts
                JOIN rooms ON rooms.slug = hosts.room_slug
                ORDER BY hosts.label
                """
            ).fetchall()
            return [self._enrich_host(connection, row, include_secret=include_secret) for row in rows]

    def get_host(self, slug: str, *, include_secret: bool = False):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    hosts.slug,
                    hosts.label,
                    hosts.room_slug,
                    hosts.enabled,
                    hosts.manual_mode,
                    hosts.notes,
                    hosts.timezone,
                    hosts.preferred_audio_pattern,
                    hosts.fallback_audio_pattern,
                    hosts.device_order_json,
                    hosts.heartbeat_token,
                    rooms.label AS room_label,
                    rooms.description AS room_description,
                    rooms.enabled AS room_enabled
                FROM hosts
                JOIN rooms ON rooms.slug = hosts.room_slug
                WHERE hosts.slug = ?
                """,
                (slug,),
            ).fetchone()
            if not row:
                return None
            return self._enrich_host(connection, row, include_secret=include_secret)

    def update_host_controls(
        self,
        slug: str,
        *,
        enabled: bool,
        manual_mode: str,
        notes: str,
        device_order=None,
    ):
        if manual_mode not in {"auto", "force_on", "force_off"}:
            raise ValueError("manual_mode must be auto, force_on, or force_off")

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT preferred_audio_pattern, fallback_audio_pattern, device_order_json
                FROM hosts
                WHERE slug = ?
                """,
                (slug,),
            ).fetchone()
            if not existing:
                raise ValueError(f"Unknown host: {slug}")

            next_device_order = _json_list(existing["device_order_json"])
            if device_order is not None:
                next_device_order = []
                for item in device_order:
                    text = str(item or "").strip()
                    if text and text not in next_device_order:
                        next_device_order.append(text)

            cursor = connection.execute(
                """
                UPDATE hosts
                SET enabled = ?, manual_mode = ?, notes = ?, preferred_audio_pattern = ?, fallback_audio_pattern = ?, device_order_json = ?, updated_at = ?
                WHERE slug = ?
                """,
                (
                    1 if enabled else 0,
                    manual_mode,
                    (notes or "").strip(),
                    existing["preferred_audio_pattern"],
                    existing["fallback_audio_pattern"],
                    json.dumps(next_device_order),
                    _utc_now(),
                    slug,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown host: {slug}")

    def replace_host_schedule(self, slug: str, schedule_rows):
        if isinstance(schedule_rows, str):
            rows = parse_schedule_text(schedule_rows)
        else:
            rows = normalize_schedule_rows(schedule_rows)
        with self._connect() as connection:
            cursor = connection.execute(
                "SELECT 1 FROM hosts WHERE slug = ?",
                (slug,),
            ).fetchone()
            if not cursor:
                raise ValueError(f"Unknown host: {slug}")

            connection.execute("DELETE FROM schedules WHERE host_slug = ?", (slug,))
            for row in rows:
                connection.execute(
                    """
                    INSERT INTO schedules (host_slug, day, start_time, end_time, enabled)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        row["day"],
                        row["start"],
                        row["end"],
                        1 if row.get("enabled", True) else 0,
                    ),
                )

    def begin_listener_session(
        self,
        room_slug: str,
        *,
        channel: str,
        participant_label: str,
        participant_key: str,
        ip_address: str,
        user_agent: str,
    ) -> int:
        timestamp = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO listener_sessions (
                    room_slug,
                    channel,
                    participant_label,
                    participant_key,
                    ip_address,
                    user_agent,
                    joined_at,
                    left_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    room_slug,
                    channel,
                    (participant_label or "").strip() or "Anonymous listener",
                    (participant_key or "").strip(),
                    (ip_address or "").strip(),
                    (user_agent or "").strip(),
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def end_listener_session(self, session_id: int):
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE listener_sessions
                SET left_at = ?
                WHERE id = ? AND left_at IS NULL
                """,
                (_utc_now(), session_id),
            )

    def list_listener_sessions(self, room_slug: str, *, active_only: bool = False, limit: int = 12):
        query = """
            SELECT id, room_slug, channel, participant_label, participant_key, ip_address, user_agent, joined_at, left_at
            FROM listener_sessions
            WHERE room_slug = ?
        """
        params: list[object] = [room_slug]
        if active_only:
            query += " AND left_at IS NULL"
        query += " ORDER BY joined_at DESC LIMIT ?"
        params.append(limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
            return [
                {
                    "id": row["id"],
                    "room_slug": row["room_slug"],
                    "channel": row["channel"],
                    "participant_label": row["participant_label"],
                    "participant_key": row["participant_key"],
                    "ip_address": row["ip_address"],
                    "user_agent": row["user_agent"],
                    "joined_at": row["joined_at"],
                    "left_at": row["left_at"],
                }
                for row in rows
            ]

    def record_heartbeat(
        self,
        host_slug: str,
        *,
        current_device: str,
        devices,
        is_ingesting: bool,
        last_error: str,
        desired_active: bool,
    ):
        timestamp = _utc_now()
        with self._connect() as connection:
            prior = connection.execute(
                """
                SELECT rooms.slug AS room_slug, source_runtime.last_error
                FROM hosts
                JOIN rooms ON rooms.slug = hosts.room_slug
                LEFT JOIN source_runtime ON source_runtime.host_slug = hosts.slug
                WHERE hosts.slug = ?
                """,
                (host_slug,),
            ).fetchone()

            connection.execute(
                """
                INSERT INTO source_runtime (
                    host_slug,
                    current_device,
                    device_list_json,
                    is_ingesting,
                    last_error,
                    desired_active,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host_slug) DO UPDATE SET
                    current_device = excluded.current_device,
                    device_list_json = excluded.device_list_json,
                    is_ingesting = excluded.is_ingesting,
                    last_error = excluded.last_error,
                    desired_active = excluded.desired_active,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    host_slug,
                    current_device or "",
                    json.dumps(_dedupe_list(devices)),
                    1 if is_ingesting else 0,
                    (last_error or "").strip(),
                    1 if desired_active else 0,
                    timestamp,
                ),
            )

            if prior and last_error and last_error != (prior["last_error"] or ""):
                connection.execute(
                    """
                    INSERT INTO meeting_incidents (room_slug, host_slug, occurred_at, severity, message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (prior["room_slug"], host_slug, timestamp, "warn", last_error),
                )

    def sync_meeting_state(self, room_slug: str, *, active: bool, host_slug: str | None = None, trigger_mode: str = "system", actor: str = ""):
        timestamp = _utc_now()
        with self._connect() as connection:
            active_session = connection.execute(
                """
                SELECT id, room_slug
                FROM meeting_sessions
                WHERE ended_at IS NULL
                ORDER BY started_at DESC
                """
            ).fetchall()

            current = next((row for row in active_session if row["room_slug"] == room_slug), None)

            if active:
                for row in active_session:
                    if row["room_slug"] != room_slug:
                        connection.execute(
                            """
                            UPDATE meeting_sessions
                            SET ended_at = ?, ended_by = ?
                            WHERE id = ?
                            """,
                            (timestamp, actor or trigger_mode, row["id"]),
                        )
                if current:
                    return int(current["id"])

                cursor = connection.execute(
                    """
                    INSERT INTO meeting_sessions (room_slug, host_slug, started_at, trigger_mode, started_by)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (room_slug, host_slug, timestamp, trigger_mode, actor or trigger_mode),
                )
                return int(cursor.lastrowid)

            if current:
                connection.execute(
                    """
                    UPDATE meeting_sessions
                    SET ended_at = ?, ended_by = ?
                    WHERE id = ?
                    """,
                    (timestamp, actor or trigger_mode, current["id"]),
                )
                return int(current["id"])

            return None

    def get_active_meeting(self):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT meeting_sessions.id, meeting_sessions.room_slug, meeting_sessions.host_slug,
                       meeting_sessions.started_at, meeting_sessions.trigger_mode,
                       rooms.label AS room_label
                FROM meeting_sessions
                JOIN rooms ON rooms.slug = meeting_sessions.room_slug
                WHERE meeting_sessions.ended_at IS NULL
                ORDER BY meeting_sessions.started_at DESC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "room_slug": row["room_slug"],
                "host_slug": row["host_slug"],
                "room_label": row["room_label"],
                "started_at": row["started_at"],
                "trigger_mode": row["trigger_mode"],
            }

    def list_meeting_sessions(self, *, limit: int = 20):
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT meeting_sessions.id, meeting_sessions.room_slug, meeting_sessions.host_slug,
                       meeting_sessions.started_at, meeting_sessions.ended_at,
                       meeting_sessions.trigger_mode, meeting_sessions.started_by, meeting_sessions.ended_by,
                       rooms.label AS room_label
                FROM meeting_sessions
                JOIN rooms ON rooms.slug = meeting_sessions.room_slug
                ORDER BY meeting_sessions.started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            sessions = []
            for row in rows:
                sessions.append(self._meeting_summary(connection, row))
            return sessions

    def _meeting_summary(self, connection, row: sqlite3.Row):
        started_at = row["started_at"]
        ended_at = row["ended_at"] or _utc_now()
        listener_count = connection.execute(
            """
            SELECT COUNT(DISTINCT participant_key)
            FROM listener_sessions
            WHERE room_slug = ?
              AND joined_at <= ?
              AND COALESCE(left_at, ?) >= ?
            """,
            (row["room_slug"], ended_at, ended_at, started_at),
        ).fetchone()[0]
        incident_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM meeting_incidents
            WHERE room_slug = ?
              AND occurred_at >= ?
              AND occurred_at <= ?
            """,
            (row["room_slug"], started_at, ended_at),
        ).fetchone()[0]
        return {
            "id": row["id"],
            "room_slug": row["room_slug"],
            "room_label": row["room_label"],
            "host_slug": row["host_slug"],
            "started_at": started_at,
            "ended_at": row["ended_at"],
            "trigger_mode": row["trigger_mode"],
            "started_by": row["started_by"],
            "ended_by": row["ended_by"],
            "listener_count": int(listener_count or 0),
            "incident_count": int(incident_count or 0),
        }

    def get_meeting_report(self, meeting_id: int):
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT meeting_sessions.id, meeting_sessions.room_slug, meeting_sessions.host_slug,
                       meeting_sessions.started_at, meeting_sessions.ended_at,
                       meeting_sessions.trigger_mode, meeting_sessions.started_by, meeting_sessions.ended_by,
                       rooms.label AS room_label
                FROM meeting_sessions
                JOIN rooms ON rooms.slug = meeting_sessions.room_slug
                WHERE meeting_sessions.id = ?
                """,
                (meeting_id,),
            ).fetchone()
            if not row:
                return None

            summary = self._meeting_summary(connection, row)
            ended_at = row["ended_at"] or _utc_now()
            listeners = connection.execute(
                """
                SELECT participant_label, channel, ip_address, user_agent, joined_at, left_at
                FROM listener_sessions
                WHERE room_slug = ?
                  AND joined_at <= ?
                  AND COALESCE(left_at, ?) >= ?
                ORDER BY joined_at
                """,
                (row["room_slug"], ended_at, ended_at, row["started_at"]),
            ).fetchall()
            incidents = connection.execute(
                """
                SELECT host_slug, occurred_at, severity, message
                FROM meeting_incidents
                WHERE room_slug = ?
                  AND occurred_at >= ?
                  AND occurred_at <= ?
                ORDER BY occurred_at
                """,
                (row["room_slug"], row["started_at"], ended_at),
            ).fetchall()

            summary["listeners"] = [
                {
                    "participant_label": listener["participant_label"],
                    "channel": listener["channel"],
                    "ip_address": listener["ip_address"],
                    "user_agent": listener["user_agent"],
                    "joined_at": listener["joined_at"],
                    "left_at": listener["left_at"],
                }
                for listener in listeners
            ]
            summary["incidents"] = [
                {
                    "host_slug": incident["host_slug"],
                    "occurred_at": incident["occurred_at"],
                    "severity": incident["severity"],
                    "message": incident["message"],
                }
                for incident in incidents
            ]
            return summary
