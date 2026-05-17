from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from piano_guard.config import SessionProjectConfig, iter_session_takes
from piano_guard.reports import Issue, issues_to_dict, write_json_report


MANUAL_CDL_VERSION_NAME = "auto-state-99-v1"
MANUAL_CDL_REPORT = "manual-cdl.json"
COLOR_REVIEW_MANIFEST = "color-review-manifest.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip_entries(session: SessionProjectConfig) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    stills_root = session.resolve_path(f"{session.reports_dir}/stills")
    for take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            angle = camera.label
            clip_id = f"{take_ref.id}/{angle}"
            still_path = stills_root / take_ref.id / f"{angle}.png"
            entries.append(
                {
                    "clip_id": clip_id,
                    "take_id": take_ref.id,
                    "angle": angle,
                    "action": "review",
                    "is_reference": False,
                    "source_path": str(take.resolve_path(camera.file)),
                    "still_path": str(still_path),
                    "still_exists": still_path.is_file(),
                }
            )
    return entries


def _angle_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    angles_by_take: dict[str, set[str]] = {}
    for entry in entries:
        angles_by_take.setdefault(entry["take_id"], set()).add(entry["angle"])

    all_angles = sorted({angle for angles in angles_by_take.values() for angle in angles})
    if angles_by_take:
        common_angles = sorted(set.intersection(*(set(angles) for angles in angles_by_take.values())))
    else:
        common_angles = []

    takes = []
    missing_by_take: dict[str, list[str]] = {}
    for take_id in sorted(angles_by_take):
        angles = sorted(angles_by_take[take_id])
        missing = [angle for angle in all_angles if angle not in angles_by_take[take_id]]
        missing_by_take[take_id] = missing
        takes.append(
            {
                "take_id": take_id,
                "angles": angles,
                "missing_angles": missing,
                "clip_count": len(angles),
            }
        )

    return {
        "all_angles": all_angles,
        "common_angles": common_angles,
        "take_count": len(angles_by_take),
        "clip_count": len(entries),
        "missing_by_take": missing_by_take,
        "takes": takes,
    }


def build_review_manifest(session: SessionProjectConfig, *, write_report: bool = True) -> dict[str, Any]:
    issues: list[Issue] = []
    entries = _clip_entries(session)
    angle_summary = _angle_summary(entries)

    for entry in entries:
        if not entry["still_exists"]:
            issues.append(
                Issue(
                    severity="fail",
                    code="still_missing",
                    message=f"{entry['clip_id']}: still not found at {entry['still_path']}",
                    context={"clip_id": entry["clip_id"], "still_path": entry["still_path"]},
                )
            )

    status = "FAIL" if any(issue.severity == "fail" for issue in issues) else ("WARN" if issues else "PASS")
    payload = {
        "session_id": session.session_id,
        "generated_at": _now_iso(),
        "reference_angle": session.reference_angle,
        "review_mode": "all_clips",
        "angle_summary": angle_summary,
        "status": status,
        "summary": (
            f"prepared {len(entries)} clip(s) for AI color review"
            if status == "PASS"
            else f"color review manifest has {len(issues)} issue(s)"
        ),
        "clips": entries,
        "issues": issues_to_dict(issues),
    }
    if write_report:
        write_json_report(session.reports_path(COLOR_REVIEW_MANIFEST), payload)
    return payload


def _read_sheet_image(path: str, *, target_width: int, target_height: int) -> np.ndarray:
    img = cv2.imread(path)
    if img is None:
        canvas = np.full((target_height, target_width, 3), 235, dtype=np.uint8)
        cv2.putText(canvas, "missing", (20, target_height // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (40, 40, 40), 2)
        return canvas

    h, w = img.shape[:2]
    scale = min(target_width / max(w, 1), target_height / max(h, 1))
    resized = cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    canvas = np.full((target_height, target_width, 3), 245, dtype=np.uint8)
    y = (target_height - resized.shape[0]) // 2
    x = (target_width - resized.shape[1]) // 2
    canvas[y : y + resized.shape[0], x : x + resized.shape[1]] = resized
    return canvas


def _draw_cell_label(sheet: np.ndarray, text: str, x: int, y: int, width: int, height: int) -> None:
    cv2.rectangle(sheet, (x, y), (x + width, y + height), (242, 242, 242), -1)
    cv2.putText(
        sheet,
        text,
        (x + 10, y + height - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.58,
        (30, 30, 30),
        1,
        cv2.LINE_AA,
    )


def _draw_missing_angle_cell(sheet: np.ndarray, x: int, y: int, width: int, height: int, take_id: str, angle: str) -> None:
    cv2.rectangle(sheet, (x, y), (x + width, y + height), (238, 238, 238), -1)
    cv2.line(sheet, (x, y), (x + width, y + height), (210, 210, 210), 1)
    cv2.line(sheet, (x + width, y), (x, y + height), (210, 210, 210), 1)
    cv2.putText(
        sheet,
        f"{take_id}/{angle}",
        (x + 10, y + 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (90, 90, 90),
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        sheet,
        "missing angle",
        (x + 10, y + 56),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (90, 90, 90),
        1,
        cv2.LINE_AA,
    )


def write_contact_sheet(
    session: SessionProjectConfig,
    out_path: Path,
    *,
    cell_width: int = 360,
    cell_height: int = 230,
    label_height: int = 34,
    header_height: int = 42,
    take_label_width: int = 120,
) -> dict[str, Any]:
    manifest = build_review_manifest(session, write_report=True)
    if manifest["status"] == "FAIL":
        return {
            "status": "FAIL",
            "out_path": str(out_path.resolve()),
            "summary": "contact sheet not written because review manifest failed",
            "manifest_status": manifest["status"],
            "issues": manifest["issues"],
        }

    clips = manifest["clips"]
    if not clips:
        return {
            "status": "FAIL",
            "out_path": str(out_path.resolve()),
            "summary": "contact sheet not written because no clips were found",
            "manifest_status": manifest["status"],
            "issues": [],
        }

    by_take: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in clips:
        by_take.setdefault(entry["take_id"], {})[entry["angle"]] = entry

    angle_columns = list(manifest["angle_summary"]["all_angles"])
    row_height = cell_height + label_height
    sheet_height = header_height + row_height * len(by_take)
    sheet_width = take_label_width + cell_width * len(angle_columns)
    sheet = np.full((sheet_height, sheet_width, 3), 255, dtype=np.uint8)

    cv2.rectangle(sheet, (0, 0), (sheet_width, header_height), (232, 232, 232), -1)
    cv2.putText(sheet, "take", (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (25, 25, 25), 1, cv2.LINE_AA)
    for col_index, angle in enumerate(angle_columns):
        x = take_label_width + col_index * cell_width
        cv2.putText(sheet, angle, (x + 10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (25, 25, 25), 1, cv2.LINE_AA)

    for row_index, take_id in enumerate(sorted(by_take)):
        y = header_height + row_index * row_height
        cv2.rectangle(sheet, (0, y), (take_label_width, y + row_height), (248, 248, 248), -1)
        cv2.putText(sheet, take_id, (12, y + 32), cv2.FONT_HERSHEY_SIMPLEX, 0.68, (25, 25, 25), 1, cv2.LINE_AA)
        for col_index, angle in enumerate(angle_columns):
            x = take_label_width + col_index * cell_width
            entry = by_take[take_id].get(angle)
            if entry is None:
                _draw_missing_angle_cell(sheet, x, y, cell_width, cell_height, take_id, angle)
                label = f"{take_id}/{angle}"
            else:
                img = _read_sheet_image(entry["still_path"], target_width=cell_width, target_height=cell_height)
                sheet[y : y + cell_height, x : x + cell_width] = img
                label = entry["clip_id"]
            _draw_cell_label(sheet, label, x, y + cell_height, cell_width, label_height)

    for col_index in range(len(angle_columns) + 1):
        x = take_label_width + col_index * cell_width
        cv2.line(sheet, (x, 0), (x, sheet_height), (215, 215, 215), 1)
    for row_index in range(len(by_take) + 1):
        y = header_height + row_index * row_height
        cv2.line(sheet, (0, y), (sheet_width, y), (215, 215, 215), 1)

    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), sheet)
    return {
        "status": "PASS" if manifest["status"] == "PASS" else "WARN",
        "out_path": str(out_path),
        "summary": f"wrote contact sheet for {len(clips)} clip(s)",
        "manifest_status": manifest["status"],
        "angle_columns": angle_columns,
        "take_count": len(by_take),
        "clip_count": len(clips),
        "issues": manifest["issues"],
    }


def append_manual_cdl_report(
    session: SessionProjectConfig,
    *,
    clip_id: str,
    slope: tuple[float, float, float],
    offset: tuple[float, float, float],
    apply_payload: dict[str, Any],
) -> Path:
    report_path = session.reports_path(MANUAL_CDL_REPORT)
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            report = {}
    else:
        report = {}

    entries = list(report.get("entries") or [])
    entries.append(
        {
            "applied_at": _now_iso(),
            "clip_id": clip_id,
            "slope": list(slope),
            "offset": list(offset),
            "version_name": MANUAL_CDL_VERSION_NAME,
            "status": apply_payload.get("status"),
            "setcdl_rc": apply_payload.get("setcdl_rc"),
            "tools_present": apply_payload.get("tools_present"),
        }
    )
    payload = {
        "session_id": session.session_id,
        "version_name": MANUAL_CDL_VERSION_NAME,
        "updated_at": _now_iso(),
        "entries": entries,
    }
    write_json_report(report_path, payload)
    return report_path
