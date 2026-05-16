"""Piano segmentation and lighting-state fingerprinting for Phase 2.

The Phase 1.5 pipeline asked the operator to annotate ROI boxes on white
keys and piano body once per session. Phase 2 (see ADR 2026-04-15) replaces
that with automatic piano segmentation: SAM 2 with CV-seeded point prompts
locates the keyboard strip and the glossy-black body, and per-window
fingerprints are computed on the resulting masks in scene-linear BT.709.

Milestone 1 scope: masking + fingerprinting primitives. Clustering and
changepoint detection live in Milestone 2; per-segment CDL fitting and
Resolve application in Milestone 3.

Design choices:
  * SAM 2 is loaded lazily (heavy import; only pay the cost when the
    pipeline is actually invoked).
  * CV seed: gradient-energy band detection reuses the algorithm proven in
    the Phase 0 probe against the live test session.
  * Fallback path: when SAM 2 confidence is below `MASK_CONFIDENCE_THRESHOLD`,
    fall back to a caller-supplied `fallback_rois` dict (typically parsed
    from `calibration.yaml`). This makes Milestone 1 a strict superset of
    Phase 1.5 — it strictly improves operators who already calibrated
    without requiring zero-annotation to work on day one.
  * Fingerprints are computed in scene-linear BT.709, never in the
    gamma-encoded `_normalize_for_analysis` path. The correct helper is
    `color_match.to_linear_bt709`, which Phase 0 Probe 2 settled as the
    default fitting space (revisit if end-to-end verification surfaces
    DaVinci WG vs BT.709 visible differences on SetCDL output).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np


# --- constants ---

ANALYSIS_WIDTH = 1920
ANALYSIS_HEIGHT = 1080
"""All masks and fingerprints operate at 1920x1080 so that calibration
ROIs drawn in the Phase 1.5 `calibrate` UI remain valid as fallback masks."""

MASK_CONFIDENCE_THRESHOLD = 0.60
"""Below this combined SAM 2 score + structural sanity check, fall back to
`fallback_rois` if provided, otherwise surface a low-confidence warning."""

DARK_LUMA_THRESHOLD = 80
"""A pixel is 'dark enough to be piano body' if its uint8 luma is below
this value. Used for the body point-prompt seed."""

TRIM_LUMA_FRACTION = 0.10
"""Drop top and bottom 10% of pixel luma before per-channel mean, to
exclude specular highlights from piano lacquer and shadow leaks."""

WHITE_KEY_LUMA_MIN = 0.10
"""Linear-space luma floor for pixels accepted into the white-key
fingerprint average. Pixels below this are assumed to be black keys,
shadows, or bad mask bleed — even when the mask includes them. The
threshold is deliberately generous: a healthy HLG white-key pixel
linearizes to > 0.3, but underexposed takes may drop to ~0.15, so 0.10
keeps the trim forgiving."""

WHITE_KEY_LUMA_MAX = 0.98
"""Specular clip cutoff for white-key pixels. Anything above this is
lost-information (the HLG→linear pipeline often pushes healthy whites
to > 0.95); including clipped pixels biases the mean toward 1.0."""

PIANO_BODY_LUMA_MAX = 0.30
"""Linear-space luma ceiling for pixels accepted into the piano-body
fingerprint average. Piano body lacquer is a mirror — it reflects
windows, ceiling lights, sheet music stands, the pianist's face.
Reflected-content pixels have luma far above the true body material
(typically ~0.01-0.05 linear). Rejecting pixels above 0.30 excludes
those reflection artifacts while keeping the darker side of any
gradient on the real body."""

PIANO_BODY_LUMA_MIN = 0.001
"""Noise-floor cutoff for body pixels. Crushed blacks carry no color
information (the codec quantizes them to zero). Same threshold as the
reference-angle validation so fingerprints aren't computed from
material that can't be fit anyway."""

MIN_ACCEPTED_MASK_PIXELS_AFTER_FILTER = 50
"""Per mask, after luma filtering, at least this many pixels must
survive or the fingerprint is flagged low-quality. 50 is ~0.002% of a
1920x1080 frame — tiny but non-zero. Below this and the per-channel
trimmed mean becomes statistically unstable."""

MIN_MASK_PIXELS = 200
"""Masks with fewer than this many pixels are treated as degenerate."""

MIN_SEGMENT_DURATION_S = 5.0
"""Segments shorter than this are merged back into their neighbors. A
5-second floor is long enough to fit most musical phrases and short enough
to preserve genuine regime changes (e.g., someone turning a lamp on)."""

CHANGEPOINT_WINDOW_SIZE = 10
"""Number of fingerprints on each side of a candidate boundary when
computing the percentile-gap statistic. At the default 1.5 s window
stride this is ~15 s of context, which filters per-chord chroma jitter
but preserves 30 s-ish illumination regime changes."""

CHANGEPOINT_K_THRESHOLD = 3.0
"""A gap must exceed ``K * median(gap)`` to be considered a candidate
changepoint. Values < 3.0 produce false positives on the test session;
values > 4.0 miss the LEFT↔RIGHT camera-group chroma shift between
angle-a/b and angle-d."""

CHANGEPOINT_CONFIDENCE_THRESHOLD = 0.7
"""A normalized gap magnitude in [0, 1]; boundaries below this are
rejected as low-confidence. Defined as
``min(1, max(0, (gap - median) / (2 * median)))``."""

HDBSCAN_MIN_CLUSTER_SIZE = 3
"""Absolute floor for min_cluster_size. Below this, noise dominates
HDBSCAN's output regardless of dataset size. Used as the lower bound
for the adaptive rule below."""

HDBSCAN_MIN_SAMPLES = 2
"""HDBSCAN conservativeness floor. Used as the lower bound for the
adaptive rule."""

HDBSCAN_CLUSTER_SELECTION_EPSILON = 0.10
"""Merge clusters whose feature-space distance is below this. Tuned from
E2E data on a 27-minute piano session: within-regime fingerprint spread
on real footage is ~0.05-0.12 in 6-D feature space (hand movements,
camera noise, minor pose shifts), so 0.10 correctly merges these while
keeping day-vs-night (~0.80 apart) distinct. The old 0.05 over-
fragmented into 30+ spurious states on real data."""

HDBSCAN_MIN_CLUSTER_FRACTION = 0.02
"""Adaptive ``min_cluster_size`` = ``max(floor, total_windows * fraction)``.
A lighting state must cover at least 2% of the session's total windows
to be considered a real regime, not a transient blip. For a typical
90-minute session at 1.5 s windows (3600 windows), this gives
min_cluster_size=72; for a 5-minute short session (200 windows), it
gives the floor of 3. Scales with data volume so the same code works
for 1-take and 10-take sessions without retuning."""

CLUSTER_COLLAPSE_SPREAD = 0.10
"""Maximum pairwise feature-vector L2 distance below which an all-noise
HDBSCAN result is re-interpreted as 'single stable state' rather than
'unresolvable'. Windows within one genuine lighting regime typically sit
within 0.02-0.05 of each other in 6-D feature space; cross-regime
distances exceed 0.5. 0.10 gives plenty of headroom without conflating
regimes."""


# --- data shapes ---


@dataclass
class PianoMasks:
    """Per-frame piano segmentation result.

    Attributes
    ----------
    white_key_mask, piano_body_mask:
        Boolean arrays at ANALYSIS_WIDTH x ANALYSIS_HEIGHT.
    confidence:
        Combined score in [0, 1]. 1.0 is ideal.
    source:
        ``"sam2"`` when both masks are from SAM 2, ``"fallback_roi"`` when
        at least one mask fell back to `fallback_rois`, ``"mixed"`` if the
        two masks came from different sources.
    strategy_issues:
        Human-readable strings describing why any SAM 2 strategy was
        skipped (exception raised, score below threshold, etc.). Populated
        by ``detect_piano_masks``. Upstream pipeline code surfaces these
        as Issues in the report so operators can see silent fallbacks.
    """

    white_key_mask: np.ndarray
    piano_body_mask: np.ndarray
    confidence: float
    source: str
    strategy_issues: list[str] = field(default_factory=list)

    @property
    def white_key_pixels(self) -> int:
        return int(self.white_key_mask.sum())

    @property
    def body_pixels(self) -> int:
        return int(self.piano_body_mask.sum())

    @property
    def both_masks_nonempty(self) -> bool:
        return self.white_key_pixels > 0 and self.body_pixels > 0


@dataclass
class LightingFingerprint:
    """Scene-linear RGB statistics for a window of frames.

    Used as the clustering feature vector in Milestone 2 and the fit input
    for per-segment CDL in Milestone 3. Values are in scene-linear BT.709
    (NOT gamma-encoded).
    """

    white_key_rgb: np.ndarray  # shape (3,), trimmed-mean
    piano_body_rgb: np.ndarray  # shape (3,), trimmed-mean
    luma_p05: float
    luma_p50: float
    luma_p95: float
    estimated_cct_kelvin: float
    white_key_pixels: int
    body_pixels: int
    window_start_s: float = 0.0
    window_end_s: float = 0.0
    frame_indices: list[int] = field(default_factory=list)


@dataclass
class LightingState:
    """One HDBSCAN cluster of fingerprints representing a single lighting
    regime. One state becomes one CDL in Milestone 3.
    """

    state_id: int
    centroid_features: np.ndarray  # shape (FEATURE_DIM,), mean of members
    representative_fingerprint: LightingFingerprint  # medoid in feature space
    member_indices: list[int] = field(default_factory=list)
    member_probabilities: list[float] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        return len(self.member_indices)


@dataclass
class Boundary:
    """A changepoint between two lighting regimes within a single clip.

    ``window_index`` is the index of the first fingerprint in the *right*
    segment — so segments are ``[prev_boundary, window_index)`` and
    ``[window_index, next_boundary)``.
    """

    window_index: int
    time_s: float
    confidence: float


# --- CV seed detection (reused from Phase 0 probe) ---


def find_keyboard_region(gray: np.ndarray, *, block: int = 32) -> tuple[int, int, int, int]:
    """Detect the keyboard bounding box via gradient-energy band analysis.

    Returns ``(x_min, y_min, x_max, y_max)`` in pixel coordinates. This is
    intentionally a rough estimate — SAM 2 refines the mask from the box
    prompt. The algorithm is lifted from the Phase 0 probe that verified
    this approach worked on all three test-session angles.
    """
    h, w = gray.shape
    grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
    grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
    grad_mag = np.sqrt(grad_x**2 + grad_y**2).astype(np.float32)

    kernel = np.ones((block, block), np.float32) / (block**2)
    energy = cv2.filter2D(grad_mag, -1, kernel)

    row_energy = energy.mean(axis=1)
    row_smooth = cv2.GaussianBlur(row_energy.reshape(-1, 1), (1, 51), 0).flatten()
    if row_smooth.max() == 0:
        return (0, 0, w, h)

    threshold = row_smooth.max() * 0.5
    above = np.where(row_smooth > threshold)[0]
    if len(above) == 0:
        return (0, 0, w, h)
    y_min = int(above.min())
    y_max = int(above.max())

    band_energy = energy[y_min:y_max, :].mean(axis=0)
    band_smooth = cv2.GaussianBlur(band_energy.reshape(-1, 1), (1, 51), 0).flatten()
    if band_smooth.max() == 0:
        return (0, y_min, w, y_max)
    col_threshold = band_smooth.max() * 0.3
    above_cols = np.where(band_smooth > col_threshold)[0]
    if len(above_cols) == 0:
        return (0, y_min, w, y_max)
    x_min = int(above_cols.min())
    x_max = int(above_cols.max())

    return (x_min, y_min, x_max, y_max)


def _find_body_seed(
    gray: np.ndarray, keyboard_mask: np.ndarray | None
) -> tuple[int, int] | None:
    """Find a point prompt for the piano body mask.

    Strategy: largest connected dark (luma < DARK_LUMA_THRESHOLD) region that
    doesn't overlap the keyboard mask. Centroid returned. None when no such
    region is usable.
    """
    dark = gray < DARK_LUMA_THRESHOLD
    if keyboard_mask is not None:
        dark = dark & ~keyboard_mask
    u8 = dark.astype(np.uint8) * 255
    contours, _ = cv2.findContours(u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    largest = max(contours, key=cv2.contourArea)
    moments = cv2.moments(largest)
    if moments["m00"] < MIN_MASK_PIXELS:
        return None
    cx = int(moments["m10"] / moments["m00"])
    cy = int(moments["m01"] / moments["m00"])
    return cx, cy


# --- SAM 2 lazy loader ---


_sam2_predictor_cache: Any = None
_sam2_load_attempted = False
_sam2_load_error: str | None = None


def _get_sam2_predictor() -> Any | None:
    """Load SAM 2 once per process. Returns None when unavailable."""
    global _sam2_predictor_cache, _sam2_load_attempted, _sam2_load_error
    if _sam2_load_attempted:
        return _sam2_predictor_cache

    _sam2_load_attempted = True

    checkpoint_path = _resolve_sam2_checkpoint()
    if checkpoint_path is None or not checkpoint_path.is_file():
        _sam2_load_error = (
            f"SAM 2 checkpoint not found "
            f"(PIANO_GUARD_SAM2_CHECKPOINT={checkpoint_path}); "
            "install via 'curl -L <sam2-small-url> -o <path>' "
            "and set PIANO_GUARD_SAM2_CHECKPOINT to the downloaded path."
        )
        return None

    try:
        import torch  # local import; heavy dependency
        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor
    except ImportError as exc:
        _sam2_load_error = f"SAM 2 or torch not importable: {exc}"
        return None

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    try:
        sam2_model = build_sam2(
            "configs/sam2.1/sam2.1_hiera_s.yaml", str(checkpoint_path), device=device
        )
        _sam2_predictor_cache = SAM2ImagePredictor(sam2_model)
    except Exception as exc:
        _sam2_load_error = f"SAM 2 load failed: {exc}"
        _sam2_predictor_cache = None
    return _sam2_predictor_cache


def _resolve_sam2_checkpoint() -> Path | None:
    import os

    env = os.environ.get("PIANO_GUARD_SAM2_CHECKPOINT")
    if env:
        return Path(env).expanduser()
    # Fall back to the Phase 0 probe location for dev convenience
    candidate = Path("/tmp/sam2_checkpoints/sam2.1_hiera_small.pt")
    if candidate.is_file():
        return candidate
    return None


def get_sam2_load_error() -> str | None:
    """Return the reason SAM 2 isn't available (or None if it is)."""
    _get_sam2_predictor()  # force load attempt if not yet tried
    return _sam2_load_error


# --- mask extraction ---


def _mask_from_box(predictor: Any, box_xyxy: tuple[int, int, int, int]) -> tuple[np.ndarray, float]:
    input_box = np.array(box_xyxy, dtype=np.float32)
    masks, scores, _ = predictor.predict(box=input_box[None, :], multimask_output=True)
    best_idx = int(np.argmax(scores))
    return masks[best_idx].astype(bool), float(scores[best_idx])


def _mask_from_point(predictor: Any, point_xy: tuple[int, int]) -> tuple[np.ndarray, float]:
    masks, scores, _ = predictor.predict(
        point_coords=np.array([point_xy], dtype=np.float32),
        point_labels=np.array([1]),
        multimask_output=True,
    )
    best_idx = int(np.argmax(scores))
    return masks[best_idx].astype(bool), float(scores[best_idx])


def _roi_dict_to_mask(
    rois: list[tuple[int, int, int, int]], shape: tuple[int, int]
) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for x, y, w, h in rois:
        x0 = max(0, x)
        y0 = max(0, y)
        x1 = min(shape[1], x + w)
        y1 = min(shape[0], y + h)
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask


def _mask_from_points(
    predictor: Any, points_xy: list[tuple[int, int]]
) -> tuple[np.ndarray, float]:
    """SAM 2 with multiple positive point prompts.

    Multiple points from the same class (e.g. white keys at bass/middle/treble)
    help SAM 2 grow the mask along the keyboard strip rather than collapsing
    to one key. All points are labeled 1 (positive).
    """
    if not points_xy:
        raise ValueError("points_xy must be non-empty")
    coords = np.array(points_xy, dtype=np.float32)
    labels = np.ones(len(points_xy), dtype=np.int32)
    masks, scores, _ = predictor.predict(
        point_coords=coords,
        point_labels=labels,
        multimask_output=True,
    )
    best_idx = int(np.argmax(scores))
    return masks[best_idx].astype(bool), float(scores[best_idx])


def _roi_centers(rois: list[tuple[int, int, int, int]]) -> list[tuple[int, int]]:
    """Convert (x, y, w, h) boxes to their center points for SAM 2 prompting."""
    return [(x + w // 2, y + h // 2) for x, y, w, h in rois]


def detect_piano_masks(
    frame_bgr: np.ndarray,
    *,
    fallback_rois: dict[str, list[tuple[int, int, int, int]]] | None = None,
) -> PianoMasks:
    """Return white-key and piano-body masks for a single frame.

    Mask-source priority (from most to least reliable):

    1. **SAM 2 with calibration.yaml point prompts** — when `fallback_rois`
       are provided, their centers are used as positive SAM 2 prompts. This
       is the most reliable path: manual ROIs provide correct seed locations
       regardless of camera angle, and SAM 2 expands them into accurate
       piano-surface masks. Labeled ``"sam2_from_rois"``.
    2. **SAM 2 with CV-seeded prompts** — `find_keyboard_region` provides a
       box prompt for keyboard, a darkest-connected-component centroid
       provides a point prompt for piano body. Works on angles where the
       keyboard dominates the frame (e.g. overhead shots) but can be fooled
       by bright windows on side-view angles. Labeled ``"sam2"``.
    3. **Raw ROI raster** — the ROI boxes are rasterized directly into
       masks. Equivalent to the Phase 1.5 fixed-ROI analysis. Labeled
       ``"fallback_roi"``.

    Used automatically in order; returns the first source that yields a
    mask with ≥ ``MIN_MASK_PIXELS`` and SAM 2 score ≥ 0.4 (when applicable).
    """
    if frame_bgr.shape[:2] != (ANALYSIS_HEIGHT, ANALYSIS_WIDTH):
        frame_bgr = cv2.resize(frame_bgr, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT))
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

    predictor = _get_sam2_predictor()
    if predictor is not None:
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        predictor.set_image(frame_rgb)

    white_key_mask: np.ndarray | None = None
    body_mask: np.ndarray | None = None
    keyboard_score = 0.0
    body_score = 0.0
    white_key_source = "fallback_roi"
    body_source = "fallback_roi"
    strategy_issues: list[str] = []

    # If SAM 2 is unavailable (missing checkpoint, import failed, MPS init
    # error, etc.), record the reason so the operator can see WHY we fell
    # straight to the raw-ROI raster path. Without this, missing SAM 2 is
    # invisible from the report — the operator just sees source=fallback_roi
    # without knowing whether SAM 2 failed or was never even attempted.
    if predictor is None:
        load_error = _sam2_load_error or "SAM 2 predictor unavailable (no reason recorded)"
        strategy_issues.append(f"sam2 unavailable: {load_error}")

    # Strategy 1: SAM 2 with ROI-center point prompts (most reliable)
    if predictor is not None and fallback_rois is not None:
        wk_boxes = fallback_rois.get("white_key_boxes") or []
        if wk_boxes:
            wk_points = _roi_centers(wk_boxes)
            try:
                kb_mask, kb_score = _mask_from_points(predictor, wk_points)
                if kb_mask.sum() >= MIN_MASK_PIXELS and kb_score >= 0.4:
                    white_key_mask = kb_mask
                    keyboard_score = kb_score
                    white_key_source = "sam2_from_rois"
                else:
                    strategy_issues.append(
                        f"sam2_from_rois white-key below threshold "
                        f"(score={kb_score:.3f}, pixels={int(kb_mask.sum())})"
                    )
            except Exception as exc:
                strategy_issues.append(f"sam2_from_rois white-key raised: {exc!r}")

        bd_boxes = fallback_rois.get("piano_body_boxes") or []
        if bd_boxes:
            bd_points = _roi_centers(bd_boxes)
            try:
                bd_mask, bd_score = _mask_from_points(predictor, bd_points)
                if white_key_mask is not None:
                    bd_mask = bd_mask & ~white_key_mask
                if bd_mask.sum() >= MIN_MASK_PIXELS and bd_score >= 0.4:
                    body_mask = bd_mask
                    body_score = bd_score
                    body_source = "sam2_from_rois"
                else:
                    strategy_issues.append(
                        f"sam2_from_rois body below threshold "
                        f"(score={bd_score:.3f}, pixels={int(bd_mask.sum())})"
                    )
            except Exception as exc:
                strategy_issues.append(f"sam2_from_rois body raised: {exc!r}")

    # Strategy 2: SAM 2 with CV-seeded prompts (for sessions without calibration)
    if predictor is not None:
        if white_key_mask is None:
            keyboard_box = find_keyboard_region(gray)
            try:
                kb_mask, kb_score = _mask_from_box(predictor, keyboard_box)
                if kb_mask.sum() >= MIN_MASK_PIXELS and kb_score >= 0.4:
                    white_key_mask = kb_mask
                    keyboard_score = kb_score
                    white_key_source = "sam2"
                else:
                    strategy_issues.append(
                        f"sam2 cv-seed white-key below threshold "
                        f"(score={kb_score:.3f}, pixels={int(kb_mask.sum())})"
                    )
            except Exception as exc:
                strategy_issues.append(f"sam2 cv-seed white-key raised: {exc!r}")
        if body_mask is None:
            body_seed = _find_body_seed(gray, white_key_mask)
            if body_seed is not None:
                try:
                    bd_mask, bd_score = _mask_from_point(predictor, body_seed)
                    if white_key_mask is not None:
                        bd_mask = bd_mask & ~white_key_mask
                    if bd_mask.sum() >= MIN_MASK_PIXELS and bd_score >= 0.4:
                        body_mask = bd_mask
                        body_score = bd_score
                        body_source = "sam2"
                    else:
                        strategy_issues.append(
                            f"sam2 cv-seed body below threshold "
                            f"(score={bd_score:.3f}, pixels={int(bd_mask.sum())})"
                        )
                except Exception as exc:
                    strategy_issues.append(f"sam2 cv-seed body raised: {exc!r}")
            else:
                strategy_issues.append("sam2 cv-seed body: no dark-region seed found")

    # Strategy 3: raw ROI raster fallback
    if fallback_rois is not None:
        shape = (ANALYSIS_HEIGHT, ANALYSIS_WIDTH)
        if white_key_mask is None:
            wk_roi = fallback_rois.get("white_key_boxes") or []
            if wk_roi:
                white_key_mask = _roi_dict_to_mask(wk_roi, shape)
                keyboard_score = 0.5
                white_key_source = "fallback_roi"
        if body_mask is None:
            bd_roi = fallback_rois.get("piano_body_boxes") or []
            if bd_roi:
                body_mask = _roi_dict_to_mask(bd_roi, shape)
                body_score = 0.5
                body_source = "fallback_roi"

    if white_key_mask is None:
        white_key_mask = np.zeros((ANALYSIS_HEIGHT, ANALYSIS_WIDTH), dtype=bool)
    if body_mask is None:
        body_mask = np.zeros((ANALYSIS_HEIGHT, ANALYSIS_WIDTH), dtype=bool)

    if white_key_source == body_source:
        source = white_key_source
    else:
        source = "mixed"

    # Combine scores: both masks must be good for high overall confidence
    confidence = min(keyboard_score, body_score)

    return PianoMasks(
        white_key_mask=white_key_mask,
        piano_body_mask=body_mask,
        confidence=confidence,
        source=source,
        strategy_issues=strategy_issues,
    )


# --- fingerprinting ---


def _trimmed_mean_masked(
    frame_linear: np.ndarray,
    mask: np.ndarray,
    *,
    luma_min: float | None = None,
    luma_max: float | None = None,
    trim: float = TRIM_LUMA_FRACTION,
) -> tuple[np.ndarray, int]:
    """Per-channel trimmed mean over the masked pixels of a linear frame.

    Returns ``(mean_rgb, accepted_pixel_count)``. The count lets callers
    detect fingerprints computed from too few surviving pixels after
    luma-range filtering — a sign that the mask is sampling the wrong
    material (e.g., a body mask landing on window reflections, or a
    white-key mask over black-key shadow gaps).

    Luma filtering happens BEFORE the percentile trim so the trim acts
    on material of the right tonal band.
    """
    pixels = frame_linear[mask]
    if pixels.size == 0:
        return np.zeros(3, dtype=np.float32), 0
    luma = 0.2126 * pixels[:, 0] + 0.7152 * pixels[:, 1] + 0.0722 * pixels[:, 2]

    # 1. Hard luma-range filter (catches reflections, shadows, clipped whites)
    keep_luma = np.ones_like(luma, dtype=bool)
    if luma_min is not None:
        keep_luma &= luma >= luma_min
    if luma_max is not None:
        keep_luma &= luma <= luma_max
    pixels_in_range = pixels[keep_luma]
    luma_in_range = luma[keep_luma]
    if pixels_in_range.size == 0:
        # Hard filter removed everything. Return zeros and let the
        # caller decide how to surface this.
        return np.zeros(3, dtype=np.float32), 0

    # 2. Percentile trim within the accepted luma band
    lo = float(np.quantile(luma_in_range, trim))
    hi = float(np.quantile(luma_in_range, 1.0 - trim))
    kept = pixels_in_range[(luma_in_range >= lo) & (luma_in_range <= hi)]
    if kept.size == 0:
        return pixels_in_range.mean(axis=0).astype(np.float32), int(pixels_in_range.shape[0])
    return kept.mean(axis=0).astype(np.float32), int(kept.shape[0])


# BT.709 primaries → CIE XYZ (D65). Same matrix as sRGB since they share primaries.
BT709_TO_XYZ = np.array(
    [
        [0.4124564, 0.3575761, 0.1804375],
        [0.2126729, 0.7151522, 0.0721750],
        [0.0193339, 0.1191920, 0.9503041],
    ],
    dtype=np.float32,
)


def estimate_cct_kelvin(rgb_linear: np.ndarray) -> float:
    """Estimate correlated color temperature (kelvin) from a single linear
    BT.709 RGB value. Uses McCamy's polynomial approximation on CIE xy.

    Returns 0.0 when the input is degenerate (all-zero or NaN).
    """
    if not np.all(np.isfinite(rgb_linear)) or rgb_linear.sum() <= 1e-6:
        return 0.0
    xyz = BT709_TO_XYZ @ rgb_linear.astype(np.float32)
    total = float(xyz.sum())
    if total <= 0:
        return 0.0
    x = float(xyz[0]) / total
    y = float(xyz[1]) / total
    # McCamy's approximation. Guard the division.
    denom = 0.1858 - y
    if abs(denom) < 1e-6:
        return 0.0
    n = (x - 0.3320) / denom
    cct = 449.0 * n**3 + 3525.0 * n**2 + 6823.3 * n + 5520.33
    # Clamp to a physically meaningful range; out-of-range values generally
    # mean the input is off the Planckian locus (e.g. heavily tinted).
    return float(max(1000.0, min(25000.0, cct)))


def compute_window_fingerprint(
    frame_linear_stack: np.ndarray,
    masks: PianoMasks,
    *,
    window_start_s: float = 0.0,
    window_end_s: float = 0.0,
    frame_indices: list[int] | None = None,
) -> LightingFingerprint:
    """Compute a LightingFingerprint from a stack of scene-linear frames.

    Parameters
    ----------
    frame_linear_stack:
        Either a single frame (H, W, 3) or a stack (N, H, W, 3). Scene-linear
        BT.709 values in [0, 1].
    masks:
        PianoMasks produced by `detect_piano_masks` on the representative
        frame of this window.
    """
    if frame_linear_stack.ndim == 3:
        stack = frame_linear_stack[np.newaxis, ...]
    elif frame_linear_stack.ndim == 4:
        stack = frame_linear_stack
    else:
        raise ValueError(f"frame_linear_stack must be HxWx3 or NxHxWx3; got shape {frame_linear_stack.shape}")

    white_key_samples: list[np.ndarray] = []
    body_samples: list[np.ndarray] = []
    luma_samples: list[np.ndarray] = []
    white_key_pixels = int(masks.white_key_mask.sum())
    body_pixels = int(masks.piano_body_mask.sum())
    # Accepted-after-filter pixel counts, accumulated across the stack.
    # Tracked per-side so the caller can tell which anchor was the
    # weak link when one fingerprint is low-confidence.
    wk_accepted_total = 0
    bd_accepted_total = 0

    for frame in stack:
        if white_key_pixels > 0:
            # White-key filter: drop pixels below WHITE_KEY_LUMA_MIN
            # (black keys, shadows bleed) and above WHITE_KEY_LUMA_MAX
            # (clipped specular highlights).
            wk_mean, wk_accepted = _trimmed_mean_masked(
                frame,
                masks.white_key_mask,
                luma_min=WHITE_KEY_LUMA_MIN,
                luma_max=WHITE_KEY_LUMA_MAX,
            )
            if wk_accepted > 0:
                white_key_samples.append(wk_mean)
                wk_accepted_total += wk_accepted
        if body_pixels > 0:
            # Body filter: drop pixels above PIANO_BODY_LUMA_MAX. These
            # are overwhelmingly reflections of windows / sheet music /
            # ceiling lights on the glossy lid — they carry scene content
            # color, not piano-body material color, and they bias the
            # fingerprint toward whatever is reflected rather than what
            # we're trying to sample. Also drop near-black noise floor.
            bd_mean, bd_accepted = _trimmed_mean_masked(
                frame,
                masks.piano_body_mask,
                luma_min=PIANO_BODY_LUMA_MIN,
                luma_max=PIANO_BODY_LUMA_MAX,
            )
            if bd_accepted > 0:
                body_samples.append(bd_mean)
                bd_accepted_total += bd_accepted
        luma = 0.2126 * frame[:, :, 0] + 0.7152 * frame[:, :, 1] + 0.0722 * frame[:, :, 2]
        luma_samples.append(luma)

    if white_key_samples:
        white_key_rgb = np.median(np.stack(white_key_samples, axis=0), axis=0)
    else:
        white_key_rgb = np.zeros(3, dtype=np.float32)
    if body_samples:
        piano_body_rgb = np.median(np.stack(body_samples, axis=0), axis=0)
    else:
        piano_body_rgb = np.zeros(3, dtype=np.float32)

    # Sanity check: piano body should be DARKER than white keys. If the
    # filter inverts the relationship (body_luma > wk_luma), one of the
    # masks almost certainly sampled the wrong material. Zero out the
    # fingerprint so downstream (cluster_states / fit_cdl) surfaces
    # the issue rather than computing a CDL on inverted anchors.
    wk_luma = float(0.2126 * white_key_rgb[0] + 0.7152 * white_key_rgb[1] + 0.0722 * white_key_rgb[2])
    bd_luma = float(0.2126 * piano_body_rgb[0] + 0.7152 * piano_body_rgb[1] + 0.0722 * piano_body_rgb[2])
    if wk_accepted_total < MIN_ACCEPTED_MASK_PIXELS_AFTER_FILTER or bd_accepted_total < MIN_ACCEPTED_MASK_PIXELS_AFTER_FILTER:
        # Mask was technically non-empty but luma filter rejected nearly
        # all of it — mask is sampling wrong material (reflection or
        # shadow). Zero out the RGB so this fingerprint can't contribute
        # a plausible-looking CDL. build_session_plan surfaces zero-
        # pixel fingerprints via the empty_mask_clip issue path.
        white_key_rgb = np.zeros(3, dtype=np.float32)
        piano_body_rgb = np.zeros(3, dtype=np.float32)
    elif wk_luma <= bd_luma:
        # Inverted relationship — body is brighter than the keys. One
        # mask is mislabeled. Zero out to refuse the fingerprint.
        white_key_rgb = np.zeros(3, dtype=np.float32)
        piano_body_rgb = np.zeros(3, dtype=np.float32)

    if luma_samples:
        luma_all = np.concatenate([l.reshape(-1) for l in luma_samples])
        luma_p05 = float(np.quantile(luma_all, 0.05))
        luma_p50 = float(np.quantile(luma_all, 0.50))
        luma_p95 = float(np.quantile(luma_all, 0.95))
    else:
        luma_p05 = luma_p50 = luma_p95 = 0.0

    cct = estimate_cct_kelvin(white_key_rgb.astype(np.float32))

    return LightingFingerprint(
        white_key_rgb=white_key_rgb.astype(np.float32),
        piano_body_rgb=piano_body_rgb.astype(np.float32),
        luma_p05=luma_p05,
        luma_p50=luma_p50,
        luma_p95=luma_p95,
        estimated_cct_kelvin=cct,
        white_key_pixels=white_key_pixels,
        body_pixels=body_pixels,
        window_start_s=window_start_s,
        window_end_s=window_end_s,
        frame_indices=list(frame_indices or []),
    )


# --- clustering ---


FEATURE_DIM = 6
"""The feature-vector dimension for clustering and changepoint detection.

Layout (index: meaning):
  0 log(white_R / white_G)  — white-key red/green chroma
  1 log(white_B / white_G)  — white-key blue/green chroma
  2 log(body_R / body_G)    — piano-body red/green chroma
  3 log(body_B / body_G)    — piano-body blue/green chroma
  4 luma_p50                — linear [0, 1]
  5 estimated_cct / 10000   — kelvin, normalized to put CCT on the same
                              order of magnitude as the log-chroma axes

Log-space chroma makes the distance metric ratio-based (perceptually more
meaningful than linear differences) and stable when the overall exposure
drifts but the spectral balance does not.
"""


def _safe_log_ratio(numerator: float, denominator: float) -> float:
    """log(numerator / denominator) with a guard against zero/negative inputs."""
    num = max(float(numerator), 1e-6)
    den = max(float(denominator), 1e-6)
    return float(np.log(num / den))


def fingerprint_feature_vector(fp: LightingFingerprint) -> np.ndarray:
    """Return the 6-D feature vector used for clustering and changepoint
    detection. See `FEATURE_DIM` for the layout.
    """
    w = np.asarray(fp.white_key_rgb, dtype=np.float32)
    b = np.asarray(fp.piano_body_rgb, dtype=np.float32)
    return np.array(
        [
            _safe_log_ratio(w[0], w[1]),
            _safe_log_ratio(w[2], w[1]),
            _safe_log_ratio(b[0], b[1]),
            _safe_log_ratio(b[2], b[1]),
            float(fp.luma_p50),
            float(fp.estimated_cct_kelvin) / 10000.0,
        ],
        dtype=np.float32,
    )


def _build_feature_matrix(fingerprints: list[LightingFingerprint]) -> np.ndarray:
    if not fingerprints:
        return np.zeros((0, FEATURE_DIM), dtype=np.float32)
    mat = np.stack([fingerprint_feature_vector(fp) for fp in fingerprints], axis=0)
    # Defensive NaN/Inf sanitization. The ``fingerprint_feature_vector``
    # helper guards against zero/negative log inputs via ``_safe_log_ratio``
    # and ``estimate_cct_kelvin`` returns 0.0 for degenerate RGB, but an
    # upstream bug producing a NaN RGB would otherwise propagate through
    # HDBSCAN and the spread computation silently.
    return np.nan_to_num(mat, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _feature_spread(features: np.ndarray) -> float:
    """Max pairwise L2 distance across a feature matrix.

    Used to distinguish a 'stable lighting' cluster (tight feature cloud
    HDBSCAN couldn't resolve) from 'genuinely unresolvable noise' (spread
    fingerprints with no density structure). Returns 0.0 for 0 or 1 rows.
    """
    n = features.shape[0]
    if n < 2:
        return 0.0
    # O(n²) but n is typically < 300 for a session; fine.
    diffs = features[:, None, :] - features[None, :, :]
    dists = np.linalg.norm(diffs, axis=-1)
    return float(dists.max())


def adaptive_cluster_params(
    total_windows: int,
) -> tuple[int, int, float]:
    """Compute adaptive HDBSCAN params from the dataset size.

    Returns ``(min_cluster_size, min_samples, cluster_selection_epsilon)``.

    Rationale: ``min_cluster_size`` is the minimum number of
    *fingerprint windows* (NOT takes) that must share a regime for it to
    become a lighting state. With 1.5-20 s windows, this varies dramatically
    across session shapes — a single-take 5-minute pickup produces ~50
    windows total, while a 10-take 90-minute session produces several
    thousand. Hard-coding ``min_cluster_size=3`` over-fragments the big
    sessions; hard-coding ``min_cluster_size=20`` loses everything on
    short sessions. The fraction-of-total rule scales naturally.

    ``min_samples`` ~ ``min_cluster_size // 3`` follows HDBSCAN's usual
    heuristic (looser connectivity than cluster-level strictness).

    ``cluster_selection_epsilon`` is scale-dependent in the feature space,
    not count-dependent, so it stays at the empirically-tuned module
    constant.
    """
    min_cluster_size = max(
        HDBSCAN_MIN_CLUSTER_SIZE,
        round(total_windows * HDBSCAN_MIN_CLUSTER_FRACTION),
    )
    min_samples = max(HDBSCAN_MIN_SAMPLES, min_cluster_size // 3)
    return min_cluster_size, min_samples, HDBSCAN_CLUSTER_SELECTION_EPSILON


def cluster_states(
    fingerprints: list[LightingFingerprint],
    *,
    min_cluster_size: int | None = None,
    min_samples: int | None = None,
    cluster_selection_epsilon: float | None = None,
) -> dict[int, LightingState]:
    """Cluster fingerprints into lighting states using HDBSCAN.

    When the three HDBSCAN params are left at ``None`` (the usual case),
    they are computed by ``adaptive_cluster_params`` from the input size.
    Explicit values override the adaptive rule — for tuning experiments
    or the ``--min-cluster-size`` / ``--cluster-epsilon`` CLI flags.

    Input order is preserved via ``member_indices``. HDBSCAN noise points
    (label -1) are gathered into their own state with ``state_id = -1`` so
    downstream code can decide whether to apply a best-effort CDL per noise
    window or to skip them.

    The representative fingerprint of each state is the medoid in feature
    space — the member whose feature vector has the smallest mean distance
    to the rest of the cluster. This beats the arithmetic mean because
    fingerprint objects cannot be averaged in general (frame_indices, etc.).
    """
    import hdbscan  # local import — heavy dependency

    if not fingerprints:
        return {}

    features = _build_feature_matrix(fingerprints)
    adaptive_mcs, adaptive_ms, adaptive_eps = adaptive_cluster_params(len(fingerprints))
    if min_cluster_size is None:
        min_cluster_size = adaptive_mcs
    if min_samples is None:
        min_samples = adaptive_ms
    if cluster_selection_epsilon is None:
        cluster_selection_epsilon = adaptive_eps

    # HDBSCAN needs at least min_cluster_size points to form a cluster.
    # For tiny inputs, fall back to single-cluster.
    if len(fingerprints) < min_cluster_size:
        labels = np.zeros(len(fingerprints), dtype=np.int64)
        probabilities = np.ones(len(fingerprints), dtype=np.float32)
    else:
        clusterer = hdbscan.HDBSCAN(
            min_cluster_size=min_cluster_size,
            min_samples=min_samples,
            cluster_selection_epsilon=cluster_selection_epsilon,
            metric="euclidean",
        )
        labels = clusterer.fit_predict(features)
        probabilities = clusterer.probabilities_

        # HDBSCAN returns all -1 for two very different situations:
        #   (a) a session with truly stable lighting — one tight cluster
        #       with so little density variation that HDBSCAN refuses to
        #       recognize structure;
        #   (b) a session whose fingerprints are genuinely spread out but
        #       don't form HDBSCAN-compatible density clusters (params too
        #       strict, multiple sparse states, noisy data).
        # Only case (a) is safe to collapse to 'one stable state'. Case (b)
        # must stay labeled as noise so that _select_reference_state / the
        # downstream report surfaces the ambiguity instead of silently
        # fabricating a high-confidence single-state plan.
        if np.all(labels == -1):
            if _feature_spread(features) < CLUSTER_COLLAPSE_SPREAD:
                labels = np.zeros(len(fingerprints), dtype=np.int64)
                probabilities = np.ones(len(fingerprints), dtype=np.float32)
            # else: keep the all-noise labels. `_select_reference_state`
            # will emit the `all_noise` WARN upstream.

    states: dict[int, LightingState] = {}
    for label in sorted(set(int(l) for l in labels)):
        member_idx = [int(i) for i, l in enumerate(labels) if int(l) == label]
        member_probs = [float(probabilities[i]) for i in member_idx]
        member_features = features[member_idx]
        centroid = member_features.mean(axis=0).astype(np.float32)

        # Medoid: member with smallest mean distance to other members
        if len(member_idx) == 1:
            medoid_idx = member_idx[0]
        else:
            dists = np.linalg.norm(member_features[:, None, :] - member_features[None, :, :], axis=-1)
            medoid_row = int(np.argmin(dists.mean(axis=1)))
            medoid_idx = member_idx[medoid_row]

        states[int(label)] = LightingState(
            state_id=int(label),
            centroid_features=centroid,
            representative_fingerprint=fingerprints[medoid_idx],
            member_indices=member_idx,
            member_probabilities=member_probs,
        )
    return states


# --- changepoint detection ---


def _compute_gaps(
    features: np.ndarray, window_size: int
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(candidate_positions, gaps)`` for the percentile-gap detector.

    Position ``i`` in ``candidate_positions`` means a boundary between
    fingerprint ``i-1`` and fingerprint ``i``. ``gaps[k]`` is the L2 norm of
    ``mean(features[i-window:i]) - mean(features[i:i+window])``.
    """
    n = features.shape[0]
    if n < 2 * window_size:
        return np.array([], dtype=np.int64), np.array([], dtype=np.float32)
    positions = np.arange(window_size, n - window_size + 1, dtype=np.int64)
    gaps = np.zeros(len(positions), dtype=np.float32)
    for k, i in enumerate(positions):
        left = features[i - window_size : i].mean(axis=0)
        right = features[i : i + window_size].mean(axis=0)
        gaps[k] = float(np.linalg.norm(right - left))
    return positions, gaps


def _normalize_confidence(gap: float, median_gap: float) -> float:
    """Map a gap to a [0, 1] confidence score.

    ``gap == median`` → 0, ``gap >= 3 * median`` → 1.
    """
    if median_gap <= 1e-9:
        return 1.0 if gap > 1e-9 else 0.0
    return float(min(1.0, max(0.0, (gap - median_gap) / (2.0 * median_gap))))


def _suppress_nearby(
    positions: np.ndarray, gaps: np.ndarray, radius: int
) -> list[int]:
    """Keep only local maxima of ``gaps`` within ``radius`` positions.

    Returns the indices *into ``positions``* of the kept maxima.
    """
    kept: list[int] = []
    for k in range(len(positions)):
        lo = max(0, k - radius)
        hi = min(len(positions), k + radius + 1)
        if gaps[k] >= gaps[lo:hi].max() - 1e-9:
            kept.append(k)
    return kept


def _segments_from_boundaries(
    boundaries: list[Boundary], fingerprints: list[LightingFingerprint]
) -> list[tuple[float, float]]:
    """Convert sorted boundary list to segment time ranges."""
    if not fingerprints:
        return []
    t_start = float(fingerprints[0].window_start_s)
    t_end = float(fingerprints[-1].window_end_s)
    if not boundaries:
        return [(t_start, t_end)]
    segments: list[tuple[float, float]] = []
    prev_t = t_start
    for b in boundaries:
        segments.append((prev_t, b.time_s))
        prev_t = b.time_s
    segments.append((prev_t, t_end))
    return segments


def _merge_short_segments(
    boundaries: list[Boundary], fingerprints: list[LightingFingerprint]
) -> list[Boundary]:
    """Drop the lowest-confidence boundary adjacent to any sub-threshold
    segment until all segments are ≥ ``MIN_SEGMENT_DURATION_S``.
    """
    boundaries = sorted(boundaries, key=lambda b: b.window_index)
    while True:
        segments = _segments_from_boundaries(boundaries, fingerprints)
        short_idx = next(
            (i for i, (s, e) in enumerate(segments) if (e - s) < MIN_SEGMENT_DURATION_S),
            None,
        )
        if short_idx is None or not boundaries:
            return boundaries

        # The short segment is between boundaries[short_idx - 1] (its start,
        # or t_start if short_idx == 0) and boundaries[short_idx] (its end,
        # or t_end if short_idx == len(segments) - 1). Drop the adjacent
        # boundary with the lower confidence.
        left = boundaries[short_idx - 1] if short_idx > 0 else None
        right = boundaries[short_idx] if short_idx < len(boundaries) else None
        if left is None and right is None:
            # Only one segment and it's too short — nothing to merge.
            return boundaries
        if left is None:
            assert right is not None
            boundaries.remove(right)
        elif right is None:
            boundaries.remove(left)
        else:
            drop = left if left.confidence <= right.confidence else right
            boundaries.remove(drop)


def _detect_changepoints_single(
    fingerprints: list[LightingFingerprint],
    *,
    window_size: int,
    k_threshold: float,
    confidence_threshold: float,
) -> list[Boundary]:
    features = _build_feature_matrix(fingerprints)
    positions, gaps = _compute_gaps(features, window_size)
    if gaps.size == 0:
        return []

    median_gap = float(np.median(gaps))
    max_gap = float(gaps.max())
    if max_gap <= 1e-9:
        # The whole clip is feature-constant — no changepoints anywhere.
        return []

    # 1. Absolute threshold: gap must exceed K * median. A median of 0 is
    #    the healthy case: a long stable clip with one sharp regime change
    #    yields mostly-zero gaps except in the narrow transition band. Any
    #    non-zero gap passes the threshold here, and non-max suppression
    #    plus the confidence-threshold filter reject the transition flanks.
    abs_mask = gaps > max(k_threshold * median_gap, 1e-9)
    # 2. Non-max suppression within ±window_size positions
    local_max_idx = set(_suppress_nearby(positions, gaps, radius=window_size))

    candidates: list[Boundary] = []
    for k, i in enumerate(positions):
        if not abs_mask[k] or k not in local_max_idx:
            continue
        conf = _normalize_confidence(float(gaps[k]), median_gap)
        if conf < confidence_threshold:
            continue
        mid_time = 0.5 * (
            float(fingerprints[i - 1].window_end_s) + float(fingerprints[i].window_start_s)
        )
        candidates.append(
            Boundary(window_index=int(i), time_s=mid_time, confidence=conf)
        )

    # Post-merge: drop boundaries that produce sub-threshold segments
    return _merge_short_segments(candidates, fingerprints)


def detect_changepoints(
    fingerprints_per_clip: dict[str, list[LightingFingerprint]],
    *,
    window_size: int = CHANGEPOINT_WINDOW_SIZE,
    k_threshold: float = CHANGEPOINT_K_THRESHOLD,
    confidence_threshold: float = CHANGEPOINT_CONFIDENCE_THRESHOLD,
) -> dict[str, list[Boundary]]:
    """Detect within-clip lighting regime changes via percentile-gap.

    For each clip, fingerprints are assumed to be in temporal order. A
    boundary is emitted when the feature-vector mean of a ``window_size``
    buffer on the right of a candidate position differs from the mean of
    the buffer on the left by more than ``k_threshold`` times the median
    gap across the clip, AND the normalized confidence is at least
    ``confidence_threshold``, AND no resulting segment is shorter than
    ``MIN_SEGMENT_DURATION_S`` seconds (short segments are merged back).
    """
    return {
        clip_id: _detect_changepoints_single(
            fps,
            window_size=window_size,
            k_threshold=k_threshold,
            confidence_threshold=confidence_threshold,
        )
        for clip_id, fps in fingerprints_per_clip.items()
    }


def segments_from_boundaries(
    boundaries: list[Boundary], fingerprints: list[LightingFingerprint]
) -> list[tuple[float, float]]:
    """Public helper: convert a boundary list to segment time ranges. Used
    by the per-segment CDL pipeline in Milestone 3.
    """
    return _segments_from_boundaries(boundaries, fingerprints)


__all__ = [
    "ANALYSIS_WIDTH",
    "ANALYSIS_HEIGHT",
    "MASK_CONFIDENCE_THRESHOLD",
    "MIN_MASK_PIXELS",
    "MIN_SEGMENT_DURATION_S",
    "FEATURE_DIM",
    "PianoMasks",
    "LightingFingerprint",
    "LightingState",
    "Boundary",
    "find_keyboard_region",
    "detect_piano_masks",
    "compute_window_fingerprint",
    "estimate_cct_kelvin",
    "fingerprint_feature_vector",
    "adaptive_cluster_params",
    "cluster_states",
    "detect_changepoints",
    "segments_from_boundaries",
    "get_sam2_load_error",
]
