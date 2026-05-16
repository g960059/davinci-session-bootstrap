from __future__ import annotations

import json
from pathlib import Path
import subprocess
from typing import Any


class ExternalToolError(RuntimeError):
    pass


def run_checked(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            check=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "command failed"
        raise ExternalToolError(f"{' '.join(args)}: {message}") from exc


def run_checked_bytes(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(args, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        stdout = (exc.stdout or b"").decode("utf-8", errors="replace").strip()
        message = stderr or stdout or "command failed"
        raise ExternalToolError(f"{' '.join(args)}: {message}") from exc


def run_json_command(args: list[str]) -> dict[str, Any]:
    result = run_checked(args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ExternalToolError(f"command did not return JSON: {' '.join(args)}") from exc


def probe_media(path: Path) -> dict[str, Any]:
    return run_json_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            (
                "format=filename,duration:"
                "stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,"
                "channels,channel_layout,sample_rate,color_space,color_transfer,color_primaries,"
                "pix_fmt,bits_per_raw_sample,bits_per_sample,duration_ts,time_base"
            ),
            "-of",
            "json",
            str(path),
        ]
    )


def frame_rate_to_float(rate: str) -> float:
    if not rate or rate == "0/0":
        return 0.0
    if "/" not in rate:
        return float(rate)
    numerator, denominator = rate.split("/", 1)
    return float(numerator) / float(denominator)


def first_stream(streams: list[dict[str, Any]], codec_type: str) -> dict[str, Any] | None:
    return next((stream for stream in streams if stream.get("codec_type") == codec_type), None)


_soxr_available: bool | None = None


def has_libsoxr() -> bool:
    """Return True if the system ffmpeg was built with --enable-libsoxr.

    Cached after the first call so the subprocess is only spawned once per
    process lifetime.
    """
    global _soxr_available
    if _soxr_available is not None:
        return _soxr_available
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            check=False,
        )
        _soxr_available = "--enable-libsoxr" in result.stdout
    except FileNotFoundError:
        _soxr_available = False
    return _soxr_available
