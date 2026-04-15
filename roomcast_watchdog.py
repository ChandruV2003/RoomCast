"""Deterministic health watchdog for RoomCast hosts and rooms."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from roomcast_store import RoomCastStore


def _parse_iso8601(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class WatchdogIssue:
    severity: str
    host_slug: str
    room_slug: str
    code: str
    message: str


def evaluate_hosts(store: RoomCastStore, *, heartbeat_stale_seconds: int, startup_grace_seconds: int):
    now = datetime.now(timezone.utc)
    heartbeat_cutoff = now - timedelta(seconds=max(5, heartbeat_stale_seconds))
    startup_cutoff = now - timedelta(seconds=max(5, startup_grace_seconds))
    issues: list[WatchdogIssue] = []

    for host in store.list_hosts():
        runtime = host.get("runtime") or {}
        room_slug = host["room_slug"]
        host_slug = host["slug"]
        last_seen = _parse_iso8601(runtime.get("last_seen_at"))
        online = bool(last_seen and last_seen >= heartbeat_cutoff)
        recently_seen = bool(last_seen and last_seen >= startup_cutoff)
        desired_active = bool(host.get("desired_active"))
        is_ingesting = bool(runtime.get("is_ingesting"))
        current_device = (runtime.get("current_device") or "").strip()
        last_error = (runtime.get("last_error") or "").strip()
        device_order = host.get("device_order") or []

        if desired_active and not online:
            issues.append(
                WatchdogIssue(
                    severity="critical",
                    host_slug=host_slug,
                    room_slug=room_slug,
                    code="host-offline",
                    message=f"{host['label']} should be active but has not heartbeated recently.",
                )
            )
            continue

        if desired_active and online and not is_ingesting and not recently_seen:
            issues.append(
                WatchdogIssue(
                    severity="critical",
                    host_slug=host_slug,
                    room_slug=room_slug,
                    code="ingest-down",
                    message=f"{host['label']} should be active but ingest is not running.",
                )
            )

        if online and last_error:
            issues.append(
                WatchdogIssue(
                    severity="warn",
                    host_slug=host_slug,
                    room_slug=room_slug,
                    code="runtime-error",
                    message=f"{host['label']} reported: {last_error}",
                )
            )

        if desired_active and online and not current_device:
            issues.append(
                WatchdogIssue(
                    severity="warn",
                    host_slug=host_slug,
                    room_slug=room_slug,
                    code="missing-device",
                    message=f"{host['label']} is active but has no current input device.",
                )
            )

        if online and current_device and device_order and current_device not in device_order:
            issues.append(
                WatchdogIssue(
                    severity="warn",
                    host_slug=host_slug,
                    room_slug=room_slug,
                    code="unexpected-device",
                    message=f"{host['label']} is using {current_device}, which is not in the saved device order.",
                )
            )

    return issues


def main():
    parser = argparse.ArgumentParser(description="RoomCast watchdog")
    parser.add_argument("--db-path", default=os.getenv("ROOMCAST_DB_PATH"), help="Path to roomcast.db")
    parser.add_argument("--heartbeat-stale-seconds", type=int, default=45, help="Seconds before a host is considered stale")
    parser.add_argument("--startup-grace-seconds", type=int, default=25, help="Grace period before treating a desired-active host as failed")
    parser.add_argument("--record-incidents", action="store_true", help="Write detected issues into meeting_incidents")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args()

    store = RoomCastStore(args.db_path)
    issues = evaluate_hosts(
        store,
        heartbeat_stale_seconds=args.heartbeat_stale_seconds,
        startup_grace_seconds=args.startup_grace_seconds,
    )

    if args.record_incidents:
        for issue in issues:
            store.record_incident(
                issue.room_slug,
                host_slug=issue.host_slug,
                severity=issue.severity,
                message=f"[watchdog:{issue.code}] {issue.message}",
            )

    if args.json:
        payload = {
            "ok": not issues,
            "issue_count": len(issues),
            "issues": [asdict(issue) for issue in issues],
        }
        print(json.dumps(payload, indent=2))
    else:
        if not issues:
            print("OK: RoomCast hosts are healthy.")
        else:
            for issue in issues:
                print(f"{issue.severity.upper()} {issue.host_slug} {issue.code}: {issue.message}")

    if any(issue.severity == "critical" for issue in issues):
        raise SystemExit(2)
    if issues:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
