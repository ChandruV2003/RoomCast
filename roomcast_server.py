"""Server-centered audio bridge app with separate public and admin views."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import threading
import hmac
import secrets
import time
import csv
import io
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from roomcast_store import RoomCastStore


STATUS_TTL_SECONDS = 45
ROOM_ALIASES = {
    "study-room": "Room A",
    "meeting-hall": "Room B",
    "diagnostics": "Diagnostics",
}
VISIBLE_ROOM_SLUGS = {"study-room", "meeting-hall"}


def _parse_iso8601(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class RoomState:
    """In-memory stream state for a single room."""

    active_host_slug: str | None = None
    active: bool = False
    started_at: str | None = None
    last_chunk_at: str | None = None
    bytes_received: int = 0
    listeners: dict[int, queue.Queue] = field(default_factory=dict)


class RoomStreamHub:
    """Broadcast incoming byte streams to all active listeners in a room."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rooms: dict[str, RoomState] = {}

    def _get_room(self, room_slug: str) -> RoomState:
        if room_slug not in self._rooms:
            self._rooms[room_slug] = RoomState()
        return self._rooms[room_slug]

    def start_broadcast(self, room_slug: str, host_slug: str):
        with self._lock:
            room = self._get_room(room_slug)
            room.active_host_slug = host_slug
            room.active = True
            room.started_at = datetime.now(timezone.utc).isoformat()
            room.last_chunk_at = None
            room.bytes_received = 0

    def publish(self, room_slug: str, chunk: bytes):
        with self._lock:
            room = self._get_room(room_slug)
            room.last_chunk_at = datetime.now(timezone.utc).isoformat()
            room.bytes_received += len(chunk)
            listeners = list(room.listeners.values())

        for listener in listeners:
            try:
                listener.put_nowait(chunk)
            except queue.Full:
                try:
                    listener.get_nowait()
                except queue.Empty:
                    pass
                try:
                    listener.put_nowait(chunk)
                except queue.Full:
                    continue

    def finish_broadcast(self, room_slug: str, host_slug: str):
        with self._lock:
            room = self._get_room(room_slug)
            if room.active_host_slug != host_slug:
                return

            room.active = False
            room.active_host_slug = None
            listeners = list(room.listeners.values())

        for listener in listeners:
            try:
                listener.put_nowait(None)
            except queue.Full:
                continue

    def listen(self, room_slug: str):
        listener = queue.Queue(maxsize=64)
        listener_id = id(listener)

        with self._lock:
            room = self._get_room(room_slug)
            room.listeners[listener_id] = listener

        try:
            while True:
                try:
                    chunk = listener.get(timeout=25)
                except queue.Empty:
                    break

                if chunk is None:
                    break
                yield chunk
        finally:
            with self._lock:
                room = self._get_room(room_slug)
                room.listeners.pop(listener_id, None)

    def status(self, room_slug: str):
        with self._lock:
            room = self._get_room(room_slug)
            return {
                "broadcasting": room.active,
                "active_host_slug": room.active_host_slug,
                "started_at": room.started_at,
                "last_chunk_at": room.last_chunk_at,
                "bytes_received": room.bytes_received,
                "listener_count": len(room.listeners),
            }


def create_app(test_config: dict | None = None, *, store: RoomCastStore | None = None, hub: RoomStreamHub | None = None):
    """Build the Flask app so tests can inject isolated state."""

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("ROOMCAST_SECRET_KEY", "roomcast-dev-secret"),
        ROOMCAST_DB_PATH=os.getenv("ROOMCAST_DB_PATH"),
        ROOMCAST_PUBLIC_NAME=os.getenv("ROOMCAST_PUBLIC_NAME", "NTC Newark WebCall"),
        ROOMCAST_LISTENER_NAME=os.getenv("ROOMCAST_LISTENER_NAME", "Main Stream Hall"),
        ROOMCAST_ADMIN_PASSWORD=os.getenv("ROOMCAST_ADMIN_PASSWORD", ""),
        ROOMCAST_TELEPHONY_SECRET=os.getenv("ROOMCAST_TELEPHONY_SECRET", ""),
        ROOMCAST_TWILIO_WEBHOOK_TOKEN=os.getenv("ROOMCAST_TWILIO_WEBHOOK_TOKEN", ""),
        ROOMCAST_GEOLOOKUP_URL=os.getenv("ROOMCAST_GEOLOOKUP_URL", "https://ipwho.is/{ip}"),
    )
    if test_config:
        app.config.update(test_config)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True

    roomcast_store = store or RoomCastStore(app.config.get("ROOMCAST_DB_PATH"))
    stream_hub = hub or RoomStreamHub()
    app.roomcast_store = roomcast_store
    app.stream_hub = stream_hub
    geolookup_cache: dict[str, tuple[float, str | None]] = {}

    def _project_name() -> str:
        return app.config["ROOMCAST_PUBLIC_NAME"]

    def _listener_name() -> str:
        return app.config["ROOMCAST_LISTENER_NAME"]

    def _display_timezone() -> ZoneInfo:
        return ZoneInfo("America/New_York")

    def _friendly_timestamp(value: str | None, *, fallback: str = "Unknown") -> str:
        parsed = _parse_iso8601(value)
        if not parsed:
            return fallback
        localized = parsed.astimezone(_display_timezone())
        hour = localized.strftime("%I").lstrip("0") or "12"
        return f"{localized.strftime('%b')} {localized.day} · {hour}:{localized.strftime('%M %p')}"

    def _friendly_clock(value: str | None) -> str:
        if not value:
            return "Unknown"
        try:
            parsed = datetime.strptime(value, "%H:%M")
        except ValueError:
            return value
        hour = parsed.strftime("%I").lstrip("0") or "12"
        return f"{hour}:{parsed.strftime('%M %p')}"

    def _device_options(host: dict, runtime: dict) -> list[str]:
        ordered = []
        for candidate in host.get("device_order", []) + runtime.get("devices", []) + [runtime.get("current_device", "")]:
            text = (candidate or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        return ordered

    def _device_slots(device_options: list[str], stored_order: list[str], *, slots: int = 3) -> list[str]:
        selected = []
        source = stored_order or device_options
        for candidate in source:
            text = (candidate or "").strip()
            if text and text not in selected:
                selected.append(text)
        while len(selected) < slots:
            selected.append("")
        return selected[:slots]

    def _schedule_rows_display(rows) -> list[dict]:
        day_labels = {
            "MON": "Mon",
            "TUE": "Tue",
            "WED": "Wed",
            "THU": "Thu",
            "FRI": "Fri",
            "SAT": "Sat",
            "SUN": "Sun",
        }
        rendered = []
        for row in rows or []:
            rendered.append(
                {
                    **row,
                    "label": f"{day_labels.get(row['day'], row['day'])} {_friendly_clock(row['start'])} - {_friendly_clock(row['end'])}",
                    "status_label": "On" if row.get("enabled", True) else "Off",
                }
            )
        return rendered

    def _compact_error(message: str | None) -> str:
        value = (message or "").strip()
        if not value:
            return ""
        lowered = value.lower()
        if "connect time" in lowered and "/api/source/heartbeat" in lowered:
            return "Server heartbeat timed out."
        if "max retries exceeded" in lowered and "httpsconnectionpool" in lowered:
            return "Could not reach the server."
        if len(value) > 140:
            return f"{value[:137].rstrip()}..."
        return value

    def _room_alias(room_slug: str, fallback: str) -> str:
        return ROOM_ALIASES.get(room_slug, fallback)

    def _primary_ip(value: str | None) -> str:
        return (value or "").split(",")[0].strip()

    def _listener_location(ip_address: str | None) -> str | None:
        ip_value = _primary_ip(ip_address)
        if not ip_value:
            return None
        try:
            parsed = ipaddress.ip_address(ip_value)
        except ValueError:
            return None
        if parsed.is_loopback or parsed.is_private or parsed.is_link_local:
            return "Local network"

        cached = geolookup_cache.get(ip_value)
        if cached and cached[0] > time.time():
            return cached[1]

        endpoint_template = app.config.get("ROOMCAST_GEOLOOKUP_URL", "").strip()
        if not endpoint_template:
            return None

        location_label = None
        try:
            with urlopen(endpoint_template.format(ip=ip_value), timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("success", True):
                parts = [
                    payload.get("city"),
                    payload.get("region") or payload.get("region_name"),
                    payload.get("country_code") or payload.get("country"),
                ]
                location_label = ", ".join(part for part in parts if part)
        except Exception:
            location_label = None

        geolookup_cache[ip_value] = (time.time() + 86400, location_label)
        return location_label

    def _decorate_listener(listener: dict):
        location_label = _listener_location(listener.get("ip_address"))
        return {
            **listener,
            "joined_label": _friendly_timestamp(listener.get("joined_at")),
            "left_label": _friendly_timestamp(listener.get("left_at"), fallback="Connected"),
            "location_label": location_label,
            "summary": " · ".join(
                part
                for part in (
                    listener.get("participant_label"),
                    location_label,
                    listener.get("channel"),
                )
                if part
            ),
        }

    def _telephony_secret() -> str:
        return app.config.get("ROOMCAST_TELEPHONY_SECRET") or app.config["SECRET_KEY"]

    def _sign_telephony_stream(room_slug: str, expires_at: int, participant_label: str, channel: str) -> str:
        payload = f"{room_slug}:{expires_at}:{participant_label}:{channel}".encode("utf-8")
        return hmac.new(_telephony_secret().encode("utf-8"), payload, hashlib.sha256).hexdigest()

    def _telephony_stream_url(
        room_slug: str,
        *,
        participant_label: str = "Phone caller",
        channel: str = "phone",
        expires_in: int = 300,
    ) -> str:
        expires_at = int(time.time()) + expires_in
        signature = _sign_telephony_stream(room_slug, expires_at, participant_label, channel)
        return url_for(
            "telephony_stream",
            room_slug=room_slug,
            exp=expires_at,
            sig=signature,
            label=participant_label,
            channel=channel,
            _external=True,
        )

    def _telephony_stream_is_valid(room_slug: str, expires_at: str, signature: str, participant_label: str, channel: str) -> bool:
        try:
            expires = int(expires_at)
        except (TypeError, ValueError):
            return False
        if expires < int(time.time()):
            return False
        expected = _sign_telephony_stream(room_slug, expires, participant_label, channel)
        return hmac.compare_digest(expected, signature or "")

    def _is_admin() -> bool:
        return bool(session.get("roomcast_admin"))

    def _is_authorized(room_slug: str) -> bool:
        return room_slug in set(session.get("allowed_rooms", []))

    def _authorized_rooms() -> set[str]:
        return set(session.get("allowed_rooms", []))

    def _allow_room(room_slug: str):
        allowed = set(session.get("allowed_rooms", []))
        allowed.add(room_slug)
        session["allowed_rooms"] = sorted(allowed)
        session.modified = True

    def _runtime_online(host: dict | None) -> bool:
        if not host or not host.get("runtime"):
            return False
        last_seen = _parse_iso8601(host["runtime"].get("last_seen_at"))
        if not last_seen:
            return False
        return last_seen >= datetime.now(timezone.utc) - timedelta(seconds=STATUS_TTL_SECONDS)

    def _host_snapshots():
        hosts = []
        for host in roomcast_store.list_hosts(include_secret=True):
            runtime = host.get("runtime") or {}
            device_options = _device_options(host, runtime)
            schedule_rows = _schedule_rows_display(host["schedules"])
            active_schedule_rows = [row for row in schedule_rows if row["enabled"]]
            hosts.append(
                {
                    **host,
                    "source_online": _runtime_online(host),
                    "current_device": runtime.get("current_device", ""),
                    "known_devices": runtime.get("devices", []),
                    "device_options": device_options,
                    "device_slots": _device_slots(device_options, host.get("device_order", [])),
                    "schedule_rows": schedule_rows,
                    "active_schedule_rows": active_schedule_rows,
                    "last_error": _compact_error(runtime.get("last_error", "")),
                    "is_ingesting": runtime.get("is_ingesting", False),
                    "last_seen_at": runtime.get("last_seen_at"),
                    "priority": host.get("priority", 0),
                    "room_alias": _room_alias(host["room_slug"], host["room_label"]),
                    "broadcasting": stream_hub.status(host["room_slug"])["broadcasting"],
                    "listener_count": stream_hub.status(host["room_slug"])["listener_count"],
                    "active_listeners": [
                        _decorate_listener(listener)
                        for listener in roomcast_store.list_listener_sessions(host["room_slug"], active_only=True, limit=6)
                    ],
                    "recent_listeners": [
                        _decorate_listener(listener)
                        for listener in roomcast_store.list_listener_sessions(host["room_slug"], active_only=False, limit=6)
                    ],
                }
            )
        ordered = sorted(hosts, key=lambda host: (host["room_alias"], host["label"]))
        return ordered

    def _visible_hosts():
        return [host for host in _host_snapshots() if host["room_slug"] in VISIBLE_ROOM_SLUGS]

    def _duration_text(started_at: str | None, ended_at: str | None = None) -> str:
        start_value = _parse_iso8601(started_at)
        end_value = _parse_iso8601(ended_at) if ended_at else datetime.now(timezone.utc)
        if not start_value or not end_value:
            return "Unknown"
        total_seconds = max(0, int((end_value - start_value).total_seconds()))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}h {minutes}m"
        if minutes:
            return f"{minutes}m {seconds}s"
        return f"{seconds}s"

    def _meeting_history(limit: int = 12):
        rows = []
        for meeting in roomcast_store.list_meeting_sessions(limit=limit):
            if meeting["room_slug"] not in VISIBLE_ROOM_SLUGS:
                continue
            rows.append(
                {
                    **meeting,
                    "room_alias": _room_alias(meeting["room_slug"], meeting["room_label"]),
                    "duration_text": _duration_text(meeting["started_at"], meeting["ended_at"]),
                    "started_label": _friendly_timestamp(meeting["started_at"]),
                }
            )
        return rows

    def _selected_room_slug():
        requested = (request.args.get("room") or "").strip()
        if requested in VISIBLE_ROOM_SLUGS:
            return requested
        active = roomcast_store.get_active_meeting()
        if active and active["room_slug"] in VISIBLE_ROOM_SLUGS:
            return active["room_slug"]
        return None

    def _panel_context():
        hosts = _visible_hosts()
        host_by_room = {host["room_slug"]: host for host in hosts}
        selected_room_slug = _selected_room_slug()
        selected_host = host_by_room.get(selected_room_slug)
        active_meeting = roomcast_store.get_active_meeting()
        active_room_slug = active_meeting["room_slug"] if active_meeting and active_meeting["room_slug"] in VISIBLE_ROOM_SLUGS else None
        focus_room_slug = active_room_slug or selected_room_slug
        focus_host = host_by_room.get(focus_room_slug)
        if active_meeting:
            active_meeting = {
                **active_meeting,
                "started_label": _friendly_timestamp(active_meeting["started_at"]),
            }

        room_options = []
        for room_slug in ("study-room", "meeting-hall"):
            host = host_by_room.get(room_slug)
            if not host:
                continue
            room_options.append(
                {
                    "slug": room_slug,
                    "alias": host["room_alias"],
                    "label": host["room_label"],
                    "selected": room_slug == selected_room_slug,
                    "active": room_slug == active_room_slug,
                    "online": host["source_online"],
                    "listener_count": host["listener_count"],
                    "is_ingesting": host["is_ingesting"],
                }
            )

        current_listeners = (focus_host or {}).get("active_listeners", [])
        recent_listeners = (focus_host or {}).get("recent_listeners", [])
        control_host = focus_host or selected_host

        if focus_host and focus_host.get("broadcasting"):
            call_state = {
                "tone": "good",
                "label": "Call live",
                "headline": f"{focus_host['room_alias']} is live",
                "detail": f"Started {active_meeting['started_label']}." if active_meeting else "Audio is flowing now.",
            }
        elif control_host and (control_host.get("desired_active") or control_host.get("is_ingesting")):
            call_state = {
                "tone": "warn",
                "label": "Starting",
                "headline": f"Starting {control_host['room_alias']}",
                "detail": "Waiting for the source laptop and live audio.",
            }
        else:
            call_state = {
                "tone": "idle",
                "label": "Standby",
                "headline": "Ready to start",
                "detail": "Press Start Call, choose Room A or Room B, and the agent will handle the rest.",
            }

        return {
            "hosts": hosts,
            "selected_host": selected_host,
            "focus_host": focus_host,
            "control_host": control_host,
            "selected_room_slug": selected_room_slug,
            "active_meeting": active_meeting,
            "active_room_slug": active_room_slug,
            "focus_room_slug": focus_room_slug,
            "room_options": room_options,
            "current_listeners": current_listeners,
            "recent_listeners": recent_listeners,
            "call_state": call_state,
            "resume_available": bool(control_host and control_host["manual_mode"] != "auto"),
            "call_history": _meeting_history(),
        }

    def _sync_visible_meetings(actor: str):
        for host in _visible_hosts():
            trigger_mode = host["manual_mode"] if host["manual_mode"] != "auto" else "schedule"
            roomcast_store.sync_meeting_state(
                host["room_slug"],
                active=host["desired_active"],
                host_slug=host["slug"],
                trigger_mode=trigger_mode,
                actor=actor,
            )

    def _device_order_from_form():
        ordered = []
        for value in request.form.getlist("device_order"):
            choice = (value or "").strip()
            if choice and choice not in ordered:
                ordered.append(choice)
        return ordered

    def _schedule_rows_from_form():
        days = request.form.getlist("schedule_day")
        starts = request.form.getlist("schedule_start")
        ends = request.form.getlist("schedule_end")
        states = request.form.getlist("schedule_enabled")
        if not (len(days) == len(starts) == len(ends) == len(states)):
            raise ValueError("Schedule entries were not submitted correctly.")

        rows = []
        for day, start, end, state in zip(days, starts, ends, states):
            if not any(((day or "").strip(), (start or "").strip(), (end or "").strip())):
                continue
            if not (day and start and end):
                raise ValueError("Each schedule row needs a day, start time, and end time.")
            rows.append(
                {
                    "day": day,
                    "start": start,
                    "end": end,
                    "enabled": (state or "1") == "1",
                }
            )
        return rows

    def _candidate_public_rooms(pin: str, allowed_room_slugs: set[str] | None = None):
        ordered_rooms = [
            room for room in roomcast_store.list_rooms()
            if room["enabled"] and room["slug"] in VISIBLE_ROOM_SLUGS
        ]
        room_order = {room["slug"]: index for index, room in enumerate(ordered_rooms)}
        candidates = []

        for room in ordered_rooms:
            if allowed_room_slugs is not None and room["slug"] not in allowed_room_slugs:
                continue
            if pin is not None and not roomcast_store.verify_room_pin(room["slug"], pin):
                continue
            snapshot = _room_snapshot(room["slug"])
            if snapshot:
                candidates.append((snapshot, room_order[snapshot["slug"]]))
        return candidates

    def _select_public_room(candidates):
        if not candidates:
            return None

        return max(
            candidates,
            key=lambda item: (
                1 if item[0]["broadcasting"] else 0,
                item[0]["host_priority"],
                1 if item[0]["desired_active"] else 0,
                1 if item[0]["source_online"] else 0,
                1 if item[0]["schedule_active"] else 0,
                -item[1],
            ),
        )[0]

    def _has_public_signal(snapshot: dict) -> bool:
        return any(
            (
                snapshot["broadcasting"],
                snapshot["desired_active"],
                snapshot["schedule_active"],
            )
        )

    def _authorize_room(pin: str, room_slug: str | None = None):
        pin_value = (pin or "").strip()
        snapshot = None
        if room_slug:
            room = roomcast_store.get_room(room_slug)
            if room and room["enabled"] and roomcast_store.verify_room_pin(room_slug, pin_value):
                snapshot = _room_snapshot(room_slug)
                if snapshot:
                    _allow_room(snapshot["slug"])
        else:
            candidates = _candidate_public_rooms(pin_value)
            if candidates:
                for candidate, _ in candidates:
                    _allow_room(candidate["slug"])
                snapshot = _select_public_room(candidates)
        if not snapshot:
            return None
        session.setdefault("listener_key", secrets.token_urlsafe(12))
        return snapshot

    def _ingest_conflicts(hosts):
        active_hosts = [host for host in hosts if host["is_ingesting"]]
        if len(active_hosts) < 2:
            return []

        ordered = sorted(active_hosts, key=lambda host: (-host["priority"], host["label"]))
        preferred = ordered[0]
        conflicts = []
        for host in ordered[1:]:
            conflicts.append(
                f"Input clash: {preferred['label']} is preferred over {host['label']}."
            )
        return conflicts

    def _room_snapshot(room_slug: str):
        room = roomcast_store.get_room(room_slug)
        if not room:
            return None

        host = None
        for candidate in _host_snapshots():
            if candidate["room_slug"] == room_slug:
                host = candidate
                break

        hub_status = stream_hub.status(room_slug)
        return {
            "slug": room["slug"],
            "label": room["label"],
            "description": room["description"],
            "enabled": room["enabled"],
            "host_slug": host["slug"] if host else None,
            "host_label": host["label"] if host else None,
            "host_notes": host["notes"] if host else "",
            "desired_active": host["desired_active"] if host else False,
            "schedule_active": host["schedule_active"] if host else False,
            "source_online": _runtime_online(host),
            "runtime": host["runtime"] if host else None,
            "broadcasting": hub_status["broadcasting"],
            "listener_count": hub_status["listener_count"],
            "last_chunk_at": hub_status["last_chunk_at"],
            "current_device": (host["runtime"] or {}).get("current_device") if host else "",
            "last_error": _compact_error((host["runtime"] or {}).get("last_error")) if host else "",
            "schedule_rows": host["schedule_rows"] if host else [],
            "next_change": host["next_change"] if host else None,
            "host_priority": host["priority"] if host else 0,
            "room_alias": _room_alias(room["slug"], room["label"]),
        }

    def _public_status():
        hosts = _visible_hosts()
        if not hosts:
            return {
                "headline": "Meeting not active right now",
                "detail": "Enter the shared PIN when the meeting begins.",
                "state": "warn",
            }

        ranked = sorted(
            hosts,
            key=lambda host: (
                1 if host["is_ingesting"] else 0,
                1 if host["desired_active"] else 0,
                host["priority"],
                1 if host["source_online"] else 0,
                1 if host["schedule_active"] else 0,
            ),
            reverse=True,
        )
        best = ranked[0]
        if best["is_ingesting"]:
            return {
                "headline": "Meeting active now",
                "detail": "Live audio is available.",
                "state": "good",
            }
        if best["desired_active"]:
            return {
                "headline": "Meeting starting",
                "detail": "Stay on this page and the audio will come through when the source is ready.",
                "state": "warn",
            }
        return {
            "headline": "Meeting not active right now",
            "detail": "Enter the shared PIN when the meeting begins.",
            "state": "warn",
        }

    def _resolve_public_room(pin: str):
        return _select_public_room(_candidate_public_rooms(pin))

    def _active_public_room():
        allowed_room_slugs = _authorized_rooms()
        if not allowed_room_slugs:
            return None
        candidates = _candidate_public_rooms(None, allowed_room_slugs=allowed_room_slugs)
        signaled = [item for item in candidates if _has_public_signal(item[0])]
        return _select_public_room(signaled)

    def _live_snapshot():
        snapshot = _active_public_room()
        if snapshot:
            return snapshot
        return {
            "slug": "",
            "label": _listener_name(),
            "description": "",
            "enabled": True,
            "host_slug": None,
            "host_label": None,
            "host_notes": "",
            "desired_active": False,
            "schedule_active": False,
            "source_online": False,
            "runtime": None,
            "broadcasting": False,
            "listener_count": 0,
            "last_chunk_at": None,
            "current_device": "",
            "last_error": "",
            "schedule_rows": [],
            "next_change": None,
            "host_priority": 0,
            "room_alias": _listener_name(),
        }

    @app.get("/z")
    def z():
        return jsonify(
            {
                "ok": True,
                "service": _project_name(),
            }
        )

    @app.get("/")
    def index():
        direct_pin = (request.args.get("pin") or "").strip()
        if direct_pin:
            snapshot = _authorize_room(direct_pin)
            if snapshot:
                return redirect(url_for("live_page"))
        return render_template_string(
            LANDING_TEMPLATE,
            project_name=_project_name(),
            listener_name=_listener_name(),
            public_status=_public_status(),
            error=request.args.get("error"),
            message=request.args.get("message"),
        )

    @app.post("/join")
    def join():
        pin = request.form.get("join_pin", "").strip()
        room_slug = request.form.get("room_slug", "").strip()
        listener_name = request.form.get("listener_name", "").strip()
        snapshot = _authorize_room(pin, room_slug=room_slug or None)

        if not snapshot:
            return redirect(url_for("index", error="PIN was not accepted."))

        if listener_name:
            session["listener_name"] = listener_name
        if not room_slug:
            return redirect(url_for("live_page"))
        return redirect(url_for("room_page", room_slug=snapshot["slug"]))

    @app.get("/p/<pin>")
    def direct_pin(pin: str):
        snapshot = _authorize_room(pin)
        if not snapshot:
            return redirect(url_for("index", error="PIN was not accepted."))
        return redirect(url_for("live_page"))

    @app.get("/live")
    def live_page():
        if not _authorized_rooms():
            return redirect(url_for("index", error="Join that room with a PIN first."))

        return render_template_string(
            ROOM_TEMPLATE,
            project_name=_project_name(),
            listener_name=_listener_name(),
            room=_live_snapshot(),
            stream_url=url_for("listen_live"),
            status_url=url_for("live_status"),
        )

    @app.get("/room/<room_slug>")
    def room_page(room_slug: str):
        snapshot = _room_snapshot(room_slug)
        if not snapshot or not snapshot["enabled"]:
            return redirect(url_for("index", error="Room is not available."))
        if not _is_authorized(room_slug):
            return redirect(url_for("index", error="Join that room with a PIN first."))

        return render_template_string(
            ROOM_TEMPLATE,
            project_name=_project_name(),
            listener_name=_listener_name(),
            room=snapshot,
            stream_url=url_for("listen", room_slug=room_slug),
            status_url=url_for("room_status", room_slug=room_slug),
        )

    @app.get("/api/live/status")
    def live_status():
        snapshot = _active_public_room()
        if not snapshot:
            if not _authorized_rooms():
                return jsonify({"error": "pin required"}), 403
            return jsonify(_live_snapshot())
        return jsonify(snapshot)

    @app.get("/api/rooms/<room_slug>/status")
    def room_status(room_slug: str):
        snapshot = _room_snapshot(room_slug)
        if not snapshot:
            return jsonify({"error": "unknown room"}), 404
        if not _is_authorized(room_slug):
            return jsonify({"error": "pin required"}), 403
        return jsonify(snapshot)

    @app.get("/admin")
    def admin_entry():
        if _is_admin():
            return redirect(url_for("admin_panel"))
        return render_template_string(
            ADMIN_LOGIN_TEMPLATE,
            project_name=_project_name(),
            error=request.args.get("error"),
        )

    @app.post("/admin/login")
    def admin_login():
        password = request.form.get("password", "")
        expected = app.config.get("ROOMCAST_ADMIN_PASSWORD", "")
        if not expected:
            return redirect(url_for("admin_entry", error="Admin access is not configured yet."))
        if not hmac.compare_digest(password, expected):
            return redirect(url_for("admin_entry", error="Password was not accepted."))

        session["roomcast_admin"] = True
        session.modified = True
        return redirect(url_for("admin_panel"))

    @app.post("/admin/logout")
    def admin_logout():
        session.pop("roomcast_admin", None)
        session.modified = True
        return redirect(url_for("index", message="Signed out of admin."))

    @app.get("/admin/panel")
    def admin_panel():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        return render_template_string(
            ADMIN_PANEL_TEMPLATE,
            project_name=_project_name(),
            **_panel_context(),
            conflicts=_ingest_conflicts(_visible_hosts()),
            message=request.args.get("message"),
            error=request.args.get("error"),
            logout_url=url_for("admin_logout"),
            settings_url=url_for("admin_settings"),
        )

    @app.get("/admin/settings")
    def admin_settings():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        hosts = _visible_hosts()
        selected_room_slug = _selected_room_slug()
        available_rooms = {host["room_slug"] for host in hosts}
        if selected_room_slug not in available_rooms:
            selected_room_slug = hosts[0]["room_slug"] if hosts else ""

        return render_template_string(
            SETTINGS_TEMPLATE,
            project_name=_project_name(),
            hosts=hosts,
            selected_room_slug=selected_room_slug,
            message=request.args.get("message"),
            error=request.args.get("error"),
            panel_url=url_for("admin_panel"),
            logout_url=url_for("admin_logout"),
        )

    @app.post("/admin/call/start")
    def admin_start_call():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        room_slug = (request.form.get("room_slug") or "").strip()
        if room_slug not in VISIBLE_ROOM_SLUGS:
            return redirect(url_for("admin_panel", error="Choose Room A or Room B."))

        for host in _visible_hosts():
            next_mode = "force_on" if host["room_slug"] == room_slug else "force_off"
            roomcast_store.update_host_controls(
                host["slug"],
                enabled=host["enabled"],
                manual_mode=next_mode,
                notes=host["notes"],
                device_order=host["device_order"],
            )
        _sync_visible_meetings("admin-start")
        return redirect(url_for("admin_panel", room=room_slug, message=f"{_room_alias(room_slug, room_slug)} is starting."))

    @app.post("/admin/call/stop")
    def admin_stop_call():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        room_slug = (request.form.get("room_slug") or "").strip() or _selected_room_slug()
        for host in _visible_hosts():
            next_mode = "force_off" if host["room_slug"] == room_slug else host["manual_mode"]
            roomcast_store.update_host_controls(
                host["slug"],
                enabled=host["enabled"],
                manual_mode=next_mode,
                notes=host["notes"],
                device_order=host["device_order"],
            )
        _sync_visible_meetings("admin-stop")
        return redirect(url_for("admin_panel", message=f"{_room_alias(room_slug, room_slug)} was stopped."))

    @app.post("/admin/call/schedule")
    def admin_use_schedule():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        for host in _visible_hosts():
            roomcast_store.update_host_controls(
                host["slug"],
                enabled=host["enabled"],
                manual_mode="auto",
                notes=host["notes"],
                device_order=host["device_order"],
            )
        _sync_visible_meetings("admin-schedule")
        return redirect(url_for("admin_panel", message="Automatic schedule restored."))

    @app.post("/admin/hosts/<slug>")
    def admin_update_host(slug: str):
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        try:
            host = roomcast_store.get_host(slug)
            room_slug = host["room_slug"] if host else ""
            manual_mode = request.form.get("manual_mode", "auto")
            enabled = request.form.get("enabled") == "on"
            notes = request.form.get("notes", "")
            device_order = _device_order_from_form()
            schedule_rows = _schedule_rows_from_form()
            roomcast_store.update_host_controls(
                slug,
                enabled=enabled,
                manual_mode=manual_mode,
                notes=notes,
                device_order=device_order,
            )
            if manual_mode == "force_on":
                for other_host in roomcast_store.list_hosts():
                    if other_host["slug"] == slug or other_host["room_slug"] not in VISIBLE_ROOM_SLUGS:
                        continue
                    if other_host["manual_mode"] == "force_on":
                        roomcast_store.update_host_controls(
                            other_host["slug"],
                            enabled=other_host["enabled"],
                            manual_mode="auto",
                            notes=other_host["notes"],
                            device_order=other_host["device_order"],
                        )
            roomcast_store.replace_host_schedule(slug, schedule_rows)
            _sync_visible_meetings("admin-settings")
            return redirect(url_for("admin_settings", room=room_slug, message=f"Updated {slug}"))
        except ValueError as exc:
            return redirect(url_for("admin_settings", room=room_slug, error=str(exc)))

    @app.get("/admin/reports/<int:meeting_id>.csv")
    def admin_report_download(meeting_id: int):
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        report = roomcast_store.get_meeting_report(meeting_id)
        if not report:
            return jsonify({"error": "unknown report"}), 404

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Meeting", report["room_label"]])
        writer.writerow(["Started", report["started_at"]])
        writer.writerow(["Ended", report["ended_at"] or "Active"])
        writer.writerow(["Listeners", report["listener_count"]])
        writer.writerow(["Incidents", report["incident_count"]])
        writer.writerow([])
        writer.writerow(["Listeners"])
        writer.writerow(["Name", "Channel", "IP", "Joined", "Left"])
        for listener in report["listeners"]:
            writer.writerow([
                listener["participant_label"],
                listener["channel"],
                listener["ip_address"],
                listener["joined_at"],
                listener["left_at"] or "",
            ])
        writer.writerow([])
        writer.writerow(["Incidents"])
        writer.writerow(["Time", "Severity", "Host", "Message"])
        for incident in report["incidents"]:
            writer.writerow([
                incident["occurred_at"],
                incident["severity"],
                incident["host_slug"] or "",
                incident["message"],
            ])
        csv_payload = output.getvalue()
        response = Response(csv_payload, mimetype="text/csv")
        response.headers["Content-Disposition"] = f'attachment; filename="webcall-report-{meeting_id}.csv"'
        return response

    @app.get("/listen/<room_slug>.mp3")
    def listen(room_slug: str):
        room = roomcast_store.get_room(room_slug)
        if not room or not room["enabled"]:
            return jsonify({"error": "unknown room"}), 404
        if not _is_authorized(room_slug):
            return jsonify({"error": "pin required"}), 403

        participant_label = session.get("listener_name") or f"Web {request.headers.get('X-Forwarded-For', request.remote_addr or 'listener')}"
        participant_key = session.get("listener_key") or request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session"), "")
        listener_session_id = roomcast_store.begin_listener_session(
            room_slug,
            channel="web",
            participant_label=participant_label,
            participant_key=participant_key or participant_label,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )

        def _stream():
            try:
                yield from stream_hub.listen(room_slug)
            finally:
                roomcast_store.end_listener_session(listener_session_id)

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(_stream(), mimetype="audio/mpeg", headers=headers)

    @app.get("/listen/live.mp3")
    def listen_live():
        if not _authorized_rooms():
            return jsonify({"error": "pin required"}), 403

        snapshot = _active_public_room()
        if not snapshot:
            headers = {
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            }
            return Response(b"", mimetype="audio/mpeg", headers=headers)

        room_slug = snapshot["slug"]
        participant_label = session.get("listener_name") or f"Web {request.headers.get('X-Forwarded-For', request.remote_addr or 'listener')}"
        participant_key = session.get("listener_key") or request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session"), "")
        listener_session_id = roomcast_store.begin_listener_session(
            room_slug,
            channel="web",
            participant_label=participant_label,
            participant_key=participant_key or participant_label,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )

        def _stream():
            try:
                yield from stream_hub.listen(room_slug)
            finally:
                roomcast_store.end_listener_session(listener_session_id)

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(_stream(), mimetype="audio/mpeg", headers=headers)

    @app.get("/telephony/stream/<room_slug>.mp3")
    def telephony_stream(room_slug: str):
        room = roomcast_store.get_room(room_slug)
        if not room or not room["enabled"]:
            return jsonify({"error": "unknown room"}), 404

        expires_at = request.args.get("exp", "")
        signature = request.args.get("sig", "")
        participant_label = (request.args.get("label") or "Phone caller").strip() or "Phone caller"
        channel = (request.args.get("channel") or "phone").strip() or "phone"
        if not _telephony_stream_is_valid(room_slug, expires_at, signature, participant_label, channel):
            return jsonify({"error": "unauthorized"}), 403

        participant_key = f"{channel}:{participant_label}:{expires_at}"
        listener_session_id = roomcast_store.begin_listener_session(
            room_slug,
            channel=channel,
            participant_label=participant_label,
            participant_key=participant_key,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )

        def _stream():
            try:
                yield from stream_hub.listen(room_slug)
            finally:
                roomcast_store.end_listener_session(listener_session_id)

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(_stream(), mimetype="audio/mpeg", headers=headers)

    @app.route("/telephony/twilio/<webhook_token>/voice", methods=["GET", "POST"])
    def twilio_voice(webhook_token: str):
        expected = app.config.get("ROOMCAST_TWILIO_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            return jsonify({"error": "not found"}), 404

        digits = (request.values.get("Digits") or "").strip()
        if not digits:
            action_url = url_for("twilio_voice", webhook_token=webhook_token, _external=True)
            body = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="dtmf" numDigits="4" timeout="6" action="{escape(action_url)}" method="POST">
    <Say>Enter pin.</Say>
  </Gather>
  <Say>Goodbye.</Say>
</Response>"""
            return Response(body, mimetype="text/xml")

        snapshot = _resolve_public_room(digits)
        if not snapshot:
            retry_url = url_for("twilio_voice", webhook_token=webhook_token, _external=True)
            body = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Pin not accepted.</Say>
  <Redirect method="POST">{escape(retry_url)}</Redirect>
</Response>"""
            return Response(body, mimetype="text/xml")

        participant_label = (request.values.get("From") or "Phone caller").strip() or "Phone caller"
        stream_url = _telephony_stream_url(snapshot["slug"], participant_label=participant_label)
        body = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say>Connecting.</Say>
  <Play>{escape(stream_url)}</Play>
</Response>"""
        return Response(body, mimetype="text/xml")

    @app.post("/api/source/heartbeat")
    def source_heartbeat():
        payload = request.get_json(silent=False)
        host_slug = (payload.get("host_slug") or "").strip()
        token = (payload.get("token") or "").strip()
        host = roomcast_store.get_host(host_slug, include_secret=True)
        if not host or token != host["heartbeat_token"]:
            return jsonify({"error": "unauthorized"}), 403

        devices = payload.get("devices") or []
        roomcast_store.record_heartbeat(
            host_slug,
            current_device=(payload.get("current_device") or "").strip(),
            devices=devices,
            is_ingesting=bool(payload.get("is_ingesting")),
            last_error=payload.get("last_error") or "",
            desired_active=host["desired_active"],
        )

        refreshed = roomcast_store.get_host(host_slug, include_secret=True)
        roomcast_store.sync_meeting_state(
            refreshed["room_slug"],
            active=refreshed["desired_active"],
            host_slug=host_slug,
            trigger_mode=refreshed["manual_mode"] if refreshed["manual_mode"] != "auto" else "schedule",
            actor="agent-heartbeat",
        )
        return jsonify(
            {
                "ok": True,
                "project": _project_name(),
                "desired_active": refreshed["desired_active"],
                "room_slug": refreshed["room_slug"],
                "room_label": refreshed["room_label"],
                "device_order": refreshed["device_order"],
                "preferred_audio_pattern": refreshed["preferred_audio_pattern"],
                "fallback_audio_pattern": refreshed["fallback_audio_pattern"],
                "ingest_url": url_for(
                    "source_ingest",
                    host_slug=host_slug,
                    token=refreshed["heartbeat_token"],
                    _external=True,
                ),
                "listen_page": url_for("room_page", room_slug=refreshed["room_slug"], _external=True),
            }
        )

    @app.post("/api/source/ingest/<host_slug>")
    def source_ingest(host_slug: str):
        token = (request.args.get("token") or "").strip()
        host = roomcast_store.get_host(host_slug, include_secret=True)
        if not host or token != host["heartbeat_token"]:
            return jsonify({"error": "unauthorized"}), 403

        room_slug = host["room_slug"]
        stream_hub.start_broadcast(room_slug, host_slug)
        try:
            while True:
                chunk = request.stream.read(16384)
                if not chunk:
                    break
                stream_hub.publish(room_slug, chunk)
        finally:
            stream_hub.finish_broadcast(room_slug, host_slug)
        return ("", 204)

    return app


app = create_app()


def main():
    host = os.getenv("ROOMCAST_HOST", "0.0.0.0")
    port = int(os.getenv("ROOMCAST_PORT", "1967"))
    app.run(host=host, port=port, threaded=True, debug=False)


LANDING_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }}</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #081118;
        --panel: rgba(10, 17, 26, 0.9);
        --text: #eef4fb;
        --muted: #9eb1c8;
        --line: rgba(146, 184, 228, 0.12);
        --accent: #72d0ff;
        --accent-2: #6ff0c2;
        --warn: #ffb770;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at 18% 18%, rgba(114, 208, 255, 0.2), transparent 24rem),
          radial-gradient(circle at 82% 78%, rgba(111, 240, 194, 0.16), transparent 24rem),
          linear-gradient(145deg, rgba(17, 32, 49, 0.94), rgba(8, 17, 24, 1)),
          var(--bg);
        min-height: 100vh;
        display: grid;
        place-items: center;
      }
      body::before {
        content: "";
        position: fixed;
        inset: 0;
        pointer-events: none;
        background:
          linear-gradient(125deg, rgba(255, 255, 255, 0.05), transparent 28%),
          linear-gradient(305deg, rgba(255, 255, 255, 0.04), transparent 34%);
      }
      main {
        width: min(100%, 520px);
        padding: 1.25rem;
      }
      .shell {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 32px;
        padding: 1.35rem;
        box-shadow: 0 28px 90px rgba(0, 0, 0, 0.34);
        backdrop-filter: blur(18px);
      }
      .eyebrow {
        display: inline-flex;
        border-radius: 999px;
        padding: 0.34rem 0.82rem;
        background: rgba(114, 208, 255, 0.08);
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.1em;
        font-size: 0.78rem;
        font-weight: 700;
      }
      h1 {
        margin: 0.9rem 0 0;
        font-size: clamp(2.5rem, 10vw, 4.25rem);
        line-height: 0.92;
        letter-spacing: -0.04em;
      }
      .banner {
        margin-top: 1rem;
        border-radius: 18px;
        padding: 0.9rem 1rem;
        background: rgba(114, 208, 255, 0.08);
        color: var(--accent);
        font-weight: 600;
      }
      .banner.error {
        background: rgba(255, 183, 112, 0.1);
        color: var(--warn);
      }
      .status-panel {
        margin-top: 1rem;
        border-radius: 22px;
        border: 1px solid var(--line);
        background:
          radial-gradient(circle at top right, rgba(112, 209, 255, 0.08), transparent 18rem),
          linear-gradient(180deg, rgba(17, 30, 45, 0.96), rgba(9, 17, 26, 0.98));
        padding: 1rem;
      }
      .status-panel.good {
        border-color: rgba(110, 240, 196, 0.2);
      }
      .status-kicker {
        font-size: 0.84rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        font-weight: 700;
      }
      .status-headline {
        margin-top: 0.45rem;
        font-size: clamp(1.4rem, 5vw, 2.4rem);
        line-height: 1;
        font-weight: 700;
      }
      .status-detail {
        margin-top: 0.55rem;
        color: var(--muted);
        line-height: 1.5;
      }
      .entry {
        margin-top: 1.1rem;
        border-radius: 28px;
        border: 1px solid var(--line);
        background:
          linear-gradient(180deg, rgba(17, 30, 45, 0.95), rgba(10, 18, 28, 0.98));
        padding: 1.15rem;
        box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.04);
      }
      .entry h2 {
        margin: 0;
        font-size: 1.05rem;
      }
      form {
        display: grid;
        gap: 0.9rem;
        margin-top: 0.95rem;
      }
      label {
        display: grid;
        gap: 0.5rem;
        font-weight: 600;
        font-size: 0.92rem;
        color: var(--muted);
      }
      input, button, a {
        font: inherit;
      }
      input {
        width: 100%;
        border: 1px solid rgba(146, 184, 228, 0.16);
        border-radius: 22px;
        background: rgba(6, 13, 20, 0.78);
        padding: 1.1rem 1rem;
        color: var(--text);
        font-size: 2rem;
        letter-spacing: 0.42em;
        text-align: center;
      }
      button {
        appearance: none;
        border: none;
        border-radius: 999px;
        padding: 0.95rem 1rem;
        background: linear-gradient(135deg, #57c7ff, #67efc0);
        color: #041018;
        font-weight: 700;
        cursor: pointer;
        box-shadow: 0 14px 34px rgba(87, 199, 255, 0.22);
      }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <span class="eyebrow">NTC Newark</span>
        <h1>{{ listener_name }}</h1>
        {% if message %}
        <div class="banner">{{ message }}</div>
        {% endif %}
        {% if error %}
        <div class="banner error">{{ error }}</div>
        {% endif %}

        <section class="status-panel {{ public_status.state }}">
          <div class="status-kicker">{{ project_name }}</div>
          <div class="status-headline">{{ public_status.headline }}</div>
          <div class="status-detail">{{ public_status.detail }}</div>
        </section>

        <section class="entry">
          <form method="post" action="{{ url_for('join') }}">
            <label>
              <input
                type="password"
                name="join_pin"
                inputmode="numeric"
                pattern="[0-9]{4}"
                minlength="4"
                maxlength="4"
                autocomplete="one-time-code"
                placeholder="Enter PIN"
                autofocus
                required
              >
            </label>
            <button type="submit">Enter</button>
          </form>
        </section>
      </section>
    </main>
  </body>
</html>
"""


ROOM_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ listener_name }} · {{ project_name }}</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #071018;
        --panel: rgba(11, 18, 28, 0.92);
        --text: #eef4fb;
        --muted: #99adc5;
        --line: rgba(146, 184, 228, 0.12);
        --accent: #70d1ff;
        --accent-2: #6ef0c4;
        --warn: #ffb770;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(112, 209, 255, 0.18), transparent 24rem),
          radial-gradient(circle at bottom right, rgba(110, 240, 196, 0.14), transparent 22rem),
          linear-gradient(160deg, rgba(13, 23, 35, 1), rgba(7, 16, 24, 1)),
          var(--bg);
        min-height: 100vh;
      }
      main {
        max-width: 760px;
        margin: 0 auto;
        padding: 1rem 1rem 3rem;
      }
      .shell {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 28px;
        padding: 1.2rem;
        box-shadow: 0 24px 70px rgba(0, 0, 0, 0.35);
      }
      .eyebrow {
        display: inline-flex;
        border-radius: 999px;
        padding: 0.28rem 0.75rem;
        background: rgba(112, 209, 255, 0.08);
        color: var(--accent);
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
      }
      h1 {
        margin: 0.8rem 0 0;
        font-size: clamp(2.4rem, 10vw, 4.5rem);
        line-height: 0.95;
        letter-spacing: -0.04em;
      }
      .stack {
        display: grid;
        gap: 0.85rem;
        margin-top: 1rem;
      }
      .panel {
        border-radius: 20px;
        border: 1px solid var(--line);
        background: linear-gradient(180deg, rgba(15, 26, 39, 0.98), rgba(9, 17, 26, 0.98));
        padding: 1rem;
      }
      .chips {
        display: flex;
        gap: 0.45rem;
        flex-wrap: wrap;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.3rem 0.75rem;
        background: rgba(112, 209, 255, 0.08);
        color: var(--accent);
        font-size: 0.88rem;
        font-weight: 600;
      }
      .chip.good {
        background: rgba(110, 240, 196, 0.12);
        color: var(--accent-2);
      }
      .chip.warn {
        background: rgba(255, 183, 112, 0.12);
        color: var(--warn);
      }
      .player-shell {
        margin-top: 1rem;
        border-radius: 22px;
        border: 1px solid var(--line);
        background:
          radial-gradient(circle at top right, rgba(112, 209, 255, 0.08), transparent 18rem),
          linear-gradient(180deg, rgba(18, 31, 46, 0.95), rgba(7, 14, 21, 0.98));
        padding: 1rem;
      }
      .listen-button {
        appearance: none;
        border: none;
        border-radius: 999px;
        padding: 0.95rem 1.1rem;
        background: linear-gradient(135deg, #57c7ff, #67efc0);
        color: #041018;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
        width: 100%;
        box-shadow: 0 14px 34px rgba(87, 199, 255, 0.22);
      }
      .listen-button[hidden] {
        display: none;
      }
      .meter {
        margin-top: 1rem;
      }
      .meter-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        color: var(--muted);
        font-size: 0.92rem;
        font-weight: 600;
      }
      .meter-track {
        margin-top: 0.45rem;
        height: 16px;
        border-radius: 999px;
        overflow: hidden;
        background: rgba(112, 209, 255, 0.08);
        border: 1px solid rgba(112, 209, 255, 0.12);
      }
      .meter-fill {
        height: 100%;
        width: 4%;
        border-radius: inherit;
        background: linear-gradient(90deg, #57c7ff, #67efc0, #ffb65b);
        transition: width 120ms linear;
      }
      .slider-wrap {
        margin-top: 1rem;
      }
      .slider-wrap label {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        color: var(--muted);
        font-weight: 600;
        font-size: 0.92rem;
      }
      input[type="range"] {
        width: 100%;
        margin-top: 0.55rem;
      }
      .small {
        margin-top: 0.9rem;
        color: var(--muted);
        font-size: 0.92rem;
      }
      audio { display: none; }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <div class="eyebrow">{{ project_name }}</div>
        <h1>{{ listener_name }}</h1>

        <section class="stack">
          <article class="panel">
            <div class="chips">
              <span class="chip {% if room.broadcasting %}good{% endif %}" id="broadcast-chip">
                {% if room.broadcasting %}Meeting active now{% else %}Waiting for meeting{% endif %}
              </span>
            </div>

            <div class="player-shell">
              <button type="button" class="listen-button" id="listen-button" hidden>Start Audio</button>

              <div class="meter">
                <div class="meter-head">
                  <span>Volume meter</span>
                  <span id="meter-label">Idle</span>
                </div>
                <div class="meter-track">
                  <div class="meter-fill" id="meter-fill"></div>
                </div>
              </div>

              <div class="slider-wrap">
                <label for="volume-slider">
                  <span>Volume</span>
                  <span id="volume-value">100%</span>
                </label>
                <input id="volume-slider" type="range" min="0" max="100" step="1" value="100">
              </div>
            </div>

            <div class="small" id="small">
              {% if room.broadcasting %}
              Meeting active now.
              {% else %}
              Meeting not active right now. Leave this page open and it will reconnect when the audio begins.
              {% endif %}
            </div>

            <audio id="stream-player" playsinline src="{{ stream_url }}"></audio>
          </article>
        </section>
      </section>
    </main>

    <script>
      const audio = document.getElementById("stream-player");
      const listenButton = document.getElementById("listen-button");
      const volumeSlider = document.getElementById("volume-slider");
      const volumeValue = document.getElementById("volume-value");
      const meterFill = document.getElementById("meter-fill");
      const meterLabel = document.getElementById("meter-label");
      const broadcastChip = document.getElementById("broadcast-chip");
      const small = document.getElementById("small");
      let audioContext;
      let analyser;
      let sourceNode;
      let meterData;

      function rebuildStreamUrl() {
        audio.src = "{{ stream_url }}" + "?v=" + Date.now();
      }

      function setChip(element, label, state) {
        element.textContent = label;
        element.className = "chip";
        if (state === "good") {
          element.classList.add("good");
        } else if (state === "warn") {
          element.classList.add("warn");
        }
      }

      function connectMeter() {
        if (sourceNode || !(window.AudioContext || window.webkitAudioContext)) {
          return;
        }
        const Context = window.AudioContext || window.webkitAudioContext;
        audioContext = audioContext || new Context();
        sourceNode = audioContext.createMediaElementSource(audio);
        analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.82;
        meterData = new Uint8Array(analyser.frequencyBinCount);
        sourceNode.connect(analyser);
        analyser.connect(audioContext.destination);

        window.setInterval(() => {
          if (!analyser) {
            return;
          }
          analyser.getByteFrequencyData(meterData);
          let peak = 0;
          for (const value of meterData) {
            peak = Math.max(peak, value);
          }
          const percent = Math.max(4, Math.min(100, Math.round((peak / 255) * 100)));
          meterFill.style.width = `${percent}%`;
          meterLabel.textContent = percent <= 6 ? "Idle" : `${percent}%`;
        }, 120);
      }

      async function startPlayback() {
        connectMeter();
        if (audioContext && audioContext.state === "suspended") {
          await audioContext.resume();
        }
        try {
          await audio.play();
          listenButton.hidden = true;
          small.textContent = "Meeting active now.";
        } catch (error) {
          listenButton.hidden = false;
          small.textContent = "Tap once if your browser blocks audio.";
        }
      }

      function refreshAudio() {
        rebuildStreamUrl();
        startPlayback().catch(() => {});
      }

      async function pollStatus() {
        try {
          const response = await fetch("{{ status_url }}", { cache: "no-store" });
          if (!response.ok) {
            return;
          }
          const status = await response.json();
          setChip(broadcastChip, status.broadcasting ? "Meeting active now" : "Waiting for meeting", status.broadcasting ? "good" : "warn");
          if (status.broadcasting && audio.paused) {
            refreshAudio();
          }
          if (status.last_error) {
            small.textContent = status.last_error;
          } else if (status.broadcasting) {
            small.textContent = "Meeting active now.";
          } else {
            small.textContent = "Meeting not active right now. Leave this page open and it will reconnect when the audio begins.";
          }
        } catch (error) {
          small.textContent = "Status refresh failed. Retrying.";
        }
      }

      volumeSlider.addEventListener("input", () => {
        const value = Number(volumeSlider.value);
        audio.volume = value / 100;
        volumeValue.textContent = `${value}%`;
      });

      listenButton.addEventListener("click", () => {
        refreshAudio();
      });
      audio.addEventListener("ended", () => setTimeout(refreshAudio, 2500));
      audio.addEventListener("error", () => setTimeout(refreshAudio, 2500));
      audio.addEventListener("playing", () => {
        listenButton.hidden = true;
      });

      rebuildStreamUrl();
      setInterval(pollStatus, 10000);
      pollStatus();
      startPlayback().catch(() => {});
    </script>
  </body>
</html>
"""


ADMIN_LOGIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }} Admin</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #081118;
        --panel: rgba(11, 18, 28, 0.92);
        --text: #eef4fb;
        --muted: #9eb1c8;
        --line: rgba(146, 184, 228, 0.12);
        --accent: #70d1ff;
        --warn: #ffb770;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(112, 209, 255, 0.18), transparent 28rem),
          linear-gradient(150deg, rgba(13, 23, 35, 1), rgba(8, 17, 24, 1)),
          var(--bg);
        min-height: 100vh;
      }
      main {
        max-width: 640px;
        margin: 0 auto;
        padding: 1.2rem 1rem 3rem;
      }
      .shell {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 30px;
        padding: 1.25rem;
        box-shadow: 0 24px 72px rgba(0, 0, 0, 0.34);
      }
      .eyebrow {
        display: inline-flex;
        border-radius: 999px;
        padding: 0.3rem 0.72rem;
        background: rgba(112, 209, 255, 0.08);
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.78rem;
        font-weight: 700;
      }
      h1 {
        margin: 0.85rem 0 0.35rem;
        font-size: clamp(2rem, 8vw, 3.6rem);
        line-height: 0.95;
      }
      p {
        margin: 0;
        color: var(--muted);
        line-height: 1.6;
      }
      .banner {
        margin-top: 1rem;
        border-radius: 16px;
        padding: 0.85rem 1rem;
        background: rgba(255, 183, 112, 0.1);
        color: var(--warn);
        font-weight: 600;
      }
      form {
        display: grid;
        gap: 0.85rem;
        margin-top: 1rem;
      }
      label {
        display: grid;
        gap: 0.35rem;
        font-weight: 600;
      }
      input, button {
        font: inherit;
      }
      input {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 16px;
        background: rgba(6, 13, 20, 0.78);
        padding: 0.9rem 0.95rem;
        color: var(--text);
      }
      button {
        appearance: none;
        border: none;
        border-radius: 999px;
        padding: 0.9rem 1rem;
        background: linear-gradient(135deg, #57c7ff, #67efc0);
        color: #041018;
        font-weight: 700;
        cursor: pointer;
      }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <div class="eyebrow">Control Panel</div>
        <h1>{{ project_name }}</h1>
        <p>Sign in to start or stop a call.</p>
        {% if error %}
        <div class="banner">{{ error }}</div>
        {% endif %}
        <form method="post" action="{{ url_for('admin_login') }}">
          <label>
            Password
            <input type="password" name="password" autocomplete="current-password" autofocus required>
          </label>
          <button type="submit">Open Panel</button>
        </form>
      </section>
    </main>
  </body>
</html>
"""


ADMIN_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }} Control Panel</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #081118;
        --panel: rgba(11, 18, 28, 0.94);
        --text: #eef4fb;
        --muted: #9eb1c8;
        --line: rgba(146, 184, 228, 0.12);
        --accent: #70d1ff;
        --accent-2: #6ef0c4;
        --warn: #ffb770;
        --bad: #ff8c8c;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(112, 209, 255, 0.18), transparent 28rem),
          radial-gradient(circle at bottom right, rgba(110, 240, 196, 0.16), transparent 24rem),
          linear-gradient(155deg, rgba(13, 23, 35, 1), rgba(8, 17, 24, 1)),
          var(--bg);
      }
      main {
        max-width: 1040px;
        margin: 0 auto;
        padding: 1rem 1rem 3rem;
      }
      .topbar {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: center;
        margin-bottom: 1rem;
      }
      .actions {
        display: flex;
        gap: 0.7rem;
        flex-wrap: wrap;
      }
      .toolbar-button {
        appearance: none;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 0.72rem 0.95rem;
        background: rgba(6, 13, 20, 0.58);
        color: var(--text);
        font-weight: 700;
        cursor: pointer;
        text-decoration: none;
      }
      .eyebrow {
        display: inline-flex;
        border-radius: 999px;
        padding: 0.28rem 0.72rem;
        background: rgba(112, 209, 255, 0.08);
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.76rem;
        font-weight: 700;
      }
      h1 {
        margin: 0.4rem 0 0;
        font-size: clamp(2.2rem, 6vw, 4rem);
        line-height: 0.92;
      }
      p {
        margin: 0;
        color: var(--muted);
        line-height: 1.6;
      }
      .banner {
        margin-top: 1rem;
        border-radius: 16px;
        padding: 0.85rem 1rem;
        font-weight: 600;
      }
      .banner.ok {
        background: rgba(110, 240, 196, 0.11);
        color: var(--accent-2);
      }
      .banner.error {
        background: rgba(255, 140, 140, 0.11);
        color: var(--bad);
      }
      .shell {
        background: var(--panel);
        border: 1px solid var(--line);
        border-radius: 30px;
        padding: 1.2rem;
        box-shadow: 0 20px 60px rgba(0, 0, 0, 0.34);
      }
      .shell + .shell {
        margin-top: 1rem;
      }
      .hero-grid {
        display: grid;
        grid-template-columns: 1.3fr 1fr;
        gap: 1rem;
      }
      .status-board {
        border-radius: 24px;
        border: 1px solid var(--line);
        background:
          radial-gradient(circle at top right, rgba(112, 209, 255, 0.1), transparent 22rem),
          linear-gradient(180deg, rgba(18, 31, 46, 0.98), rgba(9, 17, 26, 0.98));
        padding: 1rem;
      }
      .status-board.compact {
        display: grid;
        gap: 0.85rem;
      }
      .status-kicker {
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.8rem;
        font-weight: 700;
      }
      .status-headline {
        margin-top: 0.55rem;
        font-size: clamp(2rem, 6vw, 3.3rem);
        line-height: 0.94;
        font-weight: 700;
      }
      .status-detail {
        margin-top: 0.7rem;
        color: var(--muted);
        font-size: 1rem;
      }
      .status-summary {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
        margin-top: 0.95rem;
      }
      .metric-grid {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 0.75rem;
        margin-top: 1rem;
      }
      .metric {
        border-radius: 18px;
        border: 1px solid rgba(146, 184, 228, 0.08);
        background: rgba(6, 13, 20, 0.52);
        padding: 0.85rem 0.9rem;
      }
      .metric-label {
        color: var(--muted);
        font-size: 0.76rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
      }
      .metric-value {
        margin-top: 0.4rem;
        font-size: 1.05rem;
        font-weight: 700;
      }
      .room-picker {
        display: grid;
        gap: 0.7rem;
      }
      .room-picker input {
        display: none;
      }
      .room-picker label {
        display: block;
      }
      .room-option {
        display: grid;
        gap: 0.45rem;
        border-radius: 22px;
        border: 1px solid var(--line);
        padding: 1rem;
        cursor: pointer;
        background: rgba(8, 15, 23, 0.82);
        transition: transform 120ms ease, border-color 120ms ease;
      }
      .room-picker input:checked + .room-option {
        border-color: rgba(112, 209, 255, 0.34);
        box-shadow: 0 0 0 1px rgba(112, 209, 255, 0.22) inset;
        transform: translateY(-1px);
      }
      .room-name {
        font-size: 1.35rem;
        font-weight: 700;
      }
      .room-meta {
        margin-top: 0.35rem;
        color: var(--muted);
        font-size: 0.94rem;
      }
      .chips {
        display: flex;
        gap: 0.45rem;
        flex-wrap: wrap;
        margin-top: 0.75rem;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.28rem 0.72rem;
        background: rgba(112, 209, 255, 0.08);
        color: var(--accent);
        font-size: 0.85rem;
        font-weight: 600;
      }
      .chip.good {
        background: rgba(110, 240, 196, 0.12);
        color: var(--accent-2);
      }
      .chip.warn {
        background: rgba(255, 183, 112, 0.12);
        color: var(--warn);
      }
      .chip.bad {
        background: rgba(255, 140, 140, 0.12);
        color: var(--bad);
      }
      .call-actions {
        display: grid;
        gap: 0.7rem;
        margin-top: 0.4rem;
      }
      .call-actions button {
        width: 100%;
        min-height: 3.4rem;
        font-size: 1rem;
      }
      .call-actions.primary {
        margin-top: 1rem;
      }
      .call-actions .secondary {
        background: rgba(6, 13, 20, 0.78);
        color: var(--accent);
        border: 1px solid var(--line);
      }
      .label {
        font-size: 0.86rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--muted);
        font-weight: 700;
      }
      .value {
        margin-top: 0.35rem;
        line-height: 1.6;
      }
      form {
        display: grid;
      }
      button, a {
        font: inherit;
      }
      .button, button {
        appearance: none;
        border: none;
        border-radius: 999px;
        padding: 0.88rem 1rem;
        background: linear-gradient(135deg, #57c7ff, #67efc0);
        color: #041018;
        font-weight: 700;
        cursor: pointer;
        text-decoration: none;
      }
      .listener-list {
        display: grid;
        gap: 0.6rem;
      }
      .section-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: baseline;
        margin-bottom: 0.8rem;
      }
      .section-note {
        color: var(--muted);
        font-size: 0.88rem;
      }
      .listener-item {
        padding: 0.72rem 0.8rem;
        border-radius: 14px;
        background: rgba(6, 13, 20, 0.6);
        border: 1px solid rgba(146, 184, 228, 0.08);
      }
      .listener-item strong {
        display: block;
        font-size: 0.95rem;
      }
      .listener-meta {
        margin-top: 0.22rem;
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.5;
      }
      .stats-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
        gap: 1rem;
      }
      .table-wrap {
        overflow-x: auto;
      }
      .history-table {
        width: 100%;
        border-collapse: collapse;
        margin-top: 0.8rem;
        min-width: 640px;
      }
      .history-table th,
      .history-table td {
        text-align: left;
        padding: 0.72rem 0.45rem;
        border-top: 1px solid var(--line);
        vertical-align: top;
      }
      .history-table th {
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.8rem;
      }
      .history-link {
        color: var(--accent);
        text-decoration: none;
        font-weight: 700;
      }
      @media (max-width: 900px) {
        .hero-grid {
          grid-template-columns: 1fr;
        }
        .actions {
          width: 100%;
        }
        .metric-grid {
          grid-template-columns: 1fr;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="topbar">
        <div>
          <span class="eyebrow">Control Panel</span>
          <h1>{{ project_name }}</h1>
        </div>
        <div class="actions">
          <a class="toolbar-button" href="{{ settings_url }}">Settings</a>
          <form method="post" action="{{ logout_url }}">
            <button class="toolbar-button" type="submit">Sign Out</button>
          </form>
        </div>
      </section>

      <section class="shell">
        <div class="hero-grid">
          <div class="status-board">
            <div class="status-kicker">Current meeting</div>
            <div class="status-headline">
              {% if active_meeting %}
              {{ "Room A" if active_meeting.room_slug == "study-room" else "Room B" }} is live
              {% else %}
              No call is running
              {% endif %}
            </div>
            <div class="status-detail">
              {% if active_meeting %}
              Started {{ active_meeting.started_label }}.
              {% else %}
              Choose Room A or Room B, then press Start Call.
              {% endif %}
            </div>
            {% if focus_host %}
            <div class="metric-grid">
              <div class="metric">
                <div class="metric-label">Room</div>
                <div class="metric-value">{{ focus_host.room_alias }}</div>
              </div>
              <div class="metric">
                <div class="metric-label">Source</div>
                <div class="metric-value">{% if focus_host.source_online %}Online{% else %}Offline{% endif %}</div>
              </div>
              <div class="metric">
                <div class="metric-label">Listeners</div>
                <div class="metric-value">{{ focus_host.listener_count }}</div>
              </div>
            </div>
            <div class="status-summary">
              <span class="chip {% if focus_host.is_ingesting %}good{% else %}warn{% endif %}">
                {% if focus_host.is_ingesting %}Streaming{% else %}Waiting{% endif %}
              </span>
              {% if focus_host.last_error %}
              <span class="chip warn">{{ focus_host.last_error }}</span>
              {% endif %}
            </div>
            {% endif %}
            {% if message %}
            <div class="banner ok">{{ message }}</div>
            {% endif %}
            {% if error %}
            <div class="banner error">{{ error }}</div>
            {% endif %}
            {% for conflict in conflicts %}
            <div class="banner error">{{ conflict }}</div>
            {% endfor %}
          </div>

          <div class="status-board compact">
            <form method="post" action="{{ url_for('admin_start_call') }}">
              <div class="room-picker">
                {% for room in room_options %}
                <label>
                  <input type="radio" name="room_slug" value="{{ room.slug }}" {% if room.selected %}checked{% endif %}>
                  <span class="room-option">
                    <span class="room-name">{{ room.alias }}</span>
                    <span class="room-meta">{{ room.label }}</span>
                    <span class="chips">
                      <span class="chip {% if room.online %}good{% else %}warn{% endif %}">{% if room.online %}Agent online{% else %}Agent offline{% endif %}</span>
                      <span class="chip {% if room.active %}good{% endif %}">{% if room.active %}Live{% else %}Standby{% endif %}</span>
                      <span class="chip {% if room.listener_count %}good{% endif %}">{{ room.listener_count }} listener{% if room.listener_count != 1 %}s{% endif %}</span>
                    </span>
                  </span>
                </label>
                {% endfor %}
              </div>
              <div class="call-actions primary">
                <button type="submit">Start Call</button>
              </div>
            </form>
            <form method="post" action="{{ url_for('admin_stop_call') }}" class="call-actions">
              <input type="hidden" name="room_slug" value="{{ active_room_slug or selected_room_slug }}">
              <button type="submit" class="secondary">Stop Call</button>
            </form>
          </div>
        </div>
      </section>

      <section class="shell">
        <div class="stats-grid">
          <div class="status-board compact">
            <div class="section-head">
              <div class="label">Current callers</div>
              <div class="section-note">{% if focus_host %}{{ focus_host.room_alias }}{% endif %}</div>
            </div>
            <div class="value">
              {% if current_listeners %}
              <div class="listener-list">
                {% for listener in current_listeners %}
                <div class="listener-item">
                  <strong>{{ listener.participant_label }}</strong>
                  <div class="listener-meta">{{ listener.location_label or "Location unavailable" }} · {{ listener.channel }}</div>
                  <div class="listener-meta">Joined {{ listener.joined_label }}</div>
                </div>
                {% endfor %}
              </div>
              {% else %}
              Nobody is connected right now.
              {% endif %}
            </div>
          </div>
          <div class="status-board compact">
            <div class="section-head">
              <div class="label">Recent callers</div>
              <div class="section-note">Latest activity</div>
            </div>
            <div class="value">
              {% if recent_listeners %}
              <div class="listener-list">
                {% for listener in recent_listeners %}
                <div class="listener-item">
                  <strong>{{ listener.participant_label }}</strong>
                  <div class="listener-meta">{{ listener.location_label or "Location unavailable" }} · {{ listener.channel }}</div>
                  <div class="listener-meta">Joined {{ listener.joined_label }}{% if listener.left_at %} · Left {{ listener.left_label }}{% endif %}</div>
                </div>
                {% endfor %}
              </div>
              {% else %}
              No recent callers yet.
              {% endif %}
            </div>
          </div>
        </div>
      </section>

      <section class="shell">
        <div class="section-head">
          <div class="label">Past calls</div>
          <div class="section-note">Reports stay downloadable</div>
        </div>
        <div class="table-wrap">
          <table class="history-table">
            <thead>
              <tr>
                <th>Room</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Listeners</th>
                <th>Incidents</th>
                <th>Report</th>
              </tr>
            </thead>
            <tbody>
              {% for meeting in call_history %}
              <tr>
                <td>{{ meeting.room_alias }}</td>
                <td>{{ meeting.started_label }}</td>
                <td>{{ meeting.duration_text }}</td>
                <td>{{ meeting.listener_count }}</td>
                <td>{{ meeting.incident_count }}</td>
                <td><a class="history-link" href="{{ url_for('admin_report_download', meeting_id=meeting.id) }}">Download</a></td>
              </tr>
              {% else %}
              <tr>
                <td colspan="6">No past calls yet.</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </section>
    </main>
  </body>
</html>
"""


ADMIN_PANEL_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }} Admin</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #081018;
        --surface: #0f1822;
        --surface-2: #142030;
        --surface-3: #1a2838;
        --text: #eef4fb;
        --muted: #99a8b8;
        --line: #213244;
        --line-strong: #31506d;
        --accent: #87d6ff;
        --accent-soft: rgba(135, 214, 255, 0.12);
        --good: #74ddb4;
        --good-soft: rgba(116, 221, 180, 0.12);
        --warn: #f6c469;
        --warn-soft: rgba(246, 196, 105, 0.12);
        --bad: #ff9b9b;
        --bad-soft: rgba(255, 155, 155, 0.12);
        --shadow: 0 18px 44px rgba(0, 0, 0, 0.24);
        --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top, rgba(135, 214, 255, 0.06), transparent 28rem),
          linear-gradient(180deg, #0d151e, #081018 44rem),
          var(--bg);
      }
      main {
        max-width: 1040px;
        margin: 0 auto;
        padding: 1rem 1rem 2.5rem;
      }
      .topbar {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: center;
        margin-bottom: 0.9rem;
      }
      .brand {
        display: grid;
        gap: 0.25rem;
      }
      .brand h1 {
        margin: 0;
        font-size: clamp(1.8rem, 4vw, 2.7rem);
        line-height: 1;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        max-width: 30rem;
        line-height: 1.4;
      }
      .top-actions {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
      }
      .eyebrow {
        display: inline-flex;
        width: fit-content;
        border-radius: 999px;
        padding: 0.22rem 0.58rem;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.02);
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.72rem;
        font-weight: 700;
        font-family: var(--mono);
      }
      button, a, input {
        font: inherit;
      }
      .utility-button,
      .primary-button,
      .ghost-button,
      .danger-button,
      .close-button {
        appearance: none;
        border-radius: 10px;
        font-weight: 700;
        cursor: pointer;
        text-decoration: none;
        white-space: nowrap;
        transition: border-color 140ms ease, background 140ms ease, transform 140ms ease, color 140ms ease;
      }
      .utility-button:hover,
      .utility-button:focus-visible,
      .primary-button:hover,
      .primary-button:focus-visible,
      .ghost-button:hover,
      .ghost-button:focus-visible,
      .danger-button:hover,
      .danger-button:focus-visible,
      .close-button:hover,
      .close-button:focus-visible,
      .room-choice:hover,
      .room-choice:focus-visible {
        transform: translateY(-1px);
      }
      .utility-button,
      .ghost-button,
      .close-button {
        border: 1px solid var(--line);
        background: var(--surface-2);
        color: var(--text);
      }
      .utility-button,
      .close-button {
        padding: 0.64rem 0.88rem;
      }
      .primary-button,
      .ghost-button,
      .danger-button {
        min-height: 2.7rem;
        padding: 0.68rem 0.92rem;
      }
      .primary-button {
        border: none;
        background: #dff4ff;
        color: #081018;
      }
      .danger-button {
        border: 1px solid rgba(255, 155, 155, 0.28);
        background: var(--bad-soft);
        color: #ffdede;
      }
      .panel {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        box-shadow: var(--shadow);
      }
      .panel + .panel {
        margin-top: 1rem;
      }
      .command-shell {
        display: grid;
        grid-template-columns: minmax(0, 1.35fr) minmax(310px, 0.85fr);
        gap: 1rem;
        padding: 1rem;
      }
      .command-card,
      .state-card,
      .info-card,
      .table-card {
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface);
        padding: 1rem;
      }
      .command-card,
      .state-card {
        display: grid;
        gap: 0.9rem;
      }
      .command-rail {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: start;
      }
      .command-copy {
        display: grid;
        gap: 0.45rem;
      }
      .command-copy h2 {
        margin: 0;
        font-size: clamp(1.6rem, 4vw, 2.2rem);
        line-height: 1;
        font-weight: 700;
      }
      .command-copy p {
        margin: 0;
        color: var(--muted);
        font-size: 0.94rem;
        line-height: 1.45;
      }
      .state-pill {
        display: inline-flex;
        align-items: center;
        width: fit-content;
        border-radius: 999px;
        padding: 0.3rem 0.64rem;
        font-size: 0.74rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        font-family: var(--mono);
      }
      .state-pill.good { background: var(--good-soft); color: var(--good); }
      .state-pill.warn { background: var(--warn-soft); color: var(--warn); }
      .state-pill.idle { background: rgba(154, 167, 182, 0.12); color: #cbd5df; }
      .pulse {
        position: relative;
      }
      .pulse::after {
        content: "";
        position: absolute;
        inset: -0.1rem;
        border-radius: inherit;
        border: 1px solid currentColor;
        opacity: 0;
        animation: pulse-ring 1.9s ease-out infinite;
      }
      @keyframes pulse-ring {
        0% { opacity: 0.38; transform: scale(1); }
        100% { opacity: 0; transform: scale(1.22); }
      }
      .command-actions {
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 0.5rem;
        flex-wrap: wrap;
        flex-shrink: 0;
      }
      .command-actions form {
        margin: 0;
      }
      .stat-strip {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 0.6rem;
      }
      .stat {
        border-radius: 10px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        padding: 0.72rem 0.8rem;
      }
      .stat span {
        display: block;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
        font-family: var(--mono);
      }
      .stat strong {
        display: block;
        margin-top: 0.35rem;
        font-size: 0.98rem;
        font-weight: 700;
      }
      .flash-stack {
        display: grid;
        gap: 0.65rem;
      }
      .banner {
        border-radius: 12px;
        border: 1px solid var(--line);
        padding: 0.78rem 0.9rem;
        font-weight: 600;
      }
      .banner.ok { background: var(--good-soft); color: var(--good); }
      .banner.error { background: var(--bad-soft); color: var(--bad); }
      .state-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: start;
      }
      .state-head h3 {
        margin: 0.2rem 0 0;
        font-size: 1.15rem;
      }
      .listener-badge {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.3rem 0.66rem;
        background: var(--surface-2);
        border: 1px solid var(--line);
        color: var(--text);
        font-size: 0.8rem;
        font-weight: 700;
        font-family: var(--mono);
      }
      .state-grid {
        display: grid;
        gap: 0.55rem;
      }
      .state-row {
        display: grid;
        grid-template-columns: 6.5rem 1fr;
        gap: 0.85rem;
        align-items: start;
        border-radius: 12px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        padding: 0.78rem 0.85rem;
      }
      .state-row strong {
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
        padding-top: 0.1rem;
      }
      .state-row span { line-height: 1.45; }
      .state-value.good { color: var(--good); }
      .state-value.warn { color: var(--warn); }
      .state-value.muted { color: var(--muted); }
      .support-note {
        border-radius: 12px;
        padding: 0.8rem 0.85rem;
        border: 1px solid var(--line);
        background: var(--surface-2);
        line-height: 1.45;
      }
      .support-note strong {
        display: block;
        margin-bottom: 0.22rem;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .support-note.warn {
        border-color: rgba(246, 196, 105, 0.2);
        background: var(--warn-soft);
      }
      .listeners-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
      }
      .info-card h3 {
        margin: 0.35rem 0 0;
        font-size: 1.18rem;
      }
      .info-card p {
        margin: 0.5rem 0 0;
        color: var(--muted);
        line-height: 1.45;
      }
      .card-label {
        display: inline-flex;
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.72rem;
        font-weight: 700;
        font-family: var(--mono);
      }
      .mini-chip-row,
      .device-list,
      .schedule-row {
        display: flex;
        gap: 0.55rem;
        flex-wrap: wrap;
        margin-top: 0.8rem;
      }
      .mini-chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.28rem 0.68rem;
        background: var(--surface-2);
        border: 1px solid var(--line);
        font-size: 0.8rem;
        font-weight: 600;
        font-family: var(--mono);
      }
      .mini-chip.good { color: var(--good); border-color: rgba(116, 221, 180, 0.24); }
      .mini-chip.warn { color: var(--warn); border-color: rgba(246, 196, 105, 0.24); }
      .mini-chip.bad { color: var(--bad); border-color: rgba(255, 155, 155, 0.24); }
      .mini-chip.off { color: var(--muted); }
      .section-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: baseline;
        margin-bottom: 0.9rem;
      }
      .section-head p {
        margin: 0;
        color: var(--muted);
      }
      .listener-summary {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: center;
        margin-bottom: 1rem;
        padding: 0.85rem 0.95rem;
        border-radius: 10px;
        border: 1px solid var(--line);
        background: var(--surface);
      }
      .listener-summary strong {
        display: block;
        font-size: 1.9rem;
        line-height: 1;
      }
      .listener-summary p {
        margin: 0.22rem 0 0;
        color: var(--muted);
      }
      .listener-list {
        display: grid;
        gap: 0.6rem;
      }
      .listener-item {
        padding: 0.82rem 0.88rem;
        border-radius: 12px;
        background: var(--surface-2);
        border: 1px solid var(--line);
      }
      .listener-item strong {
        display: block;
        font-size: 0.96rem;
      }
      .listener-meta {
        margin-top: 0.22rem;
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.5;
      }
      .reports-shell {
        padding: 1rem;
      }
      .table-wrap { overflow-x: auto; }
      .history-table {
        width: 100%;
        border-collapse: collapse;
        min-width: 640px;
      }
      .history-table th,
      .history-table td {
        text-align: left;
        padding: 0.78rem 0.55rem;
        border-top: 1px solid var(--line);
        vertical-align: top;
      }
      .history-table th {
        color: var(--muted);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.72rem;
        font-family: var(--mono);
      }
      .history-link {
        color: var(--accent);
        text-decoration: none;
        font-weight: 700;
      }
      .empty-state {
        margin: 0;
        color: var(--muted);
      }
      .room-dialog {
        border: 1px solid var(--line);
        border-radius: 16px;
        background: var(--surface);
        color: var(--text);
        padding: 1rem;
        width: min(680px, calc(100vw - 2rem));
        box-shadow: var(--shadow);
      }
      .room-dialog::backdrop {
        background: rgba(3, 6, 10, 0.72);
      }
      .dialog-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: start;
        margin-bottom: 1rem;
      }
      .dialog-head h3 {
        margin: 0.2rem 0 0;
        font-size: 1.4rem;
      }
      .dialog-head p {
        margin: 0.35rem 0 0;
        color: var(--muted);
      }
      .room-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.85rem;
      }
      .room-grid form { margin: 0; }
      .room-choice {
        width: 100%;
        text-align: left;
        border-radius: 12px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        color: var(--text);
        padding: 1rem;
        cursor: pointer;
        transition: border-color 140ms ease, transform 140ms ease, background 140ms ease;
      }
      .room-choice:hover,
      .room-choice:focus-visible {
        border-color: var(--line-strong);
        background: var(--surface-3);
      }
      .room-choice strong {
        display: block;
        font-size: 1.08rem;
      }
      .room-choice p {
        margin: 0.35rem 0 0;
        color: var(--muted);
      }
      .room-choice .mini-chip-row { margin-top: 0.85rem; }
      @media (max-width: 900px) {
        .command-shell,
        .listeners-grid,
        .room-grid {
          grid-template-columns: 1fr;
        }
        .command-rail {
          flex-direction: column;
        }
        .stat-strip {
          grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .topbar,
        .listener-summary,
        .state-head,
        .dialog-head {
          align-items: stretch;
        }
        .topbar,
        .listener-summary,
        .dialog-head {
          flex-direction: column;
        }
      }
      @media (max-width: 640px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .panel,
        .command-card,
        .state-card,
        .info-card,
        .table-card {
          border-radius: 14px;
        }
        .stat-strip {
          grid-template-columns: 1fr 1fr;
        }
        .command-actions form,
        .command-actions button,
        .command-actions a {
          width: 100%;
        }
        .primary-button,
        .ghost-button,
        .danger-button {
          width: 100%;
        }
        .state-row {
          grid-template-columns: 1fr;
          gap: 0.35rem;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="topbar">
        <div class="brand">
          <span class="eyebrow">Control Panel</span>
          <h1>{{ project_name }}</h1>
          <p>Start a call, watch the line, and see who is listening.</p>
        </div>
        <div class="top-actions">
          <a class="utility-button" href="{{ settings_url }}">Settings</a>
          <form method="post" action="{{ logout_url }}">
            <button class="utility-button" type="submit">Sign Out</button>
          </form>
        </div>
      </section>

      <section class="panel command-shell">
        <article class="command-card">
          <div class="command-rail">
            <div class="command-copy">
              <span class="state-pill {{ call_state.tone }} {% if call_state.tone != 'idle' %}pulse{% endif %}">{{ call_state.label }}</span>
              <h2>{{ call_state.headline }}</h2>
              <p>{{ call_state.detail }}</p>
            </div>

            <div class="command-actions">
              {% if active_meeting %}
              <form method="post" action="{{ url_for('admin_stop_call') }}">
                <input type="hidden" name="room_slug" value="{{ active_room_slug }}">
                <button class="danger-button" type="submit">Stop Call</button>
              </form>
              <button class="ghost-button" type="button" data-open-room-dialog>Change Room</button>
              {% else %}
              <button class="primary-button" type="button" data-open-room-dialog>Start Call</button>
              {% endif %}
              {% if resume_available %}
              <form method="post" action="{{ url_for('admin_use_schedule') }}">
                <button class="ghost-button" type="submit">Return to Auto</button>
              </form>
              {% endif %}
            </div>
          </div>

          {% if control_host %}
          <div class="stat-strip">
            <div class="stat">
              <span>Room</span>
              <strong>{{ control_host.room_alias }}</strong>
            </div>
            <div class="stat">
              <span>Agent</span>
              <strong>{% if control_host.source_online %}Online{% else %}Offline{% endif %}</strong>
            </div>
            <div class="stat">
              <span>Input</span>
              <strong>{{ control_host.current_device or "Waiting for device" }}</strong>
            </div>
            <div class="stat">
              <span>Listeners</span>
              <strong>{{ focus_host.listener_count if focus_host else 0 }}</strong>
            </div>
          </div>
          {% endif %}

          <div class="flash-stack">
            {% if message %}
            <div class="banner ok">{{ message }}</div>
            {% endif %}
            {% if error %}
            <div class="banner error">{{ error }}</div>
            {% endif %}
            {% for conflict in conflicts %}
            <div class="banner error">{{ conflict }}</div>
            {% endfor %}
          </div>
        </article>

        <aside class="state-card">
          <div class="state-head">
            <div>
              <span class="eyebrow">System State</span>
              <h3>Current status</h3>
            </div>
            <div class="listener-badge">{{ focus_host.listener_count if focus_host else 0 }} listening</div>
          </div>
          <div class="state-grid">
            <div class="state-row">
              <strong>Room</strong>
              <span class="state-value {% if control_host %}good{% else %}muted{% endif %}">
                {{ control_host.room_alias if control_host else "Waiting for a room selection." }}
              </span>
            </div>
            <div class="state-row">
              <strong>Agent</strong>
              <span class="state-value {% if control_host and control_host.source_online %}good{% elif control_host %}warn{% else %}muted{% endif %}">
                {% if control_host and control_host.source_online %}Laptop agent online.{% elif control_host %}Laptop agent is offline.{% else %}No room selected yet.{% endif %}
              </span>
            </div>
            <div class="state-row">
              <strong>Input</strong>
              <span class="state-value {% if control_host and control_host.current_device %}good{% else %}muted{% endif %}">
                {{ control_host.current_device if control_host and control_host.current_device else "Waiting for a reported input device." }}
              </span>
            </div>
            <div class="state-row">
              <strong>Stream</strong>
              <span class="state-value {% if control_host and control_host.broadcasting %}good{% elif control_host and control_host.desired_active %}warn{% else %}muted{% endif %}">
                {% if control_host and control_host.broadcasting %}Live audio is available now.{% elif control_host and control_host.desired_active %}Starting and waiting for source audio.{% else %}Idle until a call is started.{% endif %}
              </span>
            </div>
            <div class="state-row">
              <strong>Mode</strong>
              <span class="state-value {% if control_host and control_host.manual_mode == 'auto' %}good{% elif control_host %}warn{% else %}muted{% endif %}">
                {% if control_host and control_host.manual_mode == 'force_on' %}Manual start is holding this room on.{% elif control_host and control_host.manual_mode == 'force_off' %}Manual stop is holding this room off.{% elif control_host %}Following the saved schedule.{% else %}No room selected yet.{% endif %}
              </span>
            </div>
          </div>
          {% if focus_host and focus_host.last_error %}
          <div class="support-note warn">
            <strong>Last warning</strong>
            <span>{{ focus_host.last_error }}</span>
          </div>
          {% endif %}
          {% if control_host and control_host.schedule_rows %}
          <div class="support-note">
            <strong>Schedule</strong>
            <div class="schedule-row">
              {% for item in control_host.schedule_rows %}
              <span class="mini-chip {% if item.enabled %}{% else %}off{% endif %}">{{ item.label }} · {{ item.status_label }}</span>
              {% endfor %}
            </div>
          </div>
          {% endif %}
        </aside>
      </section>

      <section class="panel" style="padding: 1rem;">
        <div class="listener-summary">
          <div>
            <strong>{{ focus_host.listener_count if focus_host else 0 }}</strong>
            <p>{% if focus_host %}Active listeners in {{ focus_host.room_alias }}{% else %}Active listeners{% endif %}</p>
          </div>
          <span class="mini-chip {% if active_meeting %}good{% elif control_host and control_host.desired_active %}warn{% endif %}">
            {% if active_meeting %}Meeting live{% elif control_host and control_host.desired_active %}Starting{% else %}Idle{% endif %}
          </span>
        </div>
        <div class="listeners-grid">
          <article class="info-card">
            <div class="section-head">
              <span class="card-label">Listening now</span>
              {% if focus_host %}
              <p>{{ focus_host.room_alias }}</p>
              {% endif %}
            </div>
            {% if current_listeners %}
            <div class="listener-list">
              {% for listener in current_listeners %}
              <div class="listener-item">
                <strong>{{ listener.participant_label }}</strong>
                <div class="listener-meta">{{ listener.location_label or "Location unavailable" }} · {{ listener.channel }}</div>
                <div class="listener-meta">Joined {{ listener.joined_label }}</div>
              </div>
              {% endfor %}
            </div>
            {% else %}
            <p class="empty-state">Nobody is connected right now.</p>
            {% endif %}
          </article>

          <article class="info-card">
            <div class="section-head">
              <span class="card-label">Recent access</span>
              <p>Latest listener activity</p>
            </div>
            {% if recent_listeners %}
            <div class="listener-list">
              {% for listener in recent_listeners %}
              <div class="listener-item">
                <strong>{{ listener.participant_label }}</strong>
                <div class="listener-meta">{{ listener.location_label or "Location unavailable" }} · {{ listener.channel }}</div>
                <div class="listener-meta">Joined {{ listener.joined_label }}{% if listener.left_at %} · Left {{ listener.left_label }}{% endif %}</div>
              </div>
              {% endfor %}
            </div>
            {% else %}
            <p class="empty-state">No recent listener activity yet.</p>
            {% endif %}
          </article>
        </div>
      </section>

      <section class="panel reports-shell">
        <article class="table-card">
          <div class="section-head">
            <div>
              <span class="card-label">Past calls</span>
              <p>Download CSV reports for completed meetings.</p>
            </div>
          </div>
          <div class="table-wrap">
            <table class="history-table">
              <thead>
                <tr>
                  <th>Room</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th>Listeners</th>
                  <th>Incidents</th>
                  <th>Report</th>
                </tr>
              </thead>
              <tbody>
                {% for meeting in call_history %}
                <tr>
                  <td>{{ meeting.room_alias }}</td>
                  <td>{{ meeting.started_label }}</td>
                  <td>{{ meeting.duration_text }}</td>
                  <td>{{ meeting.listener_count }}</td>
                  <td>{{ meeting.incident_count }}</td>
                  <td><a class="history-link" href="{{ url_for('admin_report_download', meeting_id=meeting.id) }}">Download</a></td>
                </tr>
                {% else %}
                <tr>
                  <td colspan="6">No past calls yet.</td>
                </tr>
                {% endfor %}
              </tbody>
            </table>
          </div>
        </article>
      </section>

      <dialog class="room-dialog" id="room-dialog">
        <div class="dialog-head">
          <div>
            <span class="eyebrow">Start Call</span>
            <h3>Choose a room</h3>
            <p>Only one room runs at a time. Selecting one room makes it the active call.</p>
          </div>
          <button class="close-button" type="button" data-close-room-dialog>Close</button>
        </div>
        <div class="room-grid">
          {% for room in room_options %}
          <form method="post" action="{{ url_for('admin_start_call') }}">
            <input type="hidden" name="room_slug" value="{{ room.slug }}">
            <button class="room-choice" type="submit">
              <strong>{{ room.alias }}</strong>
              <p>{{ room.label }}</p>
              <div class="mini-chip-row">
                <span class="mini-chip {% if room.online %}good{% else %}warn{% endif %}">{% if room.online %}Agent online{% else %}Agent offline{% endif %}</span>
                <span class="mini-chip {% if room.active %}good{% endif %}">{% if room.active %}Live now{% else %}Standby{% endif %}</span>
                <span class="mini-chip">{{ room.listener_count }} listener{% if room.listener_count != 1 %}s{% endif %}</span>
              </div>
            </button>
          </form>
          {% endfor %}
        </div>
      </dialog>
    </main>
    <script>
      (() => {
        const roomDialog = document.getElementById("room-dialog");
        if (roomDialog) {
          document.querySelectorAll("[data-open-room-dialog]").forEach((button) => {
            button.addEventListener("click", () => roomDialog.showModal());
          });
          document.querySelectorAll("[data-close-room-dialog]").forEach((button) => {
            button.addEventListener("click", () => roomDialog.close());
          });
        }
      })();
    </script>
  </body>
</html>
"""


SETTINGS_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }} Settings</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #081018;
        --surface: #0f1822;
        --surface-2: #142030;
        --surface-3: #1a2838;
        --text: #eef4fb;
        --muted: #99a8b8;
        --line: #213244;
        --line-strong: #31506d;
        --accent: #87d6ff;
        --good: #74ddb4;
        --good-soft: rgba(116, 221, 180, 0.12);
        --bad: #ff9b9b;
        --bad-soft: rgba(255, 155, 155, 0.12);
        --shadow: 0 18px 44px rgba(0, 0, 0, 0.24);
        --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top, rgba(135, 214, 255, 0.06), transparent 28rem),
          linear-gradient(180deg, #0d151e, #081018 44rem),
          var(--bg);
      }
      main {
        max-width: 1040px;
        margin: 0 auto;
        padding: 1rem 1rem 2.5rem;
      }
      .topbar {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 1rem;
        align-items: center;
        margin-bottom: 1rem;
      }
      .brand {
        display: grid;
        gap: 0.25rem;
        justify-items: center;
        text-align: center;
      }
      .brand h1 {
        margin: 0;
        font-size: clamp(1.7rem, 4vw, 2.5rem);
        line-height: 1;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        max-width: 30rem;
        line-height: 1.4;
      }
      .panel {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        box-shadow: var(--shadow);
      }
      .panel + .panel { margin-top: 1rem; }
      .eyebrow {
        display: inline-flex;
        width: fit-content;
        border-radius: 999px;
        padding: 0.22rem 0.58rem;
        border: 1px solid var(--line);
        background: rgba(255, 255, 255, 0.02);
        color: var(--accent);
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-size: 0.72rem;
        font-weight: 700;
        font-family: var(--mono);
      }
      p {
        margin: 0;
        color: var(--muted);
        line-height: 1.45;
      }
      .top-actions {
        display: flex;
        gap: 0.65rem;
        justify-self: end;
      }
      .utility-button,
      .primary-button,
      .secondary-button,
      .row-button,
      .room-tab {
        appearance: none;
        font: inherit;
        border-radius: 12px;
        border: 1px solid var(--line);
        color: var(--text);
        text-decoration: none;
        transition: border-color 140ms ease, background 140ms ease, transform 140ms ease, color 140ms ease;
      }
      .utility-button,
      .secondary-button,
      .row-button,
      .room-tab {
        background: var(--surface-2);
      }
      .utility-button,
      .secondary-button,
      .row-button {
        padding: 0.74rem 0.92rem;
        font-weight: 700;
        cursor: pointer;
      }
      .primary-button {
        padding: 0.84rem 1.4rem;
        background: #dff4ff;
        color: #081018;
        border: none;
        font-weight: 800;
        cursor: pointer;
        min-width: 14rem;
      }
      .utility-button:hover,
      .utility-button:focus-visible,
      .primary-button:hover,
      .primary-button:focus-visible,
      .secondary-button:hover,
      .secondary-button:focus-visible,
      .row-button:hover,
      .row-button:focus-visible,
      .room-tab:hover,
      .room-tab:focus-visible {
        transform: translateY(-1px);
        border-color: var(--line-strong);
      }
      .banner {
        border-radius: 12px;
        padding: 0.82rem 0.95rem;
        font-weight: 600;
        border: 1px solid var(--line);
        margin: 1rem;
      }
      .banner.ok {
        background: var(--good-soft);
        color: var(--good);
      }
      .banner.error {
        background: var(--bad-soft);
        color: var(--bad);
      }
      .notice {
        padding: 1rem;
      }
      .room-tabs {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.85rem;
        margin-bottom: 1rem;
      }
      .room-tab {
        display: grid;
        gap: 0.15rem;
        padding: 0.9rem 1rem;
        text-align: left;
        cursor: pointer;
      }
      .room-tab strong {
        font-size: 1rem;
      }
      .room-tab span {
        color: var(--muted);
        font-size: 0.88rem;
      }
      .room-tab.is-active {
        background: var(--surface-3);
        border-color: rgba(135, 214, 255, 0.42);
        box-shadow: inset 0 0 0 1px rgba(135, 214, 255, 0.12);
      }
      .workspace {
        padding: 1rem;
      }
      .workspace.is-hidden {
        display: none;
      }
      .workspace form {
        display: grid;
        gap: 1rem;
      }
      .workspace-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: start;
      }
      .workspace-head h2 {
        margin: 0.25rem 0 0;
        font-size: 1.45rem;
      }
      .sub {
        margin-top: 0.28rem;
        color: var(--muted);
      }
      .availability {
        display: inline-flex;
        align-items: center;
        gap: 0.6rem;
        border-radius: 999px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        padding: 0.62rem 0.9rem;
        font-weight: 700;
        white-space: nowrap;
      }
      .availability input {
        width: 1rem;
        height: 1rem;
        margin: 0;
      }
      .workspace-grid {
        display: grid;
        grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
        gap: 1rem;
      }
      .workspace-stack {
        display: grid;
        gap: 1rem;
      }
      .block,
      .sidebar-note {
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        padding: 1rem;
      }
      .block-head {
        display: flex;
        justify-content: space-between;
        gap: 0.8rem;
        align-items: baseline;
        margin-bottom: 0.85rem;
      }
      .block-head strong {
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        font-family: var(--mono);
      }
      .block-head p {
        font-size: 0.86rem;
      }
      .field-note {
        color: var(--muted);
        font-size: 0.84rem;
        line-height: 1.45;
      }
      .input-empty {
        border-radius: 12px;
        border: 1px dashed var(--line);
        background: rgba(255, 255, 255, 0.02);
        padding: 0.95rem 1rem;
      }
      .device-list {
        display: flex;
        gap: 0.45rem;
        flex-wrap: wrap;
      }
      .device-chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.26rem 0.68rem;
        background: var(--surface-3);
        border: 1px solid var(--line);
        color: var(--accent);
        font-size: 0.8rem;
        font-weight: 600;
        font-family: var(--mono);
      }
      input[type="time"],
      select {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 10px;
        background: var(--surface-2);
        padding: 0.78rem 0.85rem;
        color: var(--text);
        font: inherit;
      }
      .order-list {
        display: grid;
        gap: 0.7rem;
      }
      .order-row {
        display: flex;
        align-items: center;
        gap: 0.85rem;
      }
      .order-row span {
        min-width: 6.5rem;
        color: var(--muted);
        font-size: 0.74rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .order-row select {
        flex: 1;
      }
      .schedule-shell {
        display: grid;
        gap: 0.8rem;
      }
      .schedule-head,
      .schedule-row {
        display: grid;
        grid-template-columns: 5.5rem 5.5rem minmax(0, 1fr) minmax(0, 1fr) 6.25rem;
        gap: 0.6rem;
        align-items: center;
      }
      .schedule-head {
        padding: 0 0.1rem;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .schedule-list {
        display: grid;
        gap: 0.65rem;
      }
      .schedule-row {
        border-radius: 12px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        padding: 0.75rem;
      }
      .schedule-cell {
        display: grid;
        gap: 0.35rem;
      }
      .schedule-cell span {
        display: none;
        color: var(--muted);
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .schedule-footer {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        flex-wrap: wrap;
        align-items: center;
      }
      .save-bar {
        display: flex;
        justify-content: center;
      }
      @media (max-width: 900px) {
        .topbar {
          grid-template-columns: 1fr;
          justify-items: start;
        }
        .brand {
          justify-items: start;
          text-align: left;
        }
        .top-actions {
          justify-self: start;
        }
        .workspace-grid {
          grid-template-columns: 1fr;
        }
        .workspace-head {
          flex-direction: column;
          align-items: stretch;
        }
      }
      @media (max-width: 720px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .room-tabs {
          grid-template-columns: 1fr;
        }
        .order-row {
          display: grid;
          gap: 0.4rem;
        }
        .schedule-head {
          display: none;
        }
        .schedule-row {
          grid-template-columns: 1fr;
        }
        .schedule-cell span {
          display: inline-flex;
        }
        .row-button,
        .primary-button {
          width: 100%;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="topbar">
        <a class="utility-button" href="{{ panel_url }}">Back</a>
        <div class="brand">
          <span class="eyebrow">Settings</span>
          <h1>{{ project_name }}</h1>
          <p>Pick a room, order its inputs, and save the weekly schedule.</p>
        </div>
        <div class="top-actions">
          <form method="post" action="{{ logout_url }}">
            <button class="utility-button" type="submit">Sign Out</button>
          </form>
        </div>
      </section>

      <section class="panel">
        {% if message %}
        <div class="banner ok">{{ message }}</div>
        {% elif error %}
        <div class="banner error">{{ error }}</div>
        {% else %}
        <div class="notice">
          <p>Starts follow the saved schedule. Automatic stopping only happens after the end time and sustained silence.</p>
        </div>
        {% endif %}
      </section>

      <section class="room-tabs">
        {% for host in hosts %}
        <button
          class="room-tab {% if host.room_slug == selected_room_slug %}is-active{% endif %}"
          type="button"
          data-room-tab="{{ host.room_slug }}"
        >
          <strong>{{ host.room_alias }}</strong>
          <span>{{ host.room_label }}</span>
        </button>
        {% endfor %}
      </section>

      {% for host in hosts %}
      <section
        class="panel workspace {% if host.room_slug != selected_room_slug %}is-hidden{% endif %}"
        data-room-panel="{{ host.room_slug }}"
      >
        <form method="post" action="{{ url_for('admin_update_host', slug=host.slug) }}">
          <input type="hidden" name="manual_mode" value="{{ host.manual_mode }}">
          <input type="hidden" name="notes" value="{{ host.notes }}">

          <div class="workspace-head">
            <div>
              <span class="eyebrow">{{ host.room_alias }}</span>
              <h2>{{ host.room_label }}</h2>
              <div class="sub">{{ host.label }}</div>
            </div>
            <label class="availability">
              <input type="checkbox" name="enabled" {% if host.enabled %}checked{% endif %}>
              Room available
            </label>
          </div>

          <div class="workspace-grid">
            <div class="workspace-stack">
              <section class="block">
                <div class="block-head">
                  <strong>Current input</strong>
                  <p>{{ host.current_device or "Waiting for the laptop agent" }}</p>
                </div>
                {% if host.known_devices %}
                <div class="device-list">
                  {% for device in host.known_devices %}
                  <span class="device-chip">{{ device }}</span>
                  {% endfor %}
                </div>
                {% else %}
                <div class="input-empty">
                  <p class="field-note">The laptop agent has not reported its inputs yet. This list updates automatically when it checks in.</p>
                </div>
                {% endif %}
              </section>

              <section class="block">
                <div class="block-head">
                  <strong>Schedule</strong>
                  <p>Starts are exact. Ends wait for silence after the scheduled time.</p>
                </div>
                <div class="schedule-shell">
                  <div class="schedule-head" aria-hidden="true">
                    <span>Status</span>
                    <span>Day</span>
                    <span>Start</span>
                    <span>End</span>
                    <span></span>
                  </div>
                  <div class="schedule-list" data-schedule-list>
                    {% for row in host.schedule_rows %}
                    <div class="schedule-row" data-schedule-row>
                      <div class="schedule-cell">
                        <span>Status</span>
                        <select name="schedule_enabled">
                          <option value="1" {% if row.enabled %}selected{% endif %}>On</option>
                          <option value="0" {% if not row.enabled %}selected{% endif %}>Off</option>
                        </select>
                      </div>
                      <div class="schedule-cell">
                        <span>Day</span>
                        <select name="schedule_day">
                          {% for day in ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"] %}
                          <option value="{{ day }}" {% if row.day == day %}selected{% endif %}>{{ day }}</option>
                          {% endfor %}
                        </select>
                      </div>
                      <div class="schedule-cell">
                        <span>Start</span>
                        <input type="time" name="schedule_start" value="{{ row.start }}">
                      </div>
                      <div class="schedule-cell">
                        <span>End</span>
                        <input type="time" name="schedule_end" value="{{ row.end }}">
                      </div>
                      <button class="row-button" type="button" data-remove-row>Remove</button>
                    </div>
                    {% endfor %}
                  </div>
                  <template data-schedule-row-template>
                    <div class="schedule-row" data-schedule-row>
                      <div class="schedule-cell">
                        <span>Status</span>
                        <select name="schedule_enabled">
                          <option value="1" selected>On</option>
                          <option value="0">Off</option>
                        </select>
                      </div>
                      <div class="schedule-cell">
                        <span>Day</span>
                        <select name="schedule_day">
                          <option value="MON">MON</option>
                          <option value="TUE">TUE</option>
                          <option value="WED">WED</option>
                          <option value="THU">THU</option>
                          <option value="FRI">FRI</option>
                          <option value="SAT">SAT</option>
                          <option value="SUN">SUN</option>
                        </select>
                      </div>
                      <div class="schedule-cell">
                        <span>Start</span>
                        <input type="time" name="schedule_start" value="19:00">
                      </div>
                      <div class="schedule-cell">
                        <span>End</span>
                        <input type="time" name="schedule_end" value="21:00">
                      </div>
                      <button class="row-button" type="button" data-remove-row>Remove</button>
                    </div>
                  </template>
                  <div class="schedule-footer">
                    <button class="secondary-button" type="button" data-add-row>Add Entry</button>
                    <span class="field-note">
                      {% if host.room_alias == "Room B" %}
                      Saturday 6:00 PM to 9:00 PM is on. Weeknight 7:00 PM to 9:00 PM rows are saved off.
                      {% else %}
                      Save standby rows here and switch them on only when you need them.
                      {% endif %}
                    </span>
                  </div>
                </div>
              </section>
            </div>

            <div class="workspace-stack">
              <section class="block">
                <div class="block-head">
                  <strong>Input order</strong>
                  <p>The agent uses the first available device in this order.</p>
                </div>
                <div class="order-list">
                  {% for slot in host.device_slots %}
                  <label class="order-row">
                    <span>{{ loop.index }}{% if loop.index == 1 %}st{% elif loop.index == 2 %}nd{% elif loop.index == 3 %}rd{% else %}th{% endif %} choice</span>
                    <select name="device_order">
                      <option value="">No preference</option>
                      {% for device in host.device_options %}
                      <option value="{{ device }}" {% if slot == device %}selected{% endif %}>{{ device }}</option>
                      {% endfor %}
                    </select>
                  </label>
                  {% endfor %}
                </div>
              </section>

              <section class="sidebar-note">
                <p class="field-note">The device list comes from the laptop agent. If a new input appears, place it in the order you want and save once.</p>
              </section>
            </div>
          </div>

          <div class="save-bar">
            <button class="primary-button" type="submit">Save Settings</button>
          </div>
        </form>
      </section>
      {% endfor %}

      <script>
        (() => {
          const tabs = [...document.querySelectorAll("[data-room-tab]")];
          const panels = [...document.querySelectorAll("[data-room-panel]")];

          const activateRoom = (roomSlug) => {
            tabs.forEach((tab) => {
              const active = tab.dataset.roomTab === roomSlug;
              tab.classList.toggle("is-active", active);
              tab.setAttribute("aria-pressed", active ? "true" : "false");
            });
            panels.forEach((panel) => {
              panel.classList.toggle("is-hidden", panel.dataset.roomPanel !== roomSlug);
            });
          };

          tabs.forEach((tab) => {
            tab.addEventListener("click", () => activateRoom(tab.dataset.roomTab));
          });

          if (tabs.length) {
            const initialActive = document.querySelector("[data-room-tab].is-active");
            activateRoom((initialActive || tabs[0]).dataset.roomTab);
          }

          document.querySelectorAll("form").forEach((form) => {
            const scheduleList = form.querySelector("[data-schedule-list]");
            const template = form.querySelector("[data-schedule-row-template]");
            const addButton = form.querySelector("[data-add-row]");
            if (!scheduleList || !template || !addButton) {
              return;
            }

            const bindRow = (row) => {
              const removeButton = row.querySelector("[data-remove-row]");
              if (!removeButton) {
                return;
              }
              removeButton.addEventListener("click", () => {
                const rows = scheduleList.querySelectorAll("[data-schedule-row]");
                if (rows.length === 1) {
                  row.querySelector("[name='schedule_enabled']").value = "1";
                  row.querySelector("[name='schedule_day']").value = "MON";
                  row.querySelector("[name='schedule_start']").value = "19:00";
                  row.querySelector("[name='schedule_end']").value = "21:00";
                  return;
                }
                row.remove();
              });
            };

            scheduleList.querySelectorAll("[data-schedule-row]").forEach(bindRow);

            addButton.addEventListener("click", () => {
              const fragment = template.content.cloneNode(true);
              const row = fragment.querySelector("[data-schedule-row]");
              bindRow(row);
              scheduleList.appendChild(fragment);
            });
          });
        })();
      </script>
    </main>
  </body>
</html>
"""


if __name__ == "__main__":
    main()
