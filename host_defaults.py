"""Default host inventory and schedules for the TurboWatch control panel."""

from copy import deepcopy

DEFAULT_TIMEZONE = "America/New_York"

_DEFAULT_HOSTS = [
    {
        "slug": "hp-pavilion-14m-ba1xx",
        "label": "HP Pavilion x360 14m-ba1xx",
        "room": "Tarry Meeting Hall",
        "enabled": True,
        "manual_mode": "auto",
        "notes": "Primary Saturday meeting laptop.",
        "timezone": DEFAULT_TIMEZONE,
        "schedules": [
            {"day": "MON", "start": "19:00", "end": "21:00", "enabled": False},
            {"day": "TUE", "start": "19:00", "end": "21:00", "enabled": False},
            {"day": "WED", "start": "19:00", "end": "21:00", "enabled": False},
            {"day": "THU", "start": "19:00", "end": "21:00", "enabled": False},
            {"day": "FRI", "start": "19:00", "end": "21:00", "enabled": False},
            {"day": "SAT", "start": "18:00", "end": "21:00", "enabled": True},
        ],
    },
    {
        "slug": "hp-envy-16-ad0xx",
        "label": "HP Envy 16-ad0xx",
        "room": "Other room / Bible study room",
        "enabled": True,
        "manual_mode": "auto",
        "notes": "Used for Sunday meetings and Wednesday Bible study.",
        "timezone": DEFAULT_TIMEZONE,
        "schedules": [
            {"day": "SUN", "start": "10:25", "end": "14:05", "enabled": True},
            {"day": "WED", "start": "18:55", "end": "21:05", "enabled": True},
        ],
    },
    {
        "slug": "leonovo-laptop-mv23gfqd",
        "label": "Lenovo laptop MV23GFQD",
        "room": "Spare / diagnostics host",
        "enabled": False,
        "manual_mode": "force_off",
        "notes": "Reachable over SSH, but not currently part of the default TurboBridge schedule.",
        "timezone": DEFAULT_TIMEZONE,
        "schedules": [],
    },
]


def get_default_hosts():
    """Return a deep copy of the seeded control-panel host configuration."""

    return deepcopy(_DEFAULT_HOSTS)
