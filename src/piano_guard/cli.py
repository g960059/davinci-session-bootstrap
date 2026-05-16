from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from piano_guard.autogroup import apply_auto_group, auto_group_plan_to_dict, plan_auto_group
from piano_guard.audio_prep import prepare_session_audio, session_audio_prep_to_dict
from piano_guard.color_qc import inspect_session_color, session_color_to_dict
from piano_guard.config import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    AngleCalibration,
    CalibrationBox,
    CalibrationFrameSource,
    SessionCalibration,
    initialize_session,
    load_calibration,
    load_session,
    load_take,
    write_calibration,
    write_session,
)
from piano_guard.reports import write_json_report
from piano_guard.resolve_ops import (
    ResolveError,
    apply_auto_color_normalization,
    apply_color_normalization,
    bootstrap_session,
    ensure_project_library,
    ensure_resolve_running,
)
from piano_guard.session_project import inspect_session_project, session_validation_to_dict


CommandHandler = Callable[[argparse.Namespace], int]


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
        return
    status = payload.get("status", "PASS")
    summary = payload.get("summary")
    if summary:
        print(f"{status}: {summary}")
    else:
        print(status)


def _issues_payload(issues: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "severity": issue.severity,
            "code": issue.code,
            "message": issue.message,
            "context": issue.context,
        }
        for issue in issues
    ]


_STATUS_RANKS = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _fold_status(current: str, new: str | None) -> str:
    """Fold a stage status into the running final status.

    `WARN` beats `PASS`, `FAIL` beats both. Unknown statuses are treated as
    `PASS` so a missing status field never lowers the final rank.
    """
    new_rank = _STATUS_RANKS.get(new or "PASS", 0)
    current_rank = _STATUS_RANKS.get(current, 0)
    if new_rank > current_rank:
        return new or current
    return current


def _find_incoming_dir(session_root: Path, incoming_dir: str | None) -> Path | None:
    if incoming_dir:
        path = (session_root / incoming_dir).resolve()
        return path if path.is_dir() else None
    for candidate in ("incoming", "incomings"):
        path = (session_root / candidate).resolve()
        if path.is_dir():
            return path
    return None


def _incoming_media_count(session_root: Path, incoming_dir: str | None) -> int:
    incoming = _find_incoming_dir(session_root, incoming_dir)
    if incoming is None:
        return 0
    media_extensions = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
    return sum(
        1
        for path in incoming.iterdir()
        if path.is_file() and not path.name.startswith(".") and path.suffix.lower() in media_extensions
    )


def _default_project_library(session_root: Path) -> tuple[str, str]:
    library_root = session_root.parent.resolve()
    return f"{library_root.name} Library", str((library_root / "Resolve Project Library").resolve())


def _prepare_session_config(
    session_root: Path,
    *,
    project_name: str | None,
    project_library_name: str | None,
    project_library_path: str | None,
    reference_angle: str | None = None,
) -> Any:
    session = initialize_session(
        session_root,
        project_name=project_name,
        project_library_name=project_library_name,
    )
    default_library_name, default_library_path = _default_project_library(session_root)
    session.resolve.project_library_name = project_library_name or session.resolve.project_library_name or default_library_name
    session.resolve.project_library_path = (
        project_library_path or session.resolve.project_library_path or default_library_path
    )
    if project_name:
        session.resolve.project_name = project_name
    if reference_angle is not None:
        session.reference_angle = reference_angle
    write_session(session)
    return load_session(session.session_path)


def _validation_payload(validation: Any) -> dict[str, Any]:
    payload = session_validation_to_dict(validation)
    payload["summary"] = (
        f"{len(validation.takes)} takes validated"
        if validation.status == "PASS"
        else f"validation produced {len(validation.issues)} issues"
    )
    return payload


def _color_qc_payload(result: Any) -> dict[str, Any]:
    payload = session_color_to_dict(result)
    payload["summary"] = (
        "color QC clean" if result.status == "PASS" else f"color QC emitted {len(result.issues)} warnings/failures"
    )
    return payload


def command_group_session(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    media_count = _incoming_media_count(session_root, args.incoming_dir)

    if media_count == 0:
        session = _prepare_session_config(
            session_root,
            project_name=args.project_name,
            project_library_name=args.project_library_name,
            project_library_path=args.project_library_path,
        )
        payload = {
            "status": "PASS",
            "summary": f"session already grouped; {len(session.takes)} takes available",
            "session_config": str(session.session_path),
            "take_count": len(session.takes),
        }
        _emit(payload, as_json=args.json)
        return 0

    plan = plan_auto_group(session_root, incoming_dir=args.incoming_dir)
    if args.dry_run:
        payload = auto_group_plan_to_dict(plan)
        payload["summary"] = f"planned {len(plan.takes)} takes from {plan.incoming_dir}"
        _emit(payload, as_json=args.json)
        return 0 if plan.status == "PASS" else 1

    if plan.status != "PASS":
        payload = auto_group_plan_to_dict(plan)
        payload["summary"] = "grouping plan is not PASS; refusing to apply"
        _emit(payload, as_json=args.json)
        return 1

    apply_result = apply_auto_group(session_root, plan=plan, write_reports=False)
    session = _prepare_session_config(
        session_root,
        project_name=args.project_name,
        project_library_name=args.project_library_name,
        project_library_path=args.project_library_path,
        reference_angle=args.reference_angle,
    )
    payload = {
        "status": apply_result.status,
        "summary": f"applied {len(apply_result.takes_created)} takes",
        "session_config": str(session.session_path),
        "takes_created": apply_result.takes_created,
        "excluded_created": apply_result.excluded_created,
    }
    _emit(payload, as_json=args.json)
    return 0


def command_prepare_resolve_session(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = _prepare_session_config(
        session_root,
        project_name=args.project_name,
        project_library_name=args.project_library_name,
        project_library_path=args.project_library_path,
        reference_angle=args.reference_angle,
    )
    validation = inspect_session_project(session, write_take_reports=False)
    if validation.status == "FAIL":
        payload = _validation_payload(validation)
        write_json_report(session.reports_path("prepare-resolve-session.json"), payload)
        _emit(payload, as_json=args.json)
        return 1

    # Calibration gate: FAIL if calibration.yaml is missing unless the
    # operator explicitly opts into the legacy advisory-only path.
    calibration_path = session.calibration_file_path()
    if not calibration_path.exists() and not getattr(args, "allow_uncalibrated", False):
        payload = {
            "status": "FAIL",
            "session_config": str(session.session_path),
            "summary": (
                f"calibration file not found at {calibration_path}; "
                "run `piano-guard calibrate <session-root>` first, or pass "
                "--allow-uncalibrated to use the legacy fixed-ROI advisory path"
            ),
            "issues": [
                {
                    "severity": "fail",
                    "code": "calibration_missing",
                    "message": f"expected {calibration_path} to exist",
                    "context": {"calibration_path": str(calibration_path)},
                }
            ],
        }
        write_json_report(session.reports_path("prepare-resolve-session.json"), payload)
        _emit(payload, as_json=args.json)
        return 1

    final_payload: dict[str, Any] = {
        "status": "PASS",
        "session_config": str(session.session_path),
        "project_library": session.resolve.project_library_name,
        "stages": {},
    }

    if args.skip_edit_audio:
        audio_payload = {
            "session_id": session.session_id,
            "status": "PASS",
            "issues": [],
            "takes": [],
            "summary": "edit-audio generation skipped",
        }
    else:
        audio_result = prepare_session_audio(
            session,
            take_inspections=validation.take_inspections,
            dry_run=args.dry_run,
        )
        audio_payload = session_audio_prep_to_dict(audio_result)
        audio_payload["summary"] = (
            "audio proxies ready" if audio_result.status == "PASS" else f"audio prep emitted {len(audio_result.issues)} issue(s)"
        )
    final_payload["stages"]["audio_prep"] = audio_payload
    final_payload["status"] = _fold_status(final_payload["status"], audio_payload.get("status"))

    if not args.dry_run and final_payload["status"] != "FAIL":
        library_payload = ensure_project_library(
            library_name=session.resolve.project_library_name or _default_project_library(session_root)[0],
            library_path=session.resolve.project_library_path or _default_project_library(session_root)[1],
        )
        restart_required = library_payload["status"] == "WARN"
        connection = ensure_resolve_running(restart=restart_required)
        connector = lambda: connection
        bootstrap_payload = bootstrap_session(session, connector=connector, write_reports=False, fresh=args.fresh)
        final_payload["stages"]["bootstrap"] = bootstrap_payload
        final_payload["status"] = _fold_status(final_payload["status"], bootstrap_payload.get("status"))

    color_payload = _color_qc_payload(
        inspect_session_color(
            session,
            reference_angle=args.reference_angle,
            export_stills=not args.dry_run,
        )
    )
    final_payload["stages"]["qc"] = color_payload
    final_payload["status"] = _fold_status(final_payload["status"], color_payload.get("status"))

    total_issue_count = sum(
        len(stage_payload.get("issues", []) or [])
        for stage_payload in final_payload["stages"].values()
    )
    warning_suffix = (
        f" ({total_issue_count} warning(s) across stages)"
        if final_payload["status"] != "PASS" and total_issue_count
        else ""
    )
    if args.dry_run:
        final_payload["summary"] = (
            f"validated {session.resolve.project_name}; no Resolve or audio files were modified"
            f"{warning_suffix}"
        )
    else:
        final_payload["summary"] = (
            f"Resolve prepared for {session.resolve.project_name}; "
            f"create multicam clips and Session_Assembly manually in Resolve"
            f"{warning_suffix}"
        )
    write_json_report(session.reports_path("prepare-resolve-session.json"), final_payload)
    _emit(final_payload, as_json=args.json)
    return 1 if final_payload["status"] == "FAIL" else 0


CALIBRATE_DISPLAY_WIDTH = 1920
CALIBRATE_DISPLAY_HEIGHT = 1080
CALIBRATE_BRIGHTNESS_BOOST = 1.6
"""Preview brightness multiplier — the display-BT.709 path is accurate but
visually dim on a bright HLG scene. A mild boost keeps the cv2.selectROIs
UI usable without altering the measurement path."""


def _load_calibration_frame(
    session_root: Path, take_id: str, video_relative_path: str, frame_position: float
) -> tuple[Any, int]:
    """Load a representative frame as uint8 RGB at CALIBRATE_DISPLAY_WIDTH x _HEIGHT.

    Returns (frame_rgb_u8, absolute_frame_index).
    """
    import cv2  # local import to keep CLI startup fast when calibrate isn't used

    take_dir = session_root / "takes" / take_id
    video_path = (take_dir / video_relative_path).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"video file not found: {video_path}")
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"unable to open video: {video_path}")
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        idx = max(0, min(total - 1, int(round(total * frame_position))))
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame_bgr = cap.read()
        if not ok or frame_bgr is None:
            raise RuntimeError(f"unable to decode frame at {idx} from {video_path}")
    finally:
        cap.release()
    resized_bgr = cv2.resize(
        frame_bgr, (CALIBRATE_DISPLAY_WIDTH, CALIBRATE_DISPLAY_HEIGHT)
    )
    frame_rgb = cv2.cvtColor(resized_bgr, cv2.COLOR_BGR2RGB)
    return frame_rgb, idx


def _calibrate_preview(frame_rgb_u8: Any) -> Any:
    """Convert the raw frame to a display-friendly preview for cv2.selectROIs.

    Input: uint8 RGB from OpenCV (HLG-encoded pixel values).
    Output: uint8 BGR, brightness-boosted by CALIBRATE_BRIGHTNESS_BOOST.

    The preview path is separate from the measurement path (which uses
    color_match.to_linear_bt709). We apply CALIBRATE_BRIGHTNESS_BOOST because
    HLG encoding looks dim when treated as sRGB for display; the boost makes
    the piano, keyboard, and operator-visible features comfortable to annotate.
    """
    import cv2
    import numpy as np

    boosted = np.clip(frame_rgb_u8.astype(np.float32) * CALIBRATE_BRIGHTNESS_BOOST, 0, 255).astype("uint8")
    return cv2.cvtColor(boosted, cv2.COLOR_RGB2BGR)


def _draw_calibrate_hud(
    display_bgr: Any,
    prompt_lines: list[str],
    boxes: list[CalibrationBox],
    accent_color: tuple[int, int, int],
    button_rect: tuple[int, int, int, int],
    button_label: str,
) -> None:
    """Overlay the instruction panel, current boxes, and the NEXT/DONE button."""
    import cv2

    height, width = display_bgr.shape[:2]

    # Draw existing boxes with sequence numbers
    for index, box in enumerate(boxes, start=1):
        cv2.rectangle(
            display_bgr,
            (box.x, box.y),
            (box.x + box.w, box.y + box.h),
            accent_color,
            2,
        )
        label = str(index)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
        cv2.rectangle(
            display_bgr,
            (box.x, box.y - th - 8),
            (box.x + tw + 8, box.y),
            accent_color,
            -1,
        )
        cv2.putText(
            display_bgr,
            label,
            (box.x + 4, box.y - 4),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )

    # Prompt panel (top-left, semi-transparent)
    panel_w = min(780, width - 40)
    line_h = 32
    panel_h = 20 + line_h * len(prompt_lines) + 10
    overlay = display_bgr.copy()
    cv2.rectangle(overlay, (20, 20), (20 + panel_w, 20 + panel_h), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, display_bgr, 0.4, 0, dst=display_bgr)
    y = 20 + line_h
    for line in prompt_lines:
        cv2.putText(
            display_bgr,
            line,
            (32, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        y += line_h

    # NEXT / DONE button (bottom-right)
    bx1, by1, bx2, by2 = button_rect
    cv2.rectangle(display_bgr, (bx1, by1), (bx2, by2), (50, 50, 50), -1)
    cv2.rectangle(display_bgr, (bx1, by1), (bx2, by2), accent_color, 3)
    (tw, th), _ = cv2.getTextSize(button_label, cv2.FONT_HERSHEY_SIMPLEX, 0.9, 2)
    tx = bx1 + ((bx2 - bx1) - tw) // 2
    ty = by1 + ((by2 - by1) + th) // 2
    cv2.putText(
        display_bgr,
        button_label,
        (tx, ty),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )


CLICK_VS_DRAG_THRESHOLD_PX = 8
"""Mouse travel (in pixels) below which the gesture is treated as a click,
above which it is treated as a drag-to-draw. 8 px is tolerant of accidental
mouse tremor while still distinguishing a deliberate drag."""


def _pick_boxes_with_mouse(
    window_title: str,
    preview_bgr: Any,
    prompt: str,
    button_label: str,
    accent_color: tuple[int, int, int] = (0, 255, 0),
    point_box_size: int = 14,
) -> list[CalibrationBox]:
    """Mouse-only ROI picker with click-to-point and drag-to-draw modes.

    Controls (no keyboard required):
      - **Click**: place a small auto-sized box (`point_box_size × point_box_size`)
        centered on the click. Works well for narrow targets like individual
        white keys where drawing a box by hand is too fiddly.
      - **Click + drag** (> CLICK_VS_DRAG_THRESHOLD_PX): draw a custom-sized
        box from drag-start to drag-end.
      - **Hover**: shows a "ghost" preview box at the cursor location in
        point mode, so the operator can see exactly where the sample will
        land before clicking.
      - **Right-click**: undo the last box.
      - **Click the bottom-right button**: finish and return.

    ESC is accepted as an optional keyboard shortcut to exit. All other
    interaction is mouse-only — this avoids the `cv2.selectROIs` failure
    mode on macOS where ENTER/SPACE events don't reach the OpenCV window
    in remote/VM environments (Tailscale, Screen Sharing, VNC).
    """
    import cv2

    height, width = preview_bgr.shape[:2]
    button_rect = (width - 240, height - 90, width - 30, height - 30)
    half = max(1, point_box_size // 2)

    state: dict[str, Any] = {
        "boxes": [],
        "dragging": False,
        "drag_start": None,
        "drag_current": None,
        "hover": None,
        "finished": False,
    }

    bx1, by1, bx2, by2 = button_rect

    def _clamp_box(bx: int, by: int, bw: int, bh: int) -> CalibrationBox:
        bx = max(0, min(width - 1, bx))
        by = max(0, min(height - 1, by))
        bw = max(1, min(width - bx, bw))
        bh = max(1, min(height - by, bh))
        return CalibrationBox(x=bx, y=by, w=bw, h=bh)

    def on_mouse(event, x, y, flags, param):
        del flags, param  # unused
        nonlocal state
        if event == cv2.EVENT_MOUSEMOVE:
            state["hover"] = (x, y)
            if state["dragging"]:
                state["drag_current"] = (x, y)
        elif event == cv2.EVENT_LBUTTONDOWN:
            # Clicking the NEXT button finishes the stage
            if bx1 <= x <= bx2 and by1 <= y <= by2:
                state["finished"] = True
                return
            state["dragging"] = True
            state["drag_start"] = (x, y)
            state["drag_current"] = (x, y)
        elif event == cv2.EVENT_LBUTTONUP:
            if not state["dragging"]:
                return
            state["dragging"] = False
            sx, sy = state["drag_start"]
            ex, ey = x, y
            travel = max(abs(ex - sx), abs(ey - sy))
            if travel < CLICK_VS_DRAG_THRESHOLD_PX:
                # Treat as a click — drop a fixed-size box centered on the click
                cx = (sx + ex) // 2
                cy = (sy + ey) // 2
                state["boxes"].append(
                    _clamp_box(cx - half, cy - half, point_box_size, point_box_size)
                )
            else:
                # Drag to draw a custom-sized box
                bx = min(sx, ex)
                by = min(sy, ey)
                bw = abs(ex - sx)
                bh = abs(ey - sy)
                state["boxes"].append(_clamp_box(bx, by, bw, bh))
        elif event == cv2.EVENT_RBUTTONUP:
            if state["boxes"]:
                state["boxes"].pop()

    prompt_lines = prompt.splitlines()

    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window_title, 1280, 720)
    cv2.setMouseCallback(window_title, on_mouse)

    try:
        while not state["finished"]:
            display = preview_bgr.copy()
            _draw_calibrate_hud(
                display,
                prompt_lines
                + [
                    f"boxes: {len(state['boxes'])}   "
                    f"click=auto {point_box_size}x{point_box_size}  drag=custom  right-click=undo"
                ],
                state["boxes"],
                accent_color,
                button_rect,
                button_label,
            )
            # Ghost preview when hovering in point mode (no drag)
            if (
                not state["dragging"]
                and state["hover"] is not None
                and not (bx1 <= state["hover"][0] <= bx2 and by1 <= state["hover"][1] <= by2)
            ):
                hx, hy = state["hover"]
                cv2.rectangle(
                    display,
                    (hx - half, hy - half),
                    (hx - half + point_box_size, hy - half + point_box_size),
                    accent_color,
                    1,
                )
            # In-progress drag preview
            if state["dragging"] and state["drag_start"] is not None:
                sx, sy = state["drag_start"]
                ex, ey = state["drag_current"]
                travel = max(abs(ex - sx), abs(ey - sy))
                if travel >= CLICK_VS_DRAG_THRESHOLD_PX:
                    cv2.rectangle(display, (sx, sy), (ex, ey), (0, 255, 255), 1)
                else:
                    cv2.rectangle(
                        display,
                        (sx - half, sy - half),
                        (sx - half + point_box_size, sy - half + point_box_size),
                        accent_color,
                        1,
                    )
            cv2.imshow(window_title, display)
            key = cv2.waitKey(30) & 0xFF
            if key == 27:  # ESC as a power-user shortcut
                state["finished"] = True
                break
    finally:
        try:
            cv2.destroyWindow(window_title)
        except Exception:
            cv2.destroyAllWindows()

    return list(state["boxes"])


def _validate_spread_warning(
    angle: str, white_key_boxes: list[CalibrationBox]
) -> str | None:
    """Warn if the white-key boxes are clustered in a narrow x-range.

    At least 2 boxes should span ≥ 1.5 octaves of keyboard width to give
    the temporal median a chance at varied hand-motion coverage.
    """
    if len(white_key_boxes) < 2:
        return None
    xs = sorted(b.x + b.w / 2 for b in white_key_boxes)
    span = xs[-1] - xs[0]
    # An 88-key keyboard at 1080p typically spans ~1400 pixels. 1.5 octaves
    # is ~1400 * (1.5/7) ≈ 300 pixels. Use 300 as the practical minimum.
    if span < 300:
        return (
            f"{angle}: white-key boxes span only {span:.0f} px; "
            f"spread them across ≥ 1.5 octaves for robust temporal sampling"
        )
    return None


def command_calibrate(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    if not session.takes:
        _print_stderr("error: session has no takes; run group-session first")
        return 1

    reference_angle = args.reference_angle or session.reference_angle
    if reference_angle is None:
        _print_stderr(
            "error: reference angle not specified; pass --reference-angle or set it in session.yaml"
        )
        return 1

    # Pick the first take that contains the reference angle as the frame source
    reference_take_ref = None
    for take_ref in session.takes:
        take = load_take(session.resolve_path(take_ref.take))
        labels = {c.label for c in take.camera_files}
        if reference_angle in labels:
            reference_take_ref = (take_ref, take)
            break
    if reference_take_ref is None:
        _print_stderr(f"error: reference angle {reference_angle!r} not found in any take")
        return 1
    reference_take_ref_id, reference_take = reference_take_ref
    reference_frame_position = 0.5

    # Enumerate all unique angles across all takes
    all_angles: list[str] = list(session.angles)
    if not all_angles:
        seen: set[str] = set()
        for take_ref in session.takes:
            tk = load_take(session.resolve_path(take_ref.take))
            for cam in tk.camera_files:
                if cam.label not in seen:
                    seen.add(cam.label)
                    all_angles.append(cam.label)

    # Move reference angle to the front so the operator sees the "anchor" angle first
    if reference_angle in all_angles:
        all_angles.remove(reference_angle)
        all_angles.insert(0, reference_angle)

    calibration_angles: dict[str, AngleCalibration] = {}
    spread_warnings: list[str] = []
    reference_frame_index = 0

    for angle in all_angles:
        # Find a take that contains this angle (prefer the reference take)
        source_take = reference_take
        if angle not in {c.label for c in source_take.camera_files}:
            for take_ref in session.takes:
                tk = load_take(session.resolve_path(take_ref.take))
                if angle in {c.label for c in tk.camera_files}:
                    source_take = tk
                    break
        video_relative = next(
            (c.file for c in source_take.camera_files if c.label == angle), None
        )
        if video_relative is None:
            _print_stderr(f"warn: angle {angle!r} has no source in any take; skipping")
            continue

        take_relative_dir = source_take.source_dir.name
        frame_rgb, frame_index = _load_calibration_frame(
            session_root, take_relative_dir, video_relative, reference_frame_position
        )
        if angle == reference_angle:
            reference_frame_index = frame_index

        preview_bgr = _calibrate_preview(frame_rgb)

        wk_prompt = (
            f"[{angle}]  WHITE KEYS  (place 3-5 points on flat white-key tops)\n"
            "Click on a key to drop a 12x12 sample. Drag for a custom box.\n"
            "Right-click to undo.  Click NEXT (bottom-right) when done."
        )
        white_boxes = _pick_boxes_with_mouse(
            f"calibrate: {angle} - white keys",
            preview_bgr,
            wk_prompt,
            button_label="NEXT ->",
            accent_color=(0, 255, 0),
            point_box_size=12,  # fits inside narrow white keys even at wide angles
        )
        if not white_boxes:
            _print_stderr(f"warn: no white-key boxes drawn for {angle}; skipping angle")
            continue
        if len(white_boxes) < 3:
            _print_stderr(
                f"warn: {angle} has only {len(white_boxes)} white-key box(es); "
                f"recommend 3-5 for robust sampling"
            )

        body_prompt = (
            f"[{angle}]  PIANO BODY  (place 2-3 points on flat glossy black surfaces)\n"
            "Avoid reflections, logos, edges.  Click=24x24 sample, drag=custom.\n"
            "Right-click to undo.  Click DONE (bottom-right) when finished."
        )
        body_boxes = _pick_boxes_with_mouse(
            f"calibrate: {angle} - body",
            preview_bgr,
            body_prompt,
            button_label="DONE ->",
            accent_color=(255, 64, 64),
            point_box_size=24,  # body surface has more uniform area, larger sample OK
        )
        if not body_boxes:
            _print_stderr(f"warn: no body boxes drawn for {angle}; skipping angle")
            continue

        calibration_angles[angle] = AngleCalibration(
            white_key_boxes=white_boxes, piano_body_boxes=body_boxes
        )
        warning = _validate_spread_warning(angle, white_boxes)
        if warning is not None:
            spread_warnings.append(warning)

    if reference_angle not in calibration_angles:
        _print_stderr(f"error: reference angle {reference_angle!r} was not calibrated; aborting")
        return 1
    if len(calibration_angles) < 2:
        _print_stderr("error: fewer than 2 angles calibrated; a cross-angle match requires ≥ 2 angles")
        return 1

    # Partial calibration (some angles skipped) must be visible in the payload,
    # not silently dropped. AGENTS.md: "no silent fallbacks, no hidden errors."
    missing_angles = [a for a in all_angles if a not in calibration_angles]

    calibration = SessionCalibration(
        session=session.session_id,
        calibrated_at=datetime.now(timezone.utc).isoformat(),
        reference_angle=reference_angle,
        frame_source=CalibrationFrameSource(
            take_id=reference_take_ref_id.id,
            frame_index=reference_frame_index,
            frame_position=reference_frame_position,
        ),
        angles=calibration_angles,
    )
    path = session.calibration_file_path()
    write_calibration(calibration, path)

    issues: list[dict[str, Any]] = []
    for missing in missing_angles:
        issues.append(
            {
                "severity": "warn",
                "code": "angle_not_calibrated",
                "message": f"angle {missing!r} was not calibrated; it will be skipped by apply-color-normalization",
                "context": {"angle": missing},
            }
        )
    for warning in spread_warnings:
        issues.append(
            {
                "severity": "warn",
                "code": "box_spread_narrow",
                "message": warning,
            }
        )

    status = "PASS" if not issues else "WARN"
    summary = f"calibration saved to {path} ({len(calibration_angles)} angles)"
    if missing_angles:
        summary += f"; {len(missing_angles)} angle(s) skipped: {', '.join(missing_angles)}"

    payload: dict[str, Any] = {
        "status": status,
        "summary": summary,
        "calibration_file": str(path),
        "reference_angle": reference_angle,
        "angles": {
            angle: {
                "white_key_box_count": len(ac.white_key_boxes),
                "piano_body_box_count": len(ac.piano_body_boxes),
            }
            for angle, ac in calibration_angles.items()
        },
        "missing_angles": missing_angles,
        "issues": issues,
    }
    _emit(payload, as_json=args.json)
    return 0


def command_apply_color_normalization(args: argparse.Namespace) -> int:
    """Read calibration, compute per-angle CDL, apply via Resolve SetCDL."""
    import cv2  # kept local because cv2 is heavy on import

    from piano_guard.color_match import (
        DEFAULT_N_FRAMES,
        AngleColors,
        extract_angle_colors,
        fit_cdl,
        validate_reference_quality,
    )
    from piano_guard.config import iter_session_takes  # not exported at top level
    from piano_guard.ingest import inspect_take

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    calibration_path = session.calibration_file_path()
    if not calibration_path.exists():
        _print_stderr(
            f"error: calibration file not found at {calibration_path}. "
            "Run `piano-guard calibrate <session-root>` first."
        )
        return 1

    calibration = load_calibration(calibration_path)
    reference_angle = calibration.reference_angle

    # Pull the reference frame (used for motion rejection) from the calibration source
    reference_take_id = calibration.frame_source.take_id
    reference_take_ref = next(
        (ref for ref in session.takes if ref.id == reference_take_id), None
    )
    if reference_take_ref is None:
        _print_stderr(
            f"error: calibration references take {reference_take_id!r} that is not in session.yaml"
        )
        return 1
    reference_take = load_take(session.resolve_path(reference_take_ref.take))
    reference_video_name = next(
        (c.file for c in reference_take.camera_files if c.label == reference_angle), None
    )
    if reference_video_name is None:
        _print_stderr(
            f"error: calibration reference angle {reference_angle!r} not found in take "
            f"{reference_take_id!r}"
        )
        return 1
    reference_video_path = reference_take.resolve_path(reference_video_name)
    cap = cv2.VideoCapture(str(reference_video_path))
    if not cap.isOpened():
        _print_stderr(f"error: unable to open reference video {reference_video_path}")
        return 1
    try:
        cap.set(cv2.CAP_PROP_POS_FRAMES, calibration.frame_source.frame_index)
        ok, frame_bgr = cap.read()
    finally:
        cap.release()
    if not ok or frame_bgr is None:
        _print_stderr(f"error: unable to decode calibration reference frame")
        return 1
    # Extract at analysis resolution (matches the calibration UI)
    frame_bgr_resized = cv2.resize(frame_bgr, (1920, 1080))
    reference_frame_rgb = cv2.cvtColor(frame_bgr_resized, cv2.COLOR_BGR2RGB)

    # Walk takes + angles, extract colors, fit CDL
    cdl_by_take_and_angle: dict[str, dict[str, dict[str, Any]]] = {}
    fit_issues: list[dict[str, Any]] = []
    reference_colors_by_take: dict[str, AngleColors] = {}
    target_colors: list[AngleColors] = []

    for take_ref, take in iter_session_takes(session):
        inspection = inspect_take(take)
        videos_by_label = {v.label: v for v in inspection.videos}
        for angle, angle_calibration in calibration.angles.items():
            if angle not in videos_by_label:
                continue
            video = videos_by_label[angle]
            try:
                colors = extract_angle_colors(
                    take,
                    angle,
                    angle_calibration,
                    reference_frame_rgb_u8=reference_frame_rgb if angle == reference_angle else None,
                    video=video,
                    n_frames=DEFAULT_N_FRAMES,
                )
            except Exception as exc:
                fit_issues.append(
                    {
                        "take_id": take_ref.id,
                        "angle": angle,
                        "severity": "warn",
                        "code": "extraction_failed",
                        "message": str(exc),
                    }
                )
                continue
            if angle == reference_angle:
                reference_colors_by_take[take_ref.id] = colors
            else:
                target_colors.append(colors)

    # Validate reference quality (first take that has valid reference colors)
    ref_issues: list[str] = []
    for take_id, ref_colors in reference_colors_by_take.items():
        ok, issues = validate_reference_quality(
            ref_colors.white_key_rgb_linear, ref_colors.body_rgb_linear
        )
        if not ok:
            ref_issues.append(
                f"{take_id}/{reference_angle}: " + "; ".join(issues)
            )
    if ref_issues:
        payload = {
            "status": "FAIL",
            "summary": (
                f"reference angle {reference_angle!r} failed data-quality checks. "
                "Re-calibrate with better boxes or pick a different reference angle."
            ),
            "issues": [
                {
                    "severity": "fail",
                    "code": "reference_angle_degenerate",
                    "message": msg,
                }
                for msg in ref_issues
            ],
        }
        write_json_report(session.reports_path("apply-color-normalization.json"), payload)
        _emit(payload, as_json=args.json)
        return 1

    # Fit CDL per take per target angle
    for target in target_colors:
        ref = reference_colors_by_take.get(target.take_id)
        if ref is None:
            fit_issues.append(
                {
                    "take_id": target.take_id,
                    "angle": target.angle,
                    "severity": "warn",
                    "code": "reference_missing_for_take",
                    "message": f"no reference-angle colors for take {target.take_id}",
                }
            )
            continue
        result = fit_cdl(ref, target)
        cdl_by_take_and_angle.setdefault(target.take_id, {})[target.angle] = {
            "slope": result.slope_rgb,
            "offset": result.offset_rgb,
            "power": result.power_rgb,
            "status": result.status,
            "code": result.code,
            "residuals": result.residuals,
        }

    # Compute the combined status: the extraction-phase fit_issues must
    # participate in status aggregation, not be silently appended after the
    # status is computed downstream.
    extraction_status = "PASS"
    for fi in fit_issues:
        sev = fi.get("severity", "warn")
        if sev == "fail":
            extraction_status = "FAIL"
            break
        if sev == "warn" and extraction_status != "FAIL":
            extraction_status = "WARN"

    # Apply to Resolve
    if args.dry_run:
        # Skip Resolve interaction, just emit the planned CDL values
        angle_count = sum(len(angles) for angles in cdl_by_take_and_angle.values())
        summary = (
            f"dry-run: computed CDL for {angle_count} angle-clip(s) across "
            f"{len(cdl_by_take_and_angle)} take(s). Rerun without --dry-run to apply to Resolve."
        )
        if fit_issues:
            summary += f" {len(fit_issues)} extraction issue(s) surfaced."
        payload = {
            "status": extraction_status,
            "summary": summary,
            "reference_angle": reference_angle,
            "cdl_by_take_and_angle": cdl_by_take_and_angle,
            "extraction_issues": fit_issues,
        }
        write_json_report(session.reports_path("apply-color-normalization.json"), payload)
        _emit(payload, as_json=args.json)
        return 1 if extraction_status == "FAIL" else 0

    # Ensure Resolve is running
    ensure_resolve_running()
    from piano_guard.resolve_ops import connect_to_resolve  # lazy import

    connection = connect_to_resolve()

    def _connector():
        return connection

    apply_payload = apply_color_normalization(
        session,
        cdl_by_take_and_angle=cdl_by_take_and_angle,
        reference_angle=reference_angle,
        connector=_connector,
    )
    # Merge extraction issues into the payload AND elevate the status
    # appropriately so extraction-stage problems are surfaced.
    apply_payload.setdefault("extraction_issues", []).extend(fit_issues)
    current_status = apply_payload.get("status", "PASS")
    combined = _fold_status(current_status, extraction_status)
    apply_payload["status"] = combined
    if fit_issues and "summary" in apply_payload:
        apply_payload["summary"] += (
            f" (+ {len(fit_issues)} extraction issue(s) surfaced)"
        )
    _emit(apply_payload, as_json=args.json)
    return 1 if apply_payload.get("status") == "FAIL" else 0


def command_manual_cdl(args: argparse.Namespace) -> int:
    """Apply a single operator-supplied CDL to one clip via a named remote version.

    Used by the /color-review skill: the AI agent reasons about per-clip
    color, decides on slope/offset values, and calls this one clip at a
    time. Each call is scoped to exactly one clip and writes to a
    named remote version (default: ``ai-manual-v1``) so the CDL path
    and the AI-assisted path are side-by-side on the source clip for
    the operator to A/B.
    """
    from piano_guard.resolve_ops import (
        apply_auto_color_normalization,
        connect_to_resolve,
    )

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    # Parse comma-separated slope / offset triples
    def _triple(raw: str, field: str) -> tuple[float, float, float]:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            raise ValueError(
                f"--{field} must be 3 comma-separated numbers, got {raw!r}"
            )
        return (float(parts[0]), float(parts[1]), float(parts[2]))

    try:
        slope = _triple(args.slope, "slope")
        offset = _triple(args.offset, "offset")
    except ValueError as exc:
        _print_stderr(f"error: {exc}")
        return 1

    # Build a single-clip state_assignment + CDL dict.
    # We pick a sentinel state_id for this manual fit. The version name
    # defaults to ai-manual-v1 but can be overridden; apply_auto_color_
    # normalization derives the version name from the state_id via
    # AUTO_STATE_VERSION_PREFIX, which doesn't match our custom name, so
    # we use the state_id convention where the version comes out as
    # auto-state-{state_id:02d}-v1 unless --version-name overrides.
    #
    # For now keep it simple and use state_id=99 → auto-state-99-v1
    # (outside the range of HDBSCAN-produced state IDs). Operators
    # running both paths see auto-state-NN-v1 (pipeline) next to
    # auto-state-99-v1 (manual) in the Versions panel.
    MANUAL_STATE_ID = 99

    # Use a generous duration so it covers the full clip regardless of
    # actual length. apply_auto_color_normalization uses segments only
    # for the multi-state-clip guard; a single-state clip is always
    # applied unconditionally.
    state_assignments = {
        args.clip_id: [(0.0, 86400.0, MANUAL_STATE_ID)]
    }
    cdl_per_state = {
        MANUAL_STATE_ID: {
            "slope": slope,
            "offset": offset,
            "power": (1.0, 1.0, 1.0),
            "status": "pass",
            "code": "manual",
        },
    }

    _print_stderr(
        f"[manual-cdl] applying slope={slope} offset={offset} to "
        f"{args.clip_id} via version auto-state-{MANUAL_STATE_ID:02d}-v1"
    )

    if args.dry_run:
        payload: dict[str, Any] = {
            "status": "PASS",
            "dry_run": True,
            "clip_id": args.clip_id,
            "slope": list(slope),
            "offset": list(offset),
            "version_name": f"auto-state-{MANUAL_STATE_ID:02d}-v1",
            "summary": "dry-run: would apply CDL; rerun without --dry-run to commit",
        }
        _emit(payload, as_json=args.json)
        return 0

    ensure_resolve_running()
    connection = connect_to_resolve()

    def _connector():
        return connection

    apply_payload = apply_auto_color_normalization(
        session,
        state_assignments=state_assignments,
        cdl_per_state=cdl_per_state,
        reference_state_id=-1,  # No clip will have dominant=-1, so none is skipped as reference
        connector=_connector,
        write_reports=False,
    )
    # Slim down to the single clip's outcome
    applied = [
        a for a in apply_payload.get("applied", []) if a.get("clip_id") == args.clip_id
    ]
    skipped = [
        s for s in apply_payload.get("skipped", []) if s.get("clip_id") == args.clip_id
    ]
    single_payload = {
        "status": apply_payload.get("status"),
        "clip_id": args.clip_id,
        "slope": list(slope),
        "offset": list(offset),
        "applied": applied,
        "skipped": skipped,
        "issues": apply_payload.get("issues", []),
        "summary": apply_payload.get("summary"),
    }
    _emit(single_payload, as_json=args.json)
    return 1 if single_payload.get("status") == "FAIL" or not applied else 0


def command_preview_cdl(args: argparse.Namespace) -> int:
    """Apply CDL math offline to a rendered still and save preview PNG.

    Used by /color-review skill to close the feedback loop without a
    Resolve round-trip (Resolve's ExportStills returns False on 20.3.2,
    blocking grab-still). Mathematically equivalent under YRGB Auto SDR
    Rec.709 project settings (Timeline = Rec.709 (Scene)).
    """
    from piano_guard.stills import preview_cdl_on_still

    source_png = Path(args.source_png).resolve()
    if not source_png.exists():
        _print_stderr(f"error: source PNG not found: {source_png}")
        return 1

    def _triple(raw: str, field: str) -> tuple[float, float, float]:
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            raise ValueError(f"--{field} must be 3 comma-separated numbers, got {raw!r}")
        return (float(parts[0]), float(parts[1]), float(parts[2]))

    try:
        slope = _triple(args.slope, "slope")
        offset = _triple(args.offset, "offset")
    except ValueError as exc:
        _print_stderr(f"error: {exc}")
        return 1

    out_path = Path(args.out).resolve()
    written = preview_cdl_on_still(source_png, slope, offset, out_path)
    payload = {
        "status": "PASS",
        "source_png": str(source_png),
        "slope": list(slope),
        "offset": list(offset),
        "out_path": str(written),
    }
    _emit(payload, as_json=args.json)
    return 0


def command_grab_still(args: argparse.Namespace) -> int:
    """Drive Resolve to render a still of a clip's current grade.

    Closes the /color-review skill's feedback loop: after manual-cdl
    writes a grade, grab-still captures the actual graded result for
    Claude (or the operator) to inspect.
    """
    from piano_guard.resolve_ops import (
        connect_to_resolve,
        grab_clip_still,
    )

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    out_path = Path(args.out).resolve()

    ensure_resolve_running()
    connection = connect_to_resolve()

    def _connector():
        return connection

    try:
        result = grab_clip_still(
            session,
            clip_id=args.clip_id,
            out_path=out_path,
            version_name=args.version_name,
            version_type=args.version_type,
            connector=_connector,
        )
    except ResolveError as exc:
        payload = {
            "status": "FAIL",
            "clip_id": args.clip_id,
            "summary": str(exc),
        }
        _emit(payload, as_json=args.json)
        return 1
    _emit(result, as_json=args.json)
    return 0 if result.get("status") == "PASS" else 1


def command_render_stills(args: argparse.Namespace) -> int:
    """Render per-clip mid-frame PNGs for the color-review skill."""
    from dataclasses import asdict

    from piano_guard.stills import DEFAULT_STILL_WIDTH, render_session_stills

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    results, issues = render_session_stills(session, width=args.width)

    payload: dict[str, Any] = {
        "session_id": session.session_id,
        "stills": [asdict(r) for r in results],
        "issues": [asdict(i) for i in issues],
        "status": "FAIL"
        if any(i.severity == "fail" for i in issues)
        else ("WARN" if issues else "PASS"),
        "summary": (
            f"rendered {len(results)} still(s) to "
            f"{session.resolve_path(f'{session.reports_dir}/stills')}"
            + (f"; {len(issues)} issue(s)" if issues else "")
        ),
        "default_width": DEFAULT_STILL_WIDTH,
    }
    write_json_report(session.reports_path("render-stills.json"), payload)
    _emit(payload, as_json=args.json)
    return 1 if payload["status"] == "FAIL" else 0


def command_auto_color_normalize(args: argparse.Namespace) -> int:
    """Phase 2 auto-segment: cluster fingerprints, fit per-state CDL, apply via Resolve."""
    from piano_guard.auto_segment_pipeline import (
        apply_library_hits,
        build_session_plan,
        fit_escape_transforms_for_states,
        write_library_entries_from_plan,
    )
    from piano_guard.fingerprint_library import (
        compute_rig_hash,
        default_library_path,
        load_library,
        save_library,
    )
    from dataclasses import asdict

    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    # Calibration is OPTIONAL in auto-segment (SAM 2 seeds its own masks when
    # no calibration exists). When present, calibration ROI centers are used
    # as SAM 2 point prompts — the most reliable masking path on the test
    # session (all 3 angles succeeded via sam2_from_rois with conf 0.44-0.90).
    calibration = None
    calibration_path = session.calibration_file_path()
    if calibration_path.exists():
        calibration = load_calibration(calibration_path)

    # Fingerprint library: load before the fit so the plan can short-circuit
    # non-reference state fits when a prior session in the same rig already
    # produced a CDL. --no-library disables the lookup AND the write-back.
    library_path = Path(args.library).expanduser().resolve() if args.library else default_library_path()
    library = None
    rig_hash = compute_rig_hash(session)
    if not args.no_library:
        library = load_library(library_path)

    _print_stderr(
        f"[auto-color-normalize] building session plan for {session.session_id} "
        f"(calibration={'yes' if calibration else 'no'}, "
        f"library={'yes' if library is not None else 'no'}, rig={rig_hash})"
    )

    plan = build_session_plan(
        session,
        calibration=calibration,
        window_seconds=args.window_seconds,
        min_cluster_size=args.min_cluster_size,
        cluster_epsilon=args.cluster_epsilon,
        reference_state_id_override=args.reference_state,
    )

    # Milestone 5: fit richer-transform escape-hatch for operator-opted states.
    escape_state_ids = set(args.escape_transform_states or [])
    if escape_state_ids:
        plan.richer_transforms_per_state = fit_escape_transforms_for_states(
            plan.states, plan.reference_state_id, escape_state_ids
        )
        unknown = escape_state_ids - set(plan.states.keys())
        if unknown:
            _print_stderr(
                f"[auto-color-normalize] WARN: --escape-transform requested "
                f"for unknown state_ids {sorted(unknown)}; will be ignored."
            )

    # Swap in cached CDLs from the library for any matching state.
    library_hits_before = 0
    room_filter: str | None = args.room if args.room is not None else None
    if library is not None:
        if room_filter is not None:
            library_hits_before = sum(
                1
                for e in library.entries
                if e.rig_hash == rig_hash and e.room_id == room_filter
            )
        else:
            library_hits_before = sum(
                1 for e in library.entries if e.rig_hash == rig_hash
            )
        apply_library_hits(plan, library, rig_hash=rig_hash, room_id=room_filter)

    # Summary payload (always produced even in dry-run)
    state_summaries: list[dict[str, Any]] = []
    for sid in sorted(plan.states.keys()):
        st = plan.states[sid]
        cdl = plan.cdl_per_state.get(sid)
        richer = plan.richer_transforms_per_state.get(sid)
        state_summaries.append(
            {
                "state_id": sid,
                "member_count": st.member_count,
                "is_reference": sid == plan.reference_state_id,
                "medoid_white_key_rgb": [float(x) for x in st.representative_fingerprint.white_key_rgb],
                "medoid_piano_body_rgb": [float(x) for x in st.representative_fingerprint.piano_body_rgb],
                "medoid_cct_kelvin": float(st.representative_fingerprint.estimated_cct_kelvin),
                "cdl_slope": list(cdl.slope_rgb) if cdl is not None else None,
                "cdl_offset": list(cdl.offset_rgb) if cdl is not None else None,
                "cdl_status": cdl.status if cdl is not None else None,
                "cdl_code": cdl.code if cdl is not None else None,
                # Milestone 5 escape: only populated when operator opted
                # this state into --escape-transform.
                "escape_mode": richer is not None,
                "escape_gain": list(richer.gain_rgb) if richer is not None else None,
                "escape_offset": list(richer.offset_rgb) if richer is not None else None,
                "escape_status": richer.status if richer is not None else None,
            }
        )

    clip_summaries: list[dict[str, Any]] = []
    for clip_id, cp in sorted(plan.clip_plans.items()):
        clip_summaries.append(
            {
                "clip_id": clip_id,
                "take_id": cp.take_id,
                "angle": cp.angle,
                "dominant_state_id": cp.dominant_state_id,
                "segment_count": cp.segment_count,
                "mask_source": cp.masks.source,
                "mask_confidence": float(cp.masks.confidence),
                "segments": [
                    {"start_s": s.start_s, "end_s": s.end_s, "state_id": s.state_id}
                    for s in cp.segments
                ],
            }
        )

    plan_payload: dict[str, Any] = {
        "session_id": session.session_id,
        "reference_state_id": plan.reference_state_id,
        "state_count": len(plan.states),
        "clip_count": len(plan.clip_plans),
        "states": state_summaries,
        "clips": clip_summaries,
        "issues": [asdict(i) for i in plan.issues],
    }

    plan_status = "FAIL" if any(i.severity == "fail" for i in plan.issues) else (
        "WARN" if any(i.severity == "warn" for i in plan.issues) else "PASS"
    )
    plan_payload["status"] = plan_status

    if plan_status == "FAIL":
        plan_payload["summary"] = (
            f"auto-color-normalize pipeline FAILED: "
            + "; ".join(i.message for i in plan.issues if i.severity == "fail")
        )
        write_json_report(
            session.reports_path("apply-auto-color-normalization.json"), plan_payload
        )
        _emit(plan_payload, as_json=args.json)
        return 1

    if args.dry_run:
        plan_payload["summary"] = (
            f"dry-run: built plan with {len(plan.states)} lighting state(s) and "
            f"{len(plan.clip_plans)} clip(s). Rerun without --dry-run to apply to Resolve."
        )
        write_json_report(
            session.reports_path("apply-auto-color-normalization.json"), plan_payload
        )
        _emit(plan_payload, as_json=args.json)
        return 0

    # --- apply to Resolve ---
    ensure_resolve_running()
    from piano_guard.resolve_ops import connect_to_resolve

    connection = connect_to_resolve()

    def _connector():
        return connection

    state_assignments = plan.as_state_assignments()
    # The CLI owns the final report write so plan + apply data land in
    # one consistent file. apply_auto_color_normalization is told NOT to
    # write its own report.
    apply_payload = apply_auto_color_normalization(
        session,
        state_assignments=state_assignments,
        cdl_per_state=plan.cdl_per_state,
        reference_state_id=plan.reference_state_id,
        richer_transforms_per_state=plan.richer_transforms_per_state or None,
        connector=_connector,
        write_reports=False,
    )

    # Merge the plan-phase summary into the apply payload so the operator
    # sees BOTH the clustering outcome and the Resolve apply outcome in a
    # single report file.
    apply_payload["plan"] = {
        k: plan_payload[k]
        for k in ("reference_state_id", "state_count", "clip_count", "states", "clips")
    }
    merged_issues = list(plan_payload.get("issues", [])) + list(apply_payload.get("issues", []))
    apply_payload["issues"] = merged_issues
    combined_status = _fold_status(apply_payload.get("status", "PASS"), plan_status)
    apply_payload["status"] = combined_status

    # Persist new library entries for states that were NOT library hits and
    # produced a usable CDL. The existing entries' match_count has already
    # been bumped by apply_library_hits.
    library_entries_written = 0
    if library is not None and combined_status != "FAIL" and not args.no_library_writeback:
        library_entries_written = write_library_entries_from_plan(
            plan,
            library,
            rig_hash=rig_hash,
            room_id=args.room or "",
        )
        save_library(library, library_path)

    apply_payload["library"] = {
        "enabled": library is not None,
        "path": str(library_path) if library is not None else None,
        "rig_hash": rig_hash,
        "room_filter": room_filter,
        "entries_for_rig_before": library_hits_before,
        "entries_written": library_entries_written,
        "writeback_suppressed": bool(args.no_library_writeback),
        "load_issues": list(library.load_issues) if library is not None else [],
        "hits": [
            i.context.get("entry_id")
            for i in plan.issues
            if i.code == "library_hit"
        ],
    }

    # Explicit per-clip accounting: even clips that failed upstream (probe,
    # fingerprint extraction, empty masks, or got skipped at apply time)
    # should appear in the report so an operator can grep a single place
    # for "what happened to clip X?".
    failed_clip_codes = {
        "video_probe_failed",
        "fingerprint_compute_failed",
        "empty_mask_clip",
        "empty_fingerprints",
    }
    failed_clips: list[dict[str, Any]] = []
    for issue_dict in apply_payload.get("issues") or []:
        code = issue_dict.get("code") if isinstance(issue_dict, dict) else None
        if code not in failed_clip_codes:
            continue
        context = issue_dict.get("context") or {}
        failed_clips.append(
            {
                "clip_id": context.get("clip_id") or "(unknown)",
                "code": code,
                "message": issue_dict.get("message"),
                "context": context,
            }
        )
    apply_payload["failed_clips"] = failed_clips

    write_json_report(
        session.reports_path("apply-auto-color-normalization.json"), apply_payload
    )
    _emit(apply_payload, as_json=args.json)
    return 1 if combined_status == "FAIL" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="piano-guard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    group_parser = subparsers.add_parser(
        "group-session",
        help="group incoming media into takes and write session/take configs",
    )
    group_parser.add_argument("session_root")
    group_parser.add_argument("--incoming-dir")
    group_parser.add_argument("--dry-run", action="store_true")
    group_parser.add_argument("--project-name")
    group_parser.add_argument("--project-library-name")
    group_parser.add_argument("--project-library-path")
    group_parser.add_argument("--reference-angle")
    group_parser.add_argument("--json", action="store_true")
    group_parser.set_defaults(func=command_group_session)

    prepare_parser = subparsers.add_parser(
        "prepare-resolve-session",
        help="prepare Resolve for editing: ensure library, bootstrap/import media, and print color QC",
    )
    prepare_parser.add_argument("session_root")
    prepare_parser.add_argument("--project-name")
    prepare_parser.add_argument("--project-library-name")
    prepare_parser.add_argument("--project-library-path")
    prepare_parser.add_argument("--reference-angle")
    prepare_parser.add_argument("--dry-run", action="store_true")
    prepare_parser.add_argument("--skip-edit-audio", action="store_true")
    prepare_parser.add_argument("--fresh", action="store_true")
    prepare_parser.add_argument(
        "--allow-uncalibrated",
        action="store_true",
        help=(
            "run against the legacy fixed-ROI advisory color QC path when no "
            "calibration.yaml is present. Without this flag, missing calibration "
            "is a hard FAIL (run `piano-guard calibrate` first)."
        ),
    )
    prepare_parser.add_argument("--json", action="store_true")
    prepare_parser.set_defaults(func=command_prepare_resolve_session)

    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="one-time per-session ROI annotation for cross-angle color matching",
    )
    calibrate_parser.add_argument("session_root")
    calibrate_parser.add_argument("--reference-angle")
    calibrate_parser.add_argument("--json", action="store_true")
    calibrate_parser.set_defaults(func=command_calibrate)

    apply_cm_parser = subparsers.add_parser(
        "apply-color-normalization",
        help=(
            "apply per-angle CDL color normalization to source clips (use before "
            "creating multicam clips)"
        ),
    )
    apply_cm_parser.add_argument("session_root")
    apply_cm_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="compute CDL and emit report without touching Resolve",
    )
    apply_cm_parser.add_argument("--json", action="store_true")
    apply_cm_parser.set_defaults(func=command_apply_color_normalization)

    manual_cdl_parser = subparsers.add_parser(
        "manual-cdl",
        help=(
            "apply a single operator-supplied CDL to one clip via a "
            "named remote version (auto-state-99-v1). Used by the "
            "/color-review skill to write AI-proposed grades one clip at "
            "a time. Coexists with pipeline-generated auto-state-NN-v1 "
            "so the operator can A/B in the Versions panel."
        ),
    )
    manual_cdl_parser.add_argument("session_root")
    manual_cdl_parser.add_argument(
        "--clip-id",
        required=True,
        help='clip identifier ("take-01/angle-b" format) — see --list-clips on auto-color-normalize --dry-run output',
    )
    manual_cdl_parser.add_argument(
        "--slope",
        required=True,
        help="comma-separated RGB slope values, e.g. '1.08,0.95,1.18'",
    )
    manual_cdl_parser.add_argument(
        "--offset",
        required=True,
        help="comma-separated RGB offset values, e.g. '0.02,0.0,-0.01'",
    )
    manual_cdl_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="emit the planned CDL without touching Resolve",
    )
    manual_cdl_parser.add_argument("--json", action="store_true")
    manual_cdl_parser.set_defaults(func=command_manual_cdl)

    preview_cdl_parser = subparsers.add_parser(
        "preview-cdl",
        help=(
            "apply ASC CDL math offline to a rendered still and save a "
            "preview PNG. Used by /color-review to see what a proposed "
            "CDL would look like, without touching Resolve. Equivalent "
            "to Resolve's SetCDL output under YRGB Auto SDR Rec.709."
        ),
    )
    preview_cdl_parser.add_argument(
        "--source-png",
        required=True,
        help="path to a render-stills PNG of the ungraded clip",
    )
    preview_cdl_parser.add_argument(
        "--slope",
        required=True,
        help="comma-separated RGB slope values",
    )
    preview_cdl_parser.add_argument(
        "--offset",
        required=True,
        help="comma-separated RGB offset values",
    )
    preview_cdl_parser.add_argument(
        "--out", required=True, help="output preview PNG path"
    )
    preview_cdl_parser.add_argument("--json", action="store_true")
    preview_cdl_parser.set_defaults(func=command_preview_cdl)

    grab_still_parser = subparsers.add_parser(
        "grab-still",
        help=(
            "drive Resolve to render a PNG still of one clip's current grade "
            "(or a specified named version). Used by the /color-review skill "
            "to close the feedback loop after manual-cdl: see the actual "
            "graded result, not just the ungraded source."
        ),
    )
    grab_still_parser.add_argument("session_root")
    grab_still_parser.add_argument(
        "--clip-id",
        required=True,
        help='clip identifier ("take-01/angle-c" form)',
    )
    grab_still_parser.add_argument(
        "--out",
        required=True,
        help="output PNG path (parent dirs created if missing)",
    )
    grab_still_parser.add_argument(
        "--version-name",
        default=None,
        help=(
            "named version to load before grabbing (e.g. 'auto-state-99-v1' "
            "for manual-cdl output, or 'Version 1' for ungraded). Default: "
            "use the clip's currently-active version."
        ),
    )
    grab_still_parser.add_argument(
        "--version-type",
        type=int,
        default=1,
        choices=[0, 1],
        help="0 = local version, 1 = remote version (default 1)",
    )
    grab_still_parser.add_argument("--json", action="store_true")
    grab_still_parser.set_defaults(func=command_grab_still)

    render_stills_parser = subparsers.add_parser(
        "render-stills",
        help=(
            "render one mid-frame PNG per (take, angle) clip for the "
            "color-review skill. HLG → Rec.709 display tonemap applied so "
            "the PNGs open correctly in standard viewers."
        ),
    )
    render_stills_parser.add_argument("session_root")
    render_stills_parser.add_argument(
        "--width",
        type=int,
        default=960,
        help="target PNG width in pixels; aspect preserved. Default 960.",
    )
    render_stills_parser.add_argument("--json", action="store_true")
    render_stills_parser.set_defaults(func=command_render_stills)

    auto_acn_parser = subparsers.add_parser(
        "auto-color-normalize",
        help=(
            "Phase 2 auto-segment: detect piano masks, cluster lighting-state "
            "fingerprints across the session, fit per-state CDL, and apply via "
            "auto-state-NN-v1 remote versions"
        ),
    )
    auto_acn_parser.add_argument("session_root")
    auto_acn_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "build and emit the clustering + fit plan without touching Resolve. "
            "Useful for inspecting state assignments before committing the grade."
        ),
    )
    auto_acn_parser.add_argument(
        "--library",
        default=None,
        help=(
            "override the default fingerprint library path "
            "(~/Library/Application Support/piano-guard/fingerprint-library.json). "
            "The library caches per-state CDLs across sessions with the same camera rig."
        ),
    )
    auto_acn_parser.add_argument(
        "--no-library",
        action="store_true",
        help=(
            "disable library lookup AND write-back for this run. Forces a "
            "cold fit for every state and does not persist new entries."
        ),
    )
    auto_acn_parser.add_argument(
        "--no-library-writeback",
        action="store_true",
        help=(
            "still use library lookup (cached CDL hits), but do NOT write new "
            "entries for this run. Use for atypical / one-off sessions whose "
            "fingerprints would pollute the shared library."
        ),
    )
    auto_acn_parser.add_argument(
        "--room",
        default=None,
        help=(
            "operator-chosen room label stored on new library entries "
            "(e.g., 'studio-A'). Not used for matching — only for human "
            "inspection of fingerprint-library.json."
        ),
    )
    auto_acn_parser.add_argument(
        "--window-seconds",
        type=float,
        default=1.5,
        help=(
            "per-window sampling stride for fingerprints. Default 1.5 s is "
            "appropriate for 2-minute takes; bump to 15-30 s for long (20+ min) "
            "takes to keep the run fast without losing lighting-regime "
            "resolution (real day-night transitions span minutes)."
        ),
    )
    auto_acn_parser.add_argument(
        "--min-cluster-size",
        type=int,
        default=None,
        help=(
            "HDBSCAN min_cluster_size override. When omitted (the usual case) "
            "it is derived from total window count via "
            "auto_segment.adaptive_cluster_params — 2%% of total windows with "
            "an absolute floor of 3. Increase to force fewer, larger states "
            "(e.g. --min-cluster-size 50 for very fine-grained E2E debug)."
        ),
    )
    auto_acn_parser.add_argument(
        "--cluster-epsilon",
        type=float,
        default=None,
        help=(
            "HDBSCAN cluster_selection_epsilon override. Merges clusters whose "
            "6-D feature-space distance is below this. Default 0.10 is tuned "
            "for real HLG 8-bit piano footage; lower to preserve finer state "
            "distinctions, raise (e.g. 0.20) to merge noisy near-duplicates."
        ),
    )
    auto_acn_parser.add_argument(
        "--reference-state",
        type=int,
        default=None,
        metavar="STATE_ID",
        help=(
            "Force a specific state_id to be the reference (target all "
            "other states to match). Default: most-populous non-noise "
            "state whose medoid passes quality gates. Use this when the "
            "auto-selected reference is spectrally extreme (e.g., the "
            "warmest angle of a warm-lit scene) and cross-angle matching "
            "over-saturates the result. Run --dry-run first to see which "
            "state_ids exist; pick one whose medoid is closer to neutral."
        ),
    )
    auto_acn_parser.add_argument(
        "--escape-transform",
        type=int,
        action="append",
        dest="escape_transform_states",
        default=None,
        metavar="STATE_ID",
        help=(
            "Milestone 5 opt-in: use the richer transform (LUT, unbounded "
            "gain/offset) for the named state_id instead of the clipped CDL. "
            "Repeatable (e.g. --escape-transform 0 --escape-transform 7). "
            "Run --dry-run first to see which state_ids exist for the session; "
            "use this flag only for states whose CDL status came back 'fail' "
            "because the [0.8, 1.25] slope bounds couldn't bridge target to "
            "reference."
        ),
    )
    auto_acn_parser.add_argument("--json", action="store_true")
    auto_acn_parser.set_defaults(func=command_auto_color_normalize)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: CommandHandler = args.func
    try:
        return handler(args)
    except (FileNotFoundError, RuntimeError, ResolveError, subprocess.CalledProcessError) as exc:
        _print_stderr(f"error: {exc}")
        return 1
