"""Still-frame rendering for AI-assisted color review.

The color-review pipeline lets a vision-capable AI agent look at per-clip
still frames, reason holistically about cross-angle color matching, and
propose conservative CDLs.

This module is the scripting half of that loop: given a session, render
one mid-frame PNG per (take, angle) clip, in a display-ready Rec.709
form so an AI agent or human can inspect the ungraded content without
a DaVinci Resolve round-trip.

Design decisions:

* **Ungraded only.** The renderer operates on the source file directly;
  it does not apply any CDL. This keeps the output representative
  of the scene as captured, which is what the AI agent needs to reason
  about "what does this clip actually look like."
* **HLG -> Rec.709 SDR.** Source is Sony α6400 PP10 HLG. Writing a PNG of a
  raw HLG frame produces near-black content (HLG 0.5 ≈ 18% scene
  reference). The renderer linearizes (HLG OETF inverse), rotates
  primaries (BT.2020 → BT.709), then applies the BT.709 OETF so the
  PNG opens correctly in standard viewers. This mirrors the repo's
  Resolve target: Rec.2100 HLG input to Rec.709 Gamma 2.4 SDR output.
* **Downscaled.** Source is 4K. A 4K PNG is ~16 MB and slow to inspect
  via multimodal review tools. Downscale to 960×540 (DEFAULT_STILL_WIDTH)
  which is plenty for color judgment and keeps files ~200-400 KB.
* **Mid-frame.** The midpoint of the clip is a reasonable scene-
  representative frame (avoids boundary artifacts like fade-ins, slates,
  pianist-walking-on). Callers can override to a specific time.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from piano_guard.config import SessionProjectConfig, iter_session_takes
from piano_guard.ingest import probe_video
from piano_guard.reports import Issue


DEFAULT_STILL_WIDTH = 960
"""PNG width in pixels for still output. 960×540 is enough to see
lighting regime / skin tone / reflection placement without being slow
to transfer over multimodal review tools."""

STILLS_SUBDIR = "stills"

BT2020_TO_BT709 = np.array(
    [
        [1.6605, -0.5876, -0.0728],
        [-0.1246, 1.1329, -0.0083],
        [-0.0182, -0.1006, 1.1187],
    ],
    dtype=np.float32,
)


@dataclass
class StillRenderResult:
    clip_id: str
    take_id: str
    angle: str
    source_path: str
    still_path: str
    source_width: int
    source_height: int
    mid_frame_index: int
    mid_frame_time_s: float
    color_pipeline: str


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


def _normalize_for_display(frame_rgb: np.ndarray, video: Any) -> np.ndarray:
    rgb = np.clip(frame_rgb.astype(np.float32), 0.0, 1.0)
    if video.color_transfer == "arib-std-b67":
        rgb = _hlg_to_linear(rgb)
    if video.color_primaries == "bt2020":
        rgb = np.tensordot(rgb, BT2020_TO_BT709.T, axes=1)
    rgb = np.clip(rgb, 0.0, 1.0)
    return np.clip(_bt709_oetf(rgb), 0.0, 1.0)


def _hlg_source_to_display_rgb(frame_bgr: np.ndarray, video) -> np.ndarray:
    """Convert a decoded BGR uint8 frame to display-ready Rec.709 uint8.

    Mirrors the QC pipeline's ``_normalize_for_analysis`` but returns
    uint8 ready for PNG encoding. For non-HLG sources (unusual for this
    project), the frame is passed through with minimal processing.
    """
    # cv2 decoded uint8 BGR → RGB float32
    frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    normalized = _normalize_for_display(frame_rgb.astype(np.float32) / 255.0, video)
    # Convert back to uint8 PNG-ready
    return np.clip(normalized * 255.0, 0.0, 255.0).astype(np.uint8)


def render_clip_still(
    video_path: Path,
    out_path: Path,
    *,
    width: int = DEFAULT_STILL_WIDTH,
    time_s: float | None = None,
) -> StillRenderResult | None:
    """Render one mid-frame PNG from a video file.

    Returns a ``StillRenderResult`` with metadata, or ``None`` if the
    source file couldn't be read. The caller is responsible for
    surfacing the failure (this function doesn't raise to keep the
    walk-many-clips loop simple).
    """
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return None
    try:
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 29.97)
        if total_frames <= 0 or fps <= 0:
            return None
        source_w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        source_h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)

        if time_s is not None:
            mid_idx = max(0, min(total_frames - 1, int(round(time_s * fps))))
        else:
            mid_idx = total_frames // 2
        mid_time = float(mid_idx) / fps

        capture.set(cv2.CAP_PROP_POS_FRAMES, mid_idx)
        ok, frame_bgr = capture.read()
        if not ok or frame_bgr is None:
            return None

        # Probe the source for its color-transfer/primaries so the
        # HLG→Rec.709 pipeline uses the right conversion. For non-HLG
        # or non-BT.2020 content the normalize step becomes a no-op.
        try:
            video = probe_video(video_path)
        except Exception:
            # Minimal fallback: produce a plain BGR→RGB PNG without
            # HLG linearization. Better than nothing; caller can see
            # a usable image even on weird sources.
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            display_uint8 = frame_rgb
        else:
            display_uint8 = _hlg_source_to_display_rgb(frame_bgr, video)

        # Downscale to target width, preserving aspect
        if source_w > 0 and width < source_w:
            scale = width / source_w
            target_h = int(round(source_h * scale))
            display_uint8 = cv2.resize(
                display_uint8, (width, target_h), interpolation=cv2.INTER_AREA
            )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # cv2 writes BGR; convert back for the PNG
        cv2.imwrite(str(out_path), cv2.cvtColor(display_uint8, cv2.COLOR_RGB2BGR))

        return StillRenderResult(
            clip_id=f"{video_path.parent.name}/{video_path.stem}",
            take_id=video_path.parent.name,
            angle=video_path.stem,
            source_path=str(video_path),
            still_path=str(out_path),
            source_width=source_w,
            source_height=source_h,
            mid_frame_index=mid_idx,
            mid_frame_time_s=mid_time,
            color_pipeline="sony_pp10_hlg_rec2100_to_sdr_rec709_gamma24",
        )
    finally:
        capture.release()


def render_session_stills(
    session: SessionProjectConfig,
    *,
    width: int = DEFAULT_STILL_WIDTH,
) -> tuple[list[StillRenderResult], list[Issue]]:
    """Render mid-frame stills for every (take, angle) clip in a session.

    Output lives at ``<session_root>/reports/stills/<take_id>/<angle>.png``.
    Returns ``(results, issues)``. Issues are warnings on per-clip
    render failures; callers surface them via the usual report payload.
    """
    results: list[StillRenderResult] = []
    issues: list[Issue] = []

    stills_root = session.resolve_path(f"{session.reports_dir}/{STILLS_SUBDIR}")

    for take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            angle = camera.label
            clip_id = f"{take_ref.id}/{angle}"
            video_path = take.resolve_path(camera.file)
            if not video_path.exists():
                issues.append(
                    Issue(
                        severity="warn",
                        code="still_source_missing",
                        message=f"{clip_id}: source file missing at {video_path}",
                        context={"clip_id": clip_id, "path": str(video_path)},
                    )
                )
                continue

            out_path = stills_root / take_ref.id / f"{angle}.png"
            # render_clip_still uses the source file's parent folder name
            # as take_id; that would yield the Takes-bin folder name
            # rather than the session's take_id. Override via explicit
            # clip_id for consistency with our session-level naming.
            render = render_clip_still(video_path, out_path, width=width)
            if render is None:
                issues.append(
                    Issue(
                        severity="warn",
                        code="still_render_failed",
                        message=f"{clip_id}: failed to render still from {video_path}",
                        context={"clip_id": clip_id, "path": str(video_path)},
                    )
                )
                continue

            # Correct the identifiers — render_clip_still derived them
            # from the path, which reflects the physical take folder
            # but not necessarily the session-level take_id.
            render.clip_id = clip_id
            render.take_id = take_ref.id
            render.angle = angle
            results.append(render)

    return results, issues


def preview_cdl_on_still(
    source_png: Path,
    slope_rgb: tuple[float, float, float],
    offset_rgb: tuple[float, float, float],
    out_path: Path,
) -> Path:
    """Apply ASC CDL math offline to a rendered still and save preview PNG.

    Closes the /color-review skill's feedback loop without a Resolve
    round-trip. The math is identical to what Resolve's SetCDL on Node 1
    does when the project is set to piano-guard's explicit SDR target
    (Input = Rec.2100 HLG, Timeline/Output = Rec.709 Gamma 2.4), so the
    CDL operates on the same Rec.709 display-review space as the PNG.

    Resolve's ``GalleryStillAlbum.ExportStills`` was attempted as the
    "true" preview source but consistently returns False on Resolve
    20.3.2 regardless of format / path / Color page activation
    (probed live 2026-04-15). This offline preview is our workaround
    and is mathematically equivalent for the project's current color
    management.

    Math: ``out = clip(in * slope + offset, 0, 1)`` per pixel, per
    channel. Power and saturation are not applied (piano-guard's CDL
    fits keep them at identity 1).

    Caveats:
      * Differs from the live Resolve viewer if the project's color
        management drifts from the repo's explicit HLG→SDR settings.
        `inspect-resolve-session` should be PASS/WARN with no
        resolve_color_management_mismatch before trusting previews.
      * Operates on display-encoded sRGB pixels, NOT scene-linear. CDL
        math in display gamma is approximate but visually accurate
        enough for skill-level decisions.
    """
    img_bgr = cv2.imread(str(source_png))
    if img_bgr is None:
        raise ValueError(f"could not read {source_png}")
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    img_f = img_rgb.astype(np.float32) / 255.0

    slope = np.array(slope_rgb, dtype=np.float32)
    offset = np.array(offset_rgb, dtype=np.float32)
    out_f = np.clip(img_f * slope + offset, 0.0, 1.0)

    out_uint8 = (out_f * 255.0).astype(np.uint8)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), cv2.cvtColor(out_uint8, cv2.COLOR_RGB2BGR))
    return out_path


__all__ = [
    "DEFAULT_STILL_WIDTH",
    "STILLS_SUBDIR",
    "StillRenderResult",
    "render_clip_still",
    "render_session_stills",
    "preview_cdl_on_still",
]
