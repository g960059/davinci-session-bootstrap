from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from piano_guard.config import SessionProjectConfig, iter_session_takes
from piano_guard.ingest import VideoClipInfo, inspect_take
from piano_guard.reports import Issue, issues_to_dict, overall_status, write_json_report


FRAME_SAMPLE_POSITIONS = (0.25, 0.5, 0.75)
KEYBOARD_ROI = (0.2, 0.45, 0.8, 0.85)
SCORE_ROI = (0.32, 0.05, 0.68, 0.32)
PIANO_BODY_ROI = (0.45, 0.10, 0.95, 0.85)
HIGHLIGHT_CLIP_THRESHOLD = 0.98
SHADOW_CLIP_THRESHOLD = 0.02
SCORE_CLIP_RATIO_WARN = 0.02
PIANO_SHADOW_WARN = 0.03
EXPOSURE_DRIFT_WARN = 0.12
CAMERA_LUMA_SPREAD_WARN = 0.08
CAMERA_NEUTRAL_DISTANCE_WARN = 0.08
MIN_NEUTRAL_PIXELS = 128
BT2020_TO_BT709 = np.array(
    [
        [1.6605, -0.5876, -0.0728],
        [-0.1246, 1.1329, -0.0083],
        [-0.0182, -0.1006, 1.1187],
    ],
    dtype=np.float32,
)


@dataclass
class VideoColorSummary:
    path: str
    label: str
    sample_count: int
    analysis_still: str | None
    roi_luma_p05: float
    roi_luma_p50: float
    roi_luma_p95: float
    roi_luma_p995: float
    score_highlight_clip_ratio: float
    piano_shadow_p05: float
    median_saturation: float
    exposure_drift: float
    neutral_balance: list[float]
    neutral_confident: bool


@dataclass
class AngleNormalizationSuggestion:
    label: str
    reference_label: str
    exposure_offset: float
    wb_delta_rgb: list[float]
    confidence: float


@dataclass
class TakeColorSummary:
    take_id: str
    status: str
    reference_angle: str
    videos: list[VideoColorSummary]
    suggestions: list[AngleNormalizationSuggestion]


@dataclass
class SessionColorQcResult:
    session_id: str
    generated_at: str
    status: str
    reference_angle: str | None
    issues: list[Issue]
    takes: list[TakeColorSummary]


def _roi(frame_rgb: np.ndarray, bounds: tuple[float, float, float, float]) -> np.ndarray:
    height, width = frame_rgb.shape[:2]
    x0 = int(width * bounds[0])
    y0 = int(height * bounds[1])
    x1 = int(width * bounds[2])
    y1 = int(height * bounds[3])
    region = frame_rgb[y0:y1, x0:x1]
    return region if region.size else frame_rgb


def _hlg_to_linear(values: np.ndarray) -> np.ndarray:
    a = 0.17883277
    b = 0.28466892
    c = 0.55991073
    return np.where(
        values <= 0.5,
        (values * values) / 3.0,
        (np.exp((values - c) / a) + b) / 12.0,
    ).astype(np.float32)


def _bt709_oetf(values: np.ndarray) -> np.ndarray:
    return np.where(
        values < 0.018,
        4.5 * values,
        1.099 * np.power(np.maximum(values, 0.0), 0.45) - 0.099,
    ).astype(np.float32)


def _normalize_for_analysis(frame_rgb: np.ndarray, video: VideoClipInfo) -> np.ndarray:
    rgb = np.clip(frame_rgb.astype(np.float32), 0.0, 1.0)
    if video.color_transfer == "arib-std-b67":
        rgb = _hlg_to_linear(rgb)
    if video.color_primaries == "bt2020":
        rgb = np.tensordot(rgb, BT2020_TO_BT709.T, axes=1)
    rgb = np.clip(rgb, 0.0, 1.0)
    return np.clip(_bt709_oetf(rgb), 0.0, 1.0)


def _normalize_neutral_balance(rgb_mean: np.ndarray) -> list[float]:
    total = float(rgb_mean.sum())
    if total <= 0.0:
        return [0.0, 0.0, 0.0]
    normalized = rgb_mean / total
    return [float(value) for value in normalized]


def _sample_video_frames(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"unable to open video for color QC: {path}")

    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    sampled_frames: list[np.ndarray] = []
    try:
        for position in FRAME_SAMPLE_POSITIONS:
            if frame_count > 0:
                frame_index = max(0, min(frame_count - 1, int(round(frame_count * position))))
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame_bgr = capture.read()
            if not ok or frame_bgr is None:
                continue
            sampled_frames.append(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0)
    finally:
        capture.release()

    if not sampled_frames:
        raise RuntimeError(f"unable to decode representative frames for color QC: {path}")
    return sampled_frames


def _export_still(path: Path | None, frame_rgb: np.ndarray) -> str | None:
    if path is None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    image = np.clip(frame_rgb * 255.0, 0.0, 255.0).astype(np.uint8)
    cv2.imwrite(str(path), cv2.cvtColor(image, cv2.COLOR_RGB2BGR))
    return str(path)


def _analyze_video_color(video: VideoClipInfo, *, still_path: Path | None = None) -> VideoColorSummary:
    frames = _sample_video_frames(Path(video.path))
    analyzed_frames = [_normalize_for_analysis(frame, video) for frame in frames]
    frame_metrics: list[dict[str, Any]] = []

    for frame_rgb in analyzed_frames:
        keyboard = _roi(frame_rgb, KEYBOARD_ROI)
        score = _roi(frame_rgb, SCORE_ROI)
        piano = _roi(frame_rgb, PIANO_BODY_ROI)

        keyboard_luma = (0.2126 * keyboard[:, :, 0]) + (0.7152 * keyboard[:, :, 1]) + (0.0722 * keyboard[:, :, 2])
        score_luma = (0.2126 * score[:, :, 0]) + (0.7152 * score[:, :, 1]) + (0.0722 * score[:, :, 2])
        piano_luma = (0.2126 * piano[:, :, 0]) + (0.7152 * piano[:, :, 1]) + (0.0722 * piano[:, :, 2])

        hsv = cv2.cvtColor((keyboard * 255.0).astype(np.uint8), cv2.COLOR_RGB2HSV).astype(np.float32)
        saturation = hsv[:, :, 1] / 255.0
        neutral_mask = (saturation < 0.12) & (keyboard_luma > 0.2) & (keyboard_luma < 0.95)
        neutral_pixels = keyboard[neutral_mask]
        neutral_confident = neutral_pixels.shape[0] >= MIN_NEUTRAL_PIXELS
        neutral_mean = neutral_pixels.mean(axis=0) if neutral_confident else keyboard.reshape(-1, 3).mean(axis=0)

        frame_metrics.append(
            {
                "p05": float(np.percentile(keyboard_luma, 5)),
                "p50": float(np.percentile(keyboard_luma, 50)),
                "p95": float(np.percentile(keyboard_luma, 95)),
                "p995": float(np.percentile(keyboard_luma, 99.5)),
                "score_highlight": float(np.mean(score_luma >= HIGHLIGHT_CLIP_THRESHOLD)),
                "piano_shadow_p05": float(np.percentile(piano_luma, 5)),
                "median_saturation": float(np.percentile(saturation, 50)),
                "neutral_balance": _normalize_neutral_balance(neutral_mean),
                "neutral_confident": neutral_confident,
            }
        )

    neutral_vectors = np.array([metric["neutral_balance"] for metric in frame_metrics], dtype=np.float32)
    still = _export_still(still_path, analyzed_frames[1 if len(analyzed_frames) > 1 else 0])
    return VideoColorSummary(
        path=video.path,
        label=video.label,
        sample_count=len(frame_metrics),
        analysis_still=still,
        roi_luma_p05=float(np.median([metric["p05"] for metric in frame_metrics])),
        roi_luma_p50=float(np.median([metric["p50"] for metric in frame_metrics])),
        roi_luma_p95=float(np.median([metric["p95"] for metric in frame_metrics])),
        roi_luma_p995=float(np.median([metric["p995"] for metric in frame_metrics])),
        score_highlight_clip_ratio=float(np.max([metric["score_highlight"] for metric in frame_metrics])),
        piano_shadow_p05=float(np.median([metric["piano_shadow_p05"] for metric in frame_metrics])),
        median_saturation=float(np.median([metric["median_saturation"] for metric in frame_metrics])),
        exposure_drift=float(max(metric["p50"] for metric in frame_metrics) - min(metric["p50"] for metric in frame_metrics)),
        neutral_balance=[float(value) for value in np.median(neutral_vectors, axis=0)],
        neutral_confident=all(metric["neutral_confident"] for metric in frame_metrics),
    )


def _append_color_tag_issues(issues: list[Issue], take_id: str, video: VideoClipInfo) -> None:
    video_name = Path(video.path).name
    if video.color_space != "bt2020nc":
        issues.append(Issue("fail", "color_space_mismatch", f"{take_id}/{video_name} color space is {video.color_space}"))
    if video.color_transfer != "arib-std-b67":
        issues.append(
            Issue("fail", "color_transfer_mismatch", f"{take_id}/{video_name} color transfer is {video.color_transfer}")
        )
    if video.color_primaries != "bt2020":
        issues.append(
            Issue("fail", "color_primaries_mismatch", f"{take_id}/{video_name} color primaries is {video.color_primaries}")
        )


def _choose_reference_angle(
    session: SessionProjectConfig,
    labels: list[str],
    requested_reference_angle: str | None,
) -> str:
    for candidate in (requested_reference_angle, session.reference_angle):
        if candidate and candidate in labels:
            return candidate
    for candidate in session.angles:
        if candidate in labels:
            return candidate
    return sorted(labels)[0]


def _normalization_suggestions(
    summaries: list[VideoColorSummary],
    reference_angle: str,
) -> list[AngleNormalizationSuggestion]:
    summary_map = {summary.label: summary for summary in summaries}
    reference = summary_map[reference_angle]
    suggestions: list[AngleNormalizationSuggestion] = []
    for summary in summaries:
        if summary.label == reference_angle:
            continue
        confidence = 1.0 if summary.neutral_confident and reference.neutral_confident else 0.5
        suggestions.append(
            AngleNormalizationSuggestion(
                label=summary.label,
                reference_label=reference_angle,
                exposure_offset=float(reference.roi_luma_p50 - summary.roi_luma_p50),
                wb_delta_rgb=[
                    float(reference.neutral_balance[index] - summary.neutral_balance[index]) for index in range(3)
                ],
                confidence=confidence,
            )
        )
    return suggestions


def inspect_session_color(
    session: SessionProjectConfig,
    *,
    reference_angle: str | None = None,
    export_stills: bool = True,
) -> SessionColorQcResult:
    issues: list[Issue] = []
    take_summaries: list[TakeColorSummary] = []
    stills_root = session.reports_path("stills") if export_stills else None

    for take_ref, take in iter_session_takes(session):
        inspection = inspect_take(take)
        summaries: list[VideoColorSummary] = []
        take_issues: list[Issue] = []

        for video in inspection.videos:
            _append_color_tag_issues(take_issues, take_ref.id, video)
            still_path = stills_root / take_ref.id / f"{video.label}.png" if stills_root is not None else None
            summary = _analyze_video_color(video, still_path=still_path)
            summaries.append(summary)

            if summary.score_highlight_clip_ratio > SCORE_CLIP_RATIO_WARN:
                take_issues.append(
                    Issue(
                        "warn",
                        "score_highlight_clip_risk",
                        f"{take_ref.id}/{video.label} score highlight clip ratio is {summary.score_highlight_clip_ratio:.3f}",
                    )
                )
            if summary.piano_shadow_p05 < PIANO_SHADOW_WARN:
                take_issues.append(
                    Issue(
                        "warn",
                        "piano_shadow_risk",
                        f"{take_ref.id}/{video.label} piano shadow p05 is {summary.piano_shadow_p05:.3f}",
                    )
                )
            if summary.exposure_drift > EXPOSURE_DRIFT_WARN:
                take_issues.append(
                    Issue(
                        "warn",
                        "exposure_drift",
                        f"{take_ref.id}/{video.label} median luma drift is {summary.exposure_drift:.3f}",
                    )
                )

        labels = [summary.label for summary in summaries]
        chosen_reference = _choose_reference_angle(session, labels, reference_angle) if labels else ""
        suggestions = _normalization_suggestions(summaries, chosen_reference) if chosen_reference else []

        if summaries:
            luma_values = [summary.roi_luma_p50 for summary in summaries]
            if max(luma_values) - min(luma_values) > CAMERA_LUMA_SPREAD_WARN:
                take_issues.append(
                    Issue(
                        "warn",
                        "camera_exposure_mismatch",
                        f"{take_ref.id} camera median-luma spread is {max(luma_values) - min(luma_values):.3f}",
                    )
                )

            neutral_vectors = np.array([summary.neutral_balance for summary in summaries], dtype=np.float32)
            median_vector = np.median(neutral_vectors, axis=0)
            distances = [float(np.abs(vector - median_vector).sum()) for vector in neutral_vectors]
            if max(distances) > CAMERA_NEUTRAL_DISTANCE_WARN:
                take_issues.append(
                    Issue(
                        "warn",
                        "camera_wb_mismatch",
                        f"{take_ref.id} neutral-balance distance is {max(distances):.3f}",
                    )
                )

        take_status = overall_status(take_issues)
        issues.extend(take_issues)
        take_summaries.append(
            TakeColorSummary(
                take_id=take_ref.id,
                status=take_status,
                reference_angle=chosen_reference,
                videos=summaries,
                suggestions=suggestions,
            )
        )

    return SessionColorQcResult(
        session_id=session.session_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=overall_status(issues),
        reference_angle=reference_angle or session.reference_angle,
        issues=issues,
        takes=take_summaries,
    )


def session_color_to_dict(result: SessionColorQcResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "generated_at": result.generated_at,
        "status": result.status,
        "reference_angle": result.reference_angle,
        "issues": issues_to_dict(result.issues),
        "takes": [
            {
                "take_id": take.take_id,
                "status": take.status,
                "reference_angle": take.reference_angle,
                "videos": [asdict(video) for video in take.videos],
                "suggestions": [asdict(suggestion) for suggestion in take.suggestions],
            }
            for take in result.takes
        ],
    }


def write_session_color_reports(session: SessionProjectConfig, result: SessionColorQcResult) -> Path:
    payload = session_color_to_dict(result)
    json_path = session.reports_path("color-qc.json")
    write_json_report(json_path, payload)
    return json_path
