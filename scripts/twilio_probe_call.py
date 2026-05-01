#!/usr/bin/env python3
"""Place and optionally record a Twilio-originated probe call to RoomCast."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from fetch_telnyx_recording import analyze_wav, load_env, safe_name  # noqa: E402


API_BASE = "https://api.twilio.com"
DEFAULT_TO_NUMBER = "+18628727904"
DEFAULT_DURATION_SECONDS = 90
TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


class TwilioApiError(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"Twilio API returned HTTP {status}: {body}")
        self.status = status
        self.body = body


def _basic_auth(account_sid: str, auth_token: str) -> str:
    token = base64.b64encode(f"{account_sid}:{auth_token}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _twilio_request(
    account_sid: str,
    auth_token: str,
    method: str,
    path: str,
    data: dict[str, str] | None = None,
) -> dict:
    body = None
    headers = {
        "Authorization": _basic_auth(account_sid, auth_token),
        "Accept": "application/json",
    }
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise TwilioApiError(exc.code, error_body) from exc
    if not payload:
        return {}
    return json.loads(payload)


def _download_recording_media(
    account_sid: str,
    auth_token: str,
    recording: dict,
    output_dir: Path,
) -> Path:
    uri = (recording.get("uri") or "").replace(".json", ".wav")
    if not uri:
        raise ValueError("recording did not include a media URI")
    recording_sid = recording.get("sid") or "recording"
    call_sid = recording.get("call_sid") or "call"
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.chmod(0o700)
    except PermissionError:
        pass
    path = output_dir / f"{safe_name(call_sid)}-{safe_name(recording_sid)}.wav"
    request = urllib.request.Request(
        f"{API_BASE}{uri}",
        headers={"Authorization": _basic_auth(account_sid, auth_token)},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        path.write_bytes(response.read())
    try:
        path.chmod(0o600)
    except PermissionError:
        pass
    return path


def _redacted_call_summary(call: dict) -> dict:
    keys = [
        "sid",
        "status",
        "direction",
        "from",
        "to",
        "start_time",
        "end_time",
        "duration",
        "price",
        "price_unit",
        "queue_time",
        "error_code",
        "error_message",
    ]
    return {key: call.get(key) for key in keys if key in call}


def _build_send_digits(pin: str, leading_waits: str) -> str:
    normalized_pin = "".join(ch for ch in pin if ch.isdigit())
    if not normalized_pin:
        return ""
    return f"{leading_waits}{normalized_pin}#"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env", help="env file with TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN")
    parser.add_argument("--from-number", default=os.getenv("TWILIO_FROM_NUMBER", "+18449902638"))
    parser.add_argument("--to-number", default=os.getenv("ROOMCAST_PHONE_NUMBER", DEFAULT_TO_NUMBER))
    parser.add_argument("--pin", default=os.getenv("ROOMCAST_PHONE_PIN", "7070"))
    parser.add_argument("--send-digits", default=os.getenv("TWILIO_SEND_DIGITS", ""))
    parser.add_argument("--leading-waits", default=os.getenv("TWILIO_SEND_DIGIT_WAITS", "wwww"))
    parser.add_argument("--duration-seconds", type=int, default=int(os.getenv("TWILIO_PROBE_DURATION_SECONDS", str(DEFAULT_DURATION_SECONDS))))
    parser.add_argument(
        "--hold-mode",
        choices=("gather", "pause"),
        default=os.getenv("TWILIO_PROBE_HOLD_MODE", "gather"),
        help="TwiML used to keep the probe call connected after DTMF is sent.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=int(os.getenv("TWILIO_PROBE_TIMEOUT_SECONDS", "30")))
    parser.add_argument("--poll-interval", type=float, default=3.0)
    parser.add_argument("--wait", action="store_true", help="poll until the call reaches a terminal status")
    parser.add_argument("--no-record", action="store_true", help="do not ask Twilio to record the probe call")
    parser.add_argument("--recording-channels", choices=("mono", "dual"), default=os.getenv("TWILIO_RECORDING_CHANNELS", "dual"))
    parser.add_argument("--download-recording", action="store_true", help="download the Twilio recording after the call completes")
    parser.add_argument("--output-dir", default="data/twilio-probe-recordings")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env(Path(args.env_file))
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()

    send_digits = args.send_digits or _build_send_digits(args.pin, args.leading_waits)
    duration = max(5, min(600, args.duration_seconds))
    if args.hold_mode == "pause":
        twiml = f'<Response><Pause length="{duration}"/></Response>'
    else:
        twiml = f'<Response><Gather input="dtmf" timeout="{duration}" numDigits="1"/></Response>'
    form = {
        "To": args.to_number,
        "From": args.from_number,
        "Twiml": twiml,
        "Timeout": str(max(5, min(120, args.timeout_seconds))),
    }
    if send_digits:
        form["SendDigits"] = send_digits
    if not args.no_record:
        form.update(
            {
                "Record": "true",
                "RecordingChannels": args.recording_channels,
                "Trim": "do-not-trim",
            }
        )

    if args.dry_run:
        redacted = dict(form)
        if "SendDigits" in redacted:
            redacted["SendDigits"] = "<redacted DTMF>"
        print(json.dumps({"dry_run": True, "request": redacted}, indent=2))
        return 0

    if not account_sid or not auth_token:
        raise SystemExit("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set in env or .env.")

    call = _twilio_request(
        account_sid,
        auth_token,
        "POST",
        f"/2010-04-01/Accounts/{account_sid}/Calls.json",
        form,
    )
    call_sid = call["sid"]
    summary = {"created": _redacted_call_summary(call)}

    final_call = call
    if args.wait:
        while (final_call.get("status") or "").lower() not in TERMINAL_STATUSES:
            time.sleep(max(1.0, args.poll_interval))
            final_call = _twilio_request(
                account_sid,
                auth_token,
                "GET",
                f"/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json",
            )
        summary["final"] = _redacted_call_summary(final_call)

    if args.download_recording:
        recordings = _twilio_request(
            account_sid,
            auth_token,
            "GET",
            f"/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}/Recordings.json",
        ).get("recordings", [])
        summary["recordings"] = [
            {
                "sid": item.get("sid"),
                "status": item.get("status"),
                "duration": item.get("duration"),
                "channels": item.get("channels"),
                "source": item.get("source"),
            }
            for item in recordings
        ]
        completed = [item for item in recordings if item.get("status") == "completed"]
        if completed:
            wav_path = _download_recording_media(
                account_sid,
                auth_token,
                completed[0],
                Path(args.output_dir),
            )
            summary["downloaded_wav"] = str(wav_path)
            summary["analysis"] = analyze_wav(wav_path)

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except TwilioApiError as exc:
        print(json.dumps({"error": "twilio_api_error", "status": exc.status, "body": exc.body}, indent=2), file=sys.stderr)
        raise SystemExit(1)
