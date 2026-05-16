"""Session-level auto-segment pipeline for Phase 2 Milestone 3.

Orchestrates:
  1. Walk every (take, angle, clip) in a session.
  2. Extract per-window ``LightingFingerprint`` time series per clip.
  3. HDBSCAN-cluster all fingerprints across the session to get
     ``LightingState`` labels.
  4. Detect within-clip changepoints, build ``SegmentAssignment`` entries.
  5. Choose a reference state and fit a ``CDLResult`` per non-reference
     state via the fingerprint medoid.

The heavy I/O + SAM 2 work is isolated in ``compute_clip_fingerprints``,
which is dependency-injectable via ``build_session_plan(..., compute_fn=)``
so unit tests can stub the mask + fingerprint computation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from piano_guard.auto_segment import (
    ANALYSIS_HEIGHT,
    ANALYSIS_WIDTH,
    Boundary,
    LightingFingerprint,
    LightingState,
    PianoMasks,
    cluster_states,
    compute_window_fingerprint,
    detect_changepoints,
    detect_piano_masks,
)
from piano_guard.color_match import (
    CDLResult,
    fit_cdl_from_fingerprints,
    to_linear_bt709,
    validate_reference_quality,
)
from piano_guard.config import (
    AngleCalibration,
    CalibrationBox,
    SessionCalibration,
    SessionProjectConfig,
    TakeConfig,
    iter_session_takes,
)
from piano_guard.ingest import VideoClipInfo
from piano_guard.reports import Issue


# --- constants ---


DEFAULT_WINDOW_SECONDS = 1.5
"""Duration of one fingerprint window. At 29.97 fps this is ~45 frames.
Shorter windows produce more temporal resolution but noisier fingerprints;
1.5 s averages over a typical musical phrase without smearing across a
lighting transition."""

DEFAULT_FRAMES_PER_WINDOW = 1
"""Number of frames sampled per window. A single mid-window frame is
usually adequate because the mask-constrained trimmed mean already
averages over thousands of pixels. Crank to 3 (begin/mid/end) if per-
window noise proves problematic."""


# --- data shapes ---


@dataclass
class SegmentAssignment:
    """One contiguous time range of a clip assigned to one lighting state."""

    clip_id: str
    start_s: float
    end_s: float
    state_id: int
    fingerprint_indices: list[int] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return float(self.end_s - self.start_s)


@dataclass
class ClipPlan:
    """Everything computed for one source clip."""

    clip_id: str            # "{take_id}/{angle}"
    take_id: str
    angle: str
    video_path: str
    fingerprints: list[LightingFingerprint]
    masks: PianoMasks
    boundaries: list[Boundary]
    segments: list[SegmentAssignment]
    dominant_state_id: int  # longest-duration state across segments

    @property
    def segment_count(self) -> int:
        return len(self.segments)


@dataclass
class SessionAutoSegmentPlan:
    """Full output of the Phase 2 auto-segment pipeline."""

    states: dict[int, LightingState]
    reference_state_id: int
    cdl_per_state: dict[int, CDLResult]
    clip_plans: dict[str, ClipPlan]
    richer_transforms_per_state: dict[int, Any] = field(default_factory=dict)
    """Milestone 5 escape-hatch transforms, keyed by state_id. Populated
    only when the operator opts specific states into
    ``--escape-transform`` on the CLI. Value type is
    ``color_match.RicherTransformResult`` but kept as Any at module
    scope to avoid an import cycle."""

    issues: list[Issue] = field(default_factory=list)

    def as_state_assignments(self) -> dict[str, list[tuple[float, float, int]]]:
        """Project into the shape expected by ``apply_auto_color_normalization``:
        clip_id → [(start_s, end_s, state_id), ...].
        """
        return {
            clip_id: [(s.start_s, s.end_s, s.state_id) for s in plan.segments]
            for clip_id, plan in self.clip_plans.items()
        }


# --- per-clip fingerprint extraction ---


def _angle_calibration_to_fallback_rois(
    angle_calibration: AngleCalibration | None,
) -> dict[str, list[tuple[int, int, int, int]]] | None:
    """Convert calibration boxes into the dict shape ``detect_piano_masks``
    expects. Returns None when no calibration is provided.
    """
    if angle_calibration is None:
        return None

    def _box_to_tuple(b: CalibrationBox) -> tuple[int, int, int, int]:
        return (int(b.x), int(b.y), int(b.w), int(b.h))

    return {
        "white_key_boxes": [_box_to_tuple(b) for b in angle_calibration.white_key_boxes],
        "piano_body_boxes": [_box_to_tuple(b) for b in angle_calibration.piano_body_boxes],
    }


def compute_clip_fingerprints(
    video: VideoClipInfo,
    *,
    angle_calibration: AngleCalibration | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    frames_per_window: int = DEFAULT_FRAMES_PER_WINDOW,
    max_windows: int | None = None,
) -> tuple[list[LightingFingerprint], PianoMasks]:
    """Extract a fingerprint time series from a single clip.

    The piano is assumed static (tripod-mounted camera), so SAM 2 masks
    are computed ONCE on a representative frame and reused for every
    window. This is ~50× faster than per-window mask detection and is
    correct for the target workflow. A lighting change does NOT move the
    piano, so the same masks remain valid.

    Returns ``(fingerprints, masks)``. ``fingerprints`` are in temporal
    order. ``masks`` is the single mask set used for all windows; its
    ``source`` and ``confidence`` fields surface which strategy succeeded.
    """
    video_path = Path(video.path)
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"unable to open video for fingerprinting: {video_path}")

    try:
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 29.97)
        if total_frames <= 0 or fps <= 0:
            raise RuntimeError(
                f"video {video_path} reports invalid metadata "
                f"(total_frames={total_frames}, fps={fps})"
            )
        duration_s = total_frames / fps

        frames_per_window_count = max(1, int(round(fps * window_seconds)))
        n_windows = max(1, int(duration_s / window_seconds))
        if max_windows is not None and n_windows > max_windows:
            n_windows = int(max_windows)

        # --- mask detection: one representative frame at clip midpoint ---
        mid_frame_idx = total_frames // 2
        capture.set(cv2.CAP_PROP_POS_FRAMES, mid_frame_idx)
        ok, mid_bgr = capture.read()
        if not ok or mid_bgr is None:
            raise RuntimeError(f"unable to read midpoint frame from {video_path}")
        mid_bgr_resized = cv2.resize(mid_bgr, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT))

        fallback_rois = _angle_calibration_to_fallback_rois(angle_calibration)
        masks = detect_piano_masks(mid_bgr_resized, fallback_rois=fallback_rois)

        # Fail-fast: if mask detection produced zero pixels on either side,
        # there is nothing meaningful to sample. Returning empty fingerprints
        # lets build_session_plan surface an explicit failure per clip
        # (empty_mask_clip) with the SAM 2 strategy_issues attached, rather
        # than silently producing zero-RGB fingerprints that cluster into
        # ghost 'states'. The ``masks`` object is still returned so the
        # caller can inspect ``strategy_issues`` for diagnostic detail.
        if not masks.both_masks_nonempty:
            return [], masks

        # --- per-window fingerprint extraction ---
        fingerprints: list[LightingFingerprint] = []
        for w in range(n_windows):
            window_start_s = w * window_seconds
            window_end_s = min(duration_s, (w + 1) * window_seconds)
            # Sample ``frames_per_window`` evenly across this window
            frame_indices_window: list[int] = []
            for k in range(frames_per_window):
                pos = (k + 0.5) / frames_per_window  # mid of sub-bin within the window
                sample_t = window_start_s + pos * (window_end_s - window_start_s)
                sample_idx = int(round(sample_t * fps))
                sample_idx = max(0, min(total_frames - 1, sample_idx))
                frame_indices_window.append(sample_idx)

            frame_stack: list[np.ndarray] = []
            for idx in frame_indices_window:
                capture.set(cv2.CAP_PROP_POS_FRAMES, idx)
                ok, frame_bgr = capture.read()
                if not ok or frame_bgr is None:
                    continue
                frame_bgr_resized = cv2.resize(frame_bgr, (ANALYSIS_WIDTH, ANALYSIS_HEIGHT))
                # BGR uint8 → linear BT.709
                frame_rgb_u8 = cv2.cvtColor(frame_bgr_resized, cv2.COLOR_BGR2RGB)
                frame_linear = to_linear_bt709(frame_rgb_u8, video)
                frame_stack.append(frame_linear)

            if not frame_stack:
                continue

            stack = np.stack(frame_stack, axis=0) if len(frame_stack) > 1 else frame_stack[0]
            fp = compute_window_fingerprint(
                stack,
                masks,
                window_start_s=window_start_s,
                window_end_s=window_end_s,
                frame_indices=frame_indices_window,
            )
            fingerprints.append(fp)

    finally:
        capture.release()

    return fingerprints, masks


# --- segment assignment ---


def assign_segments_to_states(
    clip_id: str,
    fingerprints: list[LightingFingerprint],
    boundaries: list[Boundary],
    state_labels: list[int],
) -> list[SegmentAssignment]:
    """Build ``SegmentAssignment``s from a clip's fingerprints, changepoint
    boundaries, and the cluster label for every fingerprint.

    Segment state = majority vote of the fingerprint labels falling inside
    the segment's (start, end) time range. Ties resolve to the lower
    state_id for determinism.
    """
    if not fingerprints:
        return []
    if len(state_labels) != len(fingerprints):
        raise ValueError(
            f"state_labels length {len(state_labels)} != fingerprints length {len(fingerprints)}"
        )

    # Ranges of (fingerprint_index_start, fingerprint_index_end_exclusive)
    # split by boundaries.
    boundaries_sorted = sorted(boundaries, key=lambda b: b.window_index)
    split_points = [b.window_index for b in boundaries_sorted]
    split_points = [0] + split_points + [len(fingerprints)]

    assignments: list[SegmentAssignment] = []
    for seg_start, seg_end in zip(split_points[:-1], split_points[1:]):
        if seg_start >= seg_end:
            continue
        seg_labels = state_labels[seg_start:seg_end]
        # Majority vote. Tie-break on lower state_id for determinism.
        counts: dict[int, int] = {}
        for lab in seg_labels:
            counts[int(lab)] = counts.get(int(lab), 0) + 1
        # Pick max count; tie-break by smaller state_id.
        state_id = min(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        start_s = float(fingerprints[seg_start].window_start_s)
        end_s = float(fingerprints[seg_end - 1].window_end_s)
        assignments.append(
            SegmentAssignment(
                clip_id=clip_id,
                start_s=start_s,
                end_s=end_s,
                state_id=state_id,
                fingerprint_indices=list(range(seg_start, seg_end)),
            )
        )
    return assignments


def _dominant_state_id(assignments: list[SegmentAssignment]) -> int:
    """Return the state_id whose segments sum to the longest duration.

    Tie-break by smallest state_id for determinism. Returns -1 when the
    assignment list is empty.
    """
    if not assignments:
        return -1
    duration_by_state: dict[int, float] = {}
    for a in assignments:
        duration_by_state[a.state_id] = duration_by_state.get(a.state_id, 0.0) + a.duration_s
    return min(
        duration_by_state.items(), key=lambda kv: (-kv[1], kv[0])
    )[0]


# --- reference-state selection and per-state CDL fit ---


def _select_reference_state(
    states: dict[int, LightingState],
) -> tuple[int, list[Issue]]:
    """Choose the reference lighting state.

    Preference order:
      1. Most-populous non-noise state whose medoid passes
         ``validate_reference_quality`` (no clipping, above noise floor,
         adequate W/B separation).
      2. Most-populous non-noise state, even if quality checks fail
         (surface a WARN so the operator knows the reference is soft).
      3. The ``-1`` noise state, if that's all we have (surface a WARN).

    Returns ``(state_id, issues)`` — issues are non-empty when we had to
    fall back to a non-ideal reference.
    """
    issues: list[Issue] = []
    if not states:
        return -1, [
            Issue(
                severity="fail",
                code="no_states",
                message="cluster_states produced no output — clip set is empty",
            )
        ]

    # Rank non-noise states by member count descending.
    non_noise = [(sid, st) for sid, st in states.items() if sid >= 0]
    non_noise.sort(key=lambda kv: (-kv[1].member_count, kv[0]))

    if not non_noise:
        # Everything is noise — fall back to the -1 state as reference so
        # the pipeline can still produce identity-CDL output.
        issues.append(
            Issue(
                severity="warn",
                code="all_noise",
                message=(
                    "HDBSCAN flagged every fingerprint as noise — using noise "
                    "cluster as reference. Consider re-running with more "
                    "fingerprints or relaxing clustering params."
                ),
            )
        )
        return -1, issues

    # First pass: seek a quality-passing candidate.
    for sid, st in non_noise:
        fp = st.representative_fingerprint
        ok, _notes = validate_reference_quality(
            np.asarray(fp.white_key_rgb, dtype=np.float32),
            np.asarray(fp.piano_body_rgb, dtype=np.float32),
        )
        if ok:
            return sid, issues

    # Fall back to the most populous, even if quality checks failed.
    sid, st = non_noise[0]
    fp = st.representative_fingerprint
    _ok, notes = validate_reference_quality(
        np.asarray(fp.white_key_rgb, dtype=np.float32),
        np.asarray(fp.piano_body_rgb, dtype=np.float32),
    )
    issues.append(
        Issue(
            severity="warn",
            code="reference_state_low_quality",
            message=(
                f"reference state-{sid} medoid fails quality checks "
                f"({'; '.join(notes)}); CDL fits against this reference may be "
                f"ill-conditioned."
            ),
            context={"reference_state_id": sid, "issues": notes},
        )
    )
    return sid, issues


def _identity_cdl(state_id: int) -> CDLResult:
    """Identity CDL (slope=1, offset=0) for a reference state — no color change."""
    return CDLResult(
        target_angle=f"state-{state_id}",
        reference_angle=f"state-{state_id}",
        slope_rgb=(1.0, 1.0, 1.0),
        offset_rgb=(0.0, 0.0, 0.0),
        power_rgb=(1.0, 1.0, 1.0),
        residuals={
            "white_key": {"chroma_r": 0.0, "chroma_b": 0.0, "luma": 0.0},
            "body": {"chroma_r": 0.0, "chroma_b": 0.0, "luma": 0.0},
        },
        status="pass",
        code="",
    )


def fit_escape_transforms_for_states(
    states: dict[int, LightingState],
    reference_state_id: int,
    escape_state_ids: set[int],
) -> dict[int, Any]:
    """Fit a richer (escape-hatch) transform for each state in
    ``escape_state_ids``, relative to the reference state's medoid.

    Silently skips the reference state itself (identity transform
    unnecessary) and noise (-1, not a valid target). States in
    ``escape_state_ids`` that aren't in ``states`` are ignored.

    Returns ``{state_id: RicherTransformResult}``. Call with an empty
    set to get an empty dict.
    """
    from piano_guard.color_match import fit_richer_transform

    if reference_state_id not in states or not escape_state_ids:
        return {}
    ref_fp = states[reference_state_id].representative_fingerprint
    out: dict[int, Any] = {}
    for sid in escape_state_ids:
        if sid == reference_state_id or sid < 0:
            continue
        state = states.get(int(sid))
        if state is None:
            continue
        out[int(sid)] = fit_richer_transform(
            ref_fp,
            state.representative_fingerprint,
            reference_label=f"state-{reference_state_id}",
            target_label=f"state-{sid}",
        )
    return out


def fit_cdls_for_states(
    states: dict[int, LightingState],
    reference_state_id: int,
) -> dict[int, CDLResult]:
    """Fit a CDL from each non-reference state's medoid back to the reference
    state's medoid.

    The reference state maps to an identity CDL so downstream code can treat
    every state uniformly. Noise state (-1) is skipped in the output unless
    it IS the reference (a degenerate edge case).
    """
    if reference_state_id not in states:
        return {}
    ref_fp = states[reference_state_id].representative_fingerprint
    out: dict[int, CDLResult] = {reference_state_id: _identity_cdl(reference_state_id)}
    for sid, st in states.items():
        if sid == reference_state_id:
            continue
        if sid < 0:
            # Noise state — skip; downstream grader should either ignore
            # segments with state_id == -1 or treat them as uncalibrated.
            continue
        result = fit_cdl_from_fingerprints(
            ref_fp,
            st.representative_fingerprint,
            reference_label=f"state-{reference_state_id}",
            target_label=f"state-{sid}",
        )
        out[sid] = result
    return out


# --- session-level orchestration ---


ComputeFingerprintsFn = Callable[
    [VideoClipInfo, AngleCalibration | None],
    tuple[list[LightingFingerprint], PianoMasks],
]


def _default_compute_fn(
    video: VideoClipInfo, angle_calibration: AngleCalibration | None
) -> tuple[list[LightingFingerprint], PianoMasks]:
    return compute_clip_fingerprints(video, angle_calibration=angle_calibration)


def build_session_plan(
    session: SessionProjectConfig,
    *,
    calibration: SessionCalibration | None = None,
    probe_fn: Callable[[Path], VideoClipInfo] | None = None,
    compute_fn: ComputeFingerprintsFn | None = None,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    min_cluster_size: int | None = None,
    cluster_epsilon: float | None = None,
    reference_state_id_override: int | None = None,
) -> SessionAutoSegmentPlan:
    """Drive the full auto-segment pipeline on a session.

    ``probe_fn`` is injectable for tests — defaults to ``ingest.probe_video``.
    ``compute_fn`` is injectable for tests — defaults to
    ``compute_clip_fingerprints`` (which opens the clip and runs SAM 2).
    ``window_seconds`` overrides ``DEFAULT_WINDOW_SECONDS`` for the per-clip
    fingerprint sampling stride. For 27-minute piano takes, bumping to 15-30 s
    reduces runtime from ~40 min to ~2-5 min without losing lighting-regime
    resolution (a real day→night transition spans multiple minutes, so 15-30 s
    resolution is plenty).

    Reference state is the most-populous state whose medoid passes quality
    checks. CDLs are fit from every other non-noise state to the reference.
    Segments are assigned to the state holding the majority of their
    fingerprints.

    ``reference_state_id_override`` — force a specific state to be the
    reference. Use when the automatic selection (most-populous, quality-
    passing) picks a state whose medoid is spectrally extreme (e.g., the
    warmest angle of a day take) and the operator prefers a more neutral
    angle as the target. See the CLI ``--reference-state`` flag.
    """
    if probe_fn is None:
        from piano_guard.ingest import probe_video
        probe_fn = probe_video
    if compute_fn is None:
        # Default compute_fn wraps compute_clip_fingerprints with the
        # caller-supplied window_seconds. When the caller passes their
        # own compute_fn (e.g. tests with stubbed fingerprints), the
        # window_seconds is a no-op for them.
        def _compute_with_window(video: VideoClipInfo, angle_cal: AngleCalibration | None):
            return compute_clip_fingerprints(
                video, angle_calibration=angle_cal, window_seconds=window_seconds
            )
        compute_fn = _compute_with_window

    issues: list[Issue] = []

    clip_plans_partial: dict[str, dict[str, Any]] = {}
    all_fingerprints: list[LightingFingerprint] = []
    fingerprint_clip_map: list[str] = []  # parallel list: which clip each fp belongs to

    for take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            angle = camera.label
            clip_id = f"{take_ref.id}/{angle}"
            video_path = take.resolve_path(camera.file)
            try:
                video = probe_fn(video_path)
            except Exception as exc:
                issues.append(
                    Issue(
                        severity="warn",
                        code="video_probe_failed",
                        message=f"{clip_id}: probe failed ({exc}); skipping clip",
                        context={"clip_id": clip_id, "path": str(video_path)},
                    )
                )
                continue

            angle_cal = None
            if calibration is not None:
                angle_cal = calibration.angles.get(angle)

            try:
                fingerprints, masks = compute_fn(video, angle_cal)
            except Exception as exc:
                issues.append(
                    Issue(
                        severity="warn",
                        code="fingerprint_compute_failed",
                        message=f"{clip_id}: fingerprint extraction failed ({exc}); skipping clip",
                        context={"clip_id": clip_id, "path": str(video_path)},
                    )
                )
                continue

            if not fingerprints:
                # Distinguish 'compute returned empty' from 'masks unusable'
                # so the operator can tell whether to retry with calibration,
                # install a SAM 2 checkpoint, or inspect the video itself.
                if not masks.both_masks_nonempty:
                    issues.append(
                        Issue(
                            severity="warn",
                            code="empty_mask_clip",
                            message=(
                                f"{clip_id}: piano masking produced zero pixels "
                                f"(white_key={masks.white_key_pixels}, "
                                f"body={masks.body_pixels}, "
                                f"source={masks.source}, confidence={masks.confidence:.2f}); "
                                f"skipping clip. Strategy failures: "
                                f"{'; '.join(masks.strategy_issues) or 'none'}"
                            ),
                            context={
                                "clip_id": clip_id,
                                "mask_source": masks.source,
                                "mask_confidence": float(masks.confidence),
                                "white_key_pixels": masks.white_key_pixels,
                                "body_pixels": masks.body_pixels,
                                "strategy_issues": list(masks.strategy_issues),
                            },
                        )
                    )
                else:
                    issues.append(
                        Issue(
                            severity="warn",
                            code="empty_fingerprints",
                            message=f"{clip_id}: produced zero fingerprints; skipping clip",
                            context={"clip_id": clip_id},
                        )
                    )
                continue

            # Masks succeeded but had SAM 2 strategy failures that were
            # recovered via fallback — surface as info so the operator
            # knows a path degraded silently.
            if masks.strategy_issues:
                issues.append(
                    Issue(
                        severity="info",
                        code="mask_strategy_fallback",
                        message=(
                            f"{clip_id}: mask source={masks.source!r} "
                            f"(conf={masks.confidence:.2f}) reached via fallback; "
                            f"failed strategies: "
                            f"{'; '.join(masks.strategy_issues)}"
                        ),
                        context={
                            "clip_id": clip_id,
                            "mask_source": masks.source,
                            "strategy_issues": list(masks.strategy_issues),
                        },
                    )
                )

            clip_plans_partial[clip_id] = {
                "clip_id": clip_id,
                "take_id": take_ref.id,
                "angle": angle,
                "video_path": str(video_path),
                "fingerprints": fingerprints,
                "masks": masks,
            }
            all_fingerprints.extend(fingerprints)
            fingerprint_clip_map.extend([clip_id] * len(fingerprints))

    if not all_fingerprints:
        return SessionAutoSegmentPlan(
            states={},
            reference_state_id=-1,
            cdl_per_state={},
            clip_plans={},
            issues=issues
            + [Issue(severity="fail", code="no_fingerprints", message="no clips produced fingerprints")],
        )

    # --- cluster ---
    states = cluster_states(
        all_fingerprints,
        min_cluster_size=min_cluster_size,
        cluster_selection_epsilon=cluster_epsilon,
    )

    # Build per-fingerprint label list so segment assignment can vote
    label_by_fp_idx: list[int] = [-1] * len(all_fingerprints)
    for sid, st in states.items():
        for fp_idx in st.member_indices:
            label_by_fp_idx[fp_idx] = sid

    # --- reference state + CDLs ---
    reference_state_id, ref_issues = _select_reference_state(states)
    if reference_state_id_override is not None:
        if reference_state_id_override in states:
            issues.append(
                Issue(
                    severity="info",
                    code="reference_state_overridden",
                    message=(
                        f"operator override: reference state = "
                        f"{reference_state_id_override} (auto-selected would have "
                        f"been {reference_state_id})."
                    ),
                    context={
                        "override_state_id": reference_state_id_override,
                        "auto_selected_state_id": reference_state_id,
                    },
                )
            )
            reference_state_id = reference_state_id_override
        else:
            issues.append(
                Issue(
                    severity="warn",
                    code="reference_state_override_ignored",
                    message=(
                        f"--reference-state {reference_state_id_override} not found "
                        f"in clustered states {sorted(states.keys())}; falling back "
                        f"to auto-selected {reference_state_id}."
                    ),
                    context={
                        "requested": reference_state_id_override,
                        "available": sorted(states.keys()),
                    },
                )
            )
    issues.extend(ref_issues)
    cdl_per_state = fit_cdls_for_states(states, reference_state_id)

    # --- per-clip changepoints + segment assignments ---
    clip_fps: dict[str, list[LightingFingerprint]] = {
        cid: p["fingerprints"] for cid, p in clip_plans_partial.items()
    }
    boundaries_per_clip = detect_changepoints(clip_fps)

    # We need per-clip labels: partition the flat label_by_fp_idx list.
    clip_labels: dict[str, list[int]] = {}
    cursor = 0
    for clip_id, fps in clip_fps.items():
        clip_labels[clip_id] = label_by_fp_idx[cursor : cursor + len(fps)]
        cursor += len(fps)

    clip_plans: dict[str, ClipPlan] = {}
    for clip_id, partial in clip_plans_partial.items():
        boundaries = boundaries_per_clip.get(clip_id, [])
        segments = assign_segments_to_states(
            clip_id,
            partial["fingerprints"],
            boundaries,
            clip_labels[clip_id],
        )
        dominant = _dominant_state_id(segments)

        # Plan-phase signal for multi-state clips. A clip that spans
        # multiple distinct non-noise lighting states will be SKIPPED by
        # apply_auto_color_normalization (remote versions are clip-scoped,
        # so per-segment grading is deferred to Milestone 5's richer-
        # transform escape hatch). Surface this at plan time too so
        # --dry-run operators see the upcoming skip before they commit.
        distinct_nonnoise = {s.state_id for s in segments if s.state_id >= 0}
        if len(distinct_nonnoise) > 1:
            non_dom = [s for s in segments if s.state_id != dominant]
            issues.append(
                Issue(
                    severity="warn",
                    code="multi_state_clip",
                    message=(
                        f"{clip_id} spans multiple lighting states "
                        f"{sorted(distinct_nonnoise)}; dominant is "
                        f"state-{dominant}. apply-auto-color-normalize will "
                        f"SKIP this clip (remote versions are clip-scoped — "
                        f"grade it manually in Resolve, or wait for Milestone "
                        f"5's per-segment escape hatch)."
                    ),
                    context={
                        "clip_id": clip_id,
                        "dominant_state_id": dominant,
                        "distinct_nonnoise_states": sorted(distinct_nonnoise),
                        "non_dominant_segments": [
                            {
                                "start_s": s.start_s,
                                "end_s": s.end_s,
                                "state_id": s.state_id,
                            }
                            for s in non_dom
                        ],
                    },
                )
            )

        clip_plans[clip_id] = ClipPlan(
            clip_id=clip_id,
            take_id=partial["take_id"],
            angle=partial["angle"],
            video_path=partial["video_path"],
            fingerprints=partial["fingerprints"],
            masks=partial["masks"],
            boundaries=boundaries,
            segments=segments,
            dominant_state_id=dominant,
        )

    return SessionAutoSegmentPlan(
        states=states,
        reference_state_id=reference_state_id,
        cdl_per_state=cdl_per_state,
        clip_plans=clip_plans,
        issues=issues,
    )


# --- library integration ---


def apply_library_hits(
    plan: SessionAutoSegmentPlan,
    library: Any,  # Library — typed as Any to avoid import cycle at module scope
    *,
    rig_hash: str,
    room_id: str | None = None,
    threshold: float | None = None,
) -> SessionAutoSegmentPlan:
    """Replace non-reference state CDLs with matching library entries.

    For each non-reference, non-noise state, check if its medoid fingerprint
    matches a library entry within the similarity threshold AND whose
    stored ``reference_fingerprint`` has not drifted from the current
    session's reference medoid. When matched:

      * Replace the fitted CDL with the library entry's cached CDL.
      * Touch the library entry (updates last_used_at and match_count).
      * Emit an ``info`` issue with code ``library_hit`` recording which
        state hit which library entry and at what similarity.

    Candidates whose stored reference has drifted are silently not
    matched; this forces a cold fit for this session, which eventually
    overwrites or supplements the stale entry via
    ``write_library_entries_from_plan``.

    The plan is mutated and returned.
    """
    from piano_guard.fingerprint_library import MATCH_THRESHOLD

    effective_threshold = MATCH_THRESHOLD if threshold is None else float(threshold)

    current_ref_fp = None
    if plan.reference_state_id in plan.states:
        current_ref_fp = plan.states[plan.reference_state_id].representative_fingerprint

    for sid, state in plan.states.items():
        if sid < 0 or sid == plan.reference_state_id:
            continue
        fp = state.representative_fingerprint
        match = library.find_match(
            fp,
            rig_hash=rig_hash,
            room_id=room_id,
            current_reference_fingerprint=current_ref_fp,
            threshold=effective_threshold,
        )
        if match is None:
            continue
        entry, similarity = match
        # Swap the fitted CDL for the library's cached one.
        plan.cdl_per_state[sid] = entry.to_cdl_result(
            target_label=f"state-{sid}",
            reference_label=f"state-{plan.reference_state_id}",
        )
        library.touch(entry.entry_id)
        plan.issues.append(
            Issue(
                severity="info",
                code="library_hit",
                message=(
                    f"state-{sid} matched library entry {entry.entry_id} "
                    f"(similarity={similarity:.4f}); reusing cached CDL "
                    f"slope={entry.cdl_slope} offset={entry.cdl_offset}"
                ),
                context={
                    "state_id": sid,
                    "entry_id": entry.entry_id,
                    "similarity": similarity,
                    "room_id": entry.room_id,
                    "state_name": entry.state_name,
                },
            )
        )

    return plan


def write_library_entries_from_plan(
    plan: SessionAutoSegmentPlan,
    library: Any,  # Library
    *,
    rig_hash: str,
    room_id: str = "",
    threshold: float | None = None,
) -> int:
    """Persist non-reference state fits to the library.

    Runs after ``apply_auto_color_normalization`` completes. Skips states
    that already have a library match (those would be redundant entries)
    and skips states whose CDL status is ``fail``. Returns the number of
    new entries added.

    ``room_id`` on the new entries matches exactly what was on existing
    entries for duplicate-detection purposes: an entry written with
    ``room_id="studio-A"`` will only de-dup against other studio-A
    entries, letting operators keep per-room library scopes cleanly
    separated even on the same physical rig.
    """
    from piano_guard.fingerprint_library import MATCH_THRESHOLD

    effective_threshold = MATCH_THRESHOLD if threshold is None else float(threshold)
    added = 0
    ref_fp = None
    if plan.reference_state_id in plan.states:
        ref_fp = plan.states[plan.reference_state_id].representative_fingerprint

    for sid, state in plan.states.items():
        if sid < 0 or sid == plan.reference_state_id:
            continue
        cdl = plan.cdl_per_state.get(sid)
        if cdl is None or cdl.status == "fail":
            continue
        fp = state.representative_fingerprint
        # Dedup check: is this state already in the library (same rig +
        # same room + similar fingerprint AND still anchored to a stored
        # reference)?
        #
        # We DO run without the drift check here because drift is a
        # lookup-time concern — a drifted entry would just not match on
        # future reads and eventually get superseded.
        #
        # BUT legacy entries (reference_fingerprint=None) pose a subtle
        # trap: at lookup time they are skipped under drift-check mode
        # (correct — we can't verify their anchor), and at dedup here
        # they would block writing a fresh anchored entry (wrong — that
        # strands the legacy entry as permanently dead). So when we have
        # a current reference to anchor against, treat unanchored legacy
        # entries as non-duplicates so a new anchored entry can replace
        # them in future lookups.
        match = library.find_match(
            fp,
            rig_hash=rig_hash,
            room_id=room_id,
            current_reference_fingerprint=None,
            threshold=effective_threshold,
        )
        if match is not None:
            matched_entry, _sim = match
            if ref_fp is not None and matched_entry.reference_fingerprint is None:
                # Legacy unanchored entry that would otherwise block this
                # write. Allow the new anchored entry to coexist; future
                # lookups will prefer the anchored one under drift-check.
                pass
            else:
                continue
        library.add_entry(
            rig_hash=rig_hash,
            room_id=room_id,
            fingerprint=fp,
            reference_fingerprint=ref_fp,
            cdl=cdl,
            state_name="",
        )
        added += 1
    return added


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "DEFAULT_FRAMES_PER_WINDOW",
    "SegmentAssignment",
    "ClipPlan",
    "SessionAutoSegmentPlan",
    "compute_clip_fingerprints",
    "assign_segments_to_states",
    "fit_cdls_for_states",
    "fit_escape_transforms_for_states",
    "build_session_plan",
    "apply_library_hits",
    "write_library_entries_from_plan",
]
