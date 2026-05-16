from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from piano_guard.config import SessionProjectConfig, load_take
from piano_guard.ingest import InspectionResult, inspection_to_dict, inspect_take, write_inspection_reports
from piano_guard.reports import Issue, issues_to_dict, overall_status, write_json_report, write_markdown_report


@dataclass
class TakeValidationSummary:
    take_id: str
    take_path: str
    source_dir: str
    status: str


@dataclass
class SessionValidationResult:
    session_id: str
    generated_at: str
    status: str
    issues: list[Issue]
    takes: list[TakeValidationSummary]
    take_inspections: dict[str, InspectionResult]


def _take_issue(issue: Issue, take_id: str) -> Issue:
    context = {"take_id": take_id, **issue.context}
    return Issue(issue.severity, issue.code, issue.message, context)


def inspect_session_project(
    session: SessionProjectConfig,
    *,
    write_take_reports: bool = True,
) -> SessionValidationResult:
    issues: list[Issue] = []
    take_summaries: list[TakeValidationSummary] = []
    take_inspections: dict[str, InspectionResult] = {}

    if not session.takes:
        issues.append(Issue("fail", "session_has_no_takes", "session.yaml contains no takes"))

    if session.logic.project_file and not session.resolve_path(session.logic.project_file).exists():
        issues.append(
            Issue(
                "warn",
                "logic_project_path_missing",
                f"Logic project path does not exist: {session.logic.project_file}",
            )
        )

    seen_ids: set[str] = set()
    for take_ref in session.takes:
        if take_ref.id in seen_ids:
            issues.append(Issue("fail", "duplicate_take_id", f"duplicate take id: {take_ref.id}"))
            continue
        seen_ids.add(take_ref.id)

        take_path = session.resolve_path(take_ref.take)
        try:
            take = load_take(take_path)
        except FileNotFoundError:
            issues.append(
                Issue(
                    "fail",
                    "take_file_missing",
                    f"{take_ref.id} take file does not exist: {take_ref.take}",
                )
            )
            take_summaries.append(
                TakeValidationSummary(
                    take_id=take_ref.id,
                    take_path=take_ref.take,
                    source_dir=str(session.session_root / "takes" / take_ref.id),
                    status="FAIL",
                )
            )
            continue
        except Exception as exc:
            issues.append(
                Issue(
                    "fail",
                    "take_load_failed",
                    f"{take_ref.id} take could not be loaded: {exc}",
                )
            )
            take_summaries.append(
                TakeValidationSummary(
                    take_id=take_ref.id,
                    take_path=take_ref.take,
                    source_dir=str(session.session_root / "takes" / take_ref.id),
                    status="FAIL",
                )
            )
            continue

        expected_take_dir = session.session_root / "takes" / take_ref.id
        if take.source_dir.resolve() != expected_take_dir.resolve():
            issues.append(
                Issue(
                    "fail",
                    "take_source_dir_mismatch",
                    f"{take_ref.id} source_dir does not match takes/{take_ref.id}",
                    {"source_dir": str(take.source_dir)},
                )
            )
        if take.session != session.session_id:
            issues.append(
                Issue(
                    "fail",
                    "take_session_mismatch",
                    f"{take_ref.id} take session {take.session} != {session.session_id}",
                )
            )
        if take.resolve.project_name != session.resolve.project_name:
            issues.append(
                Issue(
                    "fail",
                    "take_project_name_mismatch",
                    f"{take_ref.id} Resolve project_name {take.resolve.project_name} != {session.resolve.project_name}",
                )
            )
        labels = [camera.label for camera in take.camera_files]
        if len(labels) != len(set(labels)):
            issues.append(
                Issue(
                    "fail",
                    "take_duplicate_camera_labels",
                    f"{take_ref.id} contains duplicate camera labels",
                )
            )
        if session.angles:
            unknown_labels = sorted(label for label in labels if label not in session.angles)
            if unknown_labels:
                issues.append(
                    Issue(
                        "fail",
                        "take_camera_label_unknown",
                        f"{take_ref.id} contains labels not present in session.yaml angles",
                        {"labels": unknown_labels},
                    )
                )

        for field in ("frame_rate", "width", "height", "input_color_space", "timeline_color_space", "output_color_space"):
            session_value = getattr(session.timeline, field)
            take_value = getattr(take.timeline, field)
            if take_value != session_value:
                issues.append(
                    Issue(
                        "fail",
                        "take_timeline_setting_mismatch",
                        f"{take_ref.id} {field} {take_value} != {session_value}",
                        {"field": field},
                    )
                )

        inspection = inspect_take(take)
        if write_take_reports:
            write_inspection_reports(take, inspection)
        take_inspections[take_ref.id] = inspection
        take_summaries.append(
            TakeValidationSummary(
                take_id=take_ref.id,
                take_path=take_ref.take,
                source_dir=str(take.source_dir),
                status=inspection.status,
            )
        )
        issues.extend(_take_issue(issue, take_ref.id) for issue in inspection.issues)

    session_frame_rates = sorted(
        {
            round(video.frame_rate, 3)
            for inspection in take_inspections.values()
            for video in inspection.videos
            if video.frame_rate > 0
        }
    )
    if len(session_frame_rates) > 1:
        issues.append(
            Issue(
                "fail",
                "mixed_session_frame_rates",
                f"session contains mixed actual frame rates: {session_frame_rates}",
            )
        )

    status = overall_status(issues)
    return SessionValidationResult(
        session_id=session.session_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        issues=issues,
        takes=take_summaries,
        take_inspections=take_inspections,
    )


def session_validation_to_dict(result: SessionValidationResult) -> dict[str, Any]:
    return {
        "session_id": result.session_id,
        "generated_at": result.generated_at,
        "status": result.status,
        "issues": issues_to_dict(result.issues),
        "takes": [asdict(take) for take in result.takes],
        "take_inspections": {
            take_id: inspection_to_dict(inspection) for take_id, inspection in result.take_inspections.items()
        },
    }


def write_session_validation_reports(
    session: SessionProjectConfig,
    result: SessionValidationResult,
) -> tuple[Path, Path]:
    payload = session_validation_to_dict(result)
    json_path = session.reports_path("session-validation.json")
    markdown_path = session.reports_path("session-validation.md")
    write_json_report(json_path, payload)
    write_markdown_report(
        markdown_path,
        title="Session Validation",
        status=result.status,
        issues=result.issues,
        sections={
            "Session": {
                "session_id": session.session_id,
                "session_title": session.session_title,
                "logic_project": session.logic.project_file or "unset",
            },
            "Takes": [f"{take.take_id}: {take.status}" for take in result.takes],
        },
    )
    return json_path, markdown_path
