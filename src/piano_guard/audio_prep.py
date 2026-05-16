from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from piano_guard.config import SessionProjectConfig, iter_session_takes, write_take
from piano_guard.fftools import first_stream, has_libsoxr, probe_media, run_checked
from piano_guard.ingest import AudioClipInfo, InspectionResult
from piano_guard.reports import Issue, issues_to_dict, overall_status


EDIT_AUDIO_SAMPLE_RATE = 48_000
EDIT_AUDIO_BITS_PER_SAMPLE = 24
EDIT_AUDIO_FILENAME = "audio-edit.wav"
SAMPLE_COUNT_TOLERANCE = 8


@dataclass
class AudioPrepTakeResult:
    take_id: str
    source_audio: str
    edit_audio: str
    action: str
    source_sample_rate: int
    source_bits_per_sample: int
    source_sample_count: int
    edit_sample_rate: int
    edit_bits_per_sample: int
    edit_sample_count: int


@dataclass
class SessionAudioPrepResult:
    session_id: str
    status: str
    issues: list[Issue]
    takes: list[AudioPrepTakeResult]


def _probe_audio(path: Path) -> AudioClipInfo:
    payload = probe_media(path)
    stream = first_stream(payload.get("streams", []), "audio")
    if stream is None:
        raise RuntimeError(f"audio stream missing: {path}")
    duration_seconds = float(payload["format"]["duration"])
    bits_per_sample = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
    if bits_per_sample <= 0 and str(stream.get("codec_name") or "").startswith("pcm_s24"):
        bits_per_sample = 24
    sample_rate = int(stream.get("sample_rate") or 0)
    sample_count = int(round(duration_seconds * sample_rate))
    return AudioClipInfo(
        path=str(path),
        duration_seconds=duration_seconds,
        sample_rate=sample_rate,
        channels=int(stream.get("channels") or 0),
        channel_layout=str(stream.get("channel_layout") or ""),
        codec_name=str(stream.get("codec_name") or ""),
        bits_per_sample=bits_per_sample,
        sample_count=sample_count,
        is_pcm=str(stream.get("codec_name") or "").startswith("pcm_"),
    )


def _expected_edit_sample_count(source: AudioClipInfo) -> int:
    if source.sample_rate > 0 and source.sample_count > 0:
        return int(round(source.sample_count * (EDIT_AUDIO_SAMPLE_RATE / source.sample_rate)))
    return int(round(source.duration_seconds * EDIT_AUDIO_SAMPLE_RATE))


def _needs_edit_proxy(source: AudioClipInfo) -> bool:
    return not (
        source.sample_rate == EDIT_AUDIO_SAMPLE_RATE
        and source.is_pcm
        and source.bits_per_sample == EDIT_AUDIO_BITS_PER_SAMPLE
    )


def _validate_edit_proxy(source: AudioClipInfo, edit: AudioClipInfo) -> list[Issue]:
    issues: list[Issue] = []
    if edit.sample_rate != EDIT_AUDIO_SAMPLE_RATE:
        issues.append(
            Issue(
                "fail",
                "edit_audio_sample_rate_mismatch",
                f"edit audio sample rate is {edit.sample_rate}, expected {EDIT_AUDIO_SAMPLE_RATE}",
            )
        )
    if not edit.is_pcm:
        issues.append(Issue("fail", "edit_audio_codec_mismatch", f"edit audio codec is {edit.codec_name}, expected PCM"))
    if edit.bits_per_sample not in (EDIT_AUDIO_BITS_PER_SAMPLE, 32):
        issues.append(
            Issue(
                "fail",
                "edit_audio_bit_depth_mismatch",
                f"edit audio bit depth is {edit.bits_per_sample}, expected {EDIT_AUDIO_BITS_PER_SAMPLE}",
            )
        )
    expected_samples = _expected_edit_sample_count(source)
    if abs(edit.sample_count - expected_samples) > SAMPLE_COUNT_TOLERANCE:
        issues.append(
            Issue(
                "fail",
                "edit_audio_sample_count_mismatch",
                f"edit audio sample count is {edit.sample_count}, expected {expected_samples}",
                {"expected_samples": expected_samples, "actual_samples": edit.sample_count},
            )
        )
    return issues


def _resample_filter() -> str:
    """Build the aresample filter string, using SoX resampler when available.

    SoX resampler (libsoxr) provides audiophile-grade 96k→48k downsampling
    with 28-bit precision. When the ffmpeg build doesn't include libsoxr
    (common on Homebrew's default formula), fall back to the built-in SWR
    resampler which is still high quality for an editing proxy.
    """
    if has_libsoxr():
        return f"aresample={EDIT_AUDIO_SAMPLE_RATE}:resampler=soxr:precision=28"
    return f"aresample={EDIT_AUDIO_SAMPLE_RATE}"


def _generate_edit_proxy(source_path: Path, edit_path: Path) -> None:
    edit_path.parent.mkdir(parents=True, exist_ok=True)
    run_checked(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source_path),
            "-map",
            "0:a:0",
            "-vn",
            "-af",
            _resample_filter(),
            "-c:a",
            "pcm_s24le",
            str(edit_path),
        ]
    )


def prepare_session_audio(
    session: SessionProjectConfig,
    *,
    take_inspections: dict[str, InspectionResult],
    dry_run: bool = False,
) -> SessionAudioPrepResult:
    issues: list[Issue] = []
    take_results: list[AudioPrepTakeResult] = []

    for take_ref, take in iter_session_takes(session):
        inspection = take_inspections[take_ref.id]
        source = inspection.master_audio
        source_path = Path(source.path)

        if not _needs_edit_proxy(source):
            take.edit_audio = None
            if not dry_run:
                write_take(take)
            take_results.append(
                AudioPrepTakeResult(
                    take_id=take_ref.id,
                    source_audio=str(source_path),
                    edit_audio=str(source_path),
                    action="reuse-source",
                    source_sample_rate=source.sample_rate,
                    source_bits_per_sample=source.bits_per_sample,
                    source_sample_count=source.sample_count,
                    edit_sample_rate=source.sample_rate,
                    edit_bits_per_sample=source.bits_per_sample,
                    edit_sample_count=source.sample_count,
                )
            )
            continue

        edit_relative = EDIT_AUDIO_FILENAME
        edit_path = take.resolve_path(edit_relative)
        action = "generate"
        if edit_path.exists():
            try:
                existing = _probe_audio(edit_path)
                proxy_issues = _validate_edit_proxy(source, existing)
                if not proxy_issues:
                    action = "reuse-existing"
                else:
                    action = "regenerate"
            except Exception:
                action = "regenerate"

        if not dry_run and action in {"generate", "regenerate"}:
            _generate_edit_proxy(source_path, edit_path)

        if dry_run and action in {"generate", "regenerate"}:
            expected_sample_count = _expected_edit_sample_count(source)
            edit_info = AudioClipInfo(
                path=str(edit_path),
                duration_seconds=expected_sample_count / EDIT_AUDIO_SAMPLE_RATE,
                sample_rate=EDIT_AUDIO_SAMPLE_RATE,
                channels=source.channels,
                channel_layout=source.channel_layout,
                codec_name="pcm_s24le",
                bits_per_sample=EDIT_AUDIO_BITS_PER_SAMPLE,
                sample_count=expected_sample_count,
                is_pcm=True,
            )
            proxy_issues: list[Issue] = []
        else:
            edit_info = _probe_audio(edit_path) if edit_path.exists() else AudioClipInfo(
                path=str(edit_path),
                duration_seconds=0.0,
                sample_rate=0,
                channels=source.channels,
                channel_layout=source.channel_layout,
                codec_name="",
                bits_per_sample=0,
                sample_count=0,
                is_pcm=False,
            )
            proxy_issues = _validate_edit_proxy(source, edit_info)
        issues.extend(
            Issue(issue.severity, issue.code, f"{take_ref.id}: {issue.message}", issue.context) for issue in proxy_issues
        )

        take.edit_audio = edit_relative
        if not dry_run:
            write_take(take)
        take_results.append(
            AudioPrepTakeResult(
                take_id=take_ref.id,
                source_audio=str(source_path),
                edit_audio=str(edit_path),
                action="would-generate" if dry_run and action in {"generate", "regenerate"} else action,
                source_sample_rate=source.sample_rate,
                source_bits_per_sample=source.bits_per_sample,
                source_sample_count=source.sample_count,
                edit_sample_rate=edit_info.sample_rate,
                edit_bits_per_sample=edit_info.bits_per_sample,
                edit_sample_count=edit_info.sample_count,
            )
        )

    return SessionAudioPrepResult(
        session_id=session.session_id,
        status=overall_status(issues),
        issues=issues,
        takes=take_results,
    )


def session_audio_prep_to_dict(result: SessionAudioPrepResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "status": result.status,
        "issues": issues_to_dict(result.issues),
        "takes": [asdict(take) for take in result.takes],
    }
