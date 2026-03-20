"""Windows-friendly RoomCast source agent."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

if os.name == "nt":
    import msvcrt
else:
    import fcntl


logger = logging.getLogger(__name__)


class RoomCastAgent:
    """Poll RoomCast for desired state and publish audio when needed."""

    def __init__(
        self,
        server_url: str,
        host_slug: str,
        token: str,
        *,
        ffmpeg_path: str | None = None,
        poll_interval: int = 1,
        device_order=None,
        preferred_audio_pattern: str | None = None,
        fallback_audio_pattern: str | None = None,
        test_tone: bool = False,
        silence_timeout_seconds: int = 45,
        silence_noise_threshold: str = "-45dB",
        restart_cooldown_seconds: int = 20,
    ):
        self.server_url = server_url.rstrip("/")
        self.host_slug = host_slug
        self.token = token
        self.repo_root = Path(__file__).resolve().parent
        self.ffmpeg_path = ffmpeg_path or self._discover_ffmpeg()
        self.poll_interval = poll_interval
        self.device_order = [str(item).strip() for item in (device_order or []) if str(item).strip()]
        self.preferred_audio_pattern = preferred_audio_pattern
        self.fallback_audio_pattern = fallback_audio_pattern
        self.test_tone = test_tone
        self.silence_timeout_seconds = silence_timeout_seconds
        self.silence_noise_threshold = silence_noise_threshold
        self.restart_cooldown_seconds = restart_cooldown_seconds
        runtime_dir = self.repo_root / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_dir = runtime_dir
        self.status_path = runtime_dir / f"{host_slug}-roomcast-agent.json"
        self.ffmpeg_log_path = runtime_dir / f"{host_slug}-roomcast-ffmpeg.log"
        self.lock_path = runtime_dir / f"{host_slug}-roomcast-agent.lock"
        self.process = None
        self._ffmpeg_log_handle = None
        self._ffmpeg_log_thread = None
        self._instance_lock_handle = None
        self.running = True
        self.current_device = "test-tone" if test_tone else ""
        self.last_error = ""
        self.cached_devices = []
        self.desired_active = False
        self.silence_triggered = False
        self.restart_not_before = 0.0
        self.stream_profile = "mp3"

    def _silence_warning_message(self) -> str:
        return (
            f"No program audio detected for {self.silence_timeout_seconds} seconds. "
            "Check the room feed if the meeting should be active."
        )

    def _discover_ffmpeg(self) -> str:
        candidates = [
            shutil.which("ffmpeg"),
            str(self.repo_root / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe"),
            str(self.repo_root / "runtime" / "ffmpeg" / "ffmpeg.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        raise FileNotFoundError("ffmpeg was not found. Install it or pass --ffmpeg-path.")

    def _process_is_running(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def acquire_instance_lock(self):
        if self._instance_lock_handle:
            return
        self._instance_lock_handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                self._instance_lock_handle.seek(0)
                msvcrt.locking(self._instance_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(self._instance_lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._instance_lock_handle.seek(0)
            self._instance_lock_handle.truncate()
            self._instance_lock_handle.write(f"{os.getpid()}\n")
            self._instance_lock_handle.flush()
        except OSError:
            self._instance_lock_handle.close()
            self._instance_lock_handle = None
            raise RuntimeError(f"Another RoomCast agent is already running for {self.host_slug}.")

    def release_instance_lock(self):
        if not self._instance_lock_handle:
            return
        try:
            if os.name == "nt":
                self._instance_lock_handle.seek(0)
                msvcrt.locking(self._instance_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._instance_lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._instance_lock_handle.close()
            self._instance_lock_handle = None

    def list_audio_devices(self):
        if self.test_tone:
            return ["test-tone"]
        if os.name != "nt":
            return []

        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-list_devices",
            "true",
            "-f",
            "dshow",
            "-i",
            "dummy",
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        output = "\n".join(filter(None, [completed.stdout, completed.stderr]))
        devices = []
        for line in output.splitlines():
            if "(audio)" not in line:
                continue
            match = re.search(r'"([^"]+)"', line)
            if match:
                devices.append(match.group(1))
        deduped = []
        for device in devices:
            if device not in deduped:
                deduped.append(device)
        self.cached_devices = deduped
        return deduped

    @staticmethod
    def _match_device(devices, pattern: str | None):
        if not pattern:
            return None
        pattern_value = pattern.casefold()
        for device in devices:
            if pattern_value in device.casefold():
                return device
        return None

    @staticmethod
    def _ordered_candidates(*groups):
        ordered = []
        for group in groups:
            for item in group or []:
                text = str(item or "").strip()
                if text and text not in ordered:
                    ordered.append(text)
        return ordered

    @staticmethod
    def _audio_profile(device_name: str | None, stream_profile: str = "mp3"):
        name = (device_name or "").casefold()
        normalize_to_mono = stream_profile == "wav_pcm24"
        if "stereo mix" in name:
            profile = {
                "channels": 2,
                "bitrate": "192k",
                "filter_prefix": [],
            }
            if normalize_to_mono:
                profile["channels"] = 1
                profile["bitrate"] = ""
                profile["filter_prefix"] = ["pan=mono|c0=.5*c0+.5*c1"]
            return profile

        # Focusrite line pairs are showing up as stereo devices even though the room feed
        # is arriving as a single mono program on the left channel. Preserve that channel
        # directly instead of averaging it with the effectively-dead right side.
        left_channel_markers = (
            "analogue 1 + 2",
            "analog 1 + 2",
            "line 1 + 2",
        )
        if any(marker in name for marker in left_channel_markers):
            return {
                "channels": 1,
                "bitrate": "128k",
                "filter_prefix": ["pan=mono|c0=c0"],
            }

        return {
            "channels": 1,
            "bitrate": "128k",
            "filter_prefix": [],
        }

    def choose_device(self, server_device_order=None, server_preferred: str | None = None, server_fallback: str | None = None):
        if self.test_tone:
            return "test-tone"

        devices = self.cached_devices or self.list_audio_devices()
        for choice in self._ordered_candidates(self.device_order, server_device_order):
            if choice in devices:
                return choice

        preferred = self.preferred_audio_pattern or server_preferred or "Scarlett"
        fallback = self.fallback_audio_pattern or server_fallback or "Microphone"
        chosen = self._match_device(devices, preferred)
        if chosen:
            return chosen
        chosen = self._match_device(devices, fallback)
        if chosen:
            return chosen
        return devices[0] if devices else None

    def send_heartbeat(self):
        stream_preview = self._audio_profile(self.current_device or None, self.stream_profile)
        payload = {
            "host_slug": self.host_slug,
            "token": self.token,
            "devices": self.cached_devices if self.cached_devices else self.list_audio_devices(),
            "current_device": self.current_device,
            "is_ingesting": self._process_is_running(),
            "last_error": self.last_error,
            "stream_profile": self.stream_profile,
            "stream_channels": stream_preview["channels"],
            "sample_rate_hz": 48000,
            "sample_bits": 24 if self.stream_profile == "wav_pcm24" else 0,
        }
        response = requests.post(
            f"{self.server_url}/api/source/heartbeat",
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def _build_ffmpeg_command(self, ingest_url: str, device_name: str, stream_profile: str | None = None):
        active_stream_profile = (stream_profile or self.stream_profile or "mp3").strip().lower() or "mp3"
        profile = self._audio_profile(device_name, active_stream_profile)
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-fflags",
            "+nobuffer",
            "-flags",
            "low_delay",
        ]

        if self.test_tone:
            command.extend(
                [
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=880:sample_rate=48000",
                ]
            )
        else:
            filter_chain = list(profile["filter_prefix"])
            filter_chain.append(f"silencedetect=noise={self.silence_noise_threshold}:d={self.silence_timeout_seconds}")
            command.extend(
                [
                    "-f",
                    "dshow",
                    "-audio_buffer_size",
                    "20",
                    "-i",
                    f"audio={device_name}",
                    "-af",
                    ",".join(filter_chain),
                ]
            )

        command.extend(
            [
                "-ac",
                str(profile["channels"]),
                "-ar",
                "48000",
            ]
        )
        if active_stream_profile == "wav_pcm24":
            command.extend(
                [
                    "-c:a",
                    "pcm_s24le",
                    "-f",
                    "s24le",
                    "-content_type",
                    "application/octet-stream",
                    "-method",
                    "POST",
                    ingest_url,
                ]
            )
            return command

        command.extend(
            [
                "-b:a",
                profile["bitrate"],
                "-flush_packets",
                "1",
                "-write_xing",
                "0",
                "-content_type",
                "audio/mpeg",
                "-f",
                "mp3",
                "-method",
                "POST",
                ingest_url,
            ]
        )
        return command

    def _monitor_ffmpeg_output(self):
        if not self.process or not self.process.stdout:
            return

        for line in self.process.stdout:
            if self._ffmpeg_log_handle:
                self._ffmpeg_log_handle.write(line.encode("utf-8", errors="replace"))
                self._ffmpeg_log_handle.flush()

            if "silence_start:" in line:
                self.silence_triggered = True
                self.last_error = self._silence_warning_message()
            elif "silence_end:" in line:
                self.silence_triggered = False
                if self.last_error == self._silence_warning_message():
                    self.last_error = ""

    def start_ingest(self, ingest_url: str, device_name: str):
        if self._process_is_running():
            return

        self.current_device = device_name
        self.silence_triggered = False
        command = self._build_ffmpeg_command(ingest_url, device_name, self.stream_profile)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        if self._ffmpeg_log_handle:
            self._ffmpeg_log_handle.close()
        self._ffmpeg_log_handle = open(self.ffmpeg_log_path, "ab")
        logger.info("Starting ingest for %s using %s", self.host_slug, device_name)
        self.process = subprocess.Popen(
            command,
            cwd=self.repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creationflags,
        )
        self._ffmpeg_log_thread = threading.Thread(target=self._monitor_ffmpeg_output, daemon=True)
        self._ffmpeg_log_thread.start()

    def stop_ingest(self, *, reason: str | None = None):
        if not self._process_is_running():
            return
        logger.info("Stopping ingest for %s", self.host_slug)
        if reason:
            self.last_error = reason
        elif self.last_error == self._silence_warning_message():
            self.last_error = ""
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None
        self.silence_triggered = False
        self.restart_not_before = time.time() + self.restart_cooldown_seconds
        if self._ffmpeg_log_thread:
            self._ffmpeg_log_thread.join(timeout=1)
            self._ffmpeg_log_thread = None
        if self._ffmpeg_log_handle:
            self._ffmpeg_log_handle.close()
            self._ffmpeg_log_handle = None

    def restart_ingest(self, ingest_url: str, device_name: str):
        if self.current_device == device_name and self._process_is_running():
            return
        self.stop_ingest(reason=f"Switching to preferred input: {device_name}")
        self.restart_not_before = 0.0
        self.last_error = ""
        self.start_ingest(ingest_url, device_name)

    def write_status(self, server_reply=None):
        payload = {
            "host_slug": self.host_slug,
            "server_url": self.server_url,
            "desired_active": self.desired_active,
            "is_ingesting": self._process_is_running(),
            "current_device": self.current_device,
            "devices": self.cached_devices,
            "last_error": self.last_error,
            "server_reply": server_reply,
            "updated_at": time.time(),
        }
        self.status_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def run_forever(self):
        while self.running:
            reply = None
            try:
                if not self.test_tone:
                    self.cached_devices = self.list_audio_devices()
                reply = self.send_heartbeat()
                self.desired_active = bool(reply.get("desired_active"))
                self.stream_profile = (reply.get("stream_profile") or self.stream_profile or "mp3").strip().lower() or "mp3"
                if self.last_error != self._silence_warning_message():
                    self.last_error = ""

                if self.desired_active:
                    device_name = self.choose_device(
                        reply.get("device_order"),
                        reply.get("preferred_audio_pattern"),
                        reply.get("fallback_audio_pattern"),
                    )
                    if not self._process_is_running():
                        if time.time() < self.restart_not_before:
                            self.write_status(server_reply=reply)
                            time.sleep(self.poll_interval)
                            continue
                        if not device_name:
                            self.last_error = "No compatible input device was found."
                        else:
                            self.start_ingest(reply["ingest_url"], device_name)
                    elif not device_name:
                        self.last_error = "No compatible input device was found."
                    elif device_name != self.current_device:
                        self.restart_ingest(reply["ingest_url"], device_name)
                    elif self.process and self.process.poll() not in (None, 0):
                        self.last_error = f"ffmpeg exited with code {self.process.returncode}"
                else:
                    self.stop_ingest()
            except Exception as exc:
                self.last_error = str(exc)
                logger.warning("RoomCast heartbeat cycle failed: %s", exc)

            if self.process and self.process.poll() not in (None, 0):
                self.last_error = f"ffmpeg exited with code {self.process.returncode}"
                self.process = None

            self.write_status(server_reply=reply)
            time.sleep(self.poll_interval)

        self.stop_ingest()
        self.write_status(server_reply={"shutdown": True})


def main():
    parser = argparse.ArgumentParser(description="RoomCast source agent")
    parser.add_argument("--server-url", required=True, help="Base URL for the RoomCast service")
    parser.add_argument("--host-slug", required=True, help="Host slug registered on the RoomCast server")
    parser.add_argument("--token", required=True, help="Heartbeat token for this source host")
    parser.add_argument("--ffmpeg-path", help="Explicit path to ffmpeg")
    parser.add_argument("--poll-interval", type=int, default=1, help="Seconds between heartbeat polls")
    parser.add_argument(
        "--device-order",
        action="append",
        default=[],
        help="Exact device name to prefer. Pass multiple times to set fallback order.",
    )
    parser.add_argument("--audio-pattern", help="Preferred input-device substring to match")
    parser.add_argument("--fallback-audio-pattern", help="Fallback input-device substring to match")
    parser.add_argument("--list-devices", action="store_true", help="List available input devices and exit")
    parser.add_argument("--test-tone", action="store_true", help="Publish a generated tone instead of a real device")
    parser.add_argument("--silence-timeout-seconds", type=int, default=15, help="Warn after this many silent seconds")
    parser.add_argument("--silence-noise-threshold", default="-45dB", help="ffmpeg silencedetect noise threshold")
    parser.add_argument("--restart-cooldown-seconds", type=int, default=20, help="Seconds to wait before retrying after an auto-stop")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler("roomcast-agent.log"),
            logging.StreamHandler(),
        ],
    )

    agent = RoomCastAgent(
        args.server_url,
        args.host_slug,
        args.token,
        ffmpeg_path=args.ffmpeg_path,
        poll_interval=args.poll_interval,
        device_order=args.device_order,
        preferred_audio_pattern=args.audio_pattern,
        fallback_audio_pattern=args.fallback_audio_pattern,
        test_tone=args.test_tone,
        silence_timeout_seconds=args.silence_timeout_seconds,
        silence_noise_threshold=args.silence_noise_threshold,
        restart_cooldown_seconds=args.restart_cooldown_seconds,
    )

    if args.list_devices:
        print("\n".join(agent.list_audio_devices()))
        return

    try:
        agent.acquire_instance_lock()
    except RuntimeError as exc:
        logger.info("%s", exc)
        return

    def _shutdown(signum, frame):
        del signum, frame
        logger.info("Shutdown requested")
        agent.running = False

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        agent.run_forever()
    finally:
        agent.release_instance_lock()


if __name__ == "__main__":
    main()
