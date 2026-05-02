"""Server-centered audio bridge app with separate public and admin views."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import os
import array
import base64
import queue
import re
import shutil
import threading
import hmac
import secrets
import time
import csv
import io
import struct
import subprocess
import tempfile
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, redirect, render_template_string, request, send_file, session, url_for
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from gunicorn.http.errors import NoMoreData as GunicornNoMoreData
except Exception:  # pragma: no cover - only available in gunicorn runtime
    GunicornNoMoreData = ()

from roomcast_store import RoomCastStore

try:
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - Python 3.13+ may not ship audioop
    audioop = None

try:
    from G722 import G722 as G722Codec  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency in tests
    G722Codec = None

try:
    import opuslib  # type: ignore
except ImportError:  # pragma: no cover - optional runtime dependency in tests
    opuslib = None


LISTENER_QUEUE_MAXSIZE = 64
# Keep ingest chunks aligned to 24-bit PCM frame boundaries so a listener can
# join mid-stream without starting in the middle of a sample and hearing static.
INGEST_CHUNK_SIZE = 3072
RECENT_CHUNK_BACKLOG = 24
LIVE_SNAPSHOT_WAIT_SECONDS = 6.0
LIVE_SNAPSHOT_POLL_SECONDS = 0.2
SIGNAL_FLOOR_DB = -90.0


STATUS_TTL_SECONDS = 45
STREAM_PROFILES = {
    "mp3": {
        "mimetype": "audio/mpeg",
    },
    "wav_pcm24": {
        "mimetype": "audio/wav",
        "channels": 1,
        "sample_rate_hz": 48000,
        "bits_per_sample": 24,
    },
}
BROWSER_STREAM_PROFILE = {
    "mimetype": "audio/wav",
    "channels": 2,
    "sample_rate_hz": 48000,
    "bits_per_sample": 16,
}
BROWSER_STREAM_CHUNK_MILLISECONDS = 125
TELEPHONY_STREAM_PROFILE = {
    "mimetype": "audio/wav",
    "channels": 1,
    "sample_rate_hz": 16000,
    "bits_per_sample": 16,
}
TELEPHONY_LISTENER_QUEUE_MAXSIZE = 384
TELEPHONY_PROMPTS = {
    "welcome_pin": {
        "label": "Welcome and PIN",
        "text": "Welcome to NTC Newark WebCall. Please enter your four-digit PIN.",
    },
    "goodbye": {
        "label": "Goodbye",
        "text": "Goodbye.",
    },
    "no_active_schedule": {
        "label": "No Scheduled Meeting",
        "text": "Welcome to NTC Newark WebCall. There is no scheduled meeting active right now. Please try again when the meeting begins. Goodbye.",
    },
    "invalid_pin": {
        "label": "Invalid PIN",
        "text": "Sorry, that pin was not accepted. Please try again.",
    },
    "no_active_conference": {
        "label": "No Active Conference",
        "text": "That pin is valid, but no conference is active right now. Please try again when the meeting begins. Goodbye.",
    },
    "conference_ending": {
        "label": "Conference Ending",
        "text": "Conference ending. Goodbye.",
    },
    "technical_difficulty": {
        "label": "Technical Difficulty",
        "text": "We experienced a technical difficulty. Please call back in a moment. Goodbye.",
    },
    "line_unavailable": {
        "label": "Line Unavailable",
        "text": "The line is no longer available. Goodbye.",
    },
}
HLS_LISTENER_QUEUE_MAXSIZE = 256
HLS_IDLE_TIMEOUT_SECONDS = 45.0
HLS_CLIENT_TTL_SECONDS = 45.0
STREAM_RECOVERY_GRACE_SECONDS = 12.0
ROOM_ALIASES = {
    "study-room": "Main Sanctuary",
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


def _wav_stream_header(*, channels: int, sample_rate_hz: int, bits_per_sample: int) -> bytes:
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate_hz * block_align
    placeholder_size = 0xFFFFFFFF
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        placeholder_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate_hz,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        placeholder_size,
    )


def _wav_file_bytes(*, channels: int, sample_rate_hz: int, bits_per_sample: int, payload: bytes) -> bytes:
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate_hz * block_align
    data_size = len(payload)
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate_hz,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_size,
    )
    return header + payload


def _pcm24le_to_int(sample: bytes) -> int:
    value = sample[0] | (sample[1] << 8) | (sample[2] << 16)
    if value & 0x800000:
        value -= 1 << 24
    return value


def _linear16_to_pcmu_sample(sample: int) -> int:
    clip = 32635
    bias = 0x84
    sign = 0x80 if sample < 0 else 0x00
    magnitude = min(abs(sample), clip) + bias
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (magnitude & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


def _linear16le_to_pcmu(pcm_bytes: bytes) -> bytes:
    if audioop is not None:
        return audioop.lin2ulaw(pcm_bytes, 2)
    payload = bytearray()
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    for offset in range(0, usable, 2):
        sample = struct.unpack_from("<h", pcm_bytes, offset)[0]
        payload.append(_linear16_to_pcmu_sample(sample))
    return bytes(payload)


def _linear16_to_pcma_sample(sample: int) -> int:
    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 1
    sample = min(sample, 0x7FFF)
    if sample < 256:
        aval = sample >> 4
    else:
        segment = 1
        value = sample >> 8
        while value > 1 and segment < 8:
            value >>= 1
            segment += 1
        aval = (segment << 4) | ((sample >> (segment + 3)) & 0x0F)
    return aval ^ mask


def _linear16le_to_pcma(pcm_bytes: bytes) -> bytes:
    if audioop is not None:
        return audioop.lin2alaw(pcm_bytes, 2)
    payload = bytearray()
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    for offset in range(0, usable, 2):
        sample = struct.unpack_from("<h", pcm_bytes, offset)[0]
        payload.append(_linear16_to_pcma_sample(sample))
    return bytes(payload)


def _linear16le_to_l16be(pcm_bytes: bytes) -> bytes:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    # Telnyx's media streaming docs list L16 but do not specify byte order.
    # Live PSTN tests produced full static with RTP/network-order PCM, while
    # Telnyx's raw linear16 docs use signed little-endian PCM.
    return pcm_bytes[:usable]


def _linear16le_to_g722(pcm_bytes: bytes, encoder) -> bytes:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    samples = array.array("h")
    samples.frombytes(pcm_bytes[:usable])
    if samples.itemsize != 2:
        raise RuntimeError("G722 encoder requires 16-bit PCM samples")
    if struct.pack("=H", 1) == b"\x00\x01":
        samples.byteswap()
    return encoder.encode(samples)


def _linear16le_to_opus(pcm_bytes: bytes, encoder) -> bytes:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    # The stream is mono PCM16, so frame_size is the sample count.
    return encoder.encode(pcm_bytes[:usable], usable // 2)


def _telnyx_rtp_payload_type(codec: str) -> int:
    normalized = (codec or "").strip().upper()
    if normalized == "PCMU":
        return 0
    if normalized == "PCMA":
        return 8
    if normalized == "G722":
        return 9
    if normalized == "OPUS":
        return 111
    if normalized == "AMR-WB":
        return 112
    return 96


def _telnyx_rtp_clock_rate(codec: str, sample_rate_hz: int) -> int:
    normalized = (codec or "").strip().upper()
    if normalized in {"PCMU", "PCMA", "G722"}:
        return 8000
    if normalized == "OPUS":
        # RFC 7587 uses a 48 kHz RTP timestamp clock for Opus regardless of
        # the original capture/sample rate.
        return 48000
    if normalized in {"AMR-WB", "L16"}:
        return 16000
    return max(8000, int(sample_rate_hz or 8000))


def _rtp_packet(payload: bytes, *, payload_type: int, sequence_number: int, timestamp: int, ssrc: int) -> bytes:
    header = struct.pack(
        "!BBHII",
        0x80,
        payload_type & 0x7F,
        sequence_number & 0xFFFF,
        timestamp & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    )
    return header + payload


def _pcm16_sine_frame(sample_rate_hz: int, frame_samples: int, frequency_hz: float, amplitude: float, phase: float) -> tuple[bytes, float]:
    sample_rate = max(1, int(sample_rate_hz))
    frame_count = max(1, int(frame_samples))
    frequency = max(20.0, float(frequency_hz))
    gain = max(0.0, min(0.95, float(amplitude)))
    phase_step = (2.0 * math.pi * frequency) / sample_rate
    samples = array.array("h")
    for _ in range(frame_count):
        samples.append(int(math.sin(phase) * gain * 32767))
        phase += phase_step
        if phase >= 2.0 * math.pi:
            phase -= 2.0 * math.pi
    if struct.pack("=H", 1) == b"\x00\x01":
        samples.byteswap()
    return samples.tobytes(), phase


def _pcm16_multitone_frame(
    sample_rate_hz: int,
    frame_samples: int,
    frequencies_hz: list[float],
    amplitude: float,
    phases: list[float],
) -> tuple[bytes, list[float]]:
    sample_rate = max(1, int(sample_rate_hz))
    frame_count = max(1, int(frame_samples))
    frequencies = [max(20.0, float(frequency)) for frequency in frequencies_hz if frequency > 0]
    if not frequencies:
        frequencies = [440.0]
    gain = max(0.0, min(0.95, float(amplitude)))
    if len(phases) != len(frequencies):
        phases = [0.0 for _ in frequencies]
    phase_steps = [(2.0 * math.pi * frequency) / sample_rate for frequency in frequencies]
    normalizer = math.sqrt(len(frequencies))
    samples = array.array("h")
    for _ in range(frame_count):
        mixed = 0.0
        for index, phase in enumerate(phases):
            mixed += math.sin(phase)
            phase += phase_steps[index]
            if phase >= 2.0 * math.pi:
                phase -= 2.0 * math.pi
            phases[index] = phase
        sample = max(-1.0, min(1.0, mixed / normalizer)) * gain
        samples.append(int(sample * 32767))
    if struct.pack("=H", 1) == b"\x00\x01":
        samples.byteswap()
    return samples.tobytes(), phases


TELEPHONY_DSP_NOISE_FLOOR_DB = -45.0
TELEPHONY_DSP_TARGET_RMS_DB = -20.0
TELEPHONY_DSP_MAX_GAIN_DB = 18.0
TELEPHONY_DSP_MIN_GAIN_DB = -6.0
TELEPHONY_DSP_GAIN_SMOOTHING = 0.25


def _pcm16le_rms_db(pcm_bytes: bytes) -> float | None:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return None
    payload = pcm_bytes[:usable]
    if audioop is not None:
        rms = audioop.rms(payload, 2)
    else:
        sum_squares = 0.0
        sample_count = 0
        for offset in range(0, usable, 2):
            sample = struct.unpack_from("<h", payload, offset)[0]
            sum_squares += float(sample) * float(sample)
            sample_count += 1
        if sample_count <= 0:
            return None
        rms = math.sqrt(sum_squares / sample_count)
    if rms <= 0:
        return None
    return 20.0 * math.log10(max(rms / 32767.0, 1e-8))


def _apply_pcm16le_gain(pcm_bytes: bytes, gain: float) -> bytes:
    usable = len(pcm_bytes) - (len(pcm_bytes) % 2)
    if usable <= 0:
        return b""
    payload = pcm_bytes[:usable]
    if abs(gain - 1.0) <= 1e-6:
        return payload
    if audioop is not None:
        return audioop.mul(payload, 2, gain)
    output = bytearray()
    for offset in range(0, usable, 2):
        sample = struct.unpack_from("<h", payload, offset)[0]
        scaled = int(sample * gain)
        output.extend(struct.pack("<h", max(-32768, min(32767, scaled))))
    return bytes(output)


def _limit_pcm16le_peak(pcm_bytes: bytes, *, peak_limit: int = 30000) -> bytes:
    if not pcm_bytes:
        return b""
    if audioop is not None:
        peak = audioop.max(pcm_bytes, 2)
    else:
        peak = 0
        for offset in range(0, len(pcm_bytes), 2):
            peak = max(peak, abs(struct.unpack_from("<h", pcm_bytes, offset)[0]))
    if peak > peak_limit:
        return _apply_pcm16le_gain(pcm_bytes, float(peak_limit) / float(peak))
    return pcm_bytes


def _signal_percent_from_db(db_value: float | None) -> float:
    if db_value is None:
        return 0.0
    clamped = max(SIGNAL_FLOOR_DB, min(0.0, float(db_value)))
    return ((clamped - SIGNAL_FLOOR_DB) / abs(SIGNAL_FLOOR_DB)) * 100.0


def _chunk_signal_stats(chunk: bytes, *, bits_per_sample: int) -> tuple[float | None, float | None]:
    if bits_per_sample not in (16, 24):
        return None, None

    bytes_per_sample = bits_per_sample // 8
    usable = len(chunk) - (len(chunk) % bytes_per_sample)
    if usable <= 0:
        return None, None

    peak_value = 0
    sum_squares = 0.0
    sample_count = 0
    full_scale = 32767 if bits_per_sample == 16 else 8388607

    for offset in range(0, usable, bytes_per_sample):
        if bits_per_sample == 24:
            sample_value = _pcm24le_to_int(chunk[offset:offset + 3])
        else:
            sample_value = struct.unpack_from("<h", chunk, offset)[0]
        peak_value = max(peak_value, abs(sample_value))
        sum_squares += float(sample_value) * float(sample_value)
        sample_count += 1

    if sample_count <= 0:
        return None, None

    rms_linear = math.sqrt(sum_squares / sample_count) / full_scale
    peak_linear = peak_value / full_scale
    rms_db = 20.0 * math.log10(max(rms_linear, 1e-8))
    peak_db = 20.0 * math.log10(max(peak_linear, 1e-8))
    return rms_db, peak_db


class TelephonyPcmTranscoder:
    """Convert live PCM frames into a phone-friendly mono PCM16 stream."""

    def __init__(
        self,
        *,
        source_channels: int,
        source_rate_hz: int,
        bits_per_sample: int,
        output_rate_hz: int,
        mono_mix: str = "left",
        phone_dsp_enabled: bool = False,
        phone_gain_db: float = 0.0,
    ):
        self.source_channels = max(1, int(source_channels or 1))
        self.source_rate_hz = max(8000, int(source_rate_hz or 48000))
        self.bits_per_sample = max(16, int(bits_per_sample or 24))
        self.output_rate_hz = max(8000, int(output_rate_hz or 8000))
        self.mono_mix = (mono_mix or "left").strip().lower()
        self.phone_dsp_enabled = bool(phone_dsp_enabled)
        self.phone_gain_db = float(phone_gain_db or 0.0)
        self._bytes_per_sample = max(1, self.bits_per_sample // 8)
        self._frame_width = self.source_channels * self._bytes_per_sample
        self._carry = b""
        self._sample_accumulator = 0
        self._rate_state = None
        self._gain_state = 1.0

    def _limit_peak(self, pcm_bytes: bytes, *, peak_limit: int = 30000) -> bytes:
        return _limit_pcm16le_peak(pcm_bytes, peak_limit=peak_limit)

    def _mono_weights(self) -> tuple[float, float]:
        if self.mono_mix in {"mix", "sum", "average", "center"}:
            return 0.5, 0.5
        if self.mono_mix in {"right", "r", "2"}:
            return 0.0, 1.0
        return 1.0, 0.0

    def _apply_phone_dsp(self, pcm_bytes: bytes) -> bytes:
        if not self.phone_dsp_enabled:
            return pcm_bytes
        if not pcm_bytes:
            return b""
        rms_db = _pcm16le_rms_db(pcm_bytes)
        if rms_db is None or rms_db <= TELEPHONY_DSP_NOISE_FLOOR_DB:
            self._gain_state = 1.0
            return pcm_bytes
        desired_gain_db = max(
            TELEPHONY_DSP_MIN_GAIN_DB,
            min(TELEPHONY_DSP_MAX_GAIN_DB, TELEPHONY_DSP_TARGET_RMS_DB - rms_db),
        )
        desired_gain = math.pow(10.0, desired_gain_db / 20.0)
        self._gain_state += (desired_gain - self._gain_state) * TELEPHONY_DSP_GAIN_SMOOTHING
        shaped = _apply_pcm16le_gain(pcm_bytes, self._gain_state)
        return self._limit_peak(shaped)

    def _apply_phone_processing(self, pcm_bytes: bytes) -> bytes:
        if not pcm_bytes:
            return b""
        shaped = pcm_bytes
        if abs(self.phone_gain_db) > 0.01:
            shaped = _apply_pcm16le_gain(shaped, math.pow(10.0, self.phone_gain_db / 20.0))
            shaped = self._limit_peak(shaped)
        return self._apply_phone_dsp(shaped)

    def transcode(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""

        data = self._carry + chunk
        usable = len(data) - (len(data) % self._frame_width)
        self._carry = data[usable:]
        if usable <= 0:
            return b""

        payload = data[:usable]

        if audioop is not None and self.source_channels in (1, 2) and self._bytes_per_sample in (1, 2, 3, 4):
            mono = payload
            if self.source_channels == 2:
                left_weight, right_weight = self._mono_weights()
                mono = audioop.tomono(payload, self._bytes_per_sample, left_weight, right_weight)
            resampled, self._rate_state = audioop.ratecv(
                mono,
                self._bytes_per_sample,
                1,
                self.source_rate_hz,
                self.output_rate_hz,
                self._rate_state,
            )
            if self._bytes_per_sample != 2:
                resampled = audioop.lin2lin(resampled, self._bytes_per_sample, 2)
            return self._apply_phone_processing(resampled)

        output = bytearray()
        for offset in range(0, usable, self._frame_width):
            frame = payload[offset:offset + self._frame_width]
            self._sample_accumulator += self.output_rate_hz
            if self._sample_accumulator < self.source_rate_hz:
                continue
            self._sample_accumulator -= self.source_rate_hz

            frame_samples = []
            for channel_index in range(self.source_channels):
                start = channel_index * self._bytes_per_sample
                stop = start + self._bytes_per_sample
                raw_sample = frame[start:stop]
                if self.bits_per_sample == 24:
                    sample_value = _pcm24le_to_int(raw_sample)
                elif self.bits_per_sample == 16:
                    sample_value = struct.unpack("<h", raw_sample)[0] << 8
                else:
                    continue
                frame_samples.append(sample_value)
            if not frame_samples:
                continue
            if self.source_channels >= 2:
                left_weight, right_weight = self._mono_weights()
                mixed = int((frame_samples[0] * left_weight) + (frame_samples[1] * right_weight))
            else:
                mixed = frame_samples[0]
            pcm16 = max(-32768, min(32767, mixed >> 8))
            output.extend(struct.pack("<h", pcm16))
        return self._apply_phone_processing(bytes(output))


class PcmStreamTranscoder:
    """Convert live PCM frames into a browser-safe PCM stream."""

    def __init__(
        self,
        *,
        source_channels: int,
        source_rate_hz: int,
        bits_per_sample: int,
        output_channels: int,
        output_rate_hz: int,
        output_bits_per_sample: int,
        gain_db: float = 0.0,
    ):
        self.source_channels = max(1, int(source_channels or 1))
        self.source_rate_hz = max(8000, int(source_rate_hz or 48000))
        self.bits_per_sample = max(16, int(bits_per_sample or 24))
        self.output_channels = max(1, int(output_channels or self.source_channels))
        self.output_rate_hz = max(8000, int(output_rate_hz or self.source_rate_hz))
        self.output_bits_per_sample = max(16, int(output_bits_per_sample or 16))
        self._source_width = max(1, self.bits_per_sample // 8)
        self._output_width = max(1, self.output_bits_per_sample // 8)
        self._frame_width = self.source_channels * self._source_width
        self._carry = b""
        self._rate_state = None
        self._sample_accumulator = 0
        self.gain_db = float(gain_db or 0.0)

    def _apply_output_processing(self, pcm_bytes: bytes) -> bytes:
        if not pcm_bytes:
            return b""
        if self.output_bits_per_sample != 16 or abs(self.gain_db) <= 0.01:
            return pcm_bytes
        shaped = _apply_pcm16le_gain(pcm_bytes, math.pow(10.0, self.gain_db / 20.0))
        return _limit_pcm16le_peak(shaped)

    def transcode(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""

        data = self._carry + chunk
        usable = len(data) - (len(data) % self._frame_width)
        self._carry = data[usable:]
        if usable <= 0:
            return b""

        payload = data[:usable]

        if (
            audioop is not None
            and self.source_channels in (1, 2)
            and self.output_channels in (1, 2)
            and self._source_width in (1, 2, 3, 4)
            and self._output_width in (1, 2, 3, 4)
        ):
            working = payload
            channel_count = self.source_channels
            if self.source_channels == 2 and self.output_channels == 1:
                working = audioop.tomono(working, self._source_width, 0.5, 0.5)
                channel_count = 1
            elif self.source_channels == 1 and self.output_channels == 2:
                working = audioop.tostereo(working, self._source_width, 1.0, 1.0)
                channel_count = 2
            if self.source_rate_hz != self.output_rate_hz:
                working, self._rate_state = audioop.ratecv(
                    working,
                    self._source_width,
                    channel_count,
                    self.source_rate_hz,
                    self.output_rate_hz,
                    self._rate_state,
                )
            if self._source_width != self._output_width:
                working = audioop.lin2lin(working, self._source_width, self._output_width)
            return self._apply_output_processing(working)

        output = bytearray()
        for offset in range(0, usable, self._frame_width):
            frame = payload[offset:offset + self._frame_width]
            self._sample_accumulator += self.output_rate_hz
            if self._sample_accumulator < self.source_rate_hz:
                continue
            self._sample_accumulator -= self.source_rate_hz

            frame_samples: list[int] = []
            for channel_index in range(self.source_channels):
                start = channel_index * self._source_width
                stop = start + self._source_width
                raw_sample = frame[start:stop]
                if self.bits_per_sample == 24:
                    sample_value = _pcm24le_to_int(raw_sample)
                elif self.bits_per_sample == 16:
                    sample_value = struct.unpack("<h", raw_sample)[0] << 8
                else:
                    continue
                frame_samples.append(sample_value)
            if not frame_samples:
                continue
            if self.source_channels == 2 and self.output_channels == 1:
                frame_samples = [int(sum(frame_samples) / len(frame_samples))]
            elif self.source_channels == 1 and self.output_channels == 2:
                frame_samples = [frame_samples[0], frame_samples[0]]

            for sample_value in frame_samples[: self.output_channels]:
                if self.output_bits_per_sample == 16:
                    scaled = max(-32768, min(32767, sample_value >> 8))
                    output.extend(struct.pack("<h", scaled))
                elif self.output_bits_per_sample == 24:
                    scaled = max(-(1 << 23), min((1 << 23) - 1, sample_value))
                    output.extend(int(scaled).to_bytes(3, "little", signed=True))
        return self._apply_output_processing(bytes(output))


@dataclass
class RoomState:
    """In-memory stream state for a single room."""

    active_host_slug: str | None = None
    active: bool = False
    started_at: str | None = None
    last_chunk_at: str | None = None
    bytes_received: int = 0
    listeners: dict[int, queue.Queue] = field(default_factory=dict)
    recent_chunks: deque[bytes] = field(default_factory=lambda: deque(maxlen=RECENT_CHUNK_BACKLOG))
    stream_channels: int = 1
    sample_rate_hz: int = 48000
    bits_per_sample: int = 24
    signal_level_db: float | None = None
    signal_peak_db: float | None = None
    signal_updated_at: str | None = None


@dataclass
class TelephonySessionState:
    session_id: str
    room_slug: str
    participant_label: str
    channel: str
    listener_id: int
    listener: queue.Queue
    buffered_chunks: tuple[bytes, ...]
    transcoder: TelephonyPcmTranscoder
    listener_session_id: int
    created_at: float
    last_access_at: float
    stream_ended: bool = False


@dataclass
class TelephonyClosureRecord:
    session_id: str
    room_slug: str
    participant_label: str
    channel: str
    reason: str
    occurred_at: float


@dataclass
class TelnyxStreamRuntime:
    stop_event: threading.Event = field(default_factory=threading.Event)
    sender_thread: threading.Thread | None = None
    stream_id: str = ""
    close_reason: str = ""
    rtp_sequence_number: int = field(default_factory=lambda: secrets.randbits(16))
    rtp_timestamp: int = field(default_factory=lambda: secrets.randbits(32))
    rtp_ssrc: int = field(default_factory=lambda: secrets.randbits(32))


@dataclass
class HlsStreamState:
    room_slug: str
    output_dir: str
    playlist_path: str
    stop_event: threading.Event = field(default_factory=threading.Event)
    ready_event: threading.Event = field(default_factory=threading.Event)
    worker_thread: threading.Thread | None = None
    process: subprocess.Popen | None = None
    listener_id: int | None = None
    last_request_at: float = field(default_factory=time.time)
    source_signature: tuple[int, int, int] = (2, 48000, 16)
    last_error: str = ""


@dataclass
class HlsClientSession:
    token: str
    room_slug: str
    listener_session_id: int
    last_seen_at: float = field(default_factory=time.time)


class RoomStreamHub:
    """Broadcast incoming byte streams to all active listeners in a room."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rooms: dict[str, RoomState] = {}

    def _get_room(self, room_slug: str) -> RoomState:
        if room_slug not in self._rooms:
            self._rooms[room_slug] = RoomState()
        return self._rooms[room_slug]

    def start_broadcast(
        self,
        room_slug: str,
        host_slug: str,
        *,
        stream_channels: int = 1,
        sample_rate_hz: int = 48000,
        bits_per_sample: int = 24,
    ):
        with self._lock:
            room = self._get_room(room_slug)
            room.active_host_slug = host_slug
            room.active = True
            room.started_at = datetime.now(timezone.utc).isoformat()
            room.last_chunk_at = None
            room.bytes_received = 0
            room.recent_chunks.clear()
            room.stream_channels = max(1, int(stream_channels or 1))
            room.sample_rate_hz = max(8000, int(sample_rate_hz or 48000))
            room.bits_per_sample = max(16, int(bits_per_sample or 24))
            room.signal_level_db = None
            room.signal_peak_db = None
            room.signal_updated_at = None

    def publish(self, room_slug: str, chunk: bytes):
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._lock:
            room = self._get_room(room_slug)
            room.last_chunk_at = now_iso
            room.bytes_received += len(chunk)
            room.recent_chunks.append(chunk)
            level_db, peak_db = _chunk_signal_stats(chunk, bits_per_sample=room.bits_per_sample)
            if level_db is not None:
                if room.signal_level_db is None:
                    room.signal_level_db = level_db
                else:
                    room.signal_level_db = (room.signal_level_db * 0.7) + (level_db * 0.3)
            if peak_db is not None:
                if room.signal_peak_db is None:
                    room.signal_peak_db = peak_db
                else:
                    room.signal_peak_db = (room.signal_peak_db * 0.55) + (peak_db * 0.45)
            if level_db is not None or peak_db is not None:
                room.signal_updated_at = now_iso
            listeners = list(room.listeners.values())

        for listener in listeners:
            try:
                listener.put_nowait(chunk)
            except queue.Full:
                while True:
                    try:
                        listener.get_nowait()
                    except queue.Empty:
                        break
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

    def open_listener(self, room_slug: str, *, maxsize: int = LISTENER_QUEUE_MAXSIZE, include_buffered: bool = True):
        listener = queue.Queue(maxsize=maxsize)
        listener_id = id(listener)
        with self._lock:
            room = self._get_room(room_slug)
            room.listeners[listener_id] = listener
            buffered_chunks = list(room.recent_chunks) if include_buffered else []
        return listener_id, listener, buffered_chunks

    def close_listener(self, room_slug: str, listener_id: int):
        with self._lock:
            room = self._get_room(room_slug)
            room.listeners.pop(listener_id, None)

    def listen(self, room_slug: str):
        listener_id, listener, buffered_chunks = self.open_listener(room_slug, maxsize=LISTENER_QUEUE_MAXSIZE)

        try:
            if buffered_chunks:
                yield b"".join(buffered_chunks)
            while True:
                try:
                    chunk = listener.get(timeout=25)
                except queue.Empty:
                    break

                if chunk is None:
                    break
                yield chunk
        finally:
            self.close_listener(room_slug, listener_id)

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
                "stream_channels": room.stream_channels,
                "sample_rate_hz": room.sample_rate_hz,
                "bits_per_sample": room.bits_per_sample,
                "signal_level_db": room.signal_level_db,
                "signal_peak_db": room.signal_peak_db,
                "signal_updated_at": room.signal_updated_at,
                "signal_level_percent": _signal_percent_from_db(room.signal_level_db),
                "signal_peak_percent": _signal_percent_from_db(room.signal_peak_db),
            }


def create_app(test_config: dict | None = None, *, store: RoomCastStore | None = None, hub: RoomStreamHub | None = None):
    """Build the Flask app so tests can inject isolated state."""

    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("ROOMCAST_SECRET_KEY", "roomcast-dev-secret"),
        ROOMCAST_DB_PATH=os.getenv("ROOMCAST_DB_PATH"),
        ROOMCAST_PUBLIC_NAME=os.getenv("ROOMCAST_PUBLIC_NAME", "NTC Newark WebCall"),
        ROOMCAST_LISTENER_NAME=os.getenv("ROOMCAST_LISTENER_NAME", "NTC Newark WebCall"),
        ROOMCAST_CALL_IN_NUMBER=os.getenv("ROOMCAST_CALL_IN_NUMBER", "+1 862 872 7904"),
        ROOMCAST_CALL_IN_PIN=os.getenv("ROOMCAST_CALL_IN_PIN", os.getenv("ROOMCAST_DEFAULT_PIN", "7070")),
        ROOMCAST_AGENT_PUBLIC_BASE_URL=os.getenv("ROOMCAST_AGENT_PUBLIC_BASE_URL", "https://ntcnas.myftp.org/webcall"),
        ROOMCAST_TELEPHONY_PUBLIC_BASE_URL=os.getenv("ROOMCAST_TELEPHONY_PUBLIC_BASE_URL", "https://ntcnas.myftp.org/webcall"),
        ROOMCAST_TELEPHONY_VOICE=os.getenv("ROOMCAST_TELEPHONY_VOICE", "Telnyx.NaturalHD.astra"),
        ROOMCAST_TELEPHONY_SEGMENT_SECONDS=float(os.getenv("ROOMCAST_TELEPHONY_SEGMENT_SECONDS", "2.5")),
        ROOMCAST_TELEPHONY_SEGMENT_TIMEOUT_SECONDS=float(os.getenv("ROOMCAST_TELEPHONY_SEGMENT_TIMEOUT_SECONDS", "3.0")),
        ROOMCAST_TELEPHONY_SESSION_TTL_SECONDS=float(os.getenv("ROOMCAST_TELEPHONY_SESSION_TTL_SECONDS", "120")),
        ROOMCAST_TELEPHONY_CLOSURE_TTL_SECONDS=float(os.getenv("ROOMCAST_TELEPHONY_CLOSURE_TTL_SECONDS", "300")),
        ROOMCAST_TELEPHONY_MONO_MIX=os.getenv("ROOMCAST_TELEPHONY_MONO_MIX", "left"),
        ROOMCAST_TELEPHONY_DSP_ENABLED=os.getenv("ROOMCAST_TELEPHONY_DSP_ENABLED", "0"),
        ROOMCAST_TELEPHONY_GAIN_DB=float(os.getenv("ROOMCAST_TELEPHONY_GAIN_DB", "0")),
        ROOMCAST_TELEPHONY_PROMPT_DIR=os.getenv("ROOMCAST_TELEPHONY_PROMPT_DIR", "/app/data/prompts"),
        ROOMCAST_TWILIO_VERIFY_CODE=os.getenv("ROOMCAST_TWILIO_VERIFY_CODE", ""),
        ROOMCAST_TWILIO_VERIFY_CALLER_ID=os.getenv("ROOMCAST_TWILIO_VERIFY_CALLER_ID", "+14157234000"),
        ROOMCAST_DIAGNOSTIC_AUDIO_DIR=os.getenv("ROOMCAST_DIAGNOSTIC_AUDIO_DIR", "/app/data/diagnostic-audio"),
        ROOMCAST_TELNYX_TRANSPORT=os.getenv("ROOMCAST_TELNYX_TRANSPORT", "websocket"),
        ROOMCAST_TELNYX_STREAM_CODEC=os.getenv("ROOMCAST_TELNYX_STREAM_CODEC", "G722"),
        ROOMCAST_TELNYX_STREAM_SAMPLE_RATE=int(os.getenv("ROOMCAST_TELNYX_STREAM_SAMPLE_RATE", "8000")),
        ROOMCAST_TELNYX_STREAM_FRAME_MS=int(os.getenv("ROOMCAST_TELNYX_STREAM_FRAME_MS", "60")),
        ROOMCAST_TELNYX_RTP_HEADER_ENABLED=os.getenv("ROOMCAST_TELNYX_RTP_HEADER_ENABLED", "0"),
        ROOMCAST_TELNYX_STREAM_RECONNECT_ENABLED=os.getenv("ROOMCAST_TELNYX_STREAM_RECONNECT_ENABLED", "1"),
        ROOMCAST_TELNYX_OPUS_APPLICATION=os.getenv("ROOMCAST_TELNYX_OPUS_APPLICATION", "audio"),
        ROOMCAST_TELNYX_OPUS_BITRATE=int(os.getenv("ROOMCAST_TELNYX_OPUS_BITRATE", "48000")),
        ROOMCAST_TELNYX_OPUS_SIGNAL=os.getenv("ROOMCAST_TELNYX_OPUS_SIGNAL", "music"),
        ROOMCAST_TELNYX_TEST_SIGNAL=os.getenv("ROOMCAST_TELNYX_TEST_SIGNAL", ""),
        ROOMCAST_TELNYX_TEST_SIGNAL_HZ=float(os.getenv("ROOMCAST_TELNYX_TEST_SIGNAL_HZ", "440")),
        ROOMCAST_TELNYX_TEST_SIGNAL_GAIN=float(os.getenv("ROOMCAST_TELNYX_TEST_SIGNAL_GAIN", "0.18")),
        ROOMCAST_TELNYX_DEBUG_TAP_ENABLED=os.getenv("ROOMCAST_TELNYX_DEBUG_TAP_ENABLED", "0"),
        ROOMCAST_TELNYX_DEBUG_TAP_DIR=os.getenv("ROOMCAST_TELNYX_DEBUG_TAP_DIR", "/app/data/telnyx-debug-taps"),
        ROOMCAST_TELNYX_DEBUG_TAP_MAX_SECONDS=float(os.getenv("ROOMCAST_TELNYX_DEBUG_TAP_MAX_SECONDS", "300")),
        ROOMCAST_TELNYX_SMS_LOG_PATH=os.getenv("ROOMCAST_TELNYX_SMS_LOG_PATH", "/app/data/telnyx-sms-webhooks.jsonl"),
        ROOMCAST_STREAM_PROFILE=os.getenv("ROOMCAST_STREAM_PROFILE", "mp3"),
        ROOMCAST_BROWSER_GAIN_DB=float(os.getenv("ROOMCAST_BROWSER_GAIN_DB", "0")),
        ROOMCAST_HLS_ENABLED=os.getenv("ROOMCAST_HLS_ENABLED", "1"),
        ROOMCAST_HLS_SEGMENT_SECONDS=float(os.getenv("ROOMCAST_HLS_SEGMENT_SECONDS", "1.0")),
        ROOMCAST_HLS_PLAYLIST_LENGTH=int(os.getenv("ROOMCAST_HLS_PLAYLIST_LENGTH", "6")),
        ROOMCAST_HLS_DELETE_THRESHOLD=int(os.getenv("ROOMCAST_HLS_DELETE_THRESHOLD", "12")),
        ROOMCAST_HLS_AUDIO_BITRATE=os.getenv("ROOMCAST_HLS_AUDIO_BITRATE", "256k"),
        ROOMCAST_HLS_START_TIMEOUT_SECONDS=float(os.getenv("ROOMCAST_HLS_START_TIMEOUT_SECONDS", "8.0")),
        ROOMCAST_HLS_IDLE_TIMEOUT_SECONDS=float(os.getenv("ROOMCAST_HLS_IDLE_TIMEOUT_SECONDS", str(HLS_IDLE_TIMEOUT_SECONDS))),
        ROOMCAST_HLS_FFMPEG_PATH=os.getenv("ROOMCAST_HLS_FFMPEG_PATH", ""),
        ROOMCAST_ADMIN_PASSWORD=os.getenv("ROOMCAST_ADMIN_PASSWORD", ""),
        ROOMCAST_TELEPHONY_SECRET=os.getenv("ROOMCAST_TELEPHONY_SECRET", ""),
        ROOMCAST_TWILIO_WEBHOOK_TOKEN=os.getenv("ROOMCAST_TWILIO_WEBHOOK_TOKEN", ""),
        ROOMCAST_TELNYX_WEBHOOK_TOKEN=os.getenv("ROOMCAST_TELNYX_WEBHOOK_TOKEN", ""),
        ROOMCAST_GEOLOOKUP_URL=os.getenv("ROOMCAST_GEOLOOKUP_URL", "https://ipwho.is/{ip}"),
    )
    if test_config:
        app.config.update(test_config)

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = True

    roomcast_store = store or RoomCastStore(app.config.get("ROOMCAST_DB_PATH"))
    stream_hub = hub or RoomStreamHub()
    sock = Sock(app)
    roomcast_store.close_orphaned_listener_sessions()
    app.roomcast_store = roomcast_store
    app.stream_hub = stream_hub
    geolookup_cache: dict[str, tuple[float, str | None]] = {}
    telephony_sessions: dict[str, TelephonySessionState] = {}
    telephony_recent_closures: dict[str, TelephonyClosureRecord] = {}
    telnyx_stream_runtimes: dict[str, TelnyxStreamRuntime] = {}
    hls_streams: dict[str, HlsStreamState] = {}
    hls_clients: dict[str, HlsClientSession] = {}
    hls_lock = threading.Lock()
    app.telephony_sessions = telephony_sessions
    app.telephony_recent_closures = telephony_recent_closures
    app.telnyx_stream_runtimes = telnyx_stream_runtimes
    app.logger.setLevel(logging.INFO)

    def _project_name() -> str:
        return app.config["ROOMCAST_PUBLIC_NAME"]

    def _listener_name() -> str:
        return app.config["ROOMCAST_LISTENER_NAME"]

    def _agent_public_base_url() -> str:
        return (app.config.get("ROOMCAST_AGENT_PUBLIC_BASE_URL") or request.host_url).rstrip("/")

    def _agent_setup(host: dict) -> dict:
        token = (host.get("heartbeat_token") or "").strip()
        host_slug = host["slug"]
        server_url = _agent_public_base_url()
        windows_command = "\n".join(
            [
                ".\\install_roomcast_agent_task.ps1 `",
                f'  -ServerUrl "{server_url}" `',
                f'  -HostSlug "{host_slug}" `',
                f'  -Token "{token}" `',
                '  -TaskName "WebCall Source Agent" `',
                "  -PollIntervalSeconds 3",
            ]
        )
        macos_command = "\n".join(
            [
                "chmod +x ./install_roomcast_agent_launchd.sh",
                "./install_roomcast_agent_launchd.sh \\",
                f'  --server-url "{server_url}" \\',
                f'  --host-slug "{host_slug}" \\',
                f'  --token "{token}" \\',
                "  --poll-interval 3",
            ]
        )
        return {
            "server_url": server_url,
            "host_slug": host_slug,
            "token": token,
            "windows_command": windows_command,
            "macos_command": macos_command,
        }

    def _call_in_info(*, include_pin: bool = True) -> dict | None:
        number = (app.config.get("ROOMCAST_CALL_IN_NUMBER") or "").strip()
        if not number:
            return None
        digits = re.sub(r"[^\d+]", "", number)
        pin = (app.config.get("ROOMCAST_CALL_IN_PIN") or "").strip() if include_pin else ""
        pin_digits = re.sub(r"\D", "", pin)
        dial_sequence = f"{digits},,{pin_digits}%23" if digits and pin_digits else digits
        return {
            "number": number,
            "tel_href": f"tel:{dial_sequence}" if dial_sequence else "",
            "pin": pin,
        }

    def _config_enabled(name: str, *, default: bool = False) -> bool:
        raw_value = app.config.get(name)
        if raw_value is None:
            return default
        if isinstance(raw_value, bool):
            return raw_value
        return str(raw_value).strip().lower() not in {"", "0", "false", "no", "off"}

    def _ffmpeg_path() -> str:
        configured = (app.config.get("ROOMCAST_HLS_FFMPEG_PATH") or "").strip()
        if configured:
            return configured
        return shutil.which("ffmpeg") or ""

    def _telnyx_debug_tap_paths(session_state: TelephonySessionState) -> tuple[Path, Path]:
        tap_dir = Path(app.config.get("ROOMCAST_TELNYX_DEBUG_TAP_DIR") or "/app/data/telnyx-debug-taps")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_session_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_state.session_id)
        base_name = f"telnyx-send-{timestamp}-{safe_session_id}"
        return tap_dir / f"{base_name}.wav", tap_dir / f"{base_name}.json"

    def _open_telnyx_debug_tap(session_state: TelephonySessionState, *, sample_rate: int, codec: str):
        if not _config_enabled("ROOMCAST_TELNYX_DEBUG_TAP_ENABLED", default=False):
            return None, None, 0
        wav_path, metadata_path = _telnyx_debug_tap_paths(session_state)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        writer = wave.open(str(wav_path), "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        max_seconds = max(1.0, float(app.config.get("ROOMCAST_TELNYX_DEBUG_TAP_MAX_SECONDS", 300)))
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "session_id": session_state.session_id,
            "room_slug": session_state.room_slug,
            "participant_label": session_state.participant_label,
            "codec": codec,
            "sample_rate_hz": sample_rate,
            "channels": 1,
            "bits_per_sample": 16,
            "source": "pre-encode PCM sent to Telnyx stream",
            "max_seconds": max_seconds,
        }
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        _telephony_log("telnyx-debug-tap-start", session_id=session_state.session_id, path=wav_path)
        return writer, metadata_path, int(max_seconds * sample_rate)

    def _close_telnyx_debug_tap(writer, metadata_path: Path | None, *, frames_written: int, close_reason: str):
        if not writer:
            return
        try:
            writer.close()
        finally:
            if metadata_path:
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metadata = {}
                metadata.update(
                    {
                        "closed_at": datetime.now(timezone.utc).isoformat(),
                        "frames_written": frames_written,
                        "duration_seconds": frames_written / max(1, int(metadata.get("sample_rate_hz") or 1)),
                        "close_reason": close_reason or "",
                    }
                )
                metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    def _hls_transport_available() -> bool:
        return _config_enabled("ROOMCAST_HLS_ENABLED", default=True) and bool(_ffmpeg_path())

    def _request_forces_direct_stream() -> bool:
        transport = (request.args.get("transport") or request.args.get("stream") or "").strip().lower()
        return transport in {"direct", "wav", "pcm"}

    def _request_prefers_hls() -> bool:
        if not _hls_transport_available():
            return False
        if _request_forces_direct_stream():
            return False
        return True

    def _telephony_log(stage: str, **fields):
        details = []
        for key, value in fields.items():
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            details.append(f"{key}={text}")
        suffix = " " + " ".join(details) if details else ""
        app.logger.info("telephony[%s]%s", stage, suffix)

    def _audit_log(
        component: str,
        event_type: str,
        message: str,
        *,
        level: str = "info",
        room_slug: str | None = None,
        host_slug: str | None = None,
        listener_session_id: int | None = None,
        meeting_session_id: int | None = None,
        **details,
    ):
        normalized_level = (level or "info").lower()
        log_method = getattr(app.logger, normalized_level, app.logger.info)
        suffix_parts = []
        for key, value in details.items():
            if value is None:
                continue
            text = str(value).strip()
            if text:
                suffix_parts.append(f"{key}={text}")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        log_method("%s[%s] %s%s", component, event_type, message, suffix)
        roomcast_store.record_event(
            component=component,
            event_type=event_type,
            message=message,
            level=normalized_level,
            room_slug=room_slug,
            host_slug=host_slug,
            listener_session_id=listener_session_id,
            meeting_session_id=meeting_session_id,
            details=details or None,
        )

    def _telephony_segment_target_bytes() -> int:
        seconds = max(1.0, float(app.config.get("ROOMCAST_TELEPHONY_SEGMENT_SECONDS", 2.5)))
        bytes_per_second = (
            TELEPHONY_STREAM_PROFILE["sample_rate_hz"]
            * TELEPHONY_STREAM_PROFILE["channels"]
            * (TELEPHONY_STREAM_PROFILE["bits_per_sample"] // 8)
        )
        return int(seconds * bytes_per_second)

    def _cleanup_recent_telephony_closures():
        expiry_seconds = max(30.0, float(app.config.get("ROOMCAST_TELEPHONY_CLOSURE_TTL_SECONDS", 300.0)))
        cutoff = time.time() - expiry_seconds
        expired = [
            session_id
            for session_id, closure in telephony_recent_closures.items()
            if closure.occurred_at < cutoff
        ]
        for session_id in expired:
            telephony_recent_closures.pop(session_id, None)

    def _recent_telephony_closure(session_id: str) -> TelephonyClosureRecord | None:
        _cleanup_recent_telephony_closures()
        return telephony_recent_closures.get(session_id)

    def _record_telephony_closure(session_state: TelephonySessionState, *, reason: str):
        if not reason:
            return
        telephony_recent_closures[session_state.session_id] = TelephonyClosureRecord(
            session_id=session_state.session_id,
            room_slug=session_state.room_slug,
            participant_label=session_state.participant_label,
            channel=session_state.channel,
            reason=reason,
            occurred_at=time.time(),
        )
        _cleanup_recent_telephony_closures()

    def _close_telephony_session(session_id: str, *, reason: str = ""):
        runtime = telnyx_stream_runtimes.pop(session_id, None)
        if runtime:
            runtime.stop_event.set()
        session_state = telephony_sessions.pop(session_id, None)
        if not session_state:
            return
        _record_telephony_closure(session_state, reason=reason or "session-closed")
        stream_hub.close_listener(session_state.room_slug, session_state.listener_id)
        roomcast_store.end_listener_session(session_state.listener_session_id)
        _telephony_log("session-close", session_id=session_id, room_slug=session_state.room_slug, reason=reason)

    def _cleanup_telephony_sessions():
        expiry_seconds = max(30.0, float(app.config.get("ROOMCAST_TELEPHONY_SESSION_TTL_SECONDS", 120.0)))
        cutoff = time.time() - expiry_seconds
        expired = [
            session_id
            for session_id, session_state in telephony_sessions.items()
            if session_state.last_access_at < cutoff
        ]
        for session_id in expired:
            _close_telephony_session(session_id, reason="expired")

    def _create_telephony_session(room_slug: str, *, participant_label: str, channel: str = "phone") -> TelephonySessionState:
        _cleanup_telephony_sessions()
        session_id = secrets.token_urlsafe(18)
        participant_key = f"{channel}:{participant_label}:{session_id}"
        listener_session_id = roomcast_store.begin_listener_session(
            room_slug,
            channel=channel,
            participant_label=participant_label,
            participant_key=participant_key,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        listener_id, listener, buffered_chunks = stream_hub.open_listener(
            room_slug,
            maxsize=TELEPHONY_LISTENER_QUEUE_MAXSIZE,
            include_buffered=True,
        )
        snapshot = _room_snapshot(room_slug)
        descriptor = _stream_descriptor(snapshot)
        session_state = TelephonySessionState(
            session_id=session_id,
            room_slug=room_slug,
            participant_label=participant_label,
            channel=channel,
            listener_id=listener_id,
            listener=listener,
            buffered_chunks=tuple(buffered_chunks),
            transcoder=TelephonyPcmTranscoder(
                source_channels=descriptor.get("channels", 1),
                source_rate_hz=descriptor.get("sample_rate_hz", 48000),
                bits_per_sample=descriptor.get("bits_per_sample", 24),
                output_rate_hz=TELEPHONY_STREAM_PROFILE["sample_rate_hz"],
                mono_mix=app.config.get("ROOMCAST_TELEPHONY_MONO_MIX", "left"),
            ),
            listener_session_id=listener_session_id,
            created_at=time.time(),
            last_access_at=time.time(),
        )
        telephony_sessions[session_id] = session_state
        telnyx_stream_runtimes[session_id] = TelnyxStreamRuntime()
        _telephony_log("session-open", session_id=session_id, room_slug=room_slug, label=participant_label, channel=channel)
        return session_state

    def _telephony_session_segment_url(session_id: str, *, sequence: int | None = None) -> str:
        cache_buster = sequence if sequence is not None else int(time.time() * 1000)
        return _telephony_public_url("telephony_session_segment", session_id=session_id, seq=cache_buster)

    def _telephony_websocket_url(endpoint: str, **values) -> str:
        url = _telephony_public_url(endpoint, **values)
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):]
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):]
        return url

    def _telnyx_stream_transport_enabled() -> bool:
        return (app.config.get("ROOMCAST_TELNYX_TRANSPORT") or "").strip().lower() == "websocket"

    def _telnyx_stream_reconnect_enabled() -> bool:
        return _config_enabled("ROOMCAST_TELNYX_STREAM_RECONNECT_ENABLED", default=True)

    def _telnyx_stream_codec() -> str:
        codec = (app.config.get("ROOMCAST_TELNYX_STREAM_CODEC") or "G722").strip().upper()
        # Only advertise codecs this TeXML bridge can actually encode. Sending
        # PCMU bytes under an unsupported codec label sounds like severe static.
        if codec in {"PCMU", "PCMA", "G722", "OPUS", "L16"}:
            return codec
        app.logger.warning("unsupported Telnyx stream codec %s; falling back to G722", codec)
        return "G722"

    def _telnyx_stream_advertised_sample_rate() -> int:
        codec = _telnyx_stream_codec()
        configured = max(8000, int(app.config.get("ROOMCAST_TELNYX_STREAM_SAMPLE_RATE", 8000)))
        if codec == "G722":
            # Telnyx's bidirectional RTP docs list G.722 as an 8 kHz stream.
            # The encoder still consumes 16 kHz wideband PCM internally.
            return 8000
        if codec in {"PCMU", "PCMA"}:
            return 8000
        if codec == "OPUS":
            return 16000 if configured >= 16000 else 8000
        if codec == "L16":
            return 16000
        return configured

    def _telnyx_stream_pcm_sample_rate() -> int:
        if _telnyx_stream_codec() == "G722":
            return 16000
        return _telnyx_stream_advertised_sample_rate()

    def _telnyx_stream_frame_bytes() -> int:
        frame_ms = max(20, int(app.config.get("ROOMCAST_TELNYX_STREAM_FRAME_MS", 20)))
        sample_rate = _telnyx_stream_pcm_sample_rate()
        return int(sample_rate * frame_ms / 1000) * 2

    def _telnyx_stream_payload_from_pcm(pcm_frame: bytes, *, g722_encoder=None, opus_encoder=None) -> bytes:
        codec = _telnyx_stream_codec()
        if codec == "G722":
            if g722_encoder is None:
                raise RuntimeError("G722 codec selected but encoder is not available")
            return _linear16le_to_g722(pcm_frame, g722_encoder)
        if codec == "OPUS":
            if opus_encoder is None:
                raise RuntimeError("OPUS codec selected but encoder is not available")
            return _linear16le_to_opus(pcm_frame, opus_encoder)
        if codec == "L16":
            return _linear16le_to_l16be(pcm_frame)
        if codec == "PCMA":
            return _linear16le_to_pcma(pcm_frame)
        if codec == "PCMU":
            return _linear16le_to_pcmu(pcm_frame)
        raise RuntimeError(f"unsupported Telnyx stream codec: {codec}")

    def _telnyx_stream_media_payload(codec_payload: bytes, runtime: TelnyxStreamRuntime, *, codec: str, sample_rate: int) -> bytes:
        if not _config_enabled("ROOMCAST_TELNYX_RTP_HEADER_ENABLED", default=False):
            return codec_payload
        return _rtp_packet(
            codec_payload,
            payload_type=_telnyx_rtp_payload_type(codec),
            sequence_number=runtime.rtp_sequence_number,
            timestamp=runtime.rtp_timestamp,
            ssrc=runtime.rtp_ssrc,
        )

    def _collect_telephony_segment(session_state: TelephonySessionState) -> tuple[bytes, bool]:
        session_state.last_access_at = time.time()
        target_bytes = _telephony_segment_target_bytes()
        timeout_seconds = max(
            0.5,
            float(app.config.get("ROOMCAST_TELEPHONY_SEGMENT_TIMEOUT_SECONDS", 3.0)),
        )
        deadline = time.time() + timeout_seconds
        pcm_payload = bytearray()
        stream_closed = False
        while len(pcm_payload) < target_bytes and time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            try:
                chunk = session_state.listener.get(timeout=remaining)
            except queue.Empty:
                break
            if chunk is None:
                stream_closed = True
                break
            converted = session_state.transcoder.transcode(chunk)
            if converted:
                pcm_payload.extend(converted)
        if len(pcm_payload) < target_bytes:
            pcm_payload.extend(b"\x00" * (target_bytes - len(pcm_payload)))
        payload = bytes(pcm_payload[:target_bytes])
        wav_bytes = _wav_file_bytes(
            channels=TELEPHONY_STREAM_PROFILE["channels"],
            sample_rate_hz=TELEPHONY_STREAM_PROFILE["sample_rate_hz"],
            bits_per_sample=TELEPHONY_STREAM_PROFILE["bits_per_sample"],
            payload=payload,
        )
        return wav_bytes, stream_closed

    def _stream_profile_name() -> str:
        configured = (app.config.get("ROOMCAST_STREAM_PROFILE") or "mp3").strip().lower()
        return configured if configured in STREAM_PROFILES else "mp3"

    def _stream_profile():
        return STREAM_PROFILES[_stream_profile_name()]

    def _stream_descriptor(snapshot: dict | None = None):
        profile_name = _stream_profile_name()
        descriptor = dict(_stream_profile())
        runtime = (snapshot or {}).get("runtime") or {}
        runtime_profile = (runtime.get("stream_profile") or "").strip().lower()
        if runtime_profile in STREAM_PROFILES:
            profile_name = runtime_profile
            descriptor = dict(STREAM_PROFILES[profile_name])
        if profile_name == "wav_pcm24":
            default_channels = descriptor.get("channels", 1)
            if (snapshot or {}).get("capture_mode") == "stereo":
                default_channels = max(2, int(default_channels or 1))
            descriptor["channels"] = max(1, int(runtime.get("stream_channels") or default_channels))
            descriptor["sample_rate_hz"] = max(8000, int(runtime.get("sample_rate_hz") or descriptor.get("sample_rate_hz", 48000)))
            descriptor["bits_per_sample"] = max(16, int(runtime.get("sample_bits") or descriptor.get("bits_per_sample", 24)))
        return descriptor

    def _browser_stream_descriptor(snapshot: dict | None = None):
        source_descriptor = _stream_descriptor(snapshot)
        descriptor = dict(BROWSER_STREAM_PROFILE)
        descriptor["channels"] = max(1, min(2, int(source_descriptor.get("channels") or descriptor["channels"])))
        source_rate_hz = max(8000, int(source_descriptor.get("sample_rate_hz") or descriptor["sample_rate_hz"]))
        descriptor["sample_rate_hz"] = min(descriptor["sample_rate_hz"], source_rate_hz)
        return descriptor

    def _new_hls_client_token() -> str:
        return secrets.token_urlsafe(12)

    def _close_hls_client(token: str):
        session_state = hls_clients.pop(token, None)
        if not session_state:
            return
        roomcast_store.end_listener_session(session_state.listener_session_id)

    def _cleanup_hls_clients():
        cutoff = time.time() - HLS_CLIENT_TTL_SECONDS
        expired = [
            token
            for token, session_state in hls_clients.items()
            if session_state.last_seen_at < cutoff
        ]
        for token in expired:
            _close_hls_client(token)

    def _touch_hls_client(room_slug: str, client_token: str):
        if not client_token:
            return
        _cleanup_hls_clients()
        session_state = hls_clients.get(client_token)
        if session_state and session_state.room_slug == room_slug:
            session_state.last_seen_at = time.time()
            return
        if session_state:
            _close_hls_client(client_token)
        participant_label = session.get("listener_name") or f"Web {request.headers.get('X-Forwarded-For', request.remote_addr or 'listener')}"
        participant_key = f"hls:{client_token}"
        listener_session_id = roomcast_store.begin_listener_session(
            room_slug,
            channel="web",
            participant_label=participant_label,
            participant_key=participant_key,
            ip_address=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        hls_clients[client_token] = HlsClientSession(
            token=client_token,
            room_slug=room_slug,
            listener_session_id=listener_session_id,
        )

    def _close_hls_stream(room_slug: str, *, reason: str = ""):
        with hls_lock:
            state = hls_streams.pop(room_slug, None)
        if not state:
            return
        state.stop_event.set()
        if state.process and state.process.poll() is None:
            try:
                if state.process.stdin:
                    state.process.stdin.close()
            except Exception:
                pass
            try:
                state.process.terminate()
            except Exception:
                pass
        if state.worker_thread and state.worker_thread.is_alive():
            state.worker_thread.join(timeout=2.0)
        if state.listener_id is not None:
            stream_hub.close_listener(room_slug, state.listener_id)
        try:
            for child in Path(state.output_dir).glob("*"):
                try:
                    child.unlink()
                except FileNotFoundError:
                    continue
            Path(state.output_dir).rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if reason:
            app.logger.info("hls[close] room=%s reason=%s", room_slug, reason)

    def _cleanup_hls_streams():
        cutoff = time.time() - max(15.0, float(app.config.get("ROOMCAST_HLS_IDLE_TIMEOUT_SECONDS", HLS_IDLE_TIMEOUT_SECONDS)))
        stale_rooms = []
        if not hls_lock.acquire(timeout=0.25):
            return
        try:
            active_streams = list(hls_streams.items())
        finally:
            hls_lock.release()
        for room_slug, state in active_streams:
            room_snapshot = _room_snapshot(room_slug)
            if state.last_request_at < cutoff or not _snapshot_should_hold_stream(room_snapshot):
                stale_rooms.append(room_slug)
        for room_slug in stale_rooms:
            _close_hls_stream(room_slug, reason="idle")

    def _hls_runtime_status(room_slug: str, *, broadcasting: bool = False, desired_active: bool = False, is_ingesting: bool = False) -> dict:
        if not _hls_transport_available():
            return {
                "enabled": False,
                "active": False,
                "ready": False,
                "client_count": 0,
                "segment_count": 0,
                "last_error": "",
                "quality_percent": 65 if broadcasting else 0,
                "quality_label": "Direct stream" if broadcasting else "Idle",
                "quality_state": "warn" if broadcasting else "idle",
                "quality_detail": "Direct WAV fallback is available.",
            }

        now = time.time()
        if not hls_lock.acquire(timeout=0.05):
            return {
                "enabled": True,
                "active": False,
                "ready": False,
                "client_count": 0,
                "segment_count": 0,
                "last_error": "HLS status is busy.",
                "quality_percent": 45 if broadcasting or is_ingesting or desired_active else 0,
                "quality_label": "Checking stream",
                "quality_state": "warn",
                "quality_detail": "The stream worker is busy; page loading is not blocked.",
            }
        try:
            state = hls_streams.get(room_slug)
            client_count = sum(
                1
                for session_state in hls_clients.values()
                if session_state.room_slug == room_slug and session_state.last_seen_at >= now - HLS_CLIENT_TTL_SECONDS
            )
        finally:
            hls_lock.release()

        ready = False
        process_active = False
        segment_count = 0
        playlist_age_seconds = None
        last_error = ""
        if state:
            ready = state.ready_event.is_set()
            process_active = bool(state.process and state.process.poll() is None)
            last_error = state.last_error
            try:
                playlist = Path(state.playlist_path)
                if playlist.exists():
                    playlist_age_seconds = max(0.0, now - playlist.stat().st_mtime)
                segment_count = len(list(Path(state.output_dir).glob("segment-*.ts")))
            except OSError:
                segment_count = 0

        active = bool(state and ready and process_active and segment_count > 0)
        if active:
            quality_percent = 100
            quality_label = "HLS live"
            quality_state = "good"
            quality_detail = f"{client_count} web listener{'s' if client_count != 1 else ''}; {segment_count} buffered segment{'s' if segment_count != 1 else ''}."
        elif broadcasting or is_ingesting:
            quality_percent = 78
            quality_label = "Ready on demand"
            quality_state = "good"
            quality_detail = "AAC/HLS will start when a web listener connects."
        elif desired_active:
            quality_percent = 38
            quality_label = "Waiting for source"
            quality_state = "warn"
            quality_detail = "Room is active, but source audio is not stable yet."
        else:
            quality_percent = 0
            quality_label = "Idle"
            quality_state = "idle"
            quality_detail = "No active web stream."

        if last_error:
            quality_percent = min(quality_percent, 35)
            quality_label = "Stream warning"
            quality_state = "warn"
            quality_detail = last_error

        return {
            "enabled": True,
            "active": active,
            "ready": ready,
            "client_count": client_count,
            "segment_count": segment_count,
            "playlist_age_seconds": playlist_age_seconds,
            "last_error": last_error,
            "quality_percent": quality_percent,
            "quality_label": quality_label,
            "quality_state": quality_state,
            "quality_detail": quality_detail,
        }

    def _hls_source_signature(snapshot: dict | None) -> tuple[int, int, int]:
        descriptor = _stream_descriptor(snapshot)
        return (
            int(descriptor.get("channels") or 1),
            int(descriptor.get("sample_rate_hz") or 48000),
            int(descriptor.get("bits_per_sample") or 24),
        )

    def _start_hls_stream(room_slug: str, snapshot: dict) -> HlsStreamState:
        output_dir = tempfile.mkdtemp(prefix=f"roomcast-hls-{room_slug}-")
        playlist_path = os.path.join(output_dir, "index.m3u8")
        state = HlsStreamState(
            room_slug=room_slug,
            output_dir=output_dir,
            playlist_path=playlist_path,
            source_signature=_hls_source_signature(snapshot),
        )
        source_descriptor = _stream_descriptor(snapshot)
        target_descriptor = dict(BROWSER_STREAM_PROFILE)
        transcoder = None
        browser_gain_db = float(app.config.get("ROOMCAST_BROWSER_GAIN_DB", 0.0))
        if (
            int(source_descriptor.get("channels") or 1) != target_descriptor["channels"]
            or int(source_descriptor.get("sample_rate_hz") or 48000) != target_descriptor["sample_rate_hz"]
            or int(source_descriptor.get("bits_per_sample") or 24) != target_descriptor["bits_per_sample"]
            or abs(browser_gain_db) > 0.01
        ):
            transcoder = PcmStreamTranscoder(
                source_channels=int(source_descriptor.get("channels") or 1),
                source_rate_hz=int(source_descriptor.get("sample_rate_hz") or 48000),
                bits_per_sample=int(source_descriptor.get("bits_per_sample") or 24),
                output_channels=target_descriptor["channels"],
                output_rate_hz=target_descriptor["sample_rate_hz"],
                output_bits_per_sample=target_descriptor["bits_per_sample"],
                gain_db=browser_gain_db,
            )

        ffmpeg_cmd = [
            _ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-fflags",
            "+genpts",
            "-f",
            "s16le",
            "-ar",
            str(target_descriptor["sample_rate_hz"]),
            "-ac",
            str(target_descriptor["channels"]),
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            str(app.config.get("ROOMCAST_HLS_AUDIO_BITRATE", "256k")),
            "-ar",
            str(target_descriptor["sample_rate_hz"]),
            "-ac",
            str(target_descriptor["channels"]),
            "-f",
            "hls",
            "-hls_time",
            str(max(0.5, float(app.config.get("ROOMCAST_HLS_SEGMENT_SECONDS", 1.0)))),
            "-hls_list_size",
            str(max(3, int(app.config.get("ROOMCAST_HLS_PLAYLIST_LENGTH", 6)))),
            "-hls_delete_threshold",
            str(max(1, int(app.config.get("ROOMCAST_HLS_DELETE_THRESHOLD", 12)))),
            "-hls_allow_cache",
            "0",
            "-hls_flags",
            "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename",
            os.path.join(output_dir, "segment-%05d.ts"),
            playlist_path,
        ]

        def _worker():
            listener_id = None
            process = None
            try:
                listener_id, listener, buffered_chunks = stream_hub.open_listener(
                    room_slug,
                    maxsize=HLS_LISTENER_QUEUE_MAXSIZE,
                    include_buffered=True,
                )
                state.listener_id = listener_id
                process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                state.process = process

                def _write_chunk(raw_chunk: bytes):
                    if not raw_chunk or process is None or process.stdin is None:
                        return
                    payload = transcoder.transcode(raw_chunk) if transcoder is not None else raw_chunk
                    if not payload:
                        return
                    try:
                        process.stdin.write(payload)
                        process.stdin.flush()
                    except (BrokenPipeError, ValueError, OSError) as exc:
                        state.last_error = f"ffmpeg input closed: {exc}"
                        state.stop_event.set()
                        return
                    if not state.ready_event.is_set() and os.path.exists(state.playlist_path) and os.path.getsize(state.playlist_path) > 0:
                        state.ready_event.set()

                for buffered_chunk in buffered_chunks:
                    if state.stop_event.is_set():
                        break
                    _write_chunk(buffered_chunk)

                while not state.stop_event.is_set():
                    try:
                        chunk = listener.get(timeout=1.0)
                    except queue.Empty:
                        if process.poll() is not None:
                            state.last_error = f"ffmpeg exited with {process.returncode}"
                            break
                        continue
                    if chunk is None:
                        break
                    _write_chunk(chunk)
            except Exception as exc:  # pragma: no cover - exercised in production/runtime
                state.last_error = str(exc)
                app.logger.exception("hls[worker-error] room=%s", room_slug)
            finally:
                state.ready_event.set()
                if process and process.stdin:
                    try:
                        process.stdin.close()
                    except Exception:
                        pass
                if process and process.poll() is None:
                    try:
                        process.terminate()
                        process.wait(timeout=2.0)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
                if listener_id is not None:
                    stream_hub.close_listener(room_slug, listener_id)

        state.worker_thread = threading.Thread(
            target=_worker,
            name=f"roomcast-hls-{room_slug}",
            daemon=True,
        )
        state.worker_thread.start()
        return state

    def _ensure_hls_stream(room_slug: str, snapshot: dict | None):
        if not snapshot or not _hls_transport_available():
            return None
        source_signature = _hls_source_signature(snapshot)
        if not hls_lock.acquire(timeout=0.5):
            return None
        try:
            state = hls_streams.get(room_slug)
            if state and (
                state.stop_event.is_set()
                or state.source_signature != source_signature
                or (state.process and state.process.poll() is not None)
            ):
                stale_state = hls_streams.pop(room_slug)
            else:
                stale_state = None
        finally:
            hls_lock.release()
        if stale_state:
            _close_hls_stream(room_slug, reason="restart")
        if not hls_lock.acquire(timeout=0.5):
            return None
        try:
            state = hls_streams.get(room_slug)
            if not state:
                state = _start_hls_stream(room_slug, snapshot)
                hls_streams[room_slug] = state
            state.last_request_at = time.time()
        finally:
            hls_lock.release()
        state.ready_event.wait(timeout=max(2.0, float(app.config.get("ROOMCAST_HLS_START_TIMEOUT_SECONDS", 8.0))))
        playlist = Path(state.playlist_path)
        if not playlist.exists() or playlist.stat().st_size <= 0:
            return None
        return state

    def _hls_playlist_response(room_slug: str, state: HlsStreamState, client_token: str):
        playlist_text = Path(state.playlist_path).read_text(encoding="utf-8")
        rewritten_lines = []
        for raw_line in playlist_text.splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                rewritten_lines.append(
                    url_for("listen_hls_segment", room_slug=room_slug, filename=line, client=client_token)
                )
            else:
                rewritten_lines.append(raw_line)
        response = Response("\n".join(rewritten_lines) + "\n", mimetype="application/vnd.apple.mpegurl")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

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

    def _telephony_public_url(endpoint: str, **values) -> str:
        base = (app.config.get("ROOMCAST_TELEPHONY_PUBLIC_BASE_URL") or request.host_url).rstrip("/")
        path = url_for(endpoint, **values)
        if "://" in path:
            split = urlsplit(path)
            path = split.path
            if split.query:
                path = f"{path}?{split.query}"
        base_path = urlsplit(base).path.rstrip("/")
        if base_path and (path == base_path or path.startswith(f"{base_path}/")):
            path = path[len(base_path) :] or "/"
        return f"{base}{path}"

    def _prompt_dir() -> Path:
        prompt_dir = Path(app.config.get("ROOMCAST_TELEPHONY_PROMPT_DIR") or "/app/data/prompts")
        prompt_dir.mkdir(parents=True, exist_ok=True)
        return prompt_dir

    def _prompt_path(prompt_key: str) -> Path:
        if prompt_key not in TELEPHONY_PROMPTS:
            raise KeyError(prompt_key)
        return _prompt_dir() / f"{prompt_key}.wav"

    def _prompt_audio_url(prompt_key: str) -> str | None:
        try:
            prompt_path = _prompt_path(prompt_key)
        except KeyError:
            return None
        if not prompt_path.exists():
            return None
        version = str(int(prompt_path.stat().st_mtime))
        return _telephony_public_url("telephony_prompt_audio", prompt_key=prompt_key, v=version)

    def _prompt_statuses() -> list[dict]:
        statuses = []
        for prompt_key, prompt in TELEPHONY_PROMPTS.items():
            prompt_path = _prompt_path(prompt_key)
            configured = prompt_path.exists()
            updated_at = datetime.fromtimestamp(prompt_path.stat().st_mtime, timezone.utc).isoformat() if configured else ""
            statuses.append(
                {
                    "key": prompt_key,
                    "label": prompt["label"],
                    "text": prompt["text"],
                    "configured": configured,
                    "updated_at": _friendly_timestamp(updated_at, fallback="Not recorded") if updated_at else "Not recorded",
                    "audio_url": _prompt_audio_url(prompt_key) if configured else "",
                }
            )
        return statuses

    def _save_prompt_upload(prompt_key: str, upload):
        if prompt_key not in TELEPHONY_PROMPTS:
            raise ValueError("Unknown prompt.")
        if not upload or not upload.filename:
            raise ValueError("No audio file was uploaded.")

        raw = upload.read()
        max_bytes = 12 * 1024 * 1024
        if not raw:
            raise ValueError("The uploaded audio file was empty.")
        if len(raw) > max_bytes:
            raise ValueError("The uploaded audio file is too large.")

        destination = _prompt_path(prompt_key)
        ffmpeg_path = app.config.get("ROOMCAST_HLS_FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg_path:
            if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
                destination.write_bytes(raw)
                return
            raise ValueError("ffmpeg is required to process browser recordings.")

        suffix = Path(upload.filename or "prompt.webm").suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source_file:
            source_file.write(raw)
            source_path = Path(source_file.name)
        tmp_output = destination.with_suffix(".tmp.wav")
        try:
            subprocess.run(
                [
                    ffmpeg_path,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source_path),
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(tmp_output),
                ],
                check=True,
                timeout=20,
            )
            tmp_output.replace(destination)
        finally:
            source_path.unlink(missing_ok=True)
            tmp_output.unlink(missing_ok=True)

    def _diagnostic_audio_dir() -> Path:
        audio_dir = Path(app.config.get("ROOMCAST_DIAGNOSTIC_AUDIO_DIR") or "/app/data/diagnostic-audio")
        audio_dir.mkdir(parents=True, exist_ok=True)
        return audio_dir

    def _diagnostic_audio_path(filename: str) -> Path:
        if not re.fullmatch(r"hearing-\d{8}T\d{6}Z-[0-9a-f]{8}\.wav", filename or ""):
            raise KeyError(filename)
        return _diagnostic_audio_dir() / filename

    def _diagnostic_audio_samples(limit: int = 12) -> list[dict]:
        samples = []
        for sample_path in sorted(_diagnostic_audio_dir().glob("hearing-*.wav"), key=lambda path: path.stat().st_mtime, reverse=True):
            metadata_path = sample_path.with_suffix(".json")
            metadata = {}
            if metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metadata = {}
            updated_at = datetime.fromtimestamp(sample_path.stat().st_mtime, timezone.utc).isoformat()
            samples.append(
                {
                    "filename": sample_path.name,
                    "created_at": _friendly_timestamp(metadata.get("created_at") or updated_at),
                    "note": metadata.get("note") or "",
                    "original_filename": metadata.get("original_filename") or "",
                    "size_kb": max(1, round(sample_path.stat().st_size / 1024)),
                    "audio_url": url_for("admin_diagnostic_audio_sample", filename=sample_path.name),
                }
            )
            if len(samples) >= limit:
                break
        return samples

    def _save_diagnostic_audio_upload(upload, note: str = "") -> str:
        if not upload or not upload.filename:
            raise ValueError("No audio file was uploaded.")

        raw = upload.read()
        max_bytes = 48 * 1024 * 1024
        if not raw:
            raise ValueError("The uploaded audio file was empty.")
        if len(raw) > max_bytes:
            raise ValueError("The uploaded audio file is too large.")

        timestamp = datetime.now(timezone.utc)
        filename = f"hearing-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}.wav"
        destination = _diagnostic_audio_dir() / filename
        ffmpeg_path = app.config.get("ROOMCAST_HLS_FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg_path:
            if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
                destination.write_bytes(raw)
            else:
                raise ValueError("ffmpeg is required to process browser recordings.")
        else:
            suffix = Path(upload.filename or "hearing.webm").suffix or ".webm"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as source_file:
                source_file.write(raw)
                source_path = Path(source_file.name)
            tmp_output = destination.with_suffix(".tmp.wav")
            try:
                subprocess.run(
                    [
                        ffmpeg_path,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(source_path),
                        "-ac",
                        "1",
                        "-ar",
                        "48000",
                        "-c:a",
                        "pcm_s16le",
                        str(tmp_output),
                    ],
                    check=True,
                    timeout=30,
                )
                tmp_output.replace(destination)
            finally:
                source_path.unlink(missing_ok=True)
                tmp_output.unlink(missing_ok=True)

        metadata = {
            "created_at": timestamp.isoformat(),
            "note": (note or "").strip()[:500],
            "original_filename": upload.filename or "",
            "content_type": upload.mimetype or "",
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
            "user_agent": request.headers.get("User-Agent", ""),
        }
        destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        return filename

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
        stream_url = _telephony_public_url(
            "telephony_stream",
            room_slug=room_slug,
            exp=expires_at,
            sig=signature,
            label=participant_label,
            channel=channel,
        )
        if stream_url.endswith(".mp3") or ".mp3?" in stream_url:
            stream_url = stream_url.replace(".mp3", ".wav", 1)
        return stream_url

    def _telephony_stream_is_valid(room_slug: str, expires_at: str, signature: str, participant_label: str, channel: str) -> bool:
        try:
            expires = int(expires_at)
        except (TypeError, ValueError):
            return False
        if expires < int(time.time()):
            return False
        expected = _sign_telephony_stream(room_slug, expires, participant_label, channel)
        return hmac.compare_digest(expected, signature or "")

    def _telephony_stream_response(stream_factory, snapshot: dict | None = None):
        source_descriptor = _stream_descriptor(snapshot)
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }

        def _stream():
            yield _wav_stream_header(
                channels=TELEPHONY_STREAM_PROFILE["channels"],
                sample_rate_hz=TELEPHONY_STREAM_PROFILE["sample_rate_hz"],
                bits_per_sample=TELEPHONY_STREAM_PROFILE["bits_per_sample"],
            )
            transcoder = TelephonyPcmTranscoder(
                source_channels=source_descriptor.get("channels", 1),
                source_rate_hz=source_descriptor.get("sample_rate_hz", 48000),
                bits_per_sample=source_descriptor.get("bits_per_sample", 24),
                output_rate_hz=TELEPHONY_STREAM_PROFILE["sample_rate_hz"],
                mono_mix=app.config.get("ROOMCAST_TELEPHONY_MONO_MIX", "left"),
                phone_dsp_enabled=_config_enabled("ROOMCAST_TELEPHONY_DSP_ENABLED", default=False),
                phone_gain_db=float(app.config.get("ROOMCAST_TELEPHONY_GAIN_DB", 0.0)),
            )
            for chunk in stream_factory():
                if not chunk:
                    continue
                if source_descriptor["mimetype"] == "audio/wav":
                    converted = transcoder.transcode(chunk)
                    if converted:
                        yield converted
                else:
                    yield chunk

        return Response(_stream(), mimetype=TELEPHONY_STREAM_PROFILE["mimetype"], headers=headers)

    def _send_telnyx_stream_audio(ws, session_state: TelephonySessionState, runtime: TelnyxStreamRuntime):
        snapshot = _room_snapshot(session_state.room_slug)
        descriptor = _stream_descriptor(snapshot)
        codec = _telnyx_stream_codec()
        sample_rate = _telnyx_stream_pcm_sample_rate()
        g722_encoder = None
        opus_encoder = None
        if codec == "G722":
            if G722Codec is None:
                runtime.close_reason = "send-error"
                _telephony_log("telnyx-stream-send-error", session_id=session_state.session_id, error="G722 encoder is not installed")
                runtime.stop_event.set()
                return
            g722_encoder = G722Codec(16000, 64000, False)
        elif codec == "OPUS":
            if opuslib is None:
                runtime.close_reason = "send-error"
                _telephony_log("telnyx-stream-send-error", session_id=session_state.session_id, error="OPUS encoder is not installed")
                runtime.stop_event.set()
                return
            application_name = (app.config.get("ROOMCAST_TELNYX_OPUS_APPLICATION") or "audio").strip().lower()
            application = opuslib.APPLICATION_AUDIO if application_name == "audio" else opuslib.APPLICATION_VOIP
            opus_encoder = opuslib.Encoder(sample_rate, 1, application)
            opus_encoder.bitrate = max(16000, int(app.config.get("ROOMCAST_TELNYX_OPUS_BITRATE", 48000)))
            opus_encoder.vbr = True
            opus_encoder.vbr_constraint = False
            signal_name = (app.config.get("ROOMCAST_TELNYX_OPUS_SIGNAL") or "music").strip().lower()
            opus_encoder.signal = opuslib.SIGNAL_VOICE if signal_name == "voice" else opuslib.SIGNAL_MUSIC
            opus_encoder.max_bandwidth = opuslib.BANDWIDTH_SUPERWIDEBAND if sample_rate >= 24000 else opuslib.BANDWIDTH_WIDEBAND
        frame_pcm_bytes = _telnyx_stream_frame_bytes()
        silence_frame = b"\x00" * frame_pcm_bytes
        transcoder = TelephonyPcmTranscoder(
            source_channels=descriptor.get("channels", 1),
            source_rate_hz=descriptor.get("sample_rate_hz", 48000),
            bits_per_sample=descriptor.get("bits_per_sample", 24),
            output_rate_hz=sample_rate,
            mono_mix=app.config.get("ROOMCAST_TELEPHONY_MONO_MIX", "left"),
            phone_dsp_enabled=_config_enabled("ROOMCAST_TELEPHONY_DSP_ENABLED", default=False),
            phone_gain_db=float(app.config.get("ROOMCAST_TELEPHONY_GAIN_DB", 0.0)),
        )
        pcm_buffer = bytearray()
        for buffered_chunk in session_state.buffered_chunks:
            if buffered_chunk:
                pcm_buffer.extend(transcoder.transcode(buffered_chunk))
        frame_seconds = max(0.02, int(app.config.get("ROOMCAST_TELNYX_STREAM_FRAME_MS", 20)) / 1000)
        rtp_timestamp_increment = max(1, int(_telnyx_rtp_clock_rate(codec, sample_rate) * frame_seconds))
        next_send_at = time.monotonic()
        max_buffer_bytes = frame_pcm_bytes * 64
        startup_buffer_bytes = frame_pcm_bytes * 20
        recent_frame_reuse_seconds = 0.35
        sender_started = len(pcm_buffer) >= startup_buffer_bytes
        last_real_frame = silence_frame
        last_audio_at = time.monotonic() if sender_started else None
        frames_sent = 0
        fallback_frames_sent = 0
        source_chunks_drained = 0
        buffer_trim_events = 0
        max_send_late_ms = 0.0
        stats_started_at = time.monotonic()
        next_stats_at = stats_started_at + 5.0
        tap_writer, tap_metadata_path, tap_max_frames = _open_telnyx_debug_tap(
            session_state,
            sample_rate=sample_rate,
            codec=codec,
        )
        tap_frames_written = 0

        def _write_debug_tap(frame_pcm: bytes):
            nonlocal tap_frames_written
            if not tap_writer or tap_frames_written >= tap_max_frames:
                return
            remaining_frames = tap_max_frames - tap_frames_written
            frame_count = len(frame_pcm) // 2
            if frame_count > remaining_frames:
                frame_pcm = frame_pcm[: remaining_frames * 2]
                frame_count = remaining_frames
            if frame_pcm:
                tap_writer.writeframesraw(frame_pcm)
                tap_frames_written += frame_count

        def _append_source_chunk(chunk: bytes):
            nonlocal buffer_trim_events
            if not chunk:
                return
            pcm_buffer.extend(transcoder.transcode(chunk))
            if len(pcm_buffer) > max_buffer_bytes:
                del pcm_buffer[:-max_buffer_bytes]
                buffer_trim_events += 1

        def _drain_source_queue(*, max_chunks: int = 256) -> bool:
            nonlocal source_chunks_drained
            for _ in range(max_chunks):
                try:
                    chunk = session_state.listener.get_nowait()
                except queue.Empty:
                    return True
                if chunk is None:
                    runtime.close_reason = "listener-closed"
                    _telephony_log("telnyx-stream-listener-closed", session_id=session_state.session_id, room_slug=session_state.room_slug)
                    runtime.stop_event.set()
                    return False
                source_chunks_drained += 1
                _append_source_chunk(chunk)
            return True

        def _log_send_stats():
            nonlocal next_stats_at
            now = time.monotonic()
            if now < next_stats_at:
                return
            elapsed = max(0.001, now - stats_started_at)
            buffered_ms = (len(pcm_buffer) / max(1, frame_pcm_bytes)) * frame_seconds * 1000
            _telephony_log(
                "telnyx-stream-send-stats",
                session_id=session_state.session_id,
                codec=codec,
                sample_rate=sample_rate,
                frame_ms=int(frame_seconds * 1000),
                frames_sent=frames_sent,
                fallback_frames=fallback_frames_sent,
                source_chunks=source_chunks_drained,
                buffer_ms=round(buffered_ms, 1),
                buffer_trim_events=buffer_trim_events,
                max_send_late_ms=round(max_send_late_ms, 1),
                send_rate_fps=round(frames_sent / elapsed, 1),
            )
            next_stats_at = now + 5.0

        try:
            test_signal = (app.config.get("ROOMCAST_TELNYX_TEST_SIGNAL") or "").strip().lower()
            if test_signal in {"1", "true", "tone", "sine", "multi", "multitone", "chord", "broadband"}:
                tone_frequency = float(app.config.get("ROOMCAST_TELNYX_TEST_SIGNAL_HZ", 440))
                tone_gain = float(app.config.get("ROOMCAST_TELNYX_TEST_SIGNAL_GAIN", 0.18))
                tone_phase = 0.0
                multitone_frequencies = [220.0, 330.0, 440.0, 660.0, 880.0, 1320.0, 1760.0, 2640.0]
                multitone_phases = [0.0 for _ in multitone_frequencies]
                frame_samples = max(1, frame_pcm_bytes // 2)
                _telephony_log(
                    "telnyx-test-signal-enabled",
                    session_id=session_state.session_id,
                    room_slug=session_state.room_slug,
                    codec=codec,
                    sample_rate=sample_rate,
                    signal=test_signal,
                    frequency_hz=tone_frequency if test_signal not in {"multi", "multitone", "chord", "broadband"} else multitone_frequencies,
                    gain=tone_gain,
                )
                while not runtime.stop_event.is_set():
                    session_state.last_access_at = time.time()
                    snapshot = _room_snapshot(session_state.room_slug)
                    if not snapshot or not _snapshot_should_hold_stream(snapshot):
                        runtime.close_reason = "room-ended"
                        _telephony_log("telnyx-stream-room-ended", session_id=session_state.session_id, room_slug=session_state.room_slug)
                        runtime.stop_event.set()
                        break
                    sleep_for = next_send_at - time.monotonic()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    if test_signal in {"multi", "multitone", "chord", "broadband"}:
                        frame_pcm, multitone_phases = _pcm16_multitone_frame(
                            sample_rate,
                            frame_samples,
                            multitone_frequencies,
                            tone_gain,
                            multitone_phases,
                        )
                    else:
                        frame_pcm, tone_phase = _pcm16_sine_frame(
                            sample_rate,
                            frame_samples,
                            tone_frequency,
                            tone_gain,
                            tone_phase,
                        )
                    _write_debug_tap(frame_pcm)
                    codec_payload = _telnyx_stream_payload_from_pcm(frame_pcm, g722_encoder=g722_encoder, opus_encoder=opus_encoder)
                    payload = _telnyx_stream_media_payload(codec_payload, runtime, codec=codec, sample_rate=sample_rate)
                    ws.send(json.dumps({"event": "media", "media": {"payload": base64.b64encode(payload).decode("ascii")}}))
                    runtime.rtp_sequence_number = (runtime.rtp_sequence_number + 1) & 0xFFFF
                    runtime.rtp_timestamp = (runtime.rtp_timestamp + rtp_timestamp_increment) & 0xFFFFFFFF
                    next_send_at = max(next_send_at + frame_seconds, time.monotonic())
                return

            while not runtime.stop_event.is_set():
                session_state.last_access_at = time.time()
                snapshot = _room_snapshot(session_state.room_slug)
                if not snapshot or not _snapshot_should_hold_stream(snapshot):
                    runtime.close_reason = "room-ended"
                    _telephony_log("telnyx-stream-room-ended", session_id=session_state.session_id, room_slug=session_state.room_slug)
                    runtime.stop_event.set()
                    break

                if not sender_started:
                    try:
                        chunk = session_state.listener.get(timeout=frame_seconds)
                    except queue.Empty:
                        chunk = b""
                    if chunk is None:
                        runtime.close_reason = "listener-closed"
                        _telephony_log("telnyx-stream-listener-closed", session_id=session_state.session_id, room_slug=session_state.room_slug)
                        runtime.stop_event.set()
                        break
                    if chunk:
                        source_chunks_drained += 1
                        _append_source_chunk(chunk)
                    if not _drain_source_queue():
                        break
                    if len(pcm_buffer) >= startup_buffer_bytes:
                        sender_started = True
                        next_send_at = time.monotonic() + frame_seconds
                    else:
                        continue

                sleep_for = next_send_at - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                send_target_at = next_send_at
                if not _drain_source_queue():
                    break

                if len(pcm_buffer) >= frame_pcm_bytes:
                    frame_pcm = bytes(pcm_buffer[:frame_pcm_bytes])
                    del pcm_buffer[:frame_pcm_bytes]
                    is_fallback = False
                    last_real_frame = frame_pcm
                    last_audio_at = time.monotonic()
                else:
                    use_last_frame = bool(last_audio_at and (time.monotonic() - last_audio_at) <= recent_frame_reuse_seconds)
                    frame_pcm = last_real_frame if use_last_frame else silence_frame
                    is_fallback = True

                _write_debug_tap(frame_pcm)
                codec_payload = _telnyx_stream_payload_from_pcm(frame_pcm, g722_encoder=g722_encoder, opus_encoder=opus_encoder)
                payload = _telnyx_stream_media_payload(codec_payload, runtime, codec=codec, sample_rate=sample_rate)
                ws.send(json.dumps({"event": "media", "media": {"payload": base64.b64encode(payload).decode("ascii")}}))
                runtime.rtp_sequence_number = (runtime.rtp_sequence_number + 1) & 0xFFFF
                runtime.rtp_timestamp = (runtime.rtp_timestamp + rtp_timestamp_increment) & 0xFFFFFFFF
                frames_sent += 1
                if is_fallback:
                    fallback_frames_sent += 1
                max_send_late_ms = max(max_send_late_ms, max(0.0, (time.monotonic() - send_target_at) * 1000))
                if time.monotonic() - send_target_at > frame_seconds:
                    next_send_at = time.monotonic() + frame_seconds
                else:
                    next_send_at = send_target_at + frame_seconds
                _log_send_stats()
        except ConnectionClosed:
            runtime.close_reason = runtime.close_reason or "technical-difficulty"
            _telephony_log("telnyx-stream-send-closed", session_id=session_state.session_id, stream_id=runtime.stream_id)
        except Exception as exc:
            runtime.close_reason = "send-error"
            _telephony_log("telnyx-stream-send-error", session_id=session_state.session_id, error=exc)
        finally:
            runtime.stop_event.set()
            _close_telnyx_debug_tap(
                tap_writer,
                tap_metadata_path,
                frames_written=tap_frames_written,
                close_reason=runtime.close_reason,
            )
            try:
                ws.close()
            except Exception:
                pass

    def _voice_prompt_xml(message: str, prompt_key: str | None = None) -> str:
        if prompt_key:
            prompt_url = _prompt_audio_url(prompt_key)
            if prompt_url:
                return f"<Play>{escape(prompt_url)}</Play>"
        voice = (app.config.get("ROOMCAST_TELEPHONY_VOICE") or "").strip()
        voice_attr = f' voice="{escape(voice)}"' if voice else ""
        return f"<Say{voice_attr}>{escape(message)}</Say>"

    def _voice_gather_xml(action_url: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Gather input="dtmf" numDigits="4" timeout="6" action="{escape(action_url)}" method="POST">
    {_voice_prompt_xml(TELEPHONY_PROMPTS["welcome_pin"]["text"], "welcome_pin")}
  </Gather>
  {_voice_prompt_xml(TELEPHONY_PROMPTS["goodbye"]["text"], "goodbye")}
  <Hangup />
</Response>"""

    def _twilio_validation_xml(validation_code: str) -> str:
        code = "".join(ch for ch in validation_code if ch.isdigit())
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Pause length="4" />
  <Play digits="ww{escape(code)}" />
  <Pause length="3" />
  <Hangup />
</Response>"""

    def _twilio_validation_pending(caller_id: str) -> bool:
        validation_code = "".join(ch for ch in str(app.config.get("ROOMCAST_TWILIO_VERIFY_CODE") or "") if ch.isdigit())
        if not validation_code:
            return False
        expected_caller = (app.config.get("ROOMCAST_TWILIO_VERIFY_CALLER_ID") or "").strip()
        normalized_caller = (caller_id or "").strip()
        return not expected_caller or normalized_caller == expected_caller

    def _voice_no_active_schedule_xml() -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_voice_prompt_xml(TELEPHONY_PROMPTS["no_active_schedule"]["text"], "no_active_schedule")}
  <Hangup />
</Response>"""

    def _voice_invalid_pin_xml(retry_url: str) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_voice_prompt_xml(TELEPHONY_PROMPTS["invalid_pin"]["text"], "invalid_pin")}
  <Redirect method="POST">{escape(retry_url)}</Redirect>
</Response>"""

    def _voice_no_active_conference_xml() -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_voice_prompt_xml(TELEPHONY_PROMPTS["no_active_conference"]["text"], "no_active_conference")}
  <Hangup />
</Response>"""

    def _voice_connect_xml(stream_url: str, continue_url: str | None = None, prompt: str | None = None) -> str:
        prompt_xml = f"\n  {_voice_prompt_xml(prompt)}" if prompt else ""
        redirect_xml = ""
        if continue_url:
            redirect_xml = f'\n  <Redirect method="POST">{escape(continue_url)}</Redirect>'
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
{prompt_xml}
  <Play>{escape(stream_url)}</Play>
{redirect_xml}
</Response>"""

    def _voice_goodbye_xml(message: str = "The line is no longer available. Goodbye.", prompt_key: str | None = None) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  {_voice_prompt_xml(message, prompt_key)}
  <Hangup />
</Response>"""

    def _voice_goodbye_message_for_reason(reason: str) -> tuple[str, str]:
        normalized = (reason or "").strip().lower()
        if normalized in {"room-ended", "normal-end", "conference-ended"}:
            return TELEPHONY_PROMPTS["conference_ending"]["text"], "conference_ending"
        if normalized in {
            "technical-difficulty",
            "stream-error",
            "server-unhealthy",
            "listener-closed",
            "source-interrupted",
            "send-error",
        }:
            return TELEPHONY_PROMPTS["technical_difficulty"]["text"], "technical_difficulty"
        return TELEPHONY_PROMPTS["line_unavailable"]["text"], "line_unavailable"

    def _voice_goodbye_xml_for_reason(reason: str) -> str:
        message, prompt_key = _voice_goodbye_message_for_reason(reason)
        return _voice_goodbye_xml(message, prompt_key)

    def _voice_telnyx_stream_xml(session_state: TelephonySessionState, webhook_token: str, continue_url: str) -> str:
        stream_url = _telephony_websocket_url(
            "telnyx_stream_socket",
            webhook_token=webhook_token,
            session_id=session_state.session_id,
        )
        status_url = _telephony_public_url("telnyx_stream_status", webhook_token=webhook_token)
        codec = _telnyx_stream_codec()
        sample_rate = _telnyx_stream_advertised_sample_rate()
        reconnect_enabled = "true" if _telnyx_stream_reconnect_enabled() else "false"
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream
      url="{escape(stream_url)}"
      name="{escape(session_state.session_id)}"
      bidirectionalMode="rtp"
      bidirectionalCodec="{escape(codec)}"
      bidirectionalSamplingRate="{sample_rate}"
      statusCallback="{escape(status_url)}"
      statusCallbackMethod="POST"
      enableReconnect="{reconnect_enabled}">
      <Parameter name="session_id" value="{escape(session_state.session_id)}" />
      <Parameter name="room_slug" value="{escape(session_state.room_slug)}" />
      <Parameter name="participant_label" value="{escape(session_state.participant_label)}" />
    </Stream>
  </Connect>
  <Redirect method="POST">{escape(continue_url)}</Redirect>
</Response>"""

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
            runtime_online = _runtime_online(host)
            hub_status = stream_hub.status(host["room_slug"])
            device_options = _device_options(host, runtime)
            schedule_rows = _schedule_rows_display(host["schedules"])
            active_schedule_rows = [row for row in schedule_rows if row["enabled"]]
            active_listener_count = roomcast_store.count_active_listener_sessions(host["room_slug"])
            hosts.append(
                {
                    **host,
                    "source_online": runtime_online,
                    "current_device": runtime.get("current_device", "") if runtime_online else "",
                    "known_devices": runtime.get("devices", []) if runtime_online else [],
                    "device_options": device_options,
                    "device_slots": _device_slots(device_options, host.get("device_order", [])),
                    "schedule_rows": schedule_rows,
                    "active_schedule_rows": active_schedule_rows,
                    "last_error": _compact_error(runtime.get("last_error", "")) if runtime_online else "",
                    "is_ingesting": bool(runtime.get("is_ingesting", False)) if runtime_online else False,
                    "last_seen_at": runtime.get("last_seen_at"),
                    "priority": host.get("priority", 0),
                    "room_alias": _room_alias(host["room_slug"], host["room_label"]),
                    "broadcasting": hub_status["broadcasting"],
                    "listener_count": active_listener_count,
                    "signal_level_db": hub_status["signal_level_db"],
                    "signal_peak_db": hub_status["signal_peak_db"],
                    "signal_updated_at": hub_status["signal_updated_at"],
                    "signal_level_percent": hub_status["signal_level_percent"],
                    "signal_peak_percent": hub_status["signal_peak_percent"],
                    "active_listeners": [
                        _decorate_listener(listener)
                        for listener in roomcast_store.list_listener_sessions(host["room_slug"], active_only=True, limit=6)
                    ],
                    "recent_listeners": [
                        _decorate_listener(listener)
                        for listener in roomcast_store.list_listener_sessions(host["room_slug"], active_only=False, limit=6)
                    ],
                    "agent_setup": _agent_setup(host),
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
        if not selected_room_slug and hosts:
            selected_room_slug = max(
                hosts,
                key=lambda host: (
                    1 if host.get("broadcasting") else 0,
                    1 if host.get("desired_active") else 0,
                    1 if host.get("is_ingesting") else 0,
                    1 if host.get("source_online") else 0,
                    -int(host.get("priority") or 0),
                ),
            )["room_slug"]
        selected_host = host_by_room.get(selected_room_slug)
        active_meeting = roomcast_store.get_active_meeting()
        active_room_slug = active_meeting["room_slug"] if active_meeting and active_meeting["room_slug"] in VISIBLE_ROOM_SLUGS else None
        focus_room_slug = active_room_slug or selected_room_slug
        focus_host = host_by_room.get(focus_room_slug)
        if active_meeting:
            active_meeting = {
                **active_meeting,
                "room_alias": _room_alias(active_meeting["room_slug"], active_meeting["room_label"]),
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
                "detail": "Press Start Call, choose a room, and the agent will handle the rest.",
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

    def _terminate_room_stream(room_slug: str):
        hub_status = stream_hub.status(room_slug)
        active_host_slug = hub_status.get("active_host_slug")
        if active_host_slug:
            stream_hub.finish_broadcast(room_slug, active_host_slug)

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
                1 if item[0]["desired_active"] else 0,
                1 if item[0].get("is_ingesting") else 0,
                1 if item[0]["source_online"] else 0,
                1 if item[0]["schedule_active"] else 0,
                item[0]["host_priority"],
                -item[1],
            ),
        )[0]

    def _has_public_signal(snapshot: dict) -> bool:
        return any(
            (
                snapshot["broadcasting"],
                snapshot.get("is_ingesting"),
                snapshot["desired_active"],
                snapshot["schedule_active"],
            )
        )

    def _snapshot_recent_stream(snapshot: dict | None, *, grace_seconds: float = STREAM_RECOVERY_GRACE_SECONDS) -> bool:
        if not snapshot:
            return False
        if snapshot.get("broadcasting"):
            return True
        last_chunk_at = _parse_iso8601(snapshot.get("last_chunk_at"))
        if not last_chunk_at:
            return False
        age_seconds = (datetime.now(timezone.utc) - last_chunk_at).total_seconds()
        return age_seconds <= max(1.0, float(grace_seconds))

    def _snapshot_should_hold_stream(snapshot: dict | None, *, grace_seconds: float = STREAM_RECOVERY_GRACE_SECONDS) -> bool:
        if not snapshot:
            return False
        return bool(
            _snapshot_recent_stream(snapshot, grace_seconds=grace_seconds)
            or snapshot.get("is_ingesting")
            or snapshot.get("desired_active")
            or snapshot.get("schedule_active")
        )

    def _any_public_meeting_active() -> bool:
        candidates = _candidate_public_rooms(None)
        return any(_snapshot_should_hold_stream(snapshot) for snapshot, _ in candidates)

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
        desired_active = host["desired_active"] if host else False
        is_ingesting = bool((host["runtime"] or {}).get("is_ingesting")) if host else False
        hls_status = _hls_runtime_status(
            room_slug,
            broadcasting=bool(hub_status["broadcasting"]),
            desired_active=bool(desired_active),
            is_ingesting=bool(is_ingesting),
        )
        return {
            "slug": room["slug"],
            "label": room["label"],
            "description": room["description"],
            "enabled": room["enabled"],
            "host_slug": host["slug"] if host else None,
            "host_label": host["label"] if host else None,
            "host_notes": host["notes"] if host else "",
            "desired_active": desired_active,
            "schedule_active": host["schedule_active"] if host else False,
            "source_online": _runtime_online(host),
            "runtime": host["runtime"] if host else None,
            "is_ingesting": is_ingesting,
            "broadcasting": hub_status["broadcasting"],
            "listener_count": host["listener_count"] if host else roomcast_store.count_active_listener_sessions(room_slug),
            "last_chunk_at": hub_status["last_chunk_at"],
            "current_device": (host["runtime"] or {}).get("current_device") if host else "",
            "last_error": _compact_error((host["runtime"] or {}).get("last_error")) if host else "",
            "schedule_rows": host["schedule_rows"] if host else [],
            "next_change": host["next_change"] if host else None,
            "host_priority": host["priority"] if host else 0,
            "capture_mode": host.get("capture_mode", "auto") if host else "auto",
            "capture_sample_rate_hz": host.get("capture_sample_rate_hz", 48000) if host else 48000,
            "room_alias": _room_alias(room["slug"], room["label"]),
            "signal_level_db": hub_status["signal_level_db"],
            "signal_peak_db": hub_status["signal_peak_db"],
            "signal_updated_at": hub_status["signal_updated_at"],
            "signal_level_percent": hub_status["signal_level_percent"],
            "signal_peak_percent": hub_status["signal_peak_percent"],
            "stream_transport": "hls" if _hls_transport_available() else "direct",
            "connection_quality_percent": hls_status["quality_percent"],
            "connection_quality_label": hls_status["quality_label"],
            "connection_quality_state": hls_status["quality_state"],
            "connection_quality_detail": hls_status["quality_detail"],
            "hls": hls_status,
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

    def _public_stream_url(*, room_slug: str | None = None) -> str:
        if _request_prefers_hls():
            client_token = _new_hls_client_token()
            if room_slug:
                return url_for("listen_hls_room_playlist", room_slug=room_slug, client=client_token)
            return url_for("listen_hls_live_playlist", client=client_token)
        if room_slug:
            return url_for("listen_wav", room_slug=room_slug)
        return url_for("listen_live_wav")

    def _public_hls_stream_url(*, room_slug: str | None = None) -> str:
        if _request_forces_direct_stream() or not _hls_transport_available():
            return ""
        client_token = _new_hls_client_token()
        if room_slug:
            return url_for("listen_hls_room_playlist", room_slug=room_slug, client=client_token)
        return url_for("listen_hls_live_playlist", client=client_token)

    def _resolve_public_room(pin: str):
        return _select_public_room(_candidate_public_rooms(pin))

    def _resolve_telephony_room(pin: str):
        candidates = _candidate_public_rooms(pin)
        if not candidates:
            return "invalid", None
        signaled = [item for item in candidates if _snapshot_should_hold_stream(item[0])]
        if not signaled:
            return "inactive", _select_public_room(candidates)
        return "active", _select_public_room(signaled)

    def _active_public_room():
        allowed_room_slugs = _authorized_rooms()
        if not allowed_room_slugs:
            return None
        candidates = _candidate_public_rooms(None, allowed_room_slugs=allowed_room_slugs)
        signaled = [item for item in candidates if _snapshot_should_hold_stream(item[0])]
        return _select_public_room(signaled)

    def _admin_monitor_room(preferred_room_slug: str | None = None):
        preferred = (preferred_room_slug or "").strip()
        if preferred in VISIBLE_ROOM_SLUGS:
            snapshot = _room_snapshot(preferred)
            if snapshot and snapshot["enabled"]:
                return snapshot

        candidates = []
        for room_slug in VISIBLE_ROOM_SLUGS:
            snapshot = _room_snapshot(room_slug)
            if not snapshot or not snapshot["enabled"]:
                continue
            candidates.append((snapshot, 0))
        return _select_public_room(candidates)

    def _await_snapshot(snapshot_resolver, *, timeout: float = LIVE_SNAPSHOT_WAIT_SECONDS):
        snapshot = snapshot_resolver()
        if snapshot:
            return snapshot

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(LIVE_SNAPSHOT_POLL_SECONDS)
            snapshot = snapshot_resolver()
            if snapshot:
                return snapshot
        return None

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
            "is_ingesting": False,
            "broadcasting": False,
            "listener_count": 0,
            "last_chunk_at": None,
            "current_device": "",
            "last_error": "",
            "schedule_rows": [],
            "next_change": None,
            "host_priority": 0,
            "room_alias": _listener_name(),
            "signal_level_db": None,
            "signal_peak_db": None,
            "signal_updated_at": None,
            "signal_level_percent": 0.0,
            "signal_peak_percent": 0.0,
            "stream_transport": "hls" if _hls_transport_available() else "direct",
            "connection_quality_percent": 0,
            "connection_quality_label": "Idle",
            "connection_quality_state": "idle",
            "connection_quality_detail": "No active web stream.",
            "hls": _hls_runtime_status("", broadcasting=False, desired_active=False, is_ingesting=False),
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
            call_in=_call_in_info(include_pin=False),
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
            call_in=_call_in_info(),
            room=_live_snapshot(),
            stream_url=_public_stream_url(),
            hls_stream_url=_public_hls_stream_url(),
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
            call_in=_call_in_info(),
            room=snapshot,
            stream_url=_public_stream_url(room_slug=room_slug),
            hls_stream_url=_public_hls_stream_url(room_slug=room_slug),
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

    @app.get("/healthz")
    def healthz():
        try:
            hosts = roomcast_store.list_hosts()
            return jsonify(
                {
                    "ok": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "host_count": len(hosts),
                    "active_rooms": [
                        host["room_slug"]
                        for host in hosts
                        if (host.get("runtime") or {}).get("is_ingesting")
                    ],
                }
            )
        except Exception as exc:
            app.logger.exception("healthz failed")
            return jsonify({"ok": False, "error": str(exc)}), 503

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

        context = _panel_context()
        return render_template_string(
            ADMIN_PANEL_TEMPLATE,
            project_name=_project_name(),
            **context,
            conflicts=_ingest_conflicts(_visible_hosts()),
            message=request.args.get("message"),
            error=request.args.get("error"),
            admin_stream_url=url_for(
                "listen_live_wav",
                monitor=1,
                room=((context.get("control_host") or {}).get("room_slug") or ""),
            ),
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

    @app.get("/admin/diagnostics/audio")
    def admin_diagnostic_audio():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        return render_template_string(
            DIAGNOSTIC_AUDIO_TEMPLATE,
            project_name=_project_name(),
            samples=_diagnostic_audio_samples(),
            message=request.args.get("message"),
            error=request.args.get("error"),
            panel_url=url_for("admin_panel"),
            settings_url=url_for("admin_settings"),
            logout_url=url_for("admin_logout"),
            upload_url=url_for("admin_upload_diagnostic_audio"),
        )

    @app.post("/admin/diagnostics/audio")
    def admin_upload_diagnostic_audio():
        if not _is_admin():
            return jsonify({"error": "unauthorized"}), 403
        try:
            filename = _save_diagnostic_audio_upload(request.files.get("audio"), request.form.get("note", ""))
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify(
                    {
                        "ok": True,
                        "filename": filename,
                        "audio_url": url_for("admin_diagnostic_audio_sample", filename=filename),
                    }
                )
            return redirect(url_for("admin_diagnostic_audio", message=f"Saved {filename}."))
        except (ValueError, subprocess.SubprocessError) as exc:
            if request.headers.get("X-Requested-With") == "fetch":
                return jsonify({"error": str(exc)}), 400
            return redirect(url_for("admin_diagnostic_audio", error=str(exc)))

    @app.get("/admin/diagnostics/audio/<filename>", endpoint="admin_diagnostic_audio_sample")
    def admin_diagnostic_audio_sample(filename: str):
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))
        try:
            sample_path = _diagnostic_audio_path(filename)
        except KeyError:
            return jsonify({"error": "unknown sample"}), 404
        if not sample_path.exists():
            return jsonify({"error": "sample not found"}), 404
        return send_file(sample_path, mimetype="audio/wav", conditional=True, max_age=0)

    @app.get("/telephony/prompts/<prompt_key>.wav", endpoint="telephony_prompt_audio")
    def telephony_prompt_audio(prompt_key: str):
        try:
            prompt_path = _prompt_path(prompt_key)
        except KeyError:
            return jsonify({"error": "unknown prompt"}), 404
        if not prompt_path.exists():
            return jsonify({"error": "prompt not recorded"}), 404
        return send_file(prompt_path, mimetype="audio/wav", conditional=True, max_age=3600)

    @app.post("/admin/prompts/<prompt_key>")
    def admin_upload_prompt(prompt_key: str):
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))
        try:
            _save_prompt_upload(prompt_key, request.files.get("audio"))
            return redirect(url_for("admin_settings", message=f"Recorded {TELEPHONY_PROMPTS[prompt_key]['label']}."))
        except (KeyError, ValueError, subprocess.SubprocessError) as exc:
            return redirect(url_for("admin_settings", error=str(exc)))

    @app.post("/admin/prompts/<prompt_key>/delete")
    def admin_delete_prompt(prompt_key: str):
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))
        try:
            _prompt_path(prompt_key).unlink(missing_ok=True)
            return redirect(url_for("admin_settings", message=f"Cleared {TELEPHONY_PROMPTS[prompt_key]['label']}."))
        except KeyError:
            return redirect(url_for("admin_settings", error="Unknown prompt."))

    @app.get("/api/admin/runtime")
    def admin_runtime():
        if not _is_admin():
            return jsonify({"error": "unauthorized"}), 403

        hosts = []
        for host in _visible_hosts():
            hosts.append(
                {
                    "slug": host["slug"],
                    "room_slug": host["room_slug"],
                    "room_alias": host["room_alias"],
                    "source_online": host["source_online"],
                    "current_device": host["current_device"],
                    "known_devices": host["known_devices"],
                    "device_options": host["device_options"],
                    "last_seen_at": host["last_seen_at"],
                }
            )
        return jsonify({"hosts": hosts})

    @app.post("/admin/call/start")
    def admin_start_call():
        if not _is_admin():
            return redirect(url_for("admin_entry", error="Admin password required."))

        room_slug = (request.form.get("room_slug") or "").strip()
        if room_slug not in VISIBLE_ROOM_SLUGS:
            return redirect(url_for("admin_panel", error="Choose a room before starting the call."))

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
        for host in _visible_hosts():
            if host["room_slug"] != room_slug:
                _terminate_room_stream(host["room_slug"])
        _audit_log(
            "admin",
            "call-started",
            f"{_room_alias(room_slug, room_slug)} was started from the control panel.",
            room_slug=room_slug,
        )
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
        _terminate_room_stream(room_slug)
        _audit_log(
            "admin",
            "call-stopped",
            f"{_room_alias(room_slug, room_slug)} was stopped from the control panel.",
            room_slug=room_slug,
        )
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
        _audit_log("admin", "schedule-restored", "Automatic scheduling was restored for visible rooms.")
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
            _audit_log(
                "admin",
                "host-settings-updated",
                f"{slug} settings were updated from the admin panel.",
                room_slug=room_slug,
                host_slug=slug,
                manual_mode=manual_mode,
                enabled=enabled,
                capture_mode=host.get("capture_mode", "auto") if host else "auto",
                device_order=device_order,
                schedule_rows=len(schedule_rows),
            )
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
        writer.writerow([])
        writer.writerow(["Audio Levels"])
        writer.writerow(["Sampled", "Host", "RMS dBFS", "Peak dBFS", "Listeners", "Device", "Quality"])
        for sample in report.get("audio_levels", []):
            writer.writerow([
                sample["sampled_at"],
                sample["host_slug"] or "",
                "" if sample["signal_level_db"] is None else f"{sample['signal_level_db']:.2f}",
                "" if sample["signal_peak_db"] is None else f"{sample['signal_peak_db']:.2f}",
                sample["listener_count"],
                sample["current_device"],
                sample["connection_quality_label"],
            ])
        csv_payload = output.getvalue()
        response = Response(csv_payload, mimetype="text/csv")
        response.headers["Content-Disposition"] = f'attachment; filename="webcall-report-{meeting_id}.csv"'
        return response

    def _audio_stream_response(stream_factory, snapshot: dict | None = None):
        source_descriptor = _stream_descriptor(snapshot)
        stream_profile = _browser_stream_descriptor(snapshot)
        transcoder = None
        browser_gain_db = float(app.config.get("ROOMCAST_BROWSER_GAIN_DB", 0.0))
        if (
            int(source_descriptor.get("channels") or 1) != stream_profile["channels"]
            or int(source_descriptor.get("sample_rate_hz") or 48000) != stream_profile["sample_rate_hz"]
            or int(source_descriptor.get("bits_per_sample") or 24) != stream_profile["bits_per_sample"]
            or abs(browser_gain_db) > 0.01
        ):
            transcoder = PcmStreamTranscoder(
                source_channels=int(source_descriptor.get("channels") or 1),
                source_rate_hz=int(source_descriptor.get("sample_rate_hz") or 48000),
                bits_per_sample=int(source_descriptor.get("bits_per_sample") or 24),
                output_channels=stream_profile["channels"],
                output_rate_hz=stream_profile["sample_rate_hz"],
                output_bits_per_sample=stream_profile["bits_per_sample"],
                gain_db=browser_gain_db,
            )

        browser_chunk_bytes = max(
            4096,
            int(
                stream_profile["sample_rate_hz"]
                * stream_profile["channels"]
                * (stream_profile["bits_per_sample"] // 8)
                * (BROWSER_STREAM_CHUNK_MILLISECONDS / 1000.0)
            ),
        )

        def _stream():
            source_iter = stream_factory()
            try:
                if stream_profile["mimetype"] == "audio/wav":
                    yield _wav_stream_header(
                        channels=stream_profile["channels"],
                        sample_rate_hz=stream_profile["sample_rate_hz"],
                        bits_per_sample=stream_profile["bits_per_sample"],
                    )
                pending = bytearray()
                for chunk in source_iter:
                    if transcoder is not None:
                        chunk = transcoder.transcode(chunk)
                    if not chunk:
                        continue
                    pending.extend(chunk)
                    while len(pending) >= browser_chunk_bytes:
                        yield bytes(pending[:browser_chunk_bytes])
                        del pending[:browser_chunk_bytes]
                if pending:
                    yield bytes(pending)
            finally:
                close_source = getattr(source_iter, "close", None)
                if close_source:
                    close_source()

        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",
        }
        return Response(_stream(), mimetype=stream_profile["mimetype"], headers=headers)

    @app.get("/listen/<room_slug>.wav", endpoint="listen_wav")
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
        return _audio_stream_response(_stream, _room_snapshot(room_slug))

    @app.get("/listen/<room_slug>.m3u8", endpoint="listen_hls_room_playlist")
    def listen_hls_room_playlist(room_slug: str):
        room = roomcast_store.get_room(room_slug)
        if not room or not room["enabled"]:
            return jsonify({"error": "unknown room"}), 404
        if not _is_authorized(room_slug):
            return jsonify({"error": "pin required"}), 403
        if not _hls_transport_available():
            return jsonify({"error": "hls unavailable"}), 503
        client_token = (request.args.get("client") or "").strip()
        if not client_token:
            return jsonify({"error": "missing client"}), 400
        snapshot = _room_snapshot(room_slug)
        if not snapshot or not _snapshot_should_hold_stream(snapshot):
            return Response(status=204)
        _touch_hls_client(room_slug, client_token)
        _cleanup_hls_streams()
        state = _ensure_hls_stream(room_slug, snapshot)
        if not state:
            return Response(status=503)
        return _hls_playlist_response(room_slug, state, client_token)

    @app.get("/listen/live.wav", endpoint="listen_live_wav")
    @app.get("/listen/live.mp3")
    def listen_live():
        admin_monitor = request.args.get("monitor") == "1" and _is_admin()
        preferred_room_slug = (request.args.get("room") or "").strip()
        if not admin_monitor and not _authorized_rooms():
            return jsonify({"error": "pin required"}), 403

        snapshot_resolver = (
            (lambda: _admin_monitor_room(preferred_room_slug))
            if admin_monitor
            else _active_public_room
        )
        snapshot = _await_snapshot(snapshot_resolver)
        if not snapshot:
            return Response(b"", mimetype=_stream_descriptor(snapshot)["mimetype"], headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            })

        room_slug = snapshot["slug"]
        participant_label = session.get("listener_name") or f"Web {request.headers.get('X-Forwarded-For', request.remote_addr or 'listener')}"
        participant_key = session.get("listener_key") or request.cookies.get(app.config.get("SESSION_COOKIE_NAME", "session"), "")
        listener_session_id = None
        if not admin_monitor:
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
                if listener_session_id is not None:
                    roomcast_store.end_listener_session(listener_session_id)
        return _audio_stream_response(_stream, snapshot)

    @app.get("/listen/live.m3u8", endpoint="listen_hls_live_playlist")
    def listen_hls_live_playlist():
        if not _authorized_rooms():
            return jsonify({"error": "pin required"}), 403
        if not _hls_transport_available():
            return jsonify({"error": "hls unavailable"}), 503
        client_token = (request.args.get("client") or "").strip()
        if not client_token:
            return jsonify({"error": "missing client"}), 400
        snapshot = _await_snapshot(_active_public_room)
        if not snapshot or not _snapshot_should_hold_stream(snapshot):
            return Response(status=204)
        room_slug = snapshot["slug"]
        _touch_hls_client(room_slug, client_token)
        _cleanup_hls_streams()
        state = _ensure_hls_stream(room_slug, snapshot)
        if not state:
            return Response(status=503)
        return _hls_playlist_response(room_slug, state, client_token)

    @app.get("/listen/hls/<room_slug>/<path:filename>", endpoint="listen_hls_segment")
    def listen_hls_segment(room_slug: str, filename: str):
        room = roomcast_store.get_room(room_slug)
        if not room or not room["enabled"]:
            return jsonify({"error": "unknown room"}), 404
        if not _is_authorized(room_slug):
            return jsonify({"error": "pin required"}), 403
        if not _hls_transport_available():
            return jsonify({"error": "hls unavailable"}), 503
        client_token = (request.args.get("client") or "").strip()
        if client_token:
            _touch_hls_client(room_slug, client_token)
        _cleanup_hls_streams()
        if not hls_lock.acquire(timeout=0.5):
            return Response(status=503)
        try:
            state = hls_streams.get(room_slug)
            if state:
                state.last_request_at = time.time()
        finally:
            hls_lock.release()
        if not state:
            return Response(status=404)
        output_root = Path(state.output_dir).resolve()
        requested_path = (output_root / filename).resolve()
        if output_root not in requested_path.parents and requested_path != output_root:
            return jsonify({"error": "invalid path"}), 400
        if not requested_path.exists() or not requested_path.is_file():
            return Response(status=404)
        suffix = requested_path.suffix.lower()
        if suffix == ".ts":
            mimetype = "video/mp2t"
        elif suffix == ".aac":
            mimetype = "audio/aac"
        elif suffix == ".m4s":
            mimetype = "video/iso.segment"
        else:
            mimetype = "application/octet-stream"
        response = Response(requested_path.read_bytes(), mimetype=mimetype)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.get("/telephony/stream/<room_slug>.wav")
    @app.get("/telephony/stream/<room_slug>.mp3")
    def telephony_stream(room_slug: str):
        participant_label = (request.args.get("label") or "Phone caller").strip() or "Phone caller"
        channel = (request.args.get("channel") or "phone").strip() or "phone"
        _telephony_log(
            "stream-request",
            room_slug=room_slug,
            channel=channel,
            label=participant_label,
            remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        room = roomcast_store.get_room(room_slug)
        if not room or not room["enabled"]:
            _telephony_log("stream-missing-room", room_slug=room_slug)
            return jsonify({"error": "unknown room"}), 404
        snapshot = _room_snapshot(room_slug)
        if _stream_descriptor(snapshot)["mimetype"] != "audio/wav":
            _telephony_log("stream-wrong-profile", room_slug=room_slug, profile=_stream_descriptor(snapshot)["mimetype"])
            return jsonify({"error": "telephony requires wav profile"}), 503

        expires_at = request.args.get("exp", "")
        signature = request.args.get("sig", "")
        if not _telephony_stream_is_valid(room_slug, expires_at, signature, participant_label, channel):
            _telephony_log("stream-unauthorized", room_slug=room_slug, channel=channel, label=participant_label)
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
            _telephony_log("stream-open", room_slug=room_slug, channel=channel, label=participant_label)
            try:
                yield from stream_hub.listen(room_slug)
            finally:
                roomcast_store.end_listener_session(listener_session_id)
                _telephony_log("stream-close", room_slug=room_slug, channel=channel, label=participant_label)
        return _telephony_stream_response(_stream, snapshot)

    @app.get("/telephony/session/<session_id>/segment.wav")
    def telephony_session_segment(session_id: str):
        _cleanup_telephony_sessions()
        session_state = telephony_sessions.get(session_id)
        if not session_state:
            _telephony_log("segment-missing-session", session_id=session_id)
            return jsonify({"error": "unknown session"}), 404
        wav_bytes, stream_closed = _collect_telephony_segment(session_state)
        session_state.stream_ended = stream_closed
        _telephony_log(
            "segment-rendered",
            session_id=session_id,
            room_slug=session_state.room_slug,
            label=session_state.participant_label,
            bytes=len(wav_bytes),
            ended=stream_closed,
        )
        headers = {
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
            "Content-Length": str(len(wav_bytes)),
        }
        return Response(wav_bytes, mimetype=TELEPHONY_STREAM_PROFILE["mimetype"], headers=headers)

    @app.route("/telephony/twilio/<webhook_token>/voice", methods=["GET", "POST"])
    def twilio_voice(webhook_token: str):
        expected = app.config.get("ROOMCAST_TWILIO_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            _telephony_log("twilio-unauthorized", remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""))
            return jsonify({"error": "not found"}), 404

        digits = (request.values.get("Digits") or "").strip()
        _telephony_log(
            "twilio-voice",
            method=request.method,
            digits=digits or "<none>",
            from_number=request.values.get("From", ""),
            remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        if not digits:
            if not _any_public_meeting_active():
                _telephony_log("twilio-no-live-meeting")
                return Response(_voice_no_active_schedule_xml(), mimetype="text/xml")
            action_url = _telephony_public_url("twilio_voice", webhook_token=webhook_token)
            return Response(_voice_gather_xml(action_url), mimetype="text/xml")

        room_state, snapshot = _resolve_telephony_room(digits)
        if room_state == "invalid" or not snapshot:
            retry_url = _telephony_public_url("twilio_voice", webhook_token=webhook_token)
            _telephony_log("twilio-invalid-pin", digits=digits, retry_url=retry_url)
            return Response(_voice_invalid_pin_xml(retry_url), mimetype="text/xml")
        if room_state == "inactive":
            _telephony_log("twilio-no-active-conference", digits=digits, room_slug=snapshot["slug"])
            return Response(_voice_no_active_conference_xml(), mimetype="text/xml")

        participant_label = (request.values.get("From") or "Phone caller").strip() or "Phone caller"
        session_state = _create_telephony_session(snapshot["slug"], participant_label=participant_label)
        continue_url = _telephony_public_url("twilio_continue", webhook_token=webhook_token, session_id=session_state.session_id)
        stream_url = _telephony_session_segment_url(session_state.session_id)
        _telephony_log("twilio-connect", room_slug=snapshot["slug"], label=participant_label, stream_url=stream_url)
        return Response(_voice_connect_xml(stream_url, continue_url), mimetype="text/xml")

    @app.route("/telephony/twilio/<webhook_token>/continue/<session_id>", methods=["GET", "POST"])
    def twilio_continue(webhook_token: str, session_id: str):
        expected = app.config.get("ROOMCAST_TWILIO_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            return jsonify({"error": "not found"}), 404
        _cleanup_telephony_sessions()
        session_state = telephony_sessions.get(session_id)
        if not session_state:
            closure = _recent_telephony_closure(session_id)
            return Response(_voice_goodbye_xml_for_reason(closure.reason if closure else ""), mimetype="text/xml")
        session_state.last_access_at = time.time()
        snapshot = _room_snapshot(session_state.room_slug)
        if not snapshot or not _snapshot_should_hold_stream(snapshot) or session_state.stream_ended:
            reason = "technical-difficulty" if session_state.stream_ended and snapshot and _snapshot_should_hold_stream(snapshot) else "room-ended"
            _close_telephony_session(session_id, reason=reason)
            return Response(_voice_goodbye_xml_for_reason(reason), mimetype="text/xml")
        continue_url = _telephony_public_url("twilio_continue", webhook_token=webhook_token, session_id=session_id)
        stream_url = _telephony_session_segment_url(session_id)
        _telephony_log("twilio-continue", session_id=session_id, room_slug=session_state.room_slug, stream_url=stream_url)
        return Response(_voice_connect_xml(stream_url, continue_url), mimetype="text/xml")

    @app.route("/telephony/telnyx/<webhook_token>/voice", methods=["GET", "POST"])
    def telnyx_voice(webhook_token: str):
        expected = app.config.get("ROOMCAST_TELNYX_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            _telephony_log("telnyx-unauthorized", remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""))
            return jsonify({"error": "not found"}), 404

        digits = (request.values.get("Digits") or "").strip()
        caller_id = request.values.get("From") or request.values.get("CallerId") or ""
        _telephony_log(
            "telnyx-voice",
            method=request.method,
            digits=digits or "<none>",
            from_number=caller_id,
            remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            user_agent=request.headers.get("User-Agent", ""),
        )
        if not digits and _twilio_validation_pending(caller_id):
            validation_code = app.config.get("ROOMCAST_TWILIO_VERIFY_CODE", "")
            _telephony_log("twilio-validation-answer", from_number=caller_id)
            return Response(_twilio_validation_xml(validation_code), mimetype="text/xml")

        if not digits:
            if not _any_public_meeting_active():
                _telephony_log("telnyx-no-live-meeting")
                return Response(_voice_no_active_schedule_xml(), mimetype="text/xml")
            action_url = _telephony_public_url("telnyx_voice", webhook_token=webhook_token)
            return Response(_voice_gather_xml(action_url), mimetype="text/xml")

        room_state, snapshot = _resolve_telephony_room(digits)
        if room_state == "invalid" or not snapshot:
            retry_url = _telephony_public_url("telnyx_voice", webhook_token=webhook_token)
            _telephony_log("telnyx-invalid-pin", digits=digits, retry_url=retry_url)
            return Response(_voice_invalid_pin_xml(retry_url), mimetype="text/xml")
        if room_state == "inactive":
            _telephony_log("telnyx-no-active-conference", digits=digits, room_slug=snapshot["slug"])
            return Response(_voice_no_active_conference_xml(), mimetype="text/xml")

        participant_label = (
            request.values.get("From")
            or request.values.get("CallerId")
            or "Phone caller"
        ).strip() or "Phone caller"
        session_state = _create_telephony_session(snapshot["slug"], participant_label=participant_label)
        continue_url = _telephony_public_url("telnyx_continue", webhook_token=webhook_token, session_id=session_state.session_id)
        if _telnyx_stream_transport_enabled():
            stream_url = _telephony_websocket_url(
                "telnyx_stream_socket",
                webhook_token=webhook_token,
                session_id=session_state.session_id,
            )
            _telephony_log("telnyx-connect-stream", room_slug=snapshot["slug"], label=participant_label, stream_url=stream_url)
            return Response(_voice_telnyx_stream_xml(session_state, webhook_token, continue_url), mimetype="text/xml")

        stream_url = _telephony_session_segment_url(session_state.session_id)
        _telephony_log("telnyx-connect", room_slug=snapshot["slug"], label=participant_label, stream_url=stream_url)
        return Response(_voice_connect_xml(stream_url, continue_url), mimetype="text/xml")

    @app.route("/telephony/telnyx/<webhook_token>/continue/<session_id>", methods=["GET", "POST"])
    def telnyx_continue(webhook_token: str, session_id: str):
        expected = app.config.get("ROOMCAST_TELNYX_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            return jsonify({"error": "not found"}), 404
        _cleanup_telephony_sessions()
        session_state = telephony_sessions.get(session_id)
        if not session_state:
            closure = _recent_telephony_closure(session_id)
            return Response(_voice_goodbye_xml_for_reason(closure.reason if closure else ""), mimetype="text/xml")
        session_state.last_access_at = time.time()
        snapshot = _room_snapshot(session_state.room_slug)
        if not snapshot or not _snapshot_should_hold_stream(snapshot) or session_state.stream_ended:
            reason = "technical-difficulty" if session_state.stream_ended and snapshot and _snapshot_should_hold_stream(snapshot) else "room-ended"
            _close_telephony_session(session_id, reason=reason)
            return Response(_voice_goodbye_xml_for_reason(reason), mimetype="text/xml")
        continue_url = _telephony_public_url("telnyx_continue", webhook_token=webhook_token, session_id=session_id)
        if _telnyx_stream_transport_enabled():
            _telephony_log("telnyx-continue-stream", session_id=session_id, room_slug=session_state.room_slug)
            return Response(_voice_telnyx_stream_xml(session_state, webhook_token, continue_url), mimetype="text/xml")
        stream_url = _telephony_session_segment_url(session_id)
        _telephony_log("telnyx-continue", session_id=session_id, room_slug=session_state.room_slug, stream_url=stream_url)
        return Response(_voice_connect_xml(stream_url, continue_url), mimetype="text/xml")

    @app.route("/telephony/telnyx/<webhook_token>/stream-status", methods=["GET", "POST"])
    def telnyx_stream_status(webhook_token: str):
        expected = app.config.get("ROOMCAST_TELNYX_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            return jsonify({"error": "not found"}), 404
        payload = request.get_json(silent=True)
        body = payload if isinstance(payload, dict) else request.form.to_dict(flat=True)
        _telephony_log(
            "telnyx-stream-status",
            remote_addr=request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            body=json.dumps(body, sort_keys=True)[:500],
        )
        return ("", 204)

    @app.route("/telephony/telnyx/<webhook_token>/sms", methods=["GET", "POST"])
    def telnyx_sms(webhook_token: str):
        expected = app.config.get("ROOMCAST_TELNYX_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            return jsonify({"error": "not found"}), 404

        payload = request.get_json(silent=True)
        body = payload if isinstance(payload, dict) else request.form.to_dict(flat=True)
        if not body:
            body = {"raw_body": request.get_data(as_text=True)[:2000]}

        event = body.get("data") if isinstance(body.get("data"), dict) else {}
        event_payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        message_text = (
            event_payload.get("text")
            or event_payload.get("body")
            or body.get("Body")
            or body.get("body")
            or ""
        )
        log_record = {
            "received_at": datetime.now(timezone.utc).isoformat(),
            "remote_addr": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            "body": body,
        }
        log_path = Path(app.config.get("ROOMCAST_TELNYX_SMS_LOG_PATH") or "/app/data/telnyx-sms-webhooks.jsonl")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(log_record, sort_keys=True) + "\n")
        except OSError:
            app.logger.exception("telnyx SMS webhook log write failed")

        _telephony_log(
            "telnyx-sms-webhook",
            remote_addr=log_record["remote_addr"],
            message_text=str(message_text)[:200],
            body=json.dumps(body, sort_keys=True)[:1000],
        )
        return jsonify({"ok": True})

    @sock.route("/telephony/telnyx/<webhook_token>/stream/<session_id>")
    def telnyx_stream_socket(ws, webhook_token: str, session_id: str):
        expected = app.config.get("ROOMCAST_TELNYX_WEBHOOK_TOKEN", "")
        if not expected or not hmac.compare_digest(webhook_token, expected):
            ws.close()
            return

        _cleanup_telephony_sessions()
        session_state = telephony_sessions.get(session_id)
        runtime = telnyx_stream_runtimes.get(session_id)
        if not session_state or not runtime:
            _telephony_log("telnyx-stream-missing-session", session_id=session_id)
            ws.close()
            return

        sender_started = False
        runtime.stop_event.clear()
        try:
            while not runtime.stop_event.is_set():
                message = ws.receive()
                if message is None:
                    break
                session_state.last_access_at = time.time()
                if not isinstance(message, str):
                    continue
                try:
                    payload = json.loads(message)
                except json.JSONDecodeError:
                    _telephony_log("telnyx-stream-bad-json", session_id=session_id)
                    continue

                event = (payload.get("event") or "").strip().lower()
                if event == "connected":
                    _telephony_log("telnyx-stream-connected", session_id=session_id)
                    continue
                if event == "start":
                    runtime.stream_id = payload.get("stream_id", "") or runtime.stream_id
                    _telephony_log(
                        "telnyx-stream-start",
                        session_id=session_id,
                        room_slug=session_state.room_slug,
                        stream_id=runtime.stream_id,
                        media_format=json.dumps((payload.get("start") or {}).get("media_format") or {}, sort_keys=True),
                    )
                    if not sender_started:
                        runtime.sender_thread = threading.Thread(
                            target=_send_telnyx_stream_audio,
                            args=(ws, session_state, runtime),
                            daemon=True,
                        )
                        runtime.sender_thread.start()
                        sender_started = True
                    continue
                if event == "dtmf":
                    _telephony_log(
                        "telnyx-stream-dtmf",
                        session_id=session_id,
                        digit=((payload.get("dtmf") or {}).get("digit") or ""),
                    )
                    continue
                if event == "error":
                    details = payload.get("payload") or {}
                    runtime.close_reason = "technical-difficulty"
                    _telephony_log(
                        "telnyx-stream-error-frame",
                        session_id=session_id,
                        code=details.get("code"),
                        title=details.get("title"),
                        detail=details.get("detail"),
                    )
                    continue
                if event == "stop":
                    snapshot = _room_snapshot(session_state.room_slug)
                    runtime.close_reason = runtime.close_reason or (
                        "room-ended" if snapshot and not _snapshot_should_hold_stream(snapshot) else "caller-ended"
                    )
                    _telephony_log("telnyx-stream-stop", session_id=session_id, stream_id=runtime.stream_id)
                    runtime.stop_event.set()
                    break
                if event == "media":
                    continue
                _telephony_log("telnyx-stream-event", session_id=session_id, event=event or "<unknown>")
        except ConnectionClosed:
            snapshot = _room_snapshot(session_state.room_slug)
            runtime.close_reason = runtime.close_reason or (
                "technical-difficulty" if snapshot and _snapshot_should_hold_stream(snapshot) else "caller-ended"
            )
            _telephony_log("telnyx-stream-disconnected", session_id=session_id, stream_id=runtime.stream_id)
        except Exception as exc:
            runtime.close_reason = runtime.close_reason or "technical-difficulty"
            _telephony_log("telnyx-stream-error", session_id=session_id, error=exc)
        finally:
            runtime.stop_event.set()
            if runtime.sender_thread and runtime.sender_thread.is_alive():
                runtime.sender_thread.join(timeout=1.0)
            runtime.sender_thread = None
            snapshot = _room_snapshot(session_state.room_slug)
            stream_should_hold = bool(snapshot and _snapshot_should_hold_stream(snapshot))
            should_keep_for_reconnect = (
                _telnyx_stream_reconnect_enabled()
                and stream_should_hold
                and (runtime.close_reason or "technical-difficulty")
                in {"technical-difficulty", "stream-error", "server-unhealthy", "listener-closed", "source-interrupted", "send-error"}
            )
            if should_keep_for_reconnect:
                session_state.last_access_at = time.time()
                runtime.close_reason = ""
                runtime.stop_event.clear()
                _telephony_log("telnyx-stream-await-reconnect", session_id=session_id, room_slug=session_state.room_slug)
            else:
                _close_telephony_session(session_id, reason=runtime.close_reason or "stream-ended")
            try:
                ws.close()
            except Exception:
                pass

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
            stream_profile=(payload.get("stream_profile") or "").strip(),
            stream_channels=int(payload.get("stream_channels") or 1),
            sample_rate_hz=int(payload.get("sample_rate_hz") or 48000),
            sample_bits=int(payload.get("sample_bits") or 0),
        )

        refreshed = roomcast_store.get_host(host_slug, include_secret=True)
        roomcast_store.sync_meeting_state(
            refreshed["room_slug"],
            active=refreshed["desired_active"],
            host_slug=host_slug,
            trigger_mode=refreshed["manual_mode"] if refreshed["manual_mode"] != "auto" else "schedule",
            actor="agent-heartbeat",
        )
        if not refreshed["desired_active"]:
            _terminate_room_stream(refreshed["room_slug"])
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
                "capture_mode": refreshed.get("capture_mode", "auto"),
                "capture_sample_rate_hz": int(refreshed.get("capture_sample_rate_hz") or 48000),
                "stream_profile": _stream_profile_name(),
                "ingest_url": url_for(
                    "source_ingest",
                    host_slug=host_slug,
                    token=refreshed["heartbeat_token"],
                    _external=True,
                ),
                "listen_page": url_for("room_page", room_slug=refreshed["room_slug"], _external=True),
            }
        )

    @app.post("/api/source/event")
    def source_event():
        payload = request.get_json(silent=False)
        host_slug = (payload.get("host_slug") or "").strip()
        token = (payload.get("token") or "").strip()
        host = roomcast_store.get_host(host_slug, include_secret=True)
        if not host or token != host["heartbeat_token"]:
            return jsonify({"error": "unauthorized"}), 403

        event_type = (payload.get("event_type") or "").strip() or "agent-event"
        level = (payload.get("level") or "info").strip().lower() or "info"
        message = (payload.get("message") or "").strip() or event_type
        details = payload.get("details") or {}
        if not isinstance(details, dict):
            details = {"value": details}
        reserved = {"room_slug", "host_slug", "listener_session_id", "meeting_session_id", "level", "message", "event_type", "component"}
        sanitized_details = {key: value for key, value in details.items() if key not in reserved}
        _audit_log(
            "agent",
            event_type,
            message,
            level=level,
            room_slug=host["room_slug"],
            host_slug=host_slug,
            **sanitized_details,
        )
        return jsonify({"ok": True})

    @app.post("/api/source/ingest/<host_slug>")
    def source_ingest(host_slug: str):
        token = (request.args.get("token") or "").strip()
        host = roomcast_store.get_host(host_slug, include_secret=True)
        if not host or token != host["heartbeat_token"]:
            return jsonify({"error": "unauthorized"}), 403

        room_slug = host["room_slug"]
        runtime = host.get("runtime") or {}
        stream_hub.start_broadcast(
            room_slug,
            host_slug,
            stream_channels=max(1, int(runtime.get("stream_channels") or (2 if host.get("capture_mode") == "stereo" else 1))),
            sample_rate_hz=max(8000, int(runtime.get("sample_rate_hz") or host.get("capture_sample_rate_hz") or 48000)),
            bits_per_sample=max(16, int(runtime.get("sample_bits") or 24)),
        )
        _audit_log(
            "source",
            "ingest-connected",
            f"{host_slug} connected its ingest stream.",
            room_slug=room_slug,
            host_slug=host_slug,
        )
        try:
            try:
                while True:
                    chunk = request.stream.read(INGEST_CHUNK_SIZE)
                    if not chunk:
                        break
                    stream_hub.publish(room_slug, chunk)
            except GunicornNoMoreData:
                app.logger.info(
                    "source-ingest disconnected host_slug=%s room_slug=%s",
                    host_slug,
                    room_slug,
                )
                _audit_log(
                    "source",
                    "ingest-disconnected",
                    f"{host_slug} disconnected its ingest stream.",
                    room_slug=room_slug,
                    host_slug=host_slug,
                )
        finally:
            stream_hub.finish_broadcast(room_slug, host_slug)
            _audit_log(
                "source",
                "ingest-finished",
                f"{host_slug} finished its ingest stream.",
                room_slug=room_slug,
                host_slug=host_slug,
            )
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
        --warn: #ffb770;
        --warn-soft: rgba(255, 183, 112, 0.12);
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
        min-height: 100vh;
      }
      main {
        max-width: 720px;
        margin: 0 auto;
        padding: 1rem 1rem 2.5rem;
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
      .shell {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        box-shadow: var(--shadow);
        padding: 1rem;
      }
      .brand {
        display: grid;
        gap: 0.3rem;
        justify-items: center;
        text-align: center;
      }
      .brand h1 {
        margin: 0.15rem 0 0;
        font-size: clamp(2.1rem, 8vw, 3.6rem);
        line-height: 0.96;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        max-width: 28rem;
        line-height: 1.45;
      }
      .banner {
        margin-top: 1rem;
        border-radius: 12px;
        padding: 0.82rem 0.95rem;
        font-weight: 600;
        border: 1px solid var(--line);
        background: var(--good-soft);
        color: var(--good);
      }
      .banner.error {
        background: var(--warn-soft);
        color: var(--warn);
      }
      .status-panel {
        margin-top: 1rem;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface);
        padding: 1rem;
        text-align: center;
      }
      .status-panel.good {
        border-color: rgba(116, 221, 180, 0.28);
      }
      .status-kicker {
        font-size: 0.76rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        font-weight: 700;
        font-family: var(--mono);
      }
      .status-headline {
        margin-top: 0.45rem;
        font-size: clamp(1.4rem, 5vw, 2rem);
        line-height: 1.05;
        font-weight: 700;
      }
      .status-detail {
        margin-top: 0.55rem;
        color: var(--muted);
        line-height: 1.45;
      }
      .entry {
        margin-top: 1.1rem;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface);
        padding: 1rem;
      }
      .call-in-card {
        margin-top: 1rem;
        border-radius: 14px;
        border: 1px solid rgba(135, 214, 255, 0.22);
        background: linear-gradient(180deg, rgba(20, 32, 48, 0.98), rgba(15, 24, 34, 0.98));
        padding: 1rem;
        text-align: center;
      }
      .call-in-card strong {
        display: block;
        color: var(--text);
        font-size: 1.02rem;
      }
      .call-in-card p {
        margin: 0.35rem 0 0;
        color: var(--muted);
      }
      .call-in-number {
        display: inline-flex;
        margin-top: 0.75rem;
        border-radius: 999px;
        padding: 0.64rem 0.9rem;
        border: 1px solid var(--line-strong);
        background: rgba(135, 214, 255, 0.08);
        color: var(--accent);
        text-decoration: none;
        font-family: var(--mono);
        font-weight: 800;
      }
      .call-in-pin {
        display: inline-flex;
        margin-top: 0.65rem;
        color: var(--muted);
        font-family: var(--mono);
        font-size: 0.9rem;
      }
      form {
        display: grid;
        gap: 0.9rem;
      }
      input, button, a {
        font: inherit;
      }
      input {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--surface-2);
        padding: 1rem;
        color: var(--text);
        font-size: clamp(1.5rem, 8vw, 2rem);
        letter-spacing: 0.32em;
        text-align: center;
        font-family: var(--mono);
      }
      button {
        appearance: none;
        border: 0;
        border-radius: 12px;
        padding: 0.84rem 1.4rem;
        background: #dff4ff;
        color: #081018;
        font-weight: 800;
        cursor: pointer;
        justify-self: center;
        min-width: 12rem;
      }
      @media (max-width: 640px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .shell,
        .status-panel,
        .entry {
          border-radius: 14px;
        }
        button {
          width: 100%;
          min-width: 0;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <div class="brand">
          <span class="eyebrow">NTC Newark</span>
          <h1>{{ project_name }}</h1>
          <p>Enter PIN to listen.</p>
        </div>
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
            <button type="submit">Enter</button>
          </form>
        </section>
        {% if call_in %}
        <section class="call-in-card" aria-label="Phone call-in alternative">
          <strong>Dial-In Alternative</strong>
          <p>Use the phone line if needed.</p>
          <span class="call-in-number">{{ call_in.number }}</span>
          {% if call_in.pin %}
          <span class="call-in-pin">PIN {{ call_in.pin }}</span>
          {% endif %}
        </section>
        {% endif %}
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
    <title>{{ project_name }}</title>
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
        --warn: #ffb770;
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
        min-height: 100vh;
      }
      main {
        max-width: 700px;
        margin: 0 auto;
        padding: 0.9rem 0.9rem 2.25rem;
      }
      .shell {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 0.95rem;
        box-shadow: var(--shadow);
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
      .brand {
        display: grid;
        gap: 0.22rem;
        justify-items: center;
        text-align: center;
      }
      .brand h1 {
        margin: 0.15rem 0 0;
        font-size: clamp(1.95rem, 7vw, 3.1rem);
        line-height: 0.96;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        max-width: 26rem;
        line-height: 1.38;
      }
      .stack {
        display: grid;
        gap: 0.85rem;
        margin-top: 0.85rem;
      }
      .panel {
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface);
        padding: 0.9rem;
      }
      .chips {
        display: flex;
        gap: 0.45rem;
        flex-wrap: wrap;
        justify-content: center;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.28rem 0.72rem;
        background: rgba(135, 214, 255, 0.08);
        color: var(--accent);
        font-size: 0.84rem;
        font-weight: 600;
      }
      .chip.good {
        background: rgba(116, 221, 180, 0.12);
        color: var(--good);
      }
      .chip.warn {
        background: rgba(255, 183, 112, 0.12);
        color: var(--warn);
      }
      .player-shell {
        margin-top: 0.75rem;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        padding: 0.9rem;
        transition: border-color 120ms linear, box-shadow 120ms linear;
      }
      .player-shell.awaiting-gesture {
        border-color: rgba(135, 214, 255, 0.36);
        box-shadow: 0 0 0 1px rgba(135, 214, 255, 0.12) inset;
      }
      .meter {
        display: grid;
        gap: 0.7rem;
      }
      .meter-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        color: var(--muted);
        font-size: 0.9rem;
        font-weight: 600;
      }
      .meter-visual {
        display: flex;
        justify-content: center;
        gap: 0.78rem;
        align-items: end;
        min-height: 8.6rem;
        padding: 0.15rem 0;
      }
      .meter-column {
        display: flex;
        flex-direction: column-reverse;
        gap: 0.34rem;
      }
      .meter-segment {
        width: 1.55rem;
        height: 0.66rem;
        border-radius: 999px;
        background: rgba(116, 221, 180, 0.06);
        border: 1px solid rgba(116, 221, 180, 0.1);
        transition: background 120ms linear, border-color 120ms linear, transform 120ms linear;
      }
      .meter-segment.is-on {
        background: linear-gradient(180deg, #93efbf 0%, #62d99e 100%);
        border-color: rgba(116, 221, 180, 0.62);
      }
      .meter-segment.is-peak {
        background: #dfffea;
        border-color: rgba(223, 255, 234, 0.92);
        box-shadow: 0 0 0 1px rgba(7, 14, 20, 0.16) inset, 0 0 8px rgba(116, 221, 180, 0.18);
      }
      .slider-wrap {
        margin-top: 0.75rem;
      }
      .quality-card {
        margin-top: 0.85rem;
        border-radius: 12px;
        border: 1px solid var(--line);
        background: rgba(8, 16, 24, 0.56);
        padding: 0.78rem;
      }
      .quality-head {
        display: flex;
        justify-content: space-between;
        gap: 0.8rem;
        align-items: center;
        color: var(--muted);
        font-size: 0.88rem;
        font-weight: 700;
      }
      .quality-label.good {
        color: var(--good);
      }
      .quality-label.warn {
        color: var(--warn);
      }
      .quality-bar {
        height: 0.62rem;
        border-radius: 999px;
        overflow: hidden;
        margin-top: 0.58rem;
        background: rgba(255, 255, 255, 0.07);
      }
      .quality-fill {
        display: block;
        width: 0%;
        height: 100%;
        border-radius: inherit;
        background: linear-gradient(90deg, var(--warn), var(--good));
        transition: width 180ms ease;
      }
      .quality-detail {
        margin: 0.48rem 0 0;
        color: var(--muted);
        font-size: 0.84rem;
        line-height: 1.4;
      }
      .call-in-card {
        margin-top: 0.85rem;
        border-radius: 14px;
        border: 1px solid rgba(135, 214, 255, 0.22);
        background: rgba(135, 214, 255, 0.06);
        padding: 0.82rem;
        text-align: center;
      }
      .call-in-card strong {
        display: block;
        font-size: 0.96rem;
      }
      .call-in-card p {
        margin: 0.26rem 0 0;
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.4;
      }
      .call-in-number {
        display: inline-flex;
        margin-top: 0.6rem;
        border-radius: 999px;
        padding: 0.56rem 0.78rem;
        border: 1px solid var(--line-strong);
        background: rgba(135, 214, 255, 0.08);
        color: var(--accent);
        text-decoration: none;
        font-family: var(--mono);
        font-weight: 800;
      }
      .call-in-pin {
        display: block;
        margin-top: 0.48rem;
        color: var(--muted);
        font-family: var(--mono);
        font-size: 0.84rem;
      }
      .resume-wrap {
        margin-top: 0.75rem;
        display: none;
        justify-content: center;
      }
      .resume-wrap.is-visible {
        display: flex;
      }
      .resume-button {
        appearance: none;
        border: 1px solid rgba(135, 214, 255, 0.28);
        border-radius: 999px;
        background: rgba(135, 214, 255, 0.12);
        color: var(--text);
        padding: 0.7rem 1.05rem;
        font: inherit;
        font-weight: 700;
        cursor: pointer;
      }
      .resume-button:hover,
      .resume-button:focus-visible {
        border-color: rgba(135, 214, 255, 0.5);
        background: rgba(135, 214, 255, 0.18);
        outline: none;
      }
      .slider-wrap label {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
        color: var(--muted);
        font-weight: 600;
        font-size: 0.9rem;
      }
      input[type="range"] {
        width: 100%;
        margin-top: 0.55rem;
      }
      .small {
        margin-top: 0.75rem;
        color: var(--muted);
        font-size: 0.9rem;
        line-height: 1.45;
        text-align: center;
      }
      audio { display: none; }
      @media (max-width: 640px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .shell,
        .panel,
        .player-shell {
          border-radius: 14px;
        }
        .meter-segment {
          width: 1.3rem;
          height: 0.58rem;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <div class="brand">
          <div class="eyebrow">{{ project_name }}</div>
          <h1>{{ project_name }}</h1>
          <p>Open this page once and audio will connect when the call is live.</p>
        </div>

        <section class="stack">
          <article class="panel">
            <div class="chips">
              <span
                class="chip {% if room.broadcasting %}good{% elif room.is_ingesting or room.desired_active %}warn{% endif %}"
                id="broadcast-chip"
              >
                {% if room.broadcasting or room.is_ingesting or room.desired_active %}Connecting audio{% else %}Waiting for meeting{% endif %}
              </span>
            </div>

            <div class="player-shell">
              <div class="meter">
                <div class="meter-head">
                  <span>Signal level</span>
                  <span id="meter-label">{% if room.broadcasting or room.is_ingesting or room.desired_active %}Connecting{% else %}Idle{% endif %}</span>
                </div>
                <div class="meter-visual" id="meter-visual">
                  <div class="meter-column" data-meter-column="left">
                    {% for band in ["low", "low", "low", "low", "mid", "mid", "mid", "mid", "mid", "high", "high", "high", "peak", "peak"] %}
                    <span class="meter-segment" data-band="{{ band }}"></span>
                    {% endfor %}
                  </div>
                  <div class="meter-column" data-meter-column="right">
                    {% for band in ["low", "low", "low", "low", "mid", "mid", "mid", "mid", "mid", "high", "high", "high", "peak", "peak"] %}
                    <span class="meter-segment" data-band="{{ band }}"></span>
                    {% endfor %}
                  </div>
                </div>
              </div>

              <div class="slider-wrap">
                <label for="volume-slider">
                  <span>Volume</span>
                  <span id="volume-value">100%</span>
                </label>
                <input id="volume-slider" type="range" min="0" max="100" step="1" value="100">
              </div>
              <div class="quality-card" aria-live="polite">
                <div class="quality-head">
                  <span>Playback buffer</span>
                  <span class="quality-label warn" id="connection-quality-label">Checking</span>
                </div>
                <div class="quality-bar" aria-hidden="true">
                  <span class="quality-fill" id="connection-quality-fill"></span>
                </div>
                <p class="quality-detail" id="connection-quality-detail">Waiting for playback status.</p>
              </div>
              <div class="resume-wrap" id="resume-wrap">
                <button type="button" class="resume-button" id="resume-button">Start Audio</button>
              </div>
            </div>

            <div class="small" id="small">
              {% if room.broadcasting %}
              Connecting live audio...
              {% elif room.is_ingesting or room.desired_active %}
              Connecting audio...
              {% else %}
              Meeting not active right now. Leave this page open and it will reconnect when the audio begins.
              {% endif %}
            </div>
            {% if call_in %}
            <div class="call-in-card">
              <strong>Dial-In Alternative</strong>
              <p>Use the phone line if needed.</p>
              {% if call_in.tel_href %}
              <a class="call-in-number" href="{{ call_in.tel_href }}">{{ call_in.number }}</a>
              {% else %}
              <span class="call-in-number">{{ call_in.number }}</span>
              {% endif %}
              {% if call_in.pin %}
              <span class="call-in-pin">PIN {{ call_in.pin }}</span>
              {% endif %}
            </div>
            {% endif %}

            <audio id="stream-player" playsinline preload="auto" autoplay src="{{ stream_url }}"></audio>
          </article>
        </section>
      </section>
    </main>

    <script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js"></script>
    <script>
      const audio = document.getElementById("stream-player");
      const playerShell = document.querySelector(".player-shell");
      const volumeSlider = document.getElementById("volume-slider");
      const volumeValue = document.getElementById("volume-value");
      const resumeWrap = document.getElementById("resume-wrap");
      const resumeButton = document.getElementById("resume-button");
      const meterColumns = [...document.querySelectorAll("[data-meter-column]")];
      const meterLabel = document.getElementById("meter-label");
      const broadcastChip = document.getElementById("broadcast-chip");
      const small = document.getElementById("small");
      const connectionQualityLabel = document.getElementById("connection-quality-label");
      const connectionQualityFill = document.getElementById("connection-quality-fill");
      const connectionQualityDetail = document.getElementById("connection-quality-detail");
      let roomActive = {{ "true" if room.broadcasting or room.is_ingesting or room.desired_active else "false" }};
      let meterPeakPercent = 0;
      let autoplayBlocked = false;
      let reconnectTimer = null;
      let stallTimer = null;
      let bufferingSettleTimer = null;
      let playbackAttemptInFlight = null;
      let lastReconnectAt = 0;
      let lastKnownLiveState = roomActive;
      const directStreamUrl = {{ stream_url | tojson }};
      const hlsStreamUrl = {{ hls_stream_url | tojson }};
      let hlsController = null;
      let baseStreamUrl = directStreamUrl;
      let streamTransport = "direct";
      let lastPlaybackIssueAt = 0;

      function withCacheBust(url) {
        const separator = url.includes("?") ? "&" : "?";
        return url + separator + "v=" + Date.now();
      }

      function browserSupportsNativeHls() {
        return Boolean(
          hlsStreamUrl &&
          (
            audio.canPlayType("application/vnd.apple.mpegurl") ||
            audio.canPlayType("application/x-mpegURL")
          )
        );
      }

      function setupBufferedTransport() {
        if (!hlsStreamUrl) {
          return false;
        }
        if (browserSupportsNativeHls()) {
          streamTransport = "native-hls";
          baseStreamUrl = hlsStreamUrl;
          audio.src = hlsStreamUrl;
          audio.load();
          return true;
        }
        if (window.Hls && window.Hls.isSupported()) {
          streamTransport = "hls-js";
          baseStreamUrl = hlsStreamUrl;
          hlsController = new window.Hls({
            enableWorker: true,
            lowLatencyMode: false,
            liveSyncDurationCount: 3,
            liveMaxLatencyDurationCount: 8,
            maxBufferLength: 30,
          });
          hlsController.attachMedia(audio);
          hlsController.loadSource(hlsStreamUrl);
          hlsController.on(window.Hls.Events.ERROR, (_event, data) => {
            if (!data || !data.fatal) {
              return;
            }
            if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR) {
              hlsController.recoverMediaError();
              return;
            }
            if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR) {
              hlsController.startLoad(-1);
              return;
            }
            queueReconnect("hls-fatal", 1200, true);
          });
          return true;
        }
        return false;
      }

      setupBufferedTransport();
      const prefersBufferedTransport = streamTransport !== "direct";
      const bufferedPlaybackReadyDelayMs = prefersBufferedTransport ? 3500 : 0;
      const initialSignalPercent = Number({{ room.signal_level_percent|default(0.0, true)|tojson }});
      const initialSignalPeakPercent = Number({{ room.signal_peak_percent|default(0.0, true)|tojson }});
      const initialSignalPeakDb = Number({{ room.signal_peak_db|default(-90.0, true)|tojson }});

      function connectingMessage() {
        return prefersBufferedTransport ? "Buffering audio..." : "Connecting audio...";
      }

      function bufferedSeconds() {
        if (!audio.buffered || audio.buffered.length === 0 || !Number.isFinite(audio.currentTime)) {
          return 0;
        }
        for (let index = audio.buffered.length - 1; index >= 0; index -= 1) {
          const start = audio.buffered.start(index);
          const end = audio.buffered.end(index);
          if (audio.currentTime >= start && audio.currentTime <= end) {
            return Math.max(0, end - audio.currentTime);
          }
        }
        return 0;
      }

      function setConnectionQuality(percent, label, state, detail) {
        const clamped = Math.max(0, Math.min(100, Math.round(percent)));
        connectionQualityFill.style.width = `${clamped}%`;
        connectionQualityLabel.textContent = label;
        connectionQualityLabel.className = "quality-label";
        if (state === "good") {
          connectionQualityLabel.classList.add("good");
        } else if (state === "warn") {
          connectionQualityLabel.classList.add("warn");
        }
        connectionQualityDetail.textContent = detail;
      }

      function updateConnectionQuality(status = null) {
        if (!roomActive) {
          setConnectionQuality(0, "Idle", "warn", "No active meeting audio right now.");
          return;
        }
        if (autoplayBlocked) {
          setConnectionQuality(20, "Action needed", "warn", "Tap Start Audio to let this browser play sound.");
          return;
        }
        const seconds = bufferedSeconds();
        const ready = !audio.paused && audio.readyState >= 2;
        const recentIssue = Date.now() - lastPlaybackIssueAt < 8000;
        if (streamTransport !== "direct") {
          let percent = Math.min(100, Math.round((seconds / 8) * 100));
          if (!ready) {
            percent = Math.min(percent, 35);
          }
          if (recentIssue) {
            percent = Math.min(percent, 55);
          }
          let label = "Connecting";
          if (ready && seconds >= 4) {
            label = "Strong";
          } else if (ready && seconds >= 1) {
            label = recentIssue ? "Stabilizing" : "Buffering";
          } else if (ready) {
            label = "Low buffer";
          } else if (recentIssue) {
            label = "Recovering";
          }
          const state = percent >= 75 ? "good" : "warn";
          const detail = `${seconds.toFixed(1)}s buffered for smooth playback.`;
          setConnectionQuality(percent, label, state, detail);
          return;
        }
        if (ready && !recentIssue) {
          setConnectionQuality(82, "Live", "good", "Direct WAV stream is playing.");
        } else if (ready) {
          setConnectionQuality(55, "Recovering", "warn", "Playback is active after a recent stall.");
        } else {
          setConnectionQuality(25, "Connecting", "warn", "Waiting for the browser to receive playable audio.");
        }
      }

      function setAutoplayPrompt(visible) {
        autoplayBlocked = visible;
        playerShell.classList.toggle("awaiting-gesture", visible);
        resumeWrap.classList.toggle("is-visible", visible);
        if (visible) {
          small.textContent = "Playback is blocked by your browser. Tap Start Audio.";
        }
      }

      function isAutoplayBlock(error) {
        if (!error) {
          return false;
        }
        const name = typeof error.name === "string" ? error.name : "";
        const message = typeof error.message === "string" ? error.message : "";
        return (
          name === "NotAllowedError" ||
          /user gesture|play\\(\\) failed because the user/i.test(message)
        );
      }

      function rebuildStreamUrl() {
        audio.pause();
        const nextUrl = withCacheBust(baseStreamUrl);
        if (streamTransport === "hls-js" && hlsController) {
          hlsController.stopLoad();
          hlsController.loadSource(nextUrl);
          hlsController.startLoad(-1);
          return;
        }
        audio.src = nextUrl;
        audio.load();
      }

      function clearReconnectTimer() {
        if (!reconnectTimer) {
          return;
        }
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }

      function clearStallTimer() {
        if (!stallTimer) {
          return;
        }
        clearTimeout(stallTimer);
        stallTimer = null;
      }

      function clearBufferingSettleTimer() {
        if (!bufferingSettleTimer) {
          return;
        }
        clearTimeout(bufferingSettleTimer);
        bufferingSettleTimer = null;
      }

      function queueReconnect(reason, delay = 1200, forceReload = true) {
        if (!roomActive || autoplayBlocked || reconnectTimer) {
          return;
        }
        lastPlaybackIssueAt = Date.now();
        updateConnectionQuality();
        const now = Date.now();
        const cooldown = Math.max(0, 1200 - (now - lastReconnectAt));
        reconnectTimer = setTimeout(() => {
          reconnectTimer = null;
          lastReconnectAt = Date.now();
          if (!roomActive) {
            return;
          }
          if (forceReload) {
            rebuildStreamUrl();
          }
          startPlayback().catch(() => {});
        }, Math.max(delay, cooldown));
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

      const meterFloorDb = -90;

      function paintMeter(percent, peakPercent = 0) {
        const clamped = Math.max(0, Math.min(100, percent));
        const segmentCount = meterColumns[0]?.children.length || 14;
        const litSegments = Math.round((clamped / 100) * segmentCount);
        const peakIndex = peakPercent > 0 ? Math.min(segmentCount - 1, Math.max(0, Math.round((peakPercent / 100) * segmentCount) - 1)) : -1;
        meterColumns.forEach((column) => {
          [...column.children].forEach((segment, index) => {
            segment.classList.toggle("is-on", index < litSegments);
            segment.classList.toggle("is-peak", index === peakIndex);
          });
        });
      }

      function applySignalTelemetry(status) {
        const percent = Number(status.signal_level_percent || 0);
        const peakPercent = Number(status.signal_peak_percent || 0);
        const peakDb = Number(status.signal_peak_db);
        meterPeakPercent = peakPercent;
        paintMeter(percent, peakPercent);
        if (!Number.isFinite(peakDb) || peakPercent <= 0.5) {
          meterLabel.textContent = "Idle";
          return;
        }
        meterLabel.textContent = `${Math.round(Math.max(meterFloorDb, Math.min(0, peakDb)))} dB`;
      }

      async function startPlayback() {
        if (playbackAttemptInFlight) {
          return playbackAttemptInFlight;
        }
        if (roomActive) {
          meterLabel.textContent = "Connecting";
          small.textContent = connectingMessage();
        }
        playbackAttemptInFlight = (async () => {
          try {
            await audio.play();
            setAutoplayPrompt(false);
            clearReconnectTimer();
            clearStallTimer();
            small.textContent = connectingMessage();
          } catch (error) {
            if (roomActive) {
              if (isAutoplayBlock(error)) {
                setAutoplayPrompt(true);
                return;
              }
              lastPlaybackIssueAt = Date.now();
              setAutoplayPrompt(false);
              small.textContent = connectingMessage();
              queueReconnect("play-error", 1200, true);
              return;
            }
            setAutoplayPrompt(false);
          } finally {
            playbackAttemptInFlight = null;
          }
        })();
        return playbackAttemptInFlight;
      }

      async function resumePlaybackFromGesture() {
        if (!autoplayBlocked || !roomActive) {
          return;
        }
        clearReconnectTimer();
        clearStallTimer();
        setAutoplayPrompt(false);
        small.textContent = connectingMessage();
        try {
          await audio.play();
          return;
        } catch (error) {
          if (isAutoplayBlock(error)) {
            setAutoplayPrompt(true);
            return;
          }
        }

        rebuildStreamUrl();
        try {
          await audio.play();
          setAutoplayPrompt(false);
        } catch (error) {
          if (isAutoplayBlock(error)) {
            setAutoplayPrompt(true);
            return;
          }
          small.textContent = connectingMessage();
          queueReconnect("gesture-play-error", 1200, false);
        }
      }

      function setPublicState(status) {
        const activeNow = !!status.broadcasting;
        const connecting = !!status.is_ingesting || !!status.desired_active;
        const listenerReady = !audio.paused && audio.readyState >= 2;
        roomActive = activeNow || connecting;
        if (activeNow && listenerReady) {
          setChip(broadcastChip, "Meeting active now", "good");
        } else if (activeNow || connecting) {
          setChip(broadcastChip, "Connecting audio", "warn");
        } else {
          setChip(broadcastChip, "Waiting for meeting", "warn");
        }
      }

      function markPlaybackReady() {
        clearBufferingSettleTimer();
        if (!roomActive) {
          return;
        }
        const applyReady = () => {
          if (!roomActive || audio.paused || audio.readyState < 2) {
            return;
          }
          meterLabel.textContent = "Live";
          setChip(broadcastChip, "Meeting active now", "good");
          small.textContent = "Meeting active now.";
        };
        if (bufferedPlaybackReadyDelayMs <= 0) {
          applyReady();
          return;
        }
        meterLabel.textContent = "Buffering";
        setChip(broadcastChip, "Connecting audio", "warn");
        small.textContent = connectingMessage();
        bufferingSettleTimer = setTimeout(() => {
          bufferingSettleTimer = null;
          applyReady();
        }, bufferedPlaybackReadyDelayMs);
      }

      async function pollStatus() {
        try {
          const response = await fetch({{ status_url | tojson }} + "?ts=" + Date.now(), { cache: "no-store" });
          if (!response.ok) {
            return;
          }
          const status = await response.json();
          applySignalTelemetry(status);
          updateConnectionQuality(status);
          const liveNow = !!status.broadcasting || !!status.is_ingesting || !!status.desired_active;
          const becameLive = liveNow && !lastKnownLiveState;
          setPublicState(status);
          lastKnownLiveState = liveNow;
          if (becameLive) {
            queueReconnect("became-live", 250, true);
          } else if (roomActive && audio.paused && !autoplayBlocked && !playbackAttemptInFlight && !reconnectTimer) {
            startPlayback().catch(() => {});
          }
          if (status.broadcasting) {
            if (audio.paused || audio.readyState < 2) {
              small.textContent = autoplayBlocked ? "Playback is blocked by your browser. Tap Start Audio." : connectingMessage();
            } else {
              small.textContent = "Meeting active now.";
            }
          } else if (status.is_ingesting || status.desired_active) {
            small.textContent = autoplayBlocked ? "Playback is blocked by your browser. Tap Start Audio." : connectingMessage();
          } else {
            clearReconnectTimer();
            audio.pause();
            small.textContent = "Meeting not active right now. Leave this page open and it will reconnect when the audio begins.";
            setAutoplayPrompt(false);
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

      audio.addEventListener("canplay", () => {
        clearStallTimer();
        updateConnectionQuality();
        if (roomActive) {
          markPlaybackReady();
        }
        if (audio.paused) {
          startPlayback().catch(() => {});
        }
      });
      audio.addEventListener("ended", () => {
        lastPlaybackIssueAt = Date.now();
        updateConnectionQuality();
        queueReconnect("ended", 800, true);
      });
      audio.addEventListener("error", () => {
        lastPlaybackIssueAt = Date.now();
        updateConnectionQuality();
        queueReconnect("error", 800, true);
      });
      audio.addEventListener("waiting", () => {
        lastPlaybackIssueAt = Date.now();
        updateConnectionQuality();
        meterLabel.textContent = "Connecting";
        setChip(broadcastChip, "Connecting audio", "warn");
        if (roomActive) {
          small.textContent = autoplayBlocked ? "Playback is blocked by your browser. Tap Start Audio." : connectingMessage();
        }
      });
      audio.addEventListener("playing", () => {
        clearStallTimer();
        setAutoplayPrompt(false);
        updateConnectionQuality();
        if (audio.readyState >= 2 || audio.currentTime > 0) {
          markPlaybackReady();
        }
      });
      audio.addEventListener("stalled", () => {
        lastPlaybackIssueAt = Date.now();
        updateConnectionQuality();
        if (!roomActive || autoplayBlocked || stallTimer) {
          return;
        }
        stallTimer = window.setTimeout(() => {
          stallTimer = null;
          if (!roomActive || !audio.paused && audio.readyState >= 2) {
            return;
          }
          queueReconnect("stalled", 1500, true);
        }, 3500);
      });
      resumeButton.addEventListener("click", () => {
        resumePlaybackFromGesture().catch(() => {});
      });
      document.addEventListener("keydown", (event) => {
        if ((event.key === "Enter" || event.key === " ") && autoplayBlocked) {
          resumePlaybackFromGesture().catch(() => {});
        }
      });

      paintMeter(0, 0);
      applySignalTelemetry({
        signal_level_percent: initialSignalPercent,
        signal_peak_percent: initialSignalPeakPercent,
        signal_peak_db: initialSignalPeakDb,
      });
      setInterval(pollStatus, 1000);
      setInterval(() => updateConnectionQuality(), 1000);
      pollStatus();
      updateConnectionQuality();
      if (roomActive) {
        startPlayback().catch(() => {});
      }
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
              {{ active_meeting.room_alias }} is live
              {% else %}
              No call is running
              {% endif %}
            </div>
            <div class="status-detail">
              {% if active_meeting %}
              Started {{ active_meeting.started_label }}.
              {% else %}
              Choose a room, then press Start Call.
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
        max-width: 1160px;
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
                <button class="ghost-button" type="submit">Turn Auto On</button>
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

        const storageKey = "roomcast-control-view";
        const tabs = [...document.querySelectorAll("[data-view-tab]")];
        const panels = [...document.querySelectorAll("[data-view-panel]")];
        const activateView = (view) => {
          tabs.forEach((tab) => {
            const active = tab.dataset.viewTab === view;
            tab.classList.toggle("is-active", active);
          });
          panels.forEach((panel) => {
            panel.classList.toggle("is-hidden", panel.dataset.viewPanel !== view);
          });
          window.localStorage.setItem(storageKey, view);
        };

        if (tabs.length && panels.length) {
          const initialView = window.localStorage.getItem(storageKey) || "live";
          activateView(initialView);
          tabs.forEach((tab) => {
            tab.addEventListener("click", () => activateView(tab.dataset.viewTab));
          });
        }

        window.setInterval(() => {
          if (document.hidden) {
            return;
          }
          if (roomDialog && roomDialog.open) {
            return;
          }
          window.location.reload();
        }, 5000);
      })();
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
        --bg: #081018;
        --surface: #0f1822;
        --surface-2: #142030;
        --text: #eef4fb;
        --muted: #99a8b8;
        --line: #213244;
        --accent: #87d6ff;
        --warn: #ffb770;
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
        min-height: 100vh;
      }
      main {
        max-width: 720px;
        margin: 0 auto;
        padding: 1rem 1rem 2.5rem;
      }
      .shell {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        padding: 1rem;
        box-shadow: var(--shadow);
      }
      .brand {
        display: grid;
        gap: 0.3rem;
        justify-items: center;
        text-align: center;
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
      .brand h1 {
        margin: 0.15rem 0 0;
        font-size: clamp(2rem, 8vw, 3.2rem);
        line-height: 0.96;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        line-height: 1.45;
      }
      .banner {
        margin-top: 1rem;
        border-radius: 12px;
        padding: 0.82rem 0.95rem;
        border: 1px solid var(--line);
        background: rgba(255, 183, 112, 0.1);
        color: var(--warn);
        font-weight: 600;
      }
      form {
        display: grid;
        gap: 0.85rem;
        margin-top: 1rem;
      }
      .field {
        display: grid;
        gap: 0.5rem;
      }
      .field label {
        color: var(--muted);
        font-size: 0.84rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 700;
        font-family: var(--mono);
      }
      input, button {
        font: inherit;
      }
      input {
        width: 100%;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--surface-2);
        padding: 0.9rem 0.95rem;
        color: var(--text);
      }
      button {
        appearance: none;
        border: 0;
        border-radius: 12px;
        padding: 0.84rem 1.4rem;
        background: #dff4ff;
        color: #081018;
        font-weight: 800;
        cursor: pointer;
        justify-self: center;
        min-width: 12rem;
      }
      @media (max-width: 640px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .shell {
          border-radius: 14px;
        }
        button {
          width: 100%;
          min-width: 0;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="shell">
        <div class="brand">
          <div class="eyebrow">Control Panel</div>
          <h1>{{ project_name }}</h1>
          <p>Sign in to start or stop the line.</p>
        </div>
        {% if error %}
        <div class="banner">{{ error }}</div>
        {% endif %}
        <form method="post" action="{{ url_for('admin_login') }}">
          <div class="field">
            <label for="password">Password</label>
            <input id="password" type="password" name="password" autocomplete="current-password" autofocus required>
          </div>
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
        --warn: #ffb770;
        --warn-soft: rgba(255, 183, 112, 0.12);
        --bad: #ff8c8c;
        --bad-soft: rgba(255, 140, 140, 0.12);
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
        max-width: 1120px;
        margin: 0 auto;
        padding: 0.9rem 0.9rem 2rem;
      }
      .topbar {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 0.85rem;
        align-items: center;
        margin-bottom: 0.9rem;
      }
      .brand {
        display: grid;
        gap: 0.22rem;
        justify-items: center;
        text-align: center;
      }
      .brand h1 {
        margin: 0.15rem 0 0;
        font-size: clamp(1.82rem, 5.6vw, 2.8rem);
        line-height: 0.96;
      }
      .brand p {
        margin: 0;
        color: var(--muted);
        max-width: 38rem;
        line-height: 1.38;
      }
      .top-actions {
        display: flex;
        gap: 0.65rem;
        justify-self: end;
      }
      .eyebrow,
      .state-pill {
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
      .panel {
        background: rgba(12, 20, 30, 0.96);
        border: 1px solid var(--line);
        border-radius: 16px;
        box-shadow: var(--shadow);
      }
      .panel + .panel {
        margin-top: 1rem;
      }
      .utility-button,
      .primary-button,
      .secondary-button,
      .ghost-button,
      .close-button,
      .room-choice,
      .view-tab {
        appearance: none;
        font: inherit;
        border-radius: 12px;
        text-decoration: none;
        transition: border-color 140ms ease, background 140ms ease, transform 140ms ease, color 140ms ease;
      }
      .utility-button,
      .ghost-button,
      .secondary-button,
      .close-button,
      .room-choice,
      .view-tab {
        border: 1px solid var(--line);
        background: var(--surface-2);
        color: var(--text);
      }
      .utility-button,
      .ghost-button,
      .secondary-button,
      .close-button {
        padding: 0.72rem 0.96rem;
        font-weight: 700;
        cursor: pointer;
      }
      .primary-button {
        border: 0;
        padding: 0.92rem 1.5rem;
        background: #dff4ff;
        color: #081018;
        font-weight: 800;
        cursor: pointer;
      }
      .primary-button,
      .ghost-button,
      .secondary-button {
        min-height: 3.55rem;
        min-width: 14.25rem;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        font-size: 1.04rem;
        padding: 0.92rem 1.45rem;
      }
      .secondary-button {
        color: var(--bad);
      }
      .auto-toggle.is-on {
        background: var(--good-soft);
        border-color: rgba(116, 221, 180, 0.34);
        color: var(--good);
        cursor: default;
      }
      .auto-toggle[disabled] {
        opacity: 1;
      }
      .auto-toggle.is-off {
        background: var(--surface-2);
        border-color: rgba(255, 183, 112, 0.22);
        color: var(--warn);
      }
      .utility-button:hover,
      .utility-button:focus-visible,
      .primary-button:hover,
      .primary-button:focus-visible,
      .ghost-button:hover,
      .ghost-button:focus-visible,
      .secondary-button:hover,
      .secondary-button:focus-visible,
      .close-button:hover,
      .close-button:focus-visible,
      .room-choice:hover,
      .room-choice:focus-visible {
        transform: translateY(-1px);
        border-color: var(--line-strong);
      }
      .control-shell {
        padding: 1rem;
        display: grid;
        gap: 0.85rem;
      }
      .transport-head {
        display: grid;
        gap: 0.35rem;
        justify-items: center;
        text-align: center;
      }
      .state-pill.good {
        color: var(--good);
        border-color: rgba(116, 221, 180, 0.28);
        background: var(--good-soft);
      }
      .state-pill.warn {
        color: var(--warn);
        border-color: rgba(255, 183, 112, 0.25);
        background: var(--warn-soft);
      }
      .transport-head h2 {
        margin: 0;
        font-size: clamp(1.55rem, 3.6vw, 2.05rem);
        line-height: 1.02;
      }
      .transport-head p {
        max-width: 32rem;
        font-size: 0.94rem;
      }
      .chips {
        display: flex;
        gap: 0.45rem;
        flex-wrap: wrap;
        justify-content: center;
      }
      .chip {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        padding: 0.26rem 0.68rem;
        background: rgba(135, 214, 255, 0.08);
        color: var(--accent);
        font-size: 0.8rem;
        font-weight: 600;
      }
      .chip.good {
        background: var(--good-soft);
        color: var(--good);
      }
      .chip.warn {
        background: var(--warn-soft);
        color: var(--warn);
      }
      .chip.bad {
        background: var(--bad-soft);
        color: var(--bad);
      }
      .transport-actions {
        display: flex;
        justify-content: center;
        gap: 0.7rem;
        flex-wrap: wrap;
      }
      .transport-actions form {
        margin: 0;
        display: flex;
      }
      .transport-actions form button,
      .transport-actions > button {
        min-width: 14.25rem;
      }
      .metric-strip {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 0.7rem;
      }
      .metric {
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        padding: 0.88rem;
        text-align: center;
      }
      .metric span {
        display: block;
        color: var(--muted);
        font-size: 0.78rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .metric strong {
        display: block;
        margin-top: 0.42rem;
        font-size: 1.58rem;
        line-height: 1.1;
      }
      .metric.is-wide strong {
        font-size: 1.28rem;
        line-height: 1.16;
      }
      .alert-stack {
        display: grid;
        gap: 0.75rem;
      }
      .monitor-panel {
        padding: 0.95rem 1rem;
      }
      .monitor-grid {
        display: grid;
        gap: 0.9rem;
        justify-items: center;
        width: 100%;
      }
      .monitor-title {
        display: flex;
        width: 100%;
        justify-content: space-between;
        align-items: center;
        gap: 1rem;
      }
      .monitor-title strong {
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        font-family: var(--mono);
      }
      .monitor-state {
        color: var(--muted);
        font-weight: 700;
      }
      .quality-card {
        width: 100%;
        border-radius: 14px;
        border: 1px solid var(--line);
        background: var(--surface);
        padding: 0.82rem;
      }
      .quality-head {
        display: flex;
        justify-content: space-between;
        gap: 0.8rem;
        align-items: center;
        color: var(--muted);
        font-size: 0.88rem;
        font-weight: 700;
      }
      .quality-label.good {
        color: var(--good);
      }
      .quality-label.warn {
        color: var(--warn);
      }
      .quality-label.bad {
        color: var(--bad);
      }
      .quality-bar {
        height: 0.62rem;
        border-radius: 999px;
        overflow: hidden;
        margin-top: 0.58rem;
        background: rgba(255, 255, 255, 0.07);
      }
      .quality-fill {
        display: block;
        width: 0%;
        height: 100%;
        border-radius: inherit;
        background: linear-gradient(90deg, var(--warn), var(--good));
        transition: width 180ms ease;
      }
      .quality-detail {
        margin: 0.48rem 0 0;
        color: var(--muted);
        font-size: 0.84rem;
        line-height: 1.4;
      }
      .meter-visual {
        display: flex;
        justify-content: center;
        gap: 0.68rem;
        align-items: end;
        min-height: 7.4rem;
      }
      .meter-column {
        display: flex;
        flex-direction: column-reverse;
        gap: 0.3rem;
      }
      .meter-segment {
        width: 1.15rem;
        height: 0.48rem;
        border-radius: 999px;
        background: rgba(116, 221, 180, 0.06);
        border: 1px solid rgba(116, 221, 180, 0.1);
        transition: background 120ms linear, border-color 120ms linear, transform 120ms linear;
      }
      .meter-segment.is-on {
        background: linear-gradient(180deg, #93efbf 0%, #62d99e 100%);
        border-color: rgba(116, 221, 180, 0.62);
      }
      .meter-segment.is-peak {
        background: #dfffea;
        border-color: rgba(223, 255, 234, 0.92);
        box-shadow: 0 0 0 1px rgba(7, 14, 20, 0.16) inset, 0 0 8px rgba(116, 221, 180, 0.18);
      }
      .banner {
        border-radius: 12px;
        padding: 0.82rem 0.95rem;
        font-weight: 600;
        border: 1px solid var(--line);
      }
      .banner.ok {
        background: var(--good-soft);
        color: var(--good);
      }
      .banner.warn {
        background: var(--warn-soft);
        color: var(--warn);
      }
      .banner.error {
        background: var(--bad-soft);
        color: var(--bad);
      }
      .view-tabs {
        display: flex;
        gap: 0.65rem;
        flex-wrap: wrap;
        margin-bottom: 0.85rem;
        justify-content: flex-start;
      }
      .view-tab {
        padding: 0.74rem 1rem;
        font-weight: 700;
        cursor: pointer;
      }
      .view-tab.is-active {
        border-color: rgba(135, 214, 255, 0.42);
        background: var(--surface-3);
        color: var(--accent);
      }
      .view-panel.is-hidden {
        display: none;
      }
      .data-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
        padding: 1rem;
      }
      .block,
      .reports {
        padding: 0.95rem;
      }
      .block {
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
      }
      .reports {
        padding-top: 0.85rem;
      }
      .section-head {
        display: flex;
        justify-content: space-between;
        gap: 1rem;
        align-items: baseline;
        margin-bottom: 0.8rem;
      }
      .section-head strong {
        font-size: 0.78rem;
        letter-spacing: 0.08em;
        text-transform: uppercase;
        color: var(--muted);
        font-family: var(--mono);
      }
      .section-head p {
        font-size: 0.9rem;
      }
      .listener-list {
        display: grid;
        gap: 0.6rem;
      }
      .listener-item {
        padding: 0.72rem 0.8rem;
        border-radius: 14px;
        background: var(--surface-2);
        border: 1px solid var(--line);
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
      .empty-state {
        color: var(--muted);
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
        font-family: var(--mono);
      }
      .history-link {
        color: var(--accent);
        text-decoration: none;
        font-weight: 700;
      }
      .room-dialog {
        width: min(100%, 640px);
        border: 1px solid var(--line);
        border-radius: 16px;
        background: rgba(12, 20, 30, 0.98);
        color: var(--text);
        box-shadow: var(--shadow);
        padding: 0;
      }
      .room-dialog::backdrop {
        background: rgba(3, 8, 14, 0.7);
        backdrop-filter: blur(6px);
      }
      .dialog-body {
        padding: 1rem;
        display: grid;
        gap: 1rem;
      }
      .dialog-head {
        display: grid;
        gap: 0.35rem;
        justify-items: center;
        text-align: center;
      }
      .dialog-head h2 {
        margin: 0;
        font-size: 1.5rem;
      }
      .room-grid {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.85rem;
      }
      .room-grid form {
        margin: 0;
      }
      .room-choice {
        width: 100%;
        display: grid;
        gap: 0.35rem;
        text-align: center;
        padding: 1rem;
        cursor: pointer;
      }
      .room-choice strong {
        font-size: 1.02rem;
      }
      .room-choice p {
        margin: 0;
      }
      .dialog-actions {
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
        .metric-strip,
        .data-grid {
          grid-template-columns: 1fr 1fr;
        }
      }
      @media (max-width: 720px) {
        main {
          padding: 0.8rem 0.8rem 2rem;
        }
        .metric-strip,
        .data-grid,
        .room-grid {
          grid-template-columns: 1fr;
        }
        .transport-actions,
        .dialog-actions {
          width: 100%;
        }
        .transport-actions form,
        .transport-actions button,
        .transport-actions a,
        .dialog-actions button,
        .primary-button,
        .secondary-button,
        .ghost-button {
          width: 100%;
        }
        .top-actions,
        .top-actions form {
          width: auto;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="topbar">
        <a class="utility-button" href="{{ settings_url }}">Settings</a>
        <div class="brand">
          <span class="eyebrow">Control Panel</span>
          <h1>{{ project_name }}</h1>
          <p>Start or stop the line, then watch the live call.</p>
        </div>
        <div class="top-actions">
          <form method="post" action="{{ logout_url }}">
            <button class="utility-button" type="submit">Sign Out</button>
          </form>
        </div>
      </section>

      <section class="panel control-shell" id="control-shell" data-admin-stream-url="{{ admin_stream_url }}">
        <div class="transport-head">
          <span class="state-pill {% if call_state.tone != 'idle' %}{{ call_state.tone }}{% endif %}">{{ call_state.label }}</span>
          <h2>{{ call_state.headline }}</h2>
          <p>{{ call_state.detail }}</p>
          {% if control_host %}
          <div class="chips">
            <span class="chip {% if control_host.source_online %}good{% else %}warn{% endif %}">
              {% if control_host.source_online %}Agent online{% else %}Agent offline{% endif %}
            </span>
            <span class="chip {% if control_host.broadcasting %}good{% elif control_host.desired_active %}warn{% endif %}">
              {% if control_host.broadcasting %}Live audio{% elif control_host.desired_active %}Starting{% else %}Idle{% endif %}
            </span>
            <span class="chip">{{ control_host.room_alias }}</span>
          </div>
          {% endif %}
        </div>

        <div class="transport-actions">
          {% if active_meeting %}
          <form method="post" action="{{ url_for('admin_stop_call') }}">
            <input type="hidden" name="room_slug" value="{{ active_room_slug }}">
            <button class="secondary-button" type="submit">Stop Call</button>
          </form>
          <button class="ghost-button" type="button" data-open-room-dialog>Change Room</button>
          {% else %}
          <button class="primary-button" type="button" data-open-room-dialog>Start Call</button>
          {% endif %}
          {% if control_host %}
          <form method="post" action="{{ url_for('admin_use_schedule') }}">
            <button
              class="ghost-button auto-toggle {% if control_host.manual_mode == 'auto' %}is-on{% else %}is-off{% endif %}"
              type="submit"
              {% if control_host.manual_mode == 'auto' %}disabled{% endif %}
            >
              Auto
            </button>
          </form>
          {% endif %}
        </div>

        <div class="metric-strip">
          <div class="metric">
            <span>Room</span>
            <strong>{{ control_host.room_alias if control_host else "No room selected" }}</strong>
          </div>
          <div class="metric">
            <span>Agent</span>
            <strong>{% if control_host and control_host.source_online %}Online{% elif control_host %}Offline{% else %}Waiting{% endif %}</strong>
          </div>
          <div class="metric is-wide">
            <span>Input</span>
            <strong>{{ control_host.current_device if control_host and control_host.current_device else "Waiting for input" }}</strong>
          </div>
          <div class="metric">
            <span>Listeners</span>
            <strong>{{ focus_host.listener_count if focus_host else 0 }}</strong>
          </div>
        </div>

        <div class="alert-stack">
          {% if message %}
          <div class="banner ok">{{ message }}</div>
          {% endif %}
          {% if error %}
          <div class="banner error">{{ error }}</div>
          {% endif %}
          {% if control_host %}
          <div class="banner {% if control_host.manual_mode == 'auto' %}ok{% else %}warn{% endif %}">
            {% if control_host.manual_mode == 'auto' %}
            Auto is on. The saved schedule can start or stop this room.
            {% elif control_host.manual_mode == 'force_on' %}
            Auto is off. Manual start is holding {{ control_host.room_alias }} on.
            {% else %}
            Auto is off. Manual stop is holding {{ control_host.room_alias }} off.
            {% endif %}
          </div>
          {% endif %}
          {% for conflict in conflicts %}
          <div class="banner error">{{ conflict }}</div>
          {% endfor %}
          {% if focus_host and focus_host.last_error %}
          <div class="banner error">{{ focus_host.last_error }}</div>
          {% endif %}
        </div>
      </section>

      <section
        class="panel monitor-panel"
        id="admin-monitor-panel"
        data-signal-level-percent="{{ (focus_host.signal_level_percent if focus_host else 0)|default(0, true) }}"
        data-signal-peak-percent="{{ (focus_host.signal_peak_percent if focus_host else 0)|default(0, true) }}"
        data-signal-peak-db="{{ (focus_host.signal_peak_db if focus_host else -90)|default(-90, true) }}"
        data-connection-quality-percent="{{ (focus_host.connection_quality_percent if focus_host else 0)|default(0, true) }}"
        data-connection-quality-label="{{ (focus_host.connection_quality_label if focus_host else 'Idle')|default('Idle', true) }}"
        data-connection-quality-state="{{ (focus_host.connection_quality_state if focus_host else 'idle')|default('idle', true) }}"
        data-connection-quality-detail="{{ (focus_host.connection_quality_detail if focus_host else 'No active web stream.')|default('No active web stream.', true) }}"
      >
        <div class="monitor-grid">
          <div class="monitor-title">
            <strong>Signal monitor</strong>
            <span class="monitor-state" id="admin-meter-label">
              {% if focus_host and (focus_host.signal_peak_percent or 0) > 0.5 %}
              {{ (focus_host.signal_peak_db or -90)|round(0, 'common')|int }} dB
              {% else %}
              Idle
              {% endif %}
            </span>
          </div>
          <div class="meter-visual" id="admin-meter-visual">
            <div class="meter-column" data-admin-meter-column="left">
              {% for band in ["low", "low", "low", "low", "mid", "mid", "mid", "mid", "mid", "high", "high", "high", "peak", "peak"] %}
              <span class="meter-segment" data-band="{{ band }}"></span>
              {% endfor %}
            </div>
            <div class="meter-column" data-admin-meter-column="right">
              {% for band in ["low", "low", "low", "low", "mid", "mid", "mid", "mid", "mid", "high", "high", "high", "peak", "peak"] %}
              <span class="meter-segment" data-band="{{ band }}"></span>
              {% endfor %}
            </div>
          </div>
          <div class="quality-card" aria-live="polite">
            <div class="quality-head">
              <span>Public stream quality</span>
              <span
                class="quality-label {{ (focus_host.connection_quality_state if focus_host else 'warn')|default('warn', true) }}"
                id="admin-connection-label"
              >
                {{ (focus_host.connection_quality_label if focus_host else 'Idle')|default('Idle', true) }}
              </span>
            </div>
            <div class="quality-bar" aria-hidden="true">
              <span
                class="quality-fill"
                id="admin-connection-fill"
                style="width: {{ (focus_host.connection_quality_percent if focus_host else 0)|default(0, true) }}%;"
              ></span>
            </div>
            <p class="quality-detail" id="admin-connection-detail">
              {{ (focus_host.connection_quality_detail if focus_host else 'No active web stream.')|default('No active web stream.', true) }}
            </p>
          </div>
        </div>
      </section>

      <section class="panel reports" id="reports-panel">
        <div class="view-tabs">
          <button class="view-tab is-active" type="button" data-view-tab="live">Live Conference</button>
          <button class="view-tab" type="button" data-view-tab="history">Past Conferences</button>
        </div>

        <div class="view-panel" data-view-panel="live">
          <article class="block">
            <div class="section-head">
              <strong>Current callers</strong>
              <p>{% if focus_host %}{{ focus_host.room_alias }}{% else %}No room selected{% endif %}</p>
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
        </div>

        <div class="view-panel is-hidden" data-view-panel="history">
          <div class="section-head">
            <strong>Past conferences</strong>
            <p>Meeting stats and report downloads</p>
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
        </div>
      </section>

      <dialog class="room-dialog" id="room-dialog">
        <div class="dialog-body">
          <div class="dialog-head">
            <span class="eyebrow">Start Call</span>
            <h2>Choose a room</h2>
            <p>Only one room runs at a time.</p>
          </div>
          <div class="room-grid">
            {% for room in room_options %}
            <form method="post" action="{{ url_for('admin_start_call') }}">
              <input type="hidden" name="room_slug" value="{{ room.slug }}">
              <button class="room-choice" type="submit">
                <strong>{{ room.alias }}</strong>
                <p>{{ room.label }}</p>
                <div class="chips">
                  <span class="chip {% if room.online %}good{% else %}warn{% endif %}">{% if room.online %}Agent online{% else %}Agent offline{% endif %}</span>
                  <span class="chip {% if room.active %}good{% endif %}">{% if room.active %}Live{% else %}Standby{% endif %}</span>
                </div>
              </button>
            </form>
            {% endfor %}
          </div>
          <div class="dialog-actions">
            <button class="close-button" type="button" data-close-room-dialog>Close</button>
          </div>
        </div>
      </dialog>

      <script>
        (() => {
          const storageKey = "roomcast-control-view";
          let adminMonitorPanel = null;
          let adminMeterLabel = null;
          let adminMeterColumns = [];
          let adminConnectionLabel = null;
          let adminConnectionFill = null;
          let adminConnectionDetail = null;
          let refreshInFlight = false;

          function bindAdminMonitorElements() {
            adminMonitorPanel = document.getElementById("admin-monitor-panel");
            adminMeterLabel = document.getElementById("admin-meter-label");
            adminMeterColumns = [...document.querySelectorAll("[data-admin-meter-column]")];
            adminConnectionLabel = document.getElementById("admin-connection-label");
            adminConnectionFill = document.getElementById("admin-connection-fill");
            adminConnectionDetail = document.getElementById("admin-connection-detail");
          }

          const adminMeterFloorDb = -90;

          function paintMeter(columns, percent, peakPercent = 0) {
            const segmentCount = columns[0]?.children.length || 14;
            const litSegments = Math.round((Math.max(0, Math.min(100, percent)) / 100) * segmentCount);
            const peakIndex = peakPercent > 0 ? Math.min(segmentCount - 1, Math.max(0, Math.round((peakPercent / 100) * segmentCount) - 1)) : -1;
            columns.forEach((column) => {
              [...column.children].forEach((segment, index) => {
                segment.classList.toggle("is-on", index < litSegments);
                segment.classList.toggle("is-peak", index === peakIndex);
              });
            });
          }

          function applyAdminSignalTelemetry() {
            if (!adminMonitorPanel || !adminMeterLabel || !adminMeterColumns.length) {
              return;
            }
            const percent = Number(adminMonitorPanel.dataset.signalLevelPercent || 0);
            const peakPercent = Number(adminMonitorPanel.dataset.signalPeakPercent || 0);
            const peakDb = Number(adminMonitorPanel.dataset.signalPeakDb);
            paintMeter(adminMeterColumns, percent, peakPercent);
            if (!Number.isFinite(peakDb) || peakPercent <= 0.5) {
              adminMeterLabel.textContent = "Idle";
              return;
            }
            adminMeterLabel.textContent = `${Math.round(Math.max(adminMeterFloorDb, Math.min(0, peakDb)))} dB`;
          }

          function applyAdminConnectionTelemetry() {
            if (!adminMonitorPanel || !adminConnectionLabel || !adminConnectionFill || !adminConnectionDetail) {
              return;
            }
            const percent = Math.max(0, Math.min(100, Number(adminMonitorPanel.dataset.connectionQualityPercent || 0)));
            const label = adminMonitorPanel.dataset.connectionQualityLabel || "Idle";
            const state = adminMonitorPanel.dataset.connectionQualityState || "idle";
            const detail = adminMonitorPanel.dataset.connectionQualityDetail || "No active web stream.";
            adminConnectionFill.style.width = `${Math.round(percent)}%`;
            adminConnectionLabel.textContent = label;
            adminConnectionLabel.className = "quality-label";
            if (state === "good") {
              adminConnectionLabel.classList.add("good");
            } else if (state === "warn") {
              adminConnectionLabel.classList.add("warn");
            } else if (state === "bad") {
              adminConnectionLabel.classList.add("bad");
            }
            adminConnectionDetail.textContent = detail;
          }

          function currentDialog() {
            return document.getElementById("room-dialog");
          }

          function bindRoomDialogControls() {
            const roomDialog = currentDialog();
            document.querySelectorAll("[data-open-room-dialog]").forEach((button) => {
              button.onclick = () => {
                if (roomDialog) {
                  roomDialog.showModal();
                }
              };
            });
            document.querySelectorAll("[data-close-room-dialog]").forEach((button) => {
              button.onclick = () => {
                if (roomDialog) {
                  roomDialog.close();
                }
              };
            });
          }

          function activateView(view) {
            document.querySelectorAll("[data-view-tab]").forEach((tab) => {
              tab.classList.toggle("is-active", tab.dataset.viewTab === view);
            });
            document.querySelectorAll("[data-view-panel]").forEach((panel) => {
              panel.classList.toggle("is-hidden", panel.dataset.viewPanel !== view);
            });
            window.localStorage.setItem(storageKey, view);
          }

          function bindTabs() {
            const initialView = window.localStorage.getItem(storageKey) || "live";
            activateView(initialView);
            document.querySelectorAll("[data-view-tab]").forEach((tab) => {
              tab.onclick = () => activateView(tab.dataset.viewTab);
            });
          }

          function clearFlashQuery() {
            const url = new URL(window.location.href);
            if (!url.searchParams.has("message") && !url.searchParams.has("error")) {
              return;
            }
            window.setTimeout(() => {
              url.searchParams.delete("message");
              url.searchParams.delete("error");
              const next = `${url.pathname}${url.search}${url.hash}`;
              window.history.replaceState({}, "", next);
            }, 1400);
          }

          async function refreshPanel() {
            const roomDialog = currentDialog();
            if (refreshInFlight || document.hidden || (roomDialog && roomDialog.open)) {
              return;
            }
            refreshInFlight = true;
            try {
              const refreshUrl = new URL(window.location.href);
              refreshUrl.searchParams.set("_ts", String(Date.now()));
              const response = await fetch(refreshUrl.toString(), {
                cache: "no-store",
                headers: { "X-Requested-With": "roomcast-refresh" },
              });
              if (!response.ok) {
                return;
              }
              const html = await response.text();
              const doc = new DOMParser().parseFromString(html, "text/html");
              const nextControl = doc.getElementById("control-shell");
              const nextReports = doc.getElementById("reports-panel");
              const nextDialog = doc.getElementById("room-dialog");
              const nextMonitorPanel = doc.getElementById("admin-monitor-panel");
              const currentControl = document.getElementById("control-shell");
              const currentReports = document.getElementById("reports-panel");
              const currentRoomDialog = currentDialog();
              const currentMonitorPanel = document.getElementById("admin-monitor-panel");

              if (nextControl && currentControl) {
                if (nextMonitorPanel && currentMonitorPanel) {
                  currentMonitorPanel.dataset.signalLevelPercent = nextMonitorPanel.dataset.signalLevelPercent || "0";
                  currentMonitorPanel.dataset.signalPeakPercent = nextMonitorPanel.dataset.signalPeakPercent || "0";
                  currentMonitorPanel.dataset.signalPeakDb = nextMonitorPanel.dataset.signalPeakDb || "-90";
                  currentMonitorPanel.dataset.connectionQualityPercent = nextMonitorPanel.dataset.connectionQualityPercent || "0";
                  currentMonitorPanel.dataset.connectionQualityLabel = nextMonitorPanel.dataset.connectionQualityLabel || "Idle";
                  currentMonitorPanel.dataset.connectionQualityState = nextMonitorPanel.dataset.connectionQualityState || "idle";
                  currentMonitorPanel.dataset.connectionQualityDetail = nextMonitorPanel.dataset.connectionQualityDetail || "No active web stream.";
                  const nextMonitorLabel = nextMonitorPanel.querySelector("#admin-meter-label");
                  const currentMonitorLabel = currentMonitorPanel.querySelector("#admin-meter-label");
                  if (nextMonitorLabel && currentMonitorLabel) {
                    currentMonitorLabel.textContent = nextMonitorLabel.textContent;
                  }
                }
                currentControl.replaceWith(nextControl);
              }
              if (nextReports && currentReports) {
                currentReports.replaceWith(nextReports);
              }
              if (nextDialog && currentRoomDialog && !currentRoomDialog.open) {
                currentRoomDialog.innerHTML = nextDialog.innerHTML;
              }

              bindRoomDialogControls();
              bindTabs();
              bindAdminMonitorElements();
              applyAdminSignalTelemetry();
              applyAdminConnectionTelemetry();
            } catch (error) {
              /* ignore transient refresh issues */
            } finally {
              refreshInFlight = false;
            }
          }

          bindAdminMonitorElements();
          applyAdminSignalTelemetry();
          applyAdminConnectionTelemetry();
          bindRoomDialogControls();
          bindTabs();
          clearFlashQuery();

          window.setInterval(refreshPanel, 750);

          document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
              refreshPanel();
            }
          });
        })();
      </script>
    </main>
  </body>
</html>
"""


ADMIN_PANEL_TEMPLATE = ADMIN_TEMPLATE


DIAGNOSTIC_AUDIO_TEMPLATE = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{ project_name }} Diagnostics</title>
    <style>
      :root {
        color-scheme: dark;
        --bg: #071018;
        --surface: #101b27;
        --surface-2: #172536;
        --text: #eef5fb;
        --muted: #9cadbd;
        --line: #263a50;
        --accent: #87d6ff;
        --good: #77e0b7;
        --bad: #ff9b9b;
        --mono: "IBM Plex Mono", "SFMono-Regular", Consolas, monospace;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(135, 214, 255, 0.12), transparent 28rem),
          linear-gradient(180deg, #0d1722, var(--bg));
        min-height: 100vh;
      }
      main {
        max-width: 980px;
        margin: 0 auto;
        padding: 1rem;
      }
      .topbar {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 0.8rem;
        align-items: center;
        margin-bottom: 1rem;
      }
      .brand {
        text-align: center;
      }
      .brand h1 {
        margin: 0;
        font-size: clamp(1.8rem, 5vw, 3rem);
      }
      .brand p,
      p {
        margin: 0;
        color: var(--muted);
        line-height: 1.5;
      }
      .top-actions,
      .button-row,
      .sample-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 0.65rem;
        align-items: center;
      }
      button,
      .button {
        appearance: none;
        border: 1px solid var(--line);
        border-radius: 12px;
        background: var(--surface-2);
        color: var(--text);
        padding: 0.72rem 1rem;
        font: inherit;
        font-weight: 700;
        text-decoration: none;
        cursor: pointer;
      }
      button:disabled {
        cursor: not-allowed;
        opacity: 0.55;
      }
      .primary {
        background: var(--accent);
        color: #061018;
        border-color: transparent;
      }
      .danger {
        border-color: rgba(255, 155, 155, 0.45);
        color: var(--bad);
      }
      .panel {
        background: rgba(16, 27, 39, 0.96);
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 1rem;
        box-shadow: 0 18px 44px rgba(0, 0, 0, 0.25);
      }
      .panel + .panel {
        margin-top: 1rem;
      }
      .eyebrow {
        display: inline-flex;
        width: fit-content;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 0.22rem 0.58rem;
        color: var(--accent);
        font: 700 0.72rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      .recorder-grid {
        display: grid;
        grid-template-columns: minmax(0, 1fr) minmax(14rem, 0.5fr);
        gap: 1rem;
        align-items: start;
      }
      .status {
        border: 1px solid var(--line);
        border-radius: 14px;
        background: rgba(255, 255, 255, 0.03);
        padding: 0.85rem;
        color: var(--muted);
      }
      .status strong {
        color: var(--text);
      }
      .timer {
        font: 700 2rem var(--mono);
        color: var(--accent);
      }
      textarea {
        width: 100%;
        min-height: 5rem;
        resize: vertical;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: #0b1420;
        color: var(--text);
        padding: 0.8rem;
        font: inherit;
      }
      .banner {
        border-radius: 14px;
        padding: 0.85rem 1rem;
        margin-bottom: 1rem;
      }
      .banner.ok {
        color: var(--good);
        background: rgba(119, 224, 183, 0.12);
        border: 1px solid rgba(119, 224, 183, 0.35);
      }
      .banner.error {
        color: var(--bad);
        background: rgba(255, 155, 155, 0.12);
        border: 1px solid rgba(255, 155, 155, 0.35);
      }
      .sample-list {
        display: grid;
        gap: 0.8rem;
      }
      .sample-card {
        display: grid;
        gap: 0.65rem;
        border: 1px solid var(--line);
        border-radius: 15px;
        padding: 0.85rem;
        background: rgba(255, 255, 255, 0.025);
      }
      .sample-meta {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        color: var(--muted);
        font-size: 0.92rem;
      }
      audio {
        width: 100%;
      }
      input[type="file"] {
        max-width: 100%;
      }
      @media (max-width: 760px) {
        .topbar,
        .recorder-grid {
          grid-template-columns: 1fr;
        }
        .brand {
          text-align: left;
        }
        .top-actions {
          justify-content: flex-start;
        }
        button,
        .button {
          width: 100%;
          text-align: center;
        }
      }
    </style>
  </head>
  <body>
    <main>
      <section class="topbar">
        <a class="button" href="{{ panel_url }}">Panel</a>
        <div class="brand">
          <span class="eyebrow">Diagnostics</span>
          <h1>Record What You Hear</h1>
          <p>Use this laptop microphone to capture the phone/webcall audio exactly as you hear it.</p>
        </div>
        <div class="top-actions">
          <a class="button" href="{{ settings_url }}">Settings</a>
          <form method="post" action="{{ logout_url }}">
            <button type="submit">Sign Out</button>
          </form>
        </div>
      </section>

      {% if message %}
      <div class="banner ok">{{ message }}</div>
      {% elif error %}
      <div class="banner error">{{ error }}</div>
      {% endif %}

      <section class="panel">
        <div class="recorder-grid">
          <div>
            <span class="eyebrow">Capture</span>
            <h2>Mic Recording</h2>
            <p>Put the phone on speaker near the laptop mic, start the call, then record 20 to 60 seconds of the problem. Browser echo cancellation/noise suppression is requested off where supported.</p>
            <div style="height: 1rem"></div>
            <textarea id="note" placeholder="Optional note, e.g. OPUS/16k phone call, static starts at 12s"></textarea>
            <div style="height: 1rem"></div>
            <div class="button-row">
              <button class="primary" type="button" id="record-button">Start Recording</button>
              <button class="danger" type="button" id="stop-button" disabled>Stop and Upload</button>
            </div>
          </div>
          <aside class="status">
            <strong id="status-label">Ready</strong>
            <div class="timer" id="timer">00:00</div>
            <p id="status-detail">The browser will ask for microphone permission.</p>
          </aside>
        </div>
      </section>

      <section class="panel">
        <span class="eyebrow">Upload</span>
        <h2>Manual Audio Upload</h2>
        <p>If browser recording fails, upload a WAV/MP3/WebM file here. It will be normalized to WAV for analysis.</p>
        <div style="height: 1rem"></div>
        <form method="post" action="{{ upload_url }}" enctype="multipart/form-data">
          <div class="button-row">
            <input type="file" name="audio" accept="audio/*" required>
            <input type="hidden" name="note" value="manual upload">
            <button type="submit">Upload File</button>
          </div>
        </form>
      </section>

      <section class="panel">
        <span class="eyebrow">Samples</span>
        <h2>Latest Recordings</h2>
        <div class="sample-list">
          {% for sample in samples %}
          <article class="sample-card">
            <audio controls preload="none" src="{{ sample.audio_url }}"></audio>
            <div class="sample-meta">
              <span>{{ sample.created_at }}</span>
              <span>{{ sample.size_kb }} KB</span>
              {% if sample.original_filename %}<span>{{ sample.original_filename }}</span>{% endif %}
            </div>
            {% if sample.note %}<p>{{ sample.note }}</p>{% endif %}
            <div class="sample-actions">
              <a class="button" href="{{ sample.audio_url }}">Open WAV</a>
              <span class="sample-meta">{{ sample.filename }}</span>
            </div>
          </article>
          {% else %}
          <p>No diagnostic recordings yet.</p>
          {% endfor %}
        </div>
      </section>
    </main>

    <script>
      (() => {
        const recordButton = document.getElementById("record-button");
        const stopButton = document.getElementById("stop-button");
        const statusLabel = document.getElementById("status-label");
        const statusDetail = document.getElementById("status-detail");
        const timer = document.getElementById("timer");
        const note = document.getElementById("note");
        const uploadUrl = {{ upload_url|tojson }};
        let recorder = null;
        let stream = null;
        let chunks = [];
        let startedAt = 0;
        let timerHandle = null;
        let maxRecordingTimer = null;

        function setStatus(label, detail) {
          statusLabel.textContent = label;
          statusDetail.textContent = detail;
        }

        function renderTimer() {
          const elapsed = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
          const minutes = String(Math.floor(elapsed / 60)).padStart(2, "0");
          const seconds = String(elapsed % 60).padStart(2, "0");
          timer.textContent = `${minutes}:${seconds}`;
        }

        function stopTracks() {
          if (stream) {
            stream.getTracks().forEach((track) => track.stop());
          }
          stream = null;
        }

        async function upload(blob) {
          if (!blob || blob.size <= 0) {
            throw new Error("The browser did not capture any audio. Try again or use Manual Audio Upload.");
          }
          const formData = new FormData();
          const extension = blob.type.includes("ogg") ? "ogg" : "webm";
          formData.append("audio", blob, `hearing-sample.${extension}`);
          formData.append("note", note.value || "");
          const controller = new AbortController();
          const timeout = window.setTimeout(() => controller.abort(), 75000);
          let response;
          try {
            response = await fetch(uploadUrl, {
              method: "POST",
              body: formData,
              credentials: "same-origin",
              headers: { "X-Requested-With": "fetch" },
              signal: controller.signal,
            });
          } catch (error) {
            if (error && error.name === "AbortError") {
              throw new Error("Upload timed out. Use Manual Audio Upload or try a shorter recording.");
            }
            throw error;
          } finally {
            window.clearTimeout(timeout);
          }
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            throw new Error(payload.error || "Upload failed.");
          }
          return payload;
        }

        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
          recordButton.disabled = true;
          setStatus("Unavailable", "This browser does not support microphone recording. Use the manual upload option.");
          return;
        }

        recordButton.onclick = async () => {
          try {
            stream = await navigator.mediaDevices.getUserMedia({
              audio: {
                echoCancellation: false,
                noiseSuppression: false,
                autoGainControl: false,
                channelCount: 1,
              },
            });
            const preferredTypes = [
              "audio/webm;codecs=opus",
              "audio/ogg;codecs=opus",
              "audio/webm",
            ];
            const mimeType = preferredTypes.find((type) => MediaRecorder.isTypeSupported(type)) || "";
            recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
            chunks = [];
            recorder.onerror = (event) => {
              clearInterval(timerHandle);
              window.clearTimeout(maxRecordingTimer);
              timerHandle = null;
              maxRecordingTimer = null;
              stopTracks();
              recordButton.disabled = false;
              stopButton.disabled = true;
              setStatus("Recording failed", (event.error && event.error.message) || "Try the manual upload option.");
            };
            recorder.ondataavailable = (event) => {
              if (event.data && event.data.size > 0) {
                chunks.push(event.data);
              }
            };
            recorder.onstop = async () => {
              clearInterval(timerHandle);
              window.clearTimeout(maxRecordingTimer);
              timerHandle = null;
              maxRecordingTimer = null;
              try {
                setStatus("Uploading", "Saving diagnostic WAV on the NAS.");
                const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
                await upload(blob);
                setStatus("Saved", "Reloading latest recordings.");
                window.location.reload();
              } catch (error) {
                setStatus("Upload failed", error.message || "Try the manual upload option.");
                recordButton.disabled = false;
                stopButton.disabled = true;
              } finally {
                stopTracks();
              }
            };
            recorder.start(1000);
            startedAt = Date.now();
            renderTimer();
            timerHandle = setInterval(renderTimer, 250);
            maxRecordingTimer = window.setTimeout(() => {
              if (recorder && recorder.state === "recording") {
                stopButton.disabled = true;
                setStatus("Processing", "Auto-stopping at 90 seconds before upload.");
                recorder.stop();
              }
            }, 90000);
            recordButton.disabled = true;
            stopButton.disabled = false;
            setStatus("Recording", "Stop when you have captured enough of the issue. Auto-stops at 90 seconds.");
          } catch (error) {
            stopTracks();
            setStatus("Mic blocked", "Allow microphone access, then try again.");
          }
        };

        stopButton.onclick = () => {
          if (recorder && recorder.state === "recording") {
            stopButton.disabled = true;
            setStatus("Processing", "Preparing recording for upload.");
            if (typeof recorder.requestData === "function") {
              recorder.requestData();
            }
            recorder.stop();
          }
        };
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
        max-width: 1120px;
        margin: 0 auto;
        padding: 0.9rem 0.9rem 2.15rem;
      }
      .topbar {
        display: grid;
        grid-template-columns: auto 1fr auto;
        gap: 0.85rem;
        align-items: center;
        margin-bottom: 0.9rem;
      }
      .brand {
        display: grid;
        gap: 0.2rem;
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
        display: inline-flex;
        align-items: center;
        justify-content: center;
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
        padding: 0.92rem 1rem;
      }
      .is-hidden {
        display: none !important;
      }
      .room-tabs {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 0.75rem;
      }
      #settings-shell {
        display: grid;
        gap: 1rem;
        margin-top: 1rem;
      }
      .room-tab {
        display: grid;
        gap: 0.15rem;
        padding: 0.82rem 0.95rem;
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
        padding: 1.12rem 1.12rem 1.2rem;
      }
      .prompt-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(17rem, 1fr));
        gap: 0.85rem;
      }
      .prompt-disclosure {
        display: grid;
        gap: 0.85rem;
      }
      .prompt-disclosure summary {
        width: fit-content;
        cursor: pointer;
        color: var(--accent);
        font-weight: 800;
      }
      .prompt-disclosure[open] summary {
        margin-bottom: 0.85rem;
      }
      .prompt-card {
        display: grid;
        gap: 0.75rem;
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        padding: 1rem;
      }
      .prompt-card h3 {
        margin: 0;
        font-size: 1rem;
      }
      .prompt-text {
        color: var(--muted);
        font-size: 0.88rem;
        line-height: 1.45;
      }
      .prompt-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 0.55rem;
        align-items: center;
      }
      .prompt-actions form {
        display: flex;
        flex-wrap: wrap;
        gap: 0.55rem;
        align-items: center;
      }
      .prompt-actions input[type="file"] {
        max-width: 13rem;
        color: var(--muted);
      }
      .prompt-status {
        color: var(--muted);
        font-size: 0.82rem;
        font-family: var(--mono);
      }
      .prompt-card audio {
        width: 100%;
      }
      .workspace.is-hidden {
        display: none;
      }
      .workspace form {
        display: grid;
        gap: 1.1rem;
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
        grid-template-columns: minmax(0, 1.55fr) minmax(18rem, 1fr);
        gap: 1rem;
        align-items: start;
      }
      .workspace-stack {
        display: grid;
        gap: 1rem;
        min-width: 0;
      }
      .block,
      .sidebar-note {
        border: 1px solid var(--line);
        border-radius: 14px;
        background: var(--surface);
        padding: 1rem;
        min-width: 0;
        overflow: hidden;
      }
      .block-head {
        display: flex;
        justify-content: space-between;
        gap: 0.8rem;
        align-items: start;
        margin-bottom: 0.85rem;
        min-width: 0;
        flex-wrap: wrap;
      }
      .block-head > * {
        min-width: 0;
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
        overflow-wrap: anywhere;
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
        min-width: 0;
        max-width: 100%;
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
        max-width: 100%;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .setup-grid {
        display: grid;
        gap: 0.75rem;
      }
      .setup-facts {
        display: grid;
        gap: 0.45rem;
        border-radius: 12px;
        background: var(--surface-2);
        border: 1px solid var(--line);
        padding: 0.78rem 0.85rem;
        font-family: var(--mono);
        font-size: 0.78rem;
        overflow-wrap: anywhere;
      }
      .setup-command {
        margin: 0;
        border-radius: 12px;
        background: #07111d;
        border: 1px solid var(--line);
        padding: 0.78rem 0.85rem;
        color: #d9f1ff;
        font: 0.75rem/1.45 var(--mono);
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .setup-label {
        color: var(--muted);
        font: 700 0.72rem var(--mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }
      input[type="time"],
      select {
        width: 100%;
        min-width: 0;
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
        min-width: 0;
      }
      .order-row {
        display: grid;
        grid-template-columns: 1fr;
        align-items: start;
        gap: 0.75rem;
        min-width: 0;
      }
      .order-row span {
        color: var(--muted);
        font-size: 0.74rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-family: var(--mono);
      }
      .order-row select {
        width: 100%;
      }
      .schedule-shell {
        display: grid;
        gap: 0.9rem;
        min-width: 0;
      }
      .schedule-head,
      .schedule-row {
        display: grid;
        grid-template-columns: 5.25rem 4.9rem minmax(0, 1fr) minmax(0, 1fr) 7.2rem;
        gap: 0.8rem;
        align-items: stretch;
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
        padding: 0.74rem;
        overflow: hidden;
      }
      .schedule-cell {
        display: grid;
        gap: 0.35rem;
      }
      .schedule-toggle {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 0.52rem;
        width: 100%;
        min-width: 0;
        min-height: 3.35rem;
        border-radius: 10px;
        border: 1px solid var(--line);
        background: var(--surface-2);
        color: var(--text);
        font: inherit;
        font-weight: 800;
        cursor: pointer;
        transition: border-color 140ms ease, background 140ms ease, color 140ms ease, transform 140ms ease;
      }
      .schedule-toggle:hover,
      .schedule-toggle:focus-visible {
        border-color: var(--line-strong);
        transform: translateY(-1px);
      }
      .schedule-toggle.is-on {
        border-color: rgba(116, 221, 180, 0.42);
        background: rgba(116, 221, 180, 0.14);
        color: var(--good);
      }
      .schedule-toggle.is-off {
        color: var(--muted);
      }
      .schedule-toggle-dot {
        width: 0.58rem;
        height: 0.58rem;
        border-radius: 999px;
        background: currentColor;
        opacity: 0.72;
      }
      .row-button {
        align-self: stretch;
        min-width: 0;
        width: 100%;
      }
      .schedule-cell > span {
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
        padding-top: 0.35rem;
      }
      @media (max-width: 1120px) {
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
          grid-template-columns: 1fr;
        }
        .schedule-head {
          display: none;
        }
        .schedule-row {
          grid-template-columns: 1fr;
          padding: 0.85rem;
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

      <section id="settings-shell">
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
          data-host-slug="{{ host.slug }}"
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
                  <p data-current-device>{{ host.current_device or "Waiting for the laptop agent" }}</p>
                </div>
                <div class="device-list {% if not host.known_devices %}is-hidden{% endif %}" data-known-device-list>
                  {% for device in host.known_devices %}
                  <span class="device-chip">{{ device }}</span>
                  {% endfor %}
                </div>
                <div class="input-empty {% if host.known_devices %}is-hidden{% endif %}" data-known-device-empty>
                  <p class="field-note">The laptop agent has not reported its inputs yet. This list updates automatically when it checks in.</p>
                </div>
              </section>

              <section class="block">
                <div class="block-head">
                  <strong>Schedule</strong>
                  <p>Enter call start times here. Standard start is 10 minutes before service.</p>
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
                        <input type="hidden" name="schedule_enabled" value="{{ '1' if row.enabled else '0' }}" data-schedule-enabled>
                        <button class="schedule-toggle {% if row.enabled %}is-on{% else %}is-off{% endif %}" type="button" data-schedule-toggle aria-pressed="{{ 'true' if row.enabled else 'false' }}">
                          <span class="schedule-toggle-dot" aria-hidden="true"></span>
                          <span data-schedule-toggle-label>{{ "ON" if row.enabled else "OFF" }}</span>
                        </button>
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
                        <input type="hidden" name="schedule_enabled" value="1" data-schedule-enabled>
                        <button class="schedule-toggle is-on" type="button" data-schedule-toggle aria-pressed="true">
                          <span class="schedule-toggle-dot" aria-hidden="true"></span>
                          <span data-schedule-toggle-label>ON</span>
                        </button>
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
                        <input type="time" name="schedule_start" value="18:50">
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
                      Keep seasonal rows Off until needed. Automatic stopping waits for silence after the scheduled end.
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
                <div class="order-list" data-order-list>
                  {% for slot in host.device_slots %}
                  <label class="order-row">
                    <span>{{ loop.index }}{% if loop.index == 1 %}st{% elif loop.index == 2 %}nd{% elif loop.index == 3 %}rd{% else %}th{% endif %} choice</span>
                    <select name="device_order" data-order-select data-slot-index="{{ loop.index0 }}">
                      <option value="">No preference</option>
                      {% for device in host.device_options %}
                      <option value="{{ device }}" {% if slot == device %}selected{% endif %}>{{ device }}</option>
                      {% endfor %}
                    </select>
                  </label>
                  {% endfor %}
                  <p class="field-note">The device list comes from the laptop agent. If a new input appears, place it in the order you want and save once.</p>
                </div>
              </section>

              <section class="block">
                <div class="block-head">
                  <strong>Agent swap</strong>
                  <p>Use these values to move {{ host.room_alias }} to another Windows or macOS computer.</p>
                </div>
                <div class="setup-grid">
                  <div class="setup-facts">
                    <div><span class="setup-label">Server</span><br>{{ host.agent_setup.server_url }}</div>
                    <div><span class="setup-label">Host slug</span><br>{{ host.agent_setup.host_slug }}</div>
                    <div><span class="setup-label">Token</span><br>{{ host.agent_setup.token }}</div>
                  </div>
                  <p class="field-note">For a replacement, stop the old agent first, then install the new computer with this same host slug and token. Do not run two computers with the same host slug at the same time.</p>
                  <div>
                    <div class="setup-label">Windows install</div>
                    <pre class="setup-command">{{ host.agent_setup.windows_command }}</pre>
                  </div>
                  <div>
                    <div class="setup-label">macOS install</div>
                    <pre class="setup-command">{{ host.agent_setup.macos_command }}</pre>
                  </div>
                </div>
              </section>
            </div>
          </div>

          <div class="save-bar">
            <button class="primary-button" type="submit">Save Settings</button>
          </div>
        </form>
        </section>
        {% endfor %}
      </section>

      <script>
        (() => {
          const storageKey = "roomcast-settings-room";
          const shellId = "settings-shell";
          let dirty = false;
          let refreshInFlight = false;
          let runtimeRefreshInFlight = false;

          function escapeHtml(value) {
            return String(value ?? "")
              .replaceAll("&", "&amp;")
              .replaceAll("<", "&lt;")
              .replaceAll(">", "&gt;")
              .replaceAll('"', "&quot;")
              .replaceAll("'", "&#39;");
          }

          function selectedRoomSlug() {
            const activeTab = document.querySelector("[data-room-tab].is-active");
            return activeTab ? activeTab.dataset.roomTab : "";
          }

          function syncRoomQuery(roomSlug) {
            const url = new URL(window.location.href);
            if (roomSlug) {
              url.searchParams.set("room", roomSlug);
            } else {
              url.searchParams.delete("room");
            }
            const next = `${url.pathname}${url.search}${url.hash}`;
            window.history.replaceState({}, "", next);
          }

          function activateRoom(roomSlug) {
            const tabs = [...document.querySelectorAll("[data-room-tab]")];
            const panels = [...document.querySelectorAll("[data-room-panel]")];
            tabs.forEach((tab) => {
              const active = tab.dataset.roomTab === roomSlug;
              tab.classList.toggle("is-active", active);
              tab.setAttribute("aria-pressed", active ? "true" : "false");
            });
            panels.forEach((panel) => {
              panel.classList.toggle("is-hidden", panel.dataset.roomPanel !== roomSlug);
            });
            if (roomSlug) {
              window.localStorage.setItem(storageKey, roomSlug);
            }
            syncRoomQuery(roomSlug);
          }

          function markDirty() {
            dirty = true;
          }

          function bindTabs() {
            const tabs = [...document.querySelectorAll("[data-room-tab]")];
            if (!tabs.length) {
              return;
            }

            tabs.forEach((tab) => {
              tab.onclick = () => activateRoom(tab.dataset.roomTab);
            });

            const queryRoom = new URL(window.location.href).searchParams.get("room");
            const remembered = window.localStorage.getItem(storageKey);
            const activeTab =
              tabs.find((tab) => tab.dataset.roomTab === queryRoom) ||
              tabs.find((tab) => tab.dataset.roomTab === remembered) ||
              document.querySelector("[data-room-tab].is-active") ||
              tabs[0];
            activateRoom(activeTab.dataset.roomTab);
          }

          function bindScheduleForm(form) {
            form.addEventListener("submit", () => {
              dirty = false;
            });

            form.querySelectorAll("input, select, textarea").forEach((field) => {
              field.addEventListener("input", markDirty);
              field.addEventListener("change", markDirty);
            });

            const scheduleList = form.querySelector("[data-schedule-list]");
            const template = form.querySelector("[data-schedule-row-template]");
            const addButton = form.querySelector("[data-add-row]");
            if (!scheduleList || !template || !addButton) {
              return;
            }

            const bindRow = (row) => {
              const enabledInput = row.querySelector("[data-schedule-enabled]");
              const toggleButton = row.querySelector("[data-schedule-toggle]");
              const toggleLabel = row.querySelector("[data-schedule-toggle-label]");
              const syncToggle = () => {
                if (!enabledInput || !toggleButton || !toggleLabel) {
                  return;
                }
                const enabled = enabledInput.value === "1";
                toggleButton.classList.toggle("is-on", enabled);
                toggleButton.classList.toggle("is-off", !enabled);
                toggleButton.setAttribute("aria-pressed", enabled ? "true" : "false");
                toggleLabel.textContent = enabled ? "ON" : "OFF";
              };
              if (enabledInput && toggleButton) {
                syncToggle();
                toggleButton.onclick = () => {
                  enabledInput.value = enabledInput.value === "1" ? "0" : "1";
                  syncToggle();
                  markDirty();
                };
              }
              const removeButton = row.querySelector("[data-remove-row]");
              if (!removeButton) {
                return;
              }
              removeButton.onclick = () => {
                markDirty();
                const rows = scheduleList.querySelectorAll("[data-schedule-row]");
                if (rows.length === 1) {
                  row.querySelector("[name='schedule_enabled']").value = "1";
                  row.querySelector("[name='schedule_day']").value = "MON";
                  row.querySelector("[name='schedule_start']").value = "18:50";
                  row.querySelector("[name='schedule_end']").value = "21:00";
                  syncToggle();
                  return;
                }
                row.remove();
              };
            };

            scheduleList.querySelectorAll("[data-schedule-row]").forEach(bindRow);

            addButton.onclick = () => {
              markDirty();
              const fragment = template.content.cloneNode(true);
              const row = fragment.querySelector("[data-schedule-row]");
              bindRow(row);
              scheduleList.appendChild(fragment);
            };
          }

          function bindWorkspace() {
            document.querySelectorAll("form").forEach(bindScheduleForm);
          }

          function bindPromptRecorder() {
            const cards = [...document.querySelectorAll("[data-prompt-card]")];
            if (!cards.length) {
              return;
            }
            const supported = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
            cards.forEach((card) => {
              const recordButton = card.querySelector("[data-record-prompt]");
              const stopButton = card.querySelector("[data-stop-prompt]");
              const status = card.querySelector("[data-prompt-status]");
              const uploadForm = card.querySelector("form[enctype='multipart/form-data']");
              if (!recordButton || !stopButton || !status || !uploadForm) {
                return;
              }
              if (!supported) {
                recordButton.disabled = true;
                status.textContent = `${status.textContent} · Browser recording unavailable; use Upload.`;
                return;
              }

              let recorder = null;
              let chunks = [];
              let mediaStream = null;

              async function uploadRecording(blob) {
                const formData = new FormData();
                const extension = blob.type.includes("ogg") ? "ogg" : "webm";
                formData.append("audio", blob, `${card.dataset.promptKey || "prompt"}.${extension}`);
                status.textContent = "Uploading recording...";
                const response = await fetch(uploadForm.action, {
                  method: "POST",
                  body: formData,
                  credentials: "same-origin",
                });
                if (!response.ok) {
                  throw new Error("Upload failed.");
                }
                window.location.href = response.url || window.location.href;
              }

              recordButton.onclick = async () => {
                try {
                  mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
                  const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "";
                  recorder = new MediaRecorder(mediaStream, mimeType ? { mimeType } : undefined);
                  chunks = [];
                  recorder.ondataavailable = (event) => {
                    if (event.data && event.data.size > 0) {
                      chunks.push(event.data);
                    }
                  };
                  recorder.onstop = async () => {
                    try {
                      const blob = new Blob(chunks, { type: recorder.mimeType || "audio/webm" });
                      await uploadRecording(blob);
                    } catch (error) {
                      status.textContent = "Recording upload failed. Try Upload instead.";
                      recordButton.disabled = false;
                      stopButton.disabled = true;
                    } finally {
                      if (mediaStream) {
                        mediaStream.getTracks().forEach((track) => track.stop());
                      }
                    }
                  };
                  recorder.start();
                  status.textContent = "Recording... press Stop when finished.";
                  recordButton.disabled = true;
                  stopButton.disabled = false;
                } catch (error) {
                  status.textContent = "Microphone access was not available. Try Upload instead.";
                }
              };

              stopButton.onclick = () => {
                if (recorder && recorder.state === "recording") {
                  stopButton.disabled = true;
                  status.textContent = "Processing recording...";
                  recorder.stop();
                }
              };
            });
          }

          function renderDeviceChips(devices) {
            return devices.map((device) => `<span class="device-chip">${escapeHtml(device)}</span>`).join("");
          }

          function renderOrderOptions(devices, selectedValue) {
            const selected = String(selectedValue || "");
            const options = ['<option value="">No preference</option>'];
            devices.forEach((device) => {
              const value = String(device || "");
              if (!value) {
                return;
              }
              options.push(
                `<option value="${escapeHtml(value)}"${selected === value ? " selected" : ""}>${escapeHtml(value)}</option>`
              );
            });
            if (selected && !devices.includes(selected)) {
              options.push(
                `<option value="${escapeHtml(selected)}" selected>${escapeHtml(selected)} (Unavailable)</option>`
              );
            }
            return options.join("");
          }

          function updateWorkspaceRuntime(host) {
            const workspace = document.querySelector(`[data-room-panel="${host.room_slug}"]`);
            if (!workspace) {
              return;
            }

            const currentDevice = workspace.querySelector("[data-current-device]");
            if (currentDevice) {
              currentDevice.textContent = host.current_device || "Waiting for the laptop agent";
            }

            const knownDeviceList = workspace.querySelector("[data-known-device-list]");
            const knownDeviceEmpty = workspace.querySelector("[data-known-device-empty]");
            if (knownDeviceList && knownDeviceEmpty) {
              if (host.known_devices.length) {
                knownDeviceList.innerHTML = renderDeviceChips(host.known_devices);
                knownDeviceList.classList.remove("is-hidden");
                knownDeviceEmpty.classList.add("is-hidden");
              } else {
                knownDeviceList.innerHTML = "";
                knownDeviceList.classList.add("is-hidden");
                knownDeviceEmpty.classList.remove("is-hidden");
              }
            }

            const selects = [...workspace.querySelectorAll("[data-order-select]")];
            selects.forEach((select) => {
              const currentValue = select.value;
              select.innerHTML = renderOrderOptions(host.device_options, currentValue);
              if (currentValue && [...select.options].some((option) => option.value === currentValue)) {
                select.value = currentValue;
              }
            });
          }

          function clearFlashQuery() {
            const url = new URL(window.location.href);
            if (!url.searchParams.has("message") && !url.searchParams.has("error")) {
              return;
            }
            window.setTimeout(() => {
              url.searchParams.delete("message");
              url.searchParams.delete("error");
              const next = `${url.pathname}${url.search}${url.hash}`;
              window.history.replaceState({}, "", next);
            }, 1400);
          }

          async function refreshSettings() {
            if (refreshInFlight || dirty || document.hidden) {
              return;
            }
            const activeElement = document.activeElement;
            if (activeElement && ["INPUT", "SELECT", "TEXTAREA"].includes(activeElement.tagName)) {
              return;
            }

            const roomSlug = selectedRoomSlug() || window.localStorage.getItem(storageKey) || "";
            if (!roomSlug) {
              return;
            }

            refreshInFlight = true;
            try {
              const refreshUrl = new URL(window.location.href);
              refreshUrl.searchParams.set("room", roomSlug);
              refreshUrl.searchParams.set("_ts", String(Date.now()));
              const response = await fetch(refreshUrl.toString(), {
                cache: "no-store",
                headers: { "X-Requested-With": "roomcast-refresh" },
              });
              if (!response.ok) {
                return;
              }
              const html = await response.text();
              const doc = new DOMParser().parseFromString(html, "text/html");
              const nextShell = doc.getElementById(shellId);
              const currentShell = document.getElementById(shellId);
              if (!nextShell || !currentShell) {
                return;
              }
              currentShell.replaceWith(nextShell);
              bindTabs();
              bindWorkspace();
              activateRoom(roomSlug);
            } catch (error) {
              /* ignore transient refresh issues */
            } finally {
              refreshInFlight = false;
            }
          }

          async function refreshRuntime() {
            if (runtimeRefreshInFlight || document.hidden) {
              return;
            }
            runtimeRefreshInFlight = true;
            try {
              const response = await fetch(`{{ url_for('admin_runtime') }}?_ts=${Date.now()}`, {
                cache: "no-store",
              });
              if (!response.ok) {
                return;
              }
              const payload = await response.json();
              (payload.hosts || []).forEach(updateWorkspaceRuntime);
            } catch (error) {
              /* ignore transient refresh issues */
            } finally {
              runtimeRefreshInFlight = false;
            }
          }

          bindTabs();
          bindWorkspace();
          bindPromptRecorder();
          clearFlashQuery();
          refreshRuntime();
          window.setInterval(refreshRuntime, 1000);
          window.setInterval(refreshSettings, 3000);
          document.addEventListener("visibilitychange", () => {
            if (!document.hidden) {
              refreshRuntime();
              refreshSettings();
            }
          });
        })();
      </script>
    </main>
  </body>
</html>
"""


if __name__ == "__main__":
    main()
