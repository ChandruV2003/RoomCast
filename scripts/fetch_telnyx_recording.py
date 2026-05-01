#!/usr/bin/env python3
"""Download and summarize recent Telnyx call recordings for RoomCast."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import urllib.parse
import urllib.request
import wave


API_BASE = "https://api.telnyx.com/v2"
DEFAULT_NUMBER = "+18628727904"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def telnyx_get(path: str, params: dict[str, str | int] | None = None) -> dict:
    api_key = os.environ.get("TELNYX_API_KEY", "")
    if not api_key:
        raise SystemExit("TELNYX_API_KEY is not set. Add it to .env or export it first.")
    url = f"{API_BASE}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")


def redact_recording(recording: dict) -> dict:
    redacted = dict(recording)
    if isinstance(redacted.get("download_urls"), dict):
        redacted["download_urls"] = {key: "<redacted signed url>" for key in redacted["download_urls"]}
    return redacted


def decode_samples(raw: bytes, sample_width: int) -> tuple[list[int], float]:
    if sample_width == 1:
        return [byte - 128 for byte in raw], 128.0
    if sample_width == 2:
        return list(struct.unpack("<" + "h" * (len(raw) // 2), raw)), 32768.0
    if sample_width == 3:
        samples: list[int] = []
        usable = len(raw) - (len(raw) % 3)
        for offset in range(0, usable, 3):
            chunk = raw[offset : offset + 3]
            sign = b"\xff" if chunk[2] & 0x80 else b"\x00"
            samples.append(int.from_bytes(chunk + sign, "little", signed=True))
        return samples, 8_388_608.0
    if sample_width == 4:
        return list(struct.unpack("<" + "i" * (len(raw) // 4), raw)), 2_147_483_648.0
    raise ValueError(f"unsupported WAV sample width: {sample_width}")


def analyze_wav(path: Path) -> dict:
    with wave.open(str(path), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        frame_count = wav_file.getnframes()
        raw = wav_file.readframes(frame_count)

    samples, full_scale = decode_samples(raw, sample_width)
    channel_metrics = []
    for channel in range(channels):
        channel_samples = samples[channel::channels]
        if not channel_samples:
            continue
        rms = math.sqrt(sum(sample * sample for sample in channel_samples) / len(channel_samples)) / full_scale
        peak = max(abs(sample) for sample in channel_samples) / full_scale
        clipped = sum(1 for sample in channel_samples if abs(sample) >= full_scale * 0.999)
        zero_crossings = sum(
            1
            for current, nxt in zip(channel_samples, channel_samples[1:])
            if (current < 0 <= nxt) or (current >= 0 > nxt)
        )
        seconds = len(channel_samples) / sample_rate if sample_rate else 0
        channel_metrics.append(
            {
                "channel": channel + 1,
                "rms_dbfs": round(20 * math.log10(rms), 2) if rms else -999,
                "peak_dbfs": round(20 * math.log10(peak), 2) if peak else -999,
                "clipped_samples": clipped,
                "zero_crossings_per_sec": round(zero_crossings / seconds, 1) if seconds else 0,
            }
        )

    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "duration_sec": round(frame_count / sample_rate, 2) if sample_rate else 0,
        "channel_metrics": channel_metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="env file containing TELNYX_API_KEY")
    parser.add_argument("--to", default=DEFAULT_NUMBER, help="destination number to match")
    parser.add_argument("--from-number", default="", help="caller number to match")
    parser.add_argument("--recording-id", default="", help="specific Telnyx recording id")
    parser.add_argument("--limit", type=int, default=25, help="number of recent recordings to inspect")
    parser.add_argument("--output-dir", default="data/telnyx-recordings", help="download target directory")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.chmod(0o700)
    except PermissionError:
        pass

    response = telnyx_get("/recordings", {"page[size]": max(1, min(args.limit, 100))})
    recordings = response.get("data") or []
    selected = None
    for recording in recordings:
        if args.recording_id and recording.get("id") != args.recording_id:
            continue
        if args.to and recording.get("to") != args.to:
            continue
        if args.from_number and recording.get("from") != args.from_number:
            continue
        if recording.get("status") != "completed":
            continue
        selected = recording
        break

    if not selected:
        print(json.dumps({"error": "no completed matching Telnyx recording found", "inspected": len(recordings)}, indent=2))
        return 1

    wav_url = (selected.get("download_urls") or {}).get("wav")
    if not wav_url:
        print(json.dumps({"error": "selected recording has no WAV download URL", "recording": redact_recording(selected)}, indent=2))
        return 1

    started = safe_name((selected.get("recording_started_at") or selected.get("created_at") or "unknown").replace(":", ""))
    recording_id = safe_name(selected["id"])
    wav_path = output_dir / f"{started}-{recording_id}.wav"
    json_path = wav_path.with_suffix(".json")

    urllib.request.urlretrieve(wav_url, wav_path)
    json_path.write_text(json.dumps(redact_recording(selected), indent=2) + "\n")
    try:
        wav_path.chmod(0o600)
        json_path.chmod(0o600)
    except PermissionError:
        pass
    analysis = analyze_wav(wav_path)
    summary = {
        "recording_id": selected.get("id"),
        "from": selected.get("from"),
        "to": selected.get("to"),
        "started_at": selected.get("recording_started_at"),
        "ended_at": selected.get("recording_ended_at"),
        "duration_millis": selected.get("duration_millis"),
        "wav_path": str(wav_path),
        "metadata_path": str(json_path),
        "analysis": analysis,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
