#!/usr/bin/env python3
"""Generate RoomCast phone prompt WAVs with the local DS677 XTTS voice reference."""

from __future__ import annotations

import argparse
import subprocess
from datetime import datetime
from pathlib import Path


DEFAULT_REFERENCE = Path(
    "/Users/chandruv/Documents/NJIT COLLEGE FILES/Fifth Year - Masters/2nd Semester/DS 677 - Deep Learning/"
    "Project/Milestones/Milestone 3 - Tasks, Report, Video Presentation/voice_clone/output/reference_24k_mono.wav"
)
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

PROMPTS = {
    "welcome_pin": "Welcome to NTC Newark WebCall. Please enter your four-digit PIN.",
    "goodbye": "Goodbye.",
    "no_active_schedule": (
        "Welcome to NTC Newark WebCall. There is no scheduled meeting active right now. "
        "Please try again when the meeting begins. Goodbye."
    ),
    "invalid_pin": "Sorry, that pin was not accepted. Please try again.",
    "no_active_conference": (
        "That pin is valid, but no conference is active right now. "
        "Please try again when the meeting begins. Goodbye."
    ),
    "conference_ending": "Conference ending. Goodbye.",
    "technical_difficulty": "We experienced a technical difficulty. Please call back in a moment. Goodbye.",
    "line_unavailable": "The line is no longer available. Goodbye.",
}


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def convert_for_telnyx(raw_path: Path, output_path: Path, *, tempo: float) -> None:
    filters = []
    if tempo != 1.0:
        filters.append(f"atempo={tempo:.3f}")
    filters.append("loudnorm=I=-19:TP=-2:LRA=9")
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(raw_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            "-af",
            ",".join(filters),
            str(output_path),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--output-dir", type=Path, default=Path("data/generated-prompts-test") / f"xtts-{datetime.now():%Y%m%d-%H%M%S}")
    parser.add_argument("--device", choices=["cpu", "mps"], default="mps")
    parser.add_argument("--tempo", type=float, default=1.0, help="Tempo multiplier for final Telnyx WAVs, e.g. 1.15 is 15% faster.")
    args = parser.parse_args()

    if not args.reference.exists():
        raise SystemExit(f"Missing reference WAV: {args.reference}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    from TTS.api import TTS

    print(f"Using reference: {args.reference}")
    print(f"Writing prompts to: {args.output_dir}")
    tts = TTS(MODEL_NAME, progress_bar=True).to(args.device)

    for key, text in PROMPTS.items():
        raw_path = args.output_dir / f"{key}_raw.wav"
        final_path = args.output_dir / f"{key}.wav"
        print(f"Generating {key}: {text}")
        tts.tts_to_file(
            text=text,
            speaker_wav=str(args.reference),
            language="en",
            file_path=str(raw_path),
            split_sentences=True,
        )
        convert_for_telnyx(raw_path, final_path, tempo=args.tempo)

    print(args.output_dir)


if __name__ == "__main__":
    main()
