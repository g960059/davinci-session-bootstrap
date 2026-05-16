"""Cross-angle CDL color matching.

Extracts per-angle white-key and piano-body reference RGB values from calibrated
ROI boxes, then fits ASC CDL parameters (Slope + Offset, Power fixed at 1.0) to
match each non-reference angle to the reference angle.

All color math is performed in **scene-linear BT.709**. The existing
`_normalize_for_analysis` path in `color_qc.py` ends with a BT.709 OETF encoding
step — that path is for display preview only. Here, the helper
`to_linear_bt709` performs HLG OETF inverse and BT.2020 → BT.709 primaries but
stops BEFORE the OETF, so values remain scene-linear. CDL operates on linear
signals; fitting in gamma-encoded space is mathematically incorrect.

Validation uses per-anchor `log` residuals rather than CIE ΔE₂₀₀₀. ΔE is
display-referred, whitepoint-dependent, and unstable for dark neutrals such as
a glossy black piano body. The three residuals (`|log(R/G)|`, `|log(B/G)|`,
`|log Y|`) map directly to the two things we care about: chromaticity match
and luminance match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from scipy import optimize

from piano_guard.color_qc import BT2020_TO_BT709, _hlg_to_linear
from piano_guard.config import AngleCalibration, CalibrationBox, SessionCalibration, TakeConfig
from piano_guard.ingest import VideoClipInfo


# --- constants ---

DEFAULT_N_FRAMES = 120
"""Number of temporal samples per clip. ~1 sample/second at 29.97 fps on a
2-minute take. Balances robustness against analysis time."""

MOTION_THRESHOLD = 0.02
"""Linear-space absdiff threshold for motion detection. ~2% scene-linear
difference from the calibration reference triggers the motion flag."""

MOTION_BOX_FRACTION = 0.10
"""A box is rejected for a given frame if more than 10% of its pixels are
in motion. Rejection is per-box, not per-frame."""

TRIM_INNER_FRACTION = 0.20
"""Drop the outer 20% of each box and use only the central 60% to avoid
edge bleed and keystone-like perspective distortion at box boundaries."""

TRIM_LUMA_FRACTION = 0.10
"""Drop the top and bottom 10% of pixel luma within a box before averaging.
Removes specular highlights and shadow leaks."""

MIN_WHITE_KEY_SAMPLES = 30
"""Per take per angle, require at least this many accepted (frame, box) samples
on the white-key side before trusting the per-take median."""

MIN_BODY_SAMPLES = 15
"""Same for the body anchor."""

SLOPE_BOUNDS = (0.80, 1.25)
OFFSET_BOUNDS = (-0.05, 0.05)

# --- richer-transform escape hatch bounds (Milestone 5) ---
# These widen the 2-point solve to cover cases CDL can't (e.g., dim target
# needing >2× gain to reach bright reference). The bounds are still tight
# enough to reject degenerate solutions (near-zero denominators, negative
# gains) but ~10× the CDL range. The result is exported as a 3D LUT on a
# separate remote version so the colorist can compare against the clipped
# CDL on the sibling version.
RICHER_SLOPE_BOUNDS = (0.10, 10.0)
RICHER_OFFSET_BOUNDS = (-0.30, 0.30)

# Linear-residual gates
PASS_THRESHOLD = 0.05
FAIL_THRESHOLD = 0.10

# Reference-angle data-quality gates
REF_WHITE_KEY_MAX = 0.99   # no channel above this in linear (clipping guard)
                           # Tolerant: HLG 8-bit + BT.2020→BT.709 matrix can push warm
                           # whites close to 1.0 without actually clipping the source.
                           # 1.0 is the only truly-lost-information value.
REF_BODY_MIN = 0.001       # no channel below this in linear (noise floor)
                           # HLG 8-bit black values commonly sit at 0.002–0.005 after
                           # linearization; 0.001 accommodates that while still
                           # flagging truly crushed blacks where the signal is lost.
REF_WB_SEPARATION = 5.0    # mean(W) / mean(B) must exceed this for a usable fit


# --- color-space conversions ---


def to_linear_bt709(frame_rgb_u8: np.ndarray, video: VideoClipInfo) -> np.ndarray:
    """Convert an sRGB-encoded uint8 frame to scene-linear BT.709.

    This is the measurement helper. It does NOT apply the BT.709 OETF at the
    end — the output is scene-linear, suitable for CDL fitting.

    Input: uint8 HxWx3 RGB as OpenCV delivers it (already BT.2020 primaries if
    the source is HLG, since ffmpeg decodes HLG into uint8 that preserves the
    HLG OETF encoding).

    Steps: uint8 → float32[0,1] → HLG OETF inverse (if HLG transfer) → BT.2020
    → BT.709 matrix (if BT.2020 primaries). Output remains float32 linear.
    """
    rgb = np.clip(frame_rgb_u8.astype(np.float32) / 255.0, 0.0, 1.0)
    if video.color_transfer == "arib-std-b67":
        rgb = _hlg_to_linear(rgb)
    if video.color_primaries == "bt2020":
        rgb = np.tensordot(rgb, BT2020_TO_BT709.T, axes=1)
    return np.clip(rgb, 0.0, 1.0)


# --- data shapes ---


@dataclass
class BoxSamples:
    """Accepted samples from a single calibration box across a take."""

    box: CalibrationBox
    channel_medians: np.ndarray  # shape (3,), per-channel median across accepted frames
    accepted_frames: int
    total_frames: int


@dataclass
class AngleColors:
    """Aggregated per-angle reference RGB values from one take."""

    angle: str
    take_id: str
    white_key_rgb_linear: np.ndarray  # shape (3,)
    body_rgb_linear: np.ndarray  # shape (3,)
    white_key_samples: int
    body_samples: int
    confidence: float  # 0.0 .. 1.0
    notes: list[str] = field(default_factory=list)


@dataclass
class RicherTransformResult:
    """Escape-hatch transform for states where CDL bounds are insufficient.

    A richer transform = per-channel gain + per-channel offset + (in v2) a
    3×3 chroma-rotation matrix + 1D luma shaper. v1 uses only the first
    two (identity matrix + identity shaper) because widening the 2-point
    CDL bounds ~10× already resolves the dominant Milestone 3 failure
    mode (dim regimes needing >1.25× gain).

    Exported as a 3D LUT (.cube) applied via ``TimelineItem.SetLUT`` on
    an ``auto-state-NN-v-escape`` remote version, sibling to the
    clipped-CDL ``auto-state-NN-v1`` so the colorist can A/B them.

    ``matrix_3x3`` is always identity in v1; the field exists so the
    serialization format doesn't change when v2 lands.

    ``shaper_1d`` is always None in v1; same forward-compat rationale.
    """

    target_angle: str
    reference_angle: str
    gain_rgb: tuple[float, float, float]
    offset_rgb: tuple[float, float, float]
    matrix_3x3: np.ndarray = field(
        default_factory=lambda: np.eye(3, dtype=np.float32)
    )
    shaper_1d: np.ndarray | None = None
    residuals: dict[str, dict[str, float]] = field(default_factory=dict)
    status: str = "pass"
    code: str = ""

    def is_identity_matrix(self) -> bool:
        return bool(np.allclose(self.matrix_3x3, np.eye(3), atol=1e-6))

    def apply(self, rgb: np.ndarray) -> np.ndarray:
        """Apply this transform to an RGB triple or stack. For v1 this is
        linear ``out = clip(gain * in + offset, 0, 1)`` since matrix is
        identity and shaper is None. In v2 this will also apply the
        matrix and shaper.
        """
        gain = np.asarray(self.gain_rgb, dtype=np.float32)
        offset = np.asarray(self.offset_rgb, dtype=np.float32)
        out = np.asarray(rgb, dtype=np.float32) * gain + offset
        if not self.is_identity_matrix():
            out = out @ self.matrix_3x3.T
        # shaper_1d application deferred to v2
        return np.clip(out, 0.0, 1.0)


@dataclass
class CDLResult:
    """ASC CDL fit result for a single target angle vs a reference angle."""

    target_angle: str
    reference_angle: str
    slope_rgb: tuple[float, float, float]
    offset_rgb: tuple[float, float, float]
    power_rgb: tuple[float, float, float] = (1.0, 1.0, 1.0)
    residuals: dict[str, dict[str, float]] = field(default_factory=dict)
    status: str = "pass"  # "pass" / "warn" / "fail"
    code: str = ""         # "", "low_confidence", "spectral_mismatch", "unmatched"

    def to_setcdl_dict(self) -> dict[str, str]:
        """Format for Resolve's TimelineItem.SetCDL API.

        Values are formatted to 6 decimal places, keeping the Resolve-bound
        strings readable while preserving more than enough precision for CDL
        accuracy (Resolve internally uses 32-bit float).
        """

        def fmt3(triple: tuple[float, float, float]) -> str:
            return f"{triple[0]:.6f} {triple[1]:.6f} {triple[2]:.6f}"

        return {
            "NodeIndex": "1",
            "Slope": fmt3(self.slope_rgb),
            "Offset": fmt3(self.offset_rgb),
            "Power": fmt3(self.power_rgb),
            "Saturation": "1",
        }


# --- reference-angle data quality ---


def validate_reference_quality(
    white_key_rgb_linear: np.ndarray, body_rgb_linear: np.ndarray
) -> tuple[bool, list[str]]:
    """Check that the reference-angle anchors are usable for a two-point fit.

    Returns (is_ok, list_of_issues).
    """
    issues: list[str] = []
    if np.any(white_key_rgb_linear > REF_WHITE_KEY_MAX):
        issues.append(
            f"white-key linear value exceeds {REF_WHITE_KEY_MAX} "
            f"(clipping): {white_key_rgb_linear.tolist()}"
        )
    if np.any(body_rgb_linear < REF_BODY_MIN):
        issues.append(
            f"body linear value below {REF_BODY_MIN} (noise floor): {body_rgb_linear.tolist()}"
        )
    wk_mean = float(white_key_rgb_linear.mean())
    bd_mean = float(body_rgb_linear.mean())
    if bd_mean <= 0:
        issues.append("body mean is zero or negative; cannot compute separation")
    else:
        separation = wk_mean / bd_mean
        if separation < REF_WB_SEPARATION:
            issues.append(
                f"white-key/body luminance separation {separation:.2f}× is below "
                f"{REF_WB_SEPARATION}×; two-point fit will be ill-conditioned"
            )
    return (not issues, issues)


# --- color extraction ---


def _extract_box_pixels_linear(
    frame_linear: np.ndarray, box: CalibrationBox, frame_wh: tuple[int, int]
) -> np.ndarray:
    """Clip the box to the frame and crop the inner 60% to avoid edge bleed.
    Returns flat (N, 3) array of linear RGB pixels.
    """
    fw, fh = frame_wh
    x0 = max(0, box.x)
    y0 = max(0, box.y)
    x1 = min(fw, box.x + box.w)
    y1 = min(fh, box.y + box.h)
    if x1 <= x0 or y1 <= y0:
        return np.empty((0, 3), dtype=np.float32)
    # Inner 60% crop
    bx = x1 - x0
    by = y1 - y0
    x0i = x0 + int(bx * TRIM_INNER_FRACTION)
    x1i = x1 - int(bx * TRIM_INNER_FRACTION)
    y0i = y0 + int(by * TRIM_INNER_FRACTION)
    y1i = y1 - int(by * TRIM_INNER_FRACTION)
    if x1i <= x0i or y1i <= y0i:
        region = frame_linear[y0:y1, x0:x1]
    else:
        region = frame_linear[y0i:y1i, x0i:x1i]
    return region.reshape(-1, 3).astype(np.float32)


def _trimmed_mean_per_channel(pixels_linear: np.ndarray) -> np.ndarray:
    """Per-channel trimmed mean: drop top/bottom TRIM_LUMA_FRACTION by luma,
    then mean the remaining pixels. Returns (3,) array or zeros if empty.
    """
    if pixels_linear.size == 0:
        return np.zeros(3, dtype=np.float32)
    luma = 0.2126 * pixels_linear[:, 0] + 0.7152 * pixels_linear[:, 1] + 0.0722 * pixels_linear[:, 2]
    lo = np.quantile(luma, TRIM_LUMA_FRACTION)
    hi = np.quantile(luma, 1.0 - TRIM_LUMA_FRACTION)
    mask = (luma >= lo) & (luma <= hi)
    kept = pixels_linear[mask]
    if kept.size == 0:
        return pixels_linear.mean(axis=0).astype(np.float32)
    return kept.mean(axis=0).astype(np.float32)


def _box_motion_fraction(
    reference_linear: np.ndarray, frame_linear: np.ndarray, box: CalibrationBox
) -> float:
    """Fraction of box pixels whose linear-space absdiff exceeds MOTION_THRESHOLD."""
    fh, fw = frame_linear.shape[:2]
    x0 = max(0, box.x)
    y0 = max(0, box.y)
    x1 = min(fw, box.x + box.w)
    y1 = min(fh, box.y + box.h)
    if x1 <= x0 or y1 <= y0:
        return 1.0
    diff = np.abs(frame_linear[y0:y1, x0:x1] - reference_linear[y0:y1, x0:x1])
    motion = np.max(diff, axis=2) > MOTION_THRESHOLD
    return float(motion.mean())


ANALYSIS_WIDTH = 1920
ANALYSIS_HEIGHT = 1080
"""All calibration boxes are drawn at this resolution by `piano-guard calibrate`
(see cli.CALIBRATE_DISPLAY_WIDTH/HEIGHT). Sampled frames must be resized to
the same resolution before extraction, otherwise box coordinates index into
the wrong region of the frame. 4K sources (native 3840×2160) would sample
the upper-left quadrant instead of the intended region, and the motion
rejection absdiff would broadcast-fail on mismatched shapes."""


def _sample_frame_positions(capture: Any, n_frames: int) -> tuple[list[np.ndarray], list[int]]:
    """Sample n_frames evenly across the clip at ANALYSIS_WIDTH × ANALYSIS_HEIGHT.

    The resize is critical: calibration boxes are drawn in the 1920×1080
    display coordinate space; sampled frames must match that space.
    """
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames: list[np.ndarray] = []
    indices: list[int] = []
    if total <= 0:
        return frames, indices
    for i in range(n_frames):
        pos = (i + 0.5) / n_frames  # avoid exact 0 and 1 to stay within clip
        idx = max(0, min(total - 1, int(round(total * pos))))
        capture.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame_bgr = capture.read()
        if not ok or frame_bgr is None:
            continue
        resized_bgr = cv2.resize(frame_bgr, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT))
        frames.append(cv2.cvtColor(resized_bgr, cv2.COLOR_BGR2RGB))
        indices.append(idx)
    return frames, indices


def extract_angle_colors(
    take: TakeConfig,
    angle: str,
    angle_calibration: AngleCalibration,
    reference_frame_rgb_u8: np.ndarray | None,
    video: VideoClipInfo,
    *,
    n_frames: int = DEFAULT_N_FRAMES,
) -> AngleColors:
    """Extract per-angle reference RGB values from a take.

    Motion rejection strategy: self-consistency against the temporal median
    of each box's trimmed-mean value across sampled frames. Per-box, the
    median across N=120 samples represents the "static" state (piano body,
    unoccluded keys). Any sample that deviates from that median by more than
    MOTION_THRESHOLD in any channel is rejected for that box.

    This does NOT require an external calibration frame. The optional
    `reference_frame_rgb_u8` is retained for backwards compatibility but is
    currently unused in the self-consistency path; callers may pass None.

    Using self-consistency instead of a single calibration frame is the
    fix for a real bug: the previous design only applied motion rejection
    when a calibration frame was passed, which in practice only happened
    for the reference angle, leaving every target angle without motion
    filtering. Hands passing through a white-key box contaminated the
    temporal median of those angles.
    """
    del reference_frame_rgb_u8  # reserved for future per-frame motion reference mode
    video_path = Path(video.path)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"unable to open video for color extraction: {video_path}")

    try:
        frames_rgb_u8, _ = _sample_frame_positions(capture, n_frames)
    finally:
        capture.release()

    if not frames_rgb_u8:
        raise RuntimeError(f"unable to decode frames for {video_path}")

    # Linearize all sampled frames once up front
    frames_linear = [to_linear_bt709(frame, video) for frame in frames_rgb_u8]
    frame_wh = (frames_linear[0].shape[1], frames_linear[0].shape[0])

    def _aggregate(boxes: list[CalibrationBox]) -> tuple[np.ndarray, int, int]:
        total_accepted = 0
        total_attempted = 0
        per_box_means: list[np.ndarray] = []
        for box in boxes:
            # Pass 1: compute the trimmed-mean RGB of the box for every sampled
            # frame. Rejection happens in pass 2 after we know the temporal
            # median per channel for this box (the "static state" reference).
            raw_samples: list[np.ndarray] = []
            for frame_linear in frames_linear:
                total_attempted += 1
                pixels = _extract_box_pixels_linear(frame_linear, box, frame_wh)
                if pixels.size == 0:
                    raw_samples.append(None)  # type: ignore[arg-type]
                    continue
                raw_samples.append(_trimmed_mean_per_channel(pixels))

            usable = [s for s in raw_samples if s is not None]
            if not usable:
                continue
            stacked_raw = np.stack(usable, axis=0)
            temporal_median = np.median(stacked_raw, axis=0)

            # Pass 2: reject samples whose per-channel deviation from the
            # temporal median exceeds MOTION_THRESHOLD in any channel.
            accepted_values: list[np.ndarray] = []
            for sample in usable:
                deviation = np.max(np.abs(sample - temporal_median))
                if deviation > MOTION_THRESHOLD:
                    continue
                accepted_values.append(sample)
                total_accepted += 1
            if accepted_values:
                stacked = np.stack(accepted_values, axis=0)
                per_box_means.append(np.median(stacked, axis=0))
        if per_box_means:
            stacked = np.stack(per_box_means, axis=0)
            aggregate = np.median(stacked, axis=0).astype(np.float32)
        else:
            aggregate = np.zeros(3, dtype=np.float32)
        return aggregate, total_accepted, total_attempted

    white_key_rgb, wk_accepted, _wk_attempted = _aggregate(angle_calibration.white_key_boxes)
    body_rgb, bd_accepted, _bd_attempted = _aggregate(angle_calibration.piano_body_boxes)

    notes: list[str] = []
    if wk_accepted < MIN_WHITE_KEY_SAMPLES:
        notes.append(
            f"low white-key sample count: {wk_accepted} < {MIN_WHITE_KEY_SAMPLES}"
        )
    if bd_accepted < MIN_BODY_SAMPLES:
        notes.append(f"low body sample count: {bd_accepted} < {MIN_BODY_SAMPLES}")

    confidence = min(
        1.0,
        (wk_accepted / max(1, MIN_WHITE_KEY_SAMPLES)) * 0.5
        + (bd_accepted / max(1, MIN_BODY_SAMPLES)) * 0.5,
    )

    return AngleColors(
        angle=angle,
        take_id=take.take_id,
        white_key_rgb_linear=white_key_rgb,
        body_rgb_linear=body_rgb,
        white_key_samples=wk_accepted,
        body_samples=bd_accepted,
        confidence=float(confidence),
        notes=notes,
    )


# --- CDL fitting ---


def _residuals_for_fit(
    params: np.ndarray,
    wk_src: np.ndarray,
    bd_src: np.ndarray,
    wk_ref: np.ndarray,
    bd_ref: np.ndarray,
) -> np.ndarray:
    """Residuals for scipy.optimize.least_squares.

    `params` is a length-6 array [slope_r, slope_g, slope_b, offset_r,
    offset_g, offset_b]. Power is fixed at 1.0.

    The CDL transform is `out = clip(in * slope + offset, 0, 1)` per channel.
    We return 6 residuals: per-channel white-key and body differences after
    applying the CDL to the source anchors.
    """
    slope = params[:3]
    offset = params[3:6]
    wk_fit = np.clip(wk_src * slope + offset, 0.0, 1.0)
    bd_fit = np.clip(bd_src * slope + offset, 0.0, 1.0)
    return np.concatenate([wk_fit - wk_ref, bd_fit - bd_ref])


def _log_ratio(values: np.ndarray, eps: float = 1e-4) -> np.ndarray:
    """log(R/G), log(B/G) style chromaticity. values shape (3,)."""
    safe = np.maximum(values, eps)
    return np.array([np.log(safe[0] / safe[1]), np.log(safe[2] / safe[1])])


def _luma_bt709(values: np.ndarray) -> float:
    return float(0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2])


def _residual_stats(fit: np.ndarray, ref: np.ndarray) -> dict[str, float]:
    """Return chroma_r, chroma_b, luma errors for a single anchor pair."""
    eps = 1e-4
    chroma_fit = _log_ratio(fit, eps)
    chroma_ref = _log_ratio(ref, eps)
    return {
        "chroma_r": float(abs(chroma_fit[0] - chroma_ref[0])),
        "chroma_b": float(abs(chroma_fit[1] - chroma_ref[1])),
        "luma": float(
            abs(np.log(max(_luma_bt709(fit), eps)) - np.log(max(_luma_bt709(ref), eps)))
        ),
    }


def _classify(
    wk_res: dict[str, float], bd_res: dict[str, float], wk_fit: np.ndarray, bd_fit: np.ndarray, wk_ref: np.ndarray, bd_ref: np.ndarray,
) -> tuple[str, str]:
    """Classify the fit quality into status + code based on the residuals."""
    max_residual = max(
        wk_res["chroma_r"], wk_res["chroma_b"], wk_res["luma"],
        bd_res["chroma_r"], bd_res["chroma_b"], bd_res["luma"],
    )

    if max_residual > FAIL_THRESHOLD:
        return ("fail", "unmatched")

    if max_residual <= PASS_THRESHOLD:
        return ("pass", "")

    # In (PASS, FAIL] band. Distinguish exposure drift vs spectral mismatch.
    # Exposure / global WB drift: chromaticity errors on wk and bd point in
    # the SAME direction (both fit is redder than ref, or both bluer).
    # Spectral mismatch: they point in OPPOSITE directions (wk bluer, bd redder
    # or vice versa) — a single 3-channel CDL can't fix that.
    wk_chroma = _log_ratio(wk_fit) - _log_ratio(wk_ref)
    bd_chroma = _log_ratio(bd_fit) - _log_ratio(bd_ref)
    # Opposite direction if chroma signs differ on either axis
    opposite = (wk_chroma[0] * bd_chroma[0] < 0) or (wk_chroma[1] * bd_chroma[1] < 0)

    if opposite and (max(wk_res["chroma_r"], wk_res["chroma_b"], bd_res["chroma_r"], bd_res["chroma_b"]) > PASS_THRESHOLD):
        return ("warn", "spectral_mismatch")
    return ("warn", "low_confidence")


def fit_cdl_from_fingerprints(
    reference,
    target,
    *,
    reference_label: str = "reference",
    target_label: str = "target",
) -> CDLResult:
    """Fit a CDL between two ``LightingFingerprint`` instances.

    This is the Milestone 3 entrypoint: per-lighting-state CDL fitting takes
    fingerprint medoids as inputs. The fingerprint ``white_key_rgb`` and
    ``piano_body_rgb`` fields are already in scene-linear BT.709, so we can
    adapt straight to an ``AngleColors`` wrapper and reuse the existing
    two-point solver without duplicating the optimization code.

    ``reference_label`` and ``target_label`` populate the ``CDLResult`` so
    downstream reports can identify which lighting state / clip was graded
    into which reference. Typical values: ``f"state-{state_id}"``.
    """
    # Local import avoids a circular dependency between color_match and
    # auto_segment (auto_segment does not import color_match at module
    # scope, but any consumer calling fit_cdl_from_fingerprints has both
    # modules loaded anyway — this keeps the static check clean).
    from piano_guard.auto_segment import LightingFingerprint

    if not isinstance(reference, LightingFingerprint):
        raise TypeError(
            f"fit_cdl_from_fingerprints expects LightingFingerprint, got {type(reference).__name__}"
        )
    if not isinstance(target, LightingFingerprint):
        raise TypeError(
            f"fit_cdl_from_fingerprints expects LightingFingerprint, got {type(target).__name__}"
        )

    ref_angle = AngleColors(
        angle=reference_label,
        take_id="fingerprint",
        white_key_rgb_linear=np.asarray(reference.white_key_rgb, dtype=np.float32),
        body_rgb_linear=np.asarray(reference.piano_body_rgb, dtype=np.float32),
        white_key_samples=int(reference.white_key_pixels),
        body_samples=int(reference.body_pixels),
        confidence=1.0,
    )
    tgt_angle = AngleColors(
        angle=target_label,
        take_id="fingerprint",
        white_key_rgb_linear=np.asarray(target.white_key_rgb, dtype=np.float32),
        body_rgb_linear=np.asarray(target.piano_body_rgb, dtype=np.float32),
        white_key_samples=int(target.white_key_pixels),
        body_samples=int(target.body_pixels),
        confidence=1.0,
    )
    return fit_cdl(ref_angle, tgt_angle)


def fit_cdl(reference: AngleColors, target: AngleColors) -> CDLResult:
    """Fit a CDL to bring target to reference via 2-point Slope + Offset match."""
    wk_ref = np.asarray(reference.white_key_rgb_linear, dtype=np.float64)
    bd_ref = np.asarray(reference.body_rgb_linear, dtype=np.float64)
    wk_tgt = np.asarray(target.white_key_rgb_linear, dtype=np.float64)
    bd_tgt = np.asarray(target.body_rgb_linear, dtype=np.float64)

    # Two-point direct solve for initialization, per channel.
    # slope = (wk_ref - bd_ref) / (wk_tgt - bd_tgt), per channel
    denom = wk_tgt - bd_tgt
    # Guard against near-zero denominator (source has no W/B separation)
    safe_denom = np.where(np.abs(denom) < 1e-4, 1e-4 * np.sign(denom + 1e-12), denom)
    slope_init = (wk_ref - bd_ref) / safe_denom
    offset_init = bd_ref - slope_init * bd_tgt

    # Clip to bounds for a feasible starting point
    slope_init = np.clip(slope_init, *SLOPE_BOUNDS)
    offset_init = np.clip(offset_init, *OFFSET_BOUNDS)

    x0 = np.concatenate([slope_init, offset_init])
    lower = np.array([SLOPE_BOUNDS[0]] * 3 + [OFFSET_BOUNDS[0]] * 3)
    upper = np.array([SLOPE_BOUNDS[1]] * 3 + [OFFSET_BOUNDS[1]] * 3)

    fit_fallback = False
    try:
        result = optimize.least_squares(
            _residuals_for_fit,
            x0,
            bounds=(lower, upper),
            loss="soft_l1",
            args=(wk_tgt, bd_tgt, wk_ref, bd_ref),
        )
        slope = result.x[:3]
        offset = result.x[3:6]
    except Exception:
        # Fall back to the direct-solve init. This is rare but worth recording
        # in the result so the operator can see why the solver didn't refine.
        slope = slope_init
        offset = offset_init
        fit_fallback = True

    # Apply the CDL to the source anchors to compute residuals
    wk_fit = np.clip(wk_tgt * slope + offset, 0.0, 1.0)
    bd_fit = np.clip(bd_tgt * slope + offset, 0.0, 1.0)

    wk_residuals = _residual_stats(wk_fit, wk_ref)
    bd_residuals = _residual_stats(bd_fit, bd_ref)

    status, code = _classify(wk_residuals, bd_residuals, wk_fit, bd_fit, wk_ref, bd_ref)
    if fit_fallback and status == "pass":
        # If the refiner crashed but the direct-solve init still satisfies the
        # gates, downgrade to WARN with a specific code so the operator knows
        # the scipy refinement didn't run.
        status = "warn"
        code = "fit_fallback_to_init"

    return CDLResult(
        target_angle=target.angle,
        reference_angle=reference.angle,
        slope_rgb=(float(slope[0]), float(slope[1]), float(slope[2])),
        offset_rgb=(float(offset[0]), float(offset[1]), float(offset[2])),
        residuals={"white_key": wk_residuals, "body": bd_residuals},
        status=status,
        code=code,
    )


def fit_richer_transform(
    reference,
    target,
    *,
    reference_label: str = "reference",
    target_label: str = "target",
) -> RicherTransformResult:
    """Milestone 5 escape-hatch fit for a (reference, target) fingerprint pair.

    Same 2-point linear solve as ``fit_cdl_from_fingerprints`` but with
    wider slope/offset bounds (see ``RICHER_SLOPE_BOUNDS`` and
    ``RICHER_OFFSET_BOUNDS``). Addresses the dominant Milestone 3 failure
    mode where CDL's [0.8, 1.25] slope clipping prevents a valid fit for
    regimes with >25% luma difference.

    v1 returns identity matrix + no shaper. v2 will add the chroma
    rotation matrix and luma shaper (parameterized by additional anchors
    or a texture-based residual).

    Status semantics mirror fit_cdl: 'pass' when all residuals are below
    PASS_THRESHOLD; 'warn' / 'fail' otherwise.
    """
    from piano_guard.auto_segment import LightingFingerprint

    if not isinstance(reference, LightingFingerprint):
        raise TypeError(
            f"fit_richer_transform expects LightingFingerprint, got {type(reference).__name__}"
        )
    if not isinstance(target, LightingFingerprint):
        raise TypeError(
            f"fit_richer_transform expects LightingFingerprint, got {type(target).__name__}"
        )

    wk_ref = np.asarray(reference.white_key_rgb, dtype=np.float64)
    bd_ref = np.asarray(reference.piano_body_rgb, dtype=np.float64)
    wk_tgt = np.asarray(target.white_key_rgb, dtype=np.float64)
    bd_tgt = np.asarray(target.piano_body_rgb, dtype=np.float64)

    # Two-point direct solve per channel (exact for 6 equations, 6 params).
    denom = wk_tgt - bd_tgt
    safe_denom = np.where(
        np.abs(denom) < 1e-4, 1e-4 * np.sign(denom + 1e-12), denom
    )
    gain = (wk_ref - bd_ref) / safe_denom
    offset = bd_ref - gain * bd_tgt

    # Clip to richer bounds — still a sanity guard but far looser than CDL.
    gain = np.clip(gain, *RICHER_SLOPE_BOUNDS)
    offset = np.clip(offset, *RICHER_OFFSET_BOUNDS)

    # Evaluate fit
    wk_fit = np.clip(wk_tgt * gain + offset, 0.0, 1.0)
    bd_fit = np.clip(bd_tgt * gain + offset, 0.0, 1.0)
    wk_residuals = _residual_stats(wk_fit, wk_ref)
    bd_residuals = _residual_stats(bd_fit, bd_ref)

    max_residual = max(
        wk_residuals["chroma_r"], wk_residuals["chroma_b"], wk_residuals["luma"],
        bd_residuals["chroma_r"], bd_residuals["chroma_b"], bd_residuals["luma"],
    )
    if max_residual > FAIL_THRESHOLD:
        status, code = "fail", "unmatched_even_with_richer"
    elif max_residual > PASS_THRESHOLD:
        status, code = "warn", "low_confidence"
    else:
        status, code = "pass", ""

    return RicherTransformResult(
        target_angle=target_label,
        reference_angle=reference_label,
        gain_rgb=(float(gain[0]), float(gain[1]), float(gain[2])),
        offset_rgb=(float(offset[0]), float(offset[1]), float(offset[2])),
        residuals={"white_key": wk_residuals, "body": bd_residuals},
        status=status,
        code=code,
    )


__all__ = [
    "to_linear_bt709",
    "BoxSamples",
    "AngleColors",
    "CDLResult",
    "RicherTransformResult",
    "validate_reference_quality",
    "extract_angle_colors",
    "fit_cdl",
    "fit_cdl_from_fingerprints",
    "fit_richer_transform",
    "PASS_THRESHOLD",
    "FAIL_THRESHOLD",
    "DEFAULT_N_FRAMES",
    "REF_WHITE_KEY_MAX",
    "REF_BODY_MIN",
    "REF_WB_SEPARATION",
    "RICHER_SLOPE_BOUNDS",
    "RICHER_OFFSET_BOUNDS",
]
