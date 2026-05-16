from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
from statistics import median
from typing import Any

from piano_guard.config import TakeConfig
from piano_guard.fftools import first_stream, frame_rate_to_float, probe_media
from piano_guard.reports import Issue, issues_to_dict, overall_status, write_json_report, write_markdown_report


@dataclass
class VideoClipInfo:
    path: str
    label: str
    duration_seconds: float
    width: int
    height: int
    frame_rate: float
    frame_rate_raw: str
    nominal_frame_rate: float
    nominal_frame_rate_raw: str
    variable_frame_rate: bool
    bit_depth: int
    pix_fmt: str
    scratch_audio_present: bool
    scratch_audio_channels: int
    scratch_audio_sample_rate: int
    color_space: str
    color_transfer: str
    color_primaries: str


@dataclass
class AudioClipInfo:
    path: str
    duration_seconds: float
    sample_rate: int
    channels: int
    channel_layout: str
    codec_name: str
    bits_per_sample: int
    sample_count: int
    is_pcm: bool


@dataclass
class InspectionResult:
    take_name: str
    generated_at: str
    status: str
    issues: list[Issue]
    videos: list[VideoClipInfo]
    master_audio: AudioClipInfo


def _check_file_exists(path: Path, issues: list[Issue], code: str) -> bool:
    if path.exists():
        return True
    issues.append(Issue("fail", code, f"missing file: {path.name}", {"path": str(path)}))
    return False


def probe_video(path: Path, *, label: str | None = None) -> VideoClipInfo:
    """Probe a single video file and return a ``VideoClipInfo``.

    This is the one-clip analogue of ``inspect_take``. Used by the Phase 2
    auto-segment pipeline, which walks clips independently of the take-level
    audio/validation logic. Raises ``ExternalToolError`` if ffprobe fails or
    ``ValueError`` if no video stream is present.
    """
    clip_probe = probe_media(path)
    video_stream = first_stream(clip_probe.get("streams", []), "video")
    audio_stream = first_stream(clip_probe.get("streams", []), "audio")
    if video_stream is None:
        raise ValueError(f"no video stream in {path}")

    avg_frame_rate_raw = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or ""
    nominal_frame_rate_raw = video_stream.get("r_frame_rate") or avg_frame_rate_raw
    frame_rate = frame_rate_to_float(avg_frame_rate_raw)
    nominal_frame_rate = frame_rate_to_float(nominal_frame_rate_raw)

    return VideoClipInfo(
        path=str(path),
        label=label or Path(path).stem,
        duration_seconds=float(clip_probe["format"]["duration"]),
        width=int(video_stream.get("width") or 0),
        height=int(video_stream.get("height") or 0),
        frame_rate=frame_rate,
        frame_rate_raw=avg_frame_rate_raw,
        nominal_frame_rate=nominal_frame_rate,
        nominal_frame_rate_raw=nominal_frame_rate_raw,
        variable_frame_rate=abs(frame_rate - nominal_frame_rate) > 0.01,
        bit_depth=_stream_bit_depth(video_stream),
        pix_fmt=str(video_stream.get("pix_fmt") or ""),
        scratch_audio_present=audio_stream is not None,
        scratch_audio_channels=int((audio_stream or {}).get("channels") or 0),
        scratch_audio_sample_rate=int((audio_stream or {}).get("sample_rate") or 0),
        color_space=video_stream.get("color_space") or "",
        color_transfer=video_stream.get("color_transfer") or "",
        color_primaries=video_stream.get("color_primaries") or "",
    )


def _stream_bit_depth(stream: dict[str, Any]) -> int:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = stream.get(key)
        if value in (None, "", "0"):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    pix_fmt = str(stream.get("pix_fmt") or "")
    match = re.search(r"(\d+)(?:le|be)?$", pix_fmt)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return 0
    if pix_fmt.endswith("p") or pix_fmt.endswith("j420p"):
        return 8
    return 0


def _audio_sample_count(stream: dict[str, Any], duration_seconds: float) -> int:
    duration_ts = stream.get("duration_ts")
    sample_rate = int(stream.get("sample_rate") or 0)
    time_base = str(stream.get("time_base") or "")
    if duration_ts not in (None, "", "N/A") and sample_rate > 0 and "/" in time_base:
        try:
            numerator, denominator = time_base.split("/", 1)
            sample_seconds = int(duration_ts) * (float(numerator) / float(denominator))
            return int(round(sample_seconds * sample_rate))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return int(round(duration_seconds * sample_rate))


def inspect_take(take: TakeConfig) -> InspectionResult:
    issues: list[Issue] = []
    videos: list[VideoClipInfo] = []

    audio_path = take.resolve_path(take.master_audio)
    if not _check_file_exists(audio_path, issues, "audio_missing"):
        raise FileNotFoundError(audio_path)

    audio_probe = probe_media(audio_path)
    audio_stream = first_stream(audio_probe.get("streams", []), "audio")
    if audio_stream is None:
        issues.append(Issue("fail", "audio_stream_missing", "master audio has no audio stream"))
        raise RuntimeError("master audio has no audio stream")

    master_audio = AudioClipInfo(
        path=str(audio_path),
        duration_seconds=float(audio_probe["format"]["duration"]),
        sample_rate=int(audio_stream.get("sample_rate") or 0),
        channels=int(audio_stream.get("channels") or 0),
        channel_layout=str(audio_stream.get("channel_layout") or ""),
        codec_name=audio_stream.get("codec_name") or "",
        bits_per_sample=_stream_bit_depth(audio_stream),
        sample_count=_audio_sample_count(audio_stream, float(audio_probe["format"]["duration"])),
        is_pcm=str(audio_stream.get("codec_name") or "").startswith("pcm_"),
    )

    for camera in take.camera_files:
        clip_path = take.resolve_path(camera.file)
        if not _check_file_exists(clip_path, issues, "video_missing"):
            continue
        clip_probe = probe_media(clip_path)
        video_stream = first_stream(clip_probe.get("streams", []), "video")
        audio_stream = first_stream(clip_probe.get("streams", []), "audio")
        if video_stream is None:
            issues.append(Issue("fail", "video_stream_missing", f"{camera.file} has no video stream"))
            continue

        avg_frame_rate_raw = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate") or ""
        nominal_frame_rate_raw = video_stream.get("r_frame_rate") or avg_frame_rate_raw
        frame_rate = frame_rate_to_float(avg_frame_rate_raw)
        nominal_frame_rate = frame_rate_to_float(nominal_frame_rate_raw)
        videos.append(
            VideoClipInfo(
                path=str(clip_path),
                label=camera.label,
                duration_seconds=float(clip_probe["format"]["duration"]),
                width=int(video_stream.get("width") or 0),
                height=int(video_stream.get("height") or 0),
                frame_rate=frame_rate,
                frame_rate_raw=avg_frame_rate_raw,
                nominal_frame_rate=nominal_frame_rate,
                nominal_frame_rate_raw=nominal_frame_rate_raw,
                variable_frame_rate=abs(frame_rate - nominal_frame_rate) > take.validation.frame_rate_tolerance,
                bit_depth=_stream_bit_depth(video_stream),
                pix_fmt=str(video_stream.get("pix_fmt") or ""),
                scratch_audio_present=audio_stream is not None,
                scratch_audio_channels=int((audio_stream or {}).get("channels") or 0),
                scratch_audio_sample_rate=int((audio_stream or {}).get("sample_rate") or 0),
                color_space=video_stream.get("color_space") or "",
                color_transfer=video_stream.get("color_transfer") or "",
                color_primaries=video_stream.get("color_primaries") or "",
            )
        )

    expected_frame_rate = float(take.timeline.frame_rate)
    distinct_frame_rates = sorted({round(video.frame_rate, 3) for video in videos if video.frame_rate > 0})
    if len(distinct_frame_rates) > 1:
        issues.append(
            Issue(
                "fail",
                "mixed_take_frame_rates",
                f"{take.take_id} contains mixed actual frame rates: {distinct_frame_rates}",
            )
        )
    for video in videos:
        if video.variable_frame_rate:
            issues.append(
                Issue(
                    "fail",
                    "variable_frame_rate",
                    f"{Path(video.path).name} avg frame rate {video.frame_rate_raw} differs from nominal {video.nominal_frame_rate_raw}",
                )
            )
        frame_rate_delta = abs(video.frame_rate - expected_frame_rate)
        if frame_rate_delta > 0.05:
            issues.append(
                Issue(
                    "fail",
                    "frame_rate_mismatch",
                    f"{Path(video.path).name} frame rate {video.frame_rate:.3f} does not match {expected_frame_rate:.3f}",
                    {"frame_rate_raw": video.frame_rate_raw},
                )
            )
        elif frame_rate_delta > take.validation.frame_rate_tolerance:
            issues.append(
                Issue(
                    "warn",
                    "frame_rate_nominal_warning",
                    f"{Path(video.path).name} frame rate {video.frame_rate:.3f} differs from configured {expected_frame_rate:.3f}",
                    {"frame_rate_raw": video.frame_rate_raw},
                )
            )
        if video.color_space != take.validation.expected_color_space:
            issues.append(
                Issue(
                    "fail",
                    "color_space_mismatch",
                    f"{Path(video.path).name} color space {video.color_space} != {take.validation.expected_color_space}",
                )
            )
        if video.color_transfer != take.validation.expected_color_transfer:
            issues.append(
                Issue(
                    "fail",
                    "color_transfer_mismatch",
                    f"{Path(video.path).name} color transfer {video.color_transfer} != {take.validation.expected_color_transfer}",
                )
            )
        if video.color_primaries != take.validation.expected_color_primaries:
            issues.append(
                Issue(
                    "fail",
                    "color_primaries_mismatch",
                    f"{Path(video.path).name} color primaries {video.color_primaries} != {take.validation.expected_color_primaries}",
                )
            )
        if not video.scratch_audio_present:
            issues.append(Issue("warn", "scratch_audio_missing", f"{Path(video.path).name} has no scratch audio"))

    if master_audio.channels not in (1, 2):
        issues.append(
            Issue(
                "fail",
                "audio_channel_layout_unsupported",
                f"master audio channel count {master_audio.channels} is unsupported",
            )
        )
    if master_audio.sample_rate <= 0:
        issues.append(Issue("fail", "audio_sample_rate_missing", "master audio sample rate could not be determined"))

    if videos:
        median_video_duration = median([video.duration_seconds for video in videos])
        for video in videos:
            delta = abs(video.duration_seconds - median_video_duration)
            if delta > take.validation.video_duration_fail_seconds:
                issues.append(
                    Issue(
                        "fail",
                        "video_duration_outlier",
                        f"{Path(video.path).name} duration differs from median by {delta:.2f}s",
                    )
                )
            elif delta > take.validation.video_duration_warn_seconds:
                issues.append(
                    Issue(
                        "warn",
                        "video_duration_warning",
                        f"{Path(video.path).name} duration differs from median by {delta:.2f}s",
                    )
                )

        audio_delta = abs(master_audio.duration_seconds - median_video_duration)
        if audio_delta > take.validation.audio_duration_fail_seconds:
            issues.append(
                Issue(
                    "fail",
                    "audio_duration_outlier",
                    f"master audio duration differs from median video by {audio_delta:.2f}s",
                )
            )
        elif audio_delta > take.validation.audio_duration_warn_seconds:
            issues.append(
                Issue(
                    "warn",
                    "audio_duration_warning",
                    f"master audio duration differs from median video by {audio_delta:.2f}s",
                )
            )

    status = overall_status(issues)
    return InspectionResult(
        take_name=take.take_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        issues=issues,
        videos=videos,
        master_audio=master_audio,
    )


def inspection_to_dict(result: InspectionResult) -> dict[str, Any]:
    return {
        "take_name": result.take_name,
        "generated_at": result.generated_at,
        "status": result.status,
        "issues": issues_to_dict(result.issues),
        "videos": [asdict(video) for video in result.videos],
        "master_audio": asdict(result.master_audio),
    }


def write_inspection_reports(take: TakeConfig, result: InspectionResult) -> tuple[Path, Path]:
    payload = inspection_to_dict(result)
    json_path = take.reports_path("ingest.json")
    markdown_path = take.reports_path("ingest.md")
    write_json_report(json_path, payload)
    write_markdown_report(
        markdown_path,
        title="Ingest Validation",
        status=result.status,
        issues=result.issues,
        sections={
            "Master Audio": asdict(result.master_audio),
            "Video Clips": [f"{video.label}: {Path(video.path).name}" for video in result.videos],
        },
    )
    return json_path, markdown_path
