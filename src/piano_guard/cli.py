from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Callable

from piano_guard.audio_prep import prepare_session_audio, session_audio_prep_to_dict
from piano_guard.autogroup import (
    apply_auto_group,
    auto_group_plan_to_dict,
    plan_auto_group,
    write_auto_group_plan_reports,
    write_take_order_reports,
)
from piano_guard.config import (
    AUDIO_EXTENSIONS,
    TimelineConfig,
    VIDEO_EXTENSIONS,
    initialize_session,
    iter_session_takes,
    load_session,
    write_session,
    write_take,
)
from piano_guard.fftools import first_stream, probe_media
from piano_guard.handoff import write_operator_handoff
from piano_guard.reports import write_json_report
from piano_guard.resolve_ops import (
    ResolveError,
    apply_manual_cdl,
    bootstrap_session,
    ensure_project_library,
    ensure_resolve_storage_locations,
    ensure_resolve_running,
    inspect_resolve_session,
    verify_resolve_session_after_reload,
)
from piano_guard.review import (
    MANUAL_CDL_VERSION_NAME,
    append_manual_cdl_report,
    build_review_manifest,
    write_contact_sheet,
)
from piano_guard.session_project import inspect_session_project, session_validation_to_dict
from piano_guard.stills import DEFAULT_STILL_WIDTH, preview_cdl_on_still, render_session_stills


CommandHandler = Callable[[argparse.Namespace], int]
_STATUS_RANKS = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _print_stderr(message: str) -> None:
    print(message, file=sys.stderr)


def _emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False))
        return
    status = payload.get("status", "PASS")
    summary = payload.get("summary")
    print(f"{status}: {summary}" if summary else status)


def _fold_status(current: str, new: str | None) -> str:
    new_rank = _STATUS_RANKS.get(new or "PASS", 0)
    current_rank = _STATUS_RANKS.get(current, 0)
    if new_rank > current_rank:
        return new or current
    return current


def _markdown_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\n", " ")


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _header in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_markdown_value(value) for value in row) + " |")
    return lines


def _stage_rows(payload: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for name, stage in (payload.get("stages") or {}).items():
        if isinstance(stage, dict):
            rows.append([name, stage.get("status", ""), stage.get("summary", "")])
    return rows


def _issue_rows(payload: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for issue in payload.get("issues") or []:
        if isinstance(issue, dict):
            rows.append([issue.get("severity", ""), issue.get("code", ""), issue.get("message", "")])
    for stage_name, stage in (payload.get("stages") or {}).items():
        if not isinstance(stage, dict):
            continue
        for issue in stage.get("issues") or []:
            if isinstance(issue, dict):
                rows.append([
                    issue.get("severity", ""),
                    f"{stage_name}:{issue.get('code', '')}",
                    issue.get("message", ""),
                ])
    return rows


def _format_color_prep_take_rows(color_prep: dict[str, Any]) -> list[list[Any]]:
    rows = []
    for take in color_prep.get("takes") or []:
        angle_parts = []
        for angle in take.get("angles") or []:
            if not isinstance(angle, dict):
                continue
            label = angle.get("angle", "")
            track = angle.get("track_index", "")
            start = angle.get("record_frame", angle.get("start_frame", ""))
            confidence = angle.get("sync_confidence")
            suffix = f", conf {confidence}" if confidence is not None else ""
            angle_parts.append(f"{label}@V{track} start {start}{suffix}")
        rows.append(
            [
                take.get("take_id", ""),
                take.get("marker_frame", ""),
                (take.get("audio") or {}).get("record_frame", take.get("audio_start", "")),
                ", ".join(angle_parts),
            ]
        )
    return rows


def _write_prepare_markdown(session: Any, payload: dict[str, Any]) -> Path:
    path = session.reports_path("prepare-resolve-session.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap = (payload.get("stages") or {}).get("bootstrap") or {}
    color_prep = bootstrap.get("color_prep") or {}
    post_reload = (payload.get("stages") or {}).get("post_reload_verification") or {}
    post_reload_color = post_reload.get("color_prep_timeline") or {}
    lines = [
        "# Prepare Resolve Session",
        "",
        f"- Status: {payload.get('status', '')}",
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}",
        f"- Session: `{session.session_root}`",
        f"- Resolve project: `{session.resolve.project_name}`",
        "",
        "## Operator Check",
        "",
        f"- Open `{session.resolve.color_prep_timeline_name}`.",
        "- Confirm timeline start timecode is `00:00:00;00`.",
        "- Use `compact-v1`, `compact-v2`, ... as packed rows.",
        "- Grade by each clip item's `angle-*` label, not by track name.",
        "- Source video clips retain embedded scratch audio after Resolve waveform sync.",
        "- Camera scratch audio is not placed in color prep; A1 is `master-audio` only.",
        "",
        "## Stages",
        "",
        *_markdown_table(["Stage", "Status", "Summary"], _stage_rows(payload) or [["(none)", "", ""]]),
        "",
        "## Color Prep Created",
        "",
        *_markdown_table(
            ["Field", "Value"],
            [
                ["timeline", color_prep.get("timeline_name", "")],
                ["layout", color_prep.get("layout", "")],
                ["start_timecode", color_prep.get("start_timecode", "")],
                ["angles", ", ".join(color_prep.get("angles") or [])],
                ["video_tracks", color_prep.get("max_video_tracks", "")],
                ["take_count", color_prep.get("take_count", "")],
            ],
        ),
        "",
        "## Post-Reload Verification",
        "",
        *_markdown_table(
            ["Field", "Value"],
            [
                ["status", post_reload.get("status", "")],
                ["closed_project", post_reload.get("closed_project", "")],
                ["reloaded_project", post_reload.get("reloaded_project", "")],
                ["start_timecode", post_reload_color.get("start_timecode", "")],
                ["video_track_count", post_reload_color.get("video_track_count", "")],
                ["audio_track_count", post_reload_color.get("audio_track_count", "")],
                ["markers", len(post_reload_color.get("markers") or {})],
            ],
        ),
        "",
        "## Take Placement",
        "",
        *_markdown_table(
            ["Take", "Marker Frame", "Audio Start", "Angles"],
            _format_color_prep_take_rows(color_prep) or [["(none)", "", "", ""]],
        ),
    ]
    issue_rows = _issue_rows(payload)
    if issue_rows:
        lines.extend(["", "## Issues", "", *_markdown_table(["Severity", "Code", "Message"], issue_rows)])
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_inspect_markdown(session: Any, payload: dict[str, Any]) -> Path:
    path = session.reports_path("inspect-resolve-session.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    color_prep = payload.get("color_prep_timeline") or {}
    lines = [
        "# Inspect Resolve Session",
        "",
        f"- Status: {payload.get('status', '')}",
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}",
        f"- Resolve project: `{payload.get('project_name', '')}`",
        f"- Summary: {payload.get('summary', '')}",
        "",
        "## Project Settings",
        "",
        *_markdown_table(["Setting", "Observed"], [[key, value] for key, value in (payload.get("settings") or {}).items()]),
        "",
        "## Color Prep Timeline",
        "",
        *_markdown_table(
            ["Field", "Value"],
            [
                ["exists", color_prep.get("exists", "")],
                ["timeline", color_prep.get("timeline_name", "")],
                ["layout", color_prep.get("layout", "")],
                ["start_timecode", color_prep.get("start_timecode", "")],
                ["start_frame", color_prep.get("start_frame", "")],
                ["angles", ", ".join(color_prep.get("expected_angles") or [])],
                ["video_tracks", ", ".join(color_prep.get("video_track_names") or [])],
                ["audio_tracks", ", ".join(color_prep.get("audio_track_names") or [])],
                ["markers", len(color_prep.get("markers") or {})],
                ["take_count", color_prep.get("take_count", "")],
            ],
        ),
        "",
        "## Take Verification",
        "",
        *_markdown_table(
            ["Take", "Marker Frame", "Audio Start", "Angles"],
            _format_color_prep_take_rows(color_prep) or [["(none)", "", "", ""]],
        ),
        "",
        "## Take Bins",
        "",
        *_markdown_table(
            ["Take", "Bin", "Missing", "Extra"],
            [
                [
                    take.get("take_id", ""),
                    "yes" if take.get("bin_exists") else "no",
                    ", ".join(take.get("missing_clips") or []),
                    ", ".join(take.get("extra_clips") or []),
                ]
                for take in payload.get("take_bins") or []
            ]
            or [["(none)", "", "", ""]],
        ),
    ]
    issue_rows = _issue_rows(payload)
    if issue_rows:
        lines.extend(["", "## Issues", "", *_markdown_table(["Severity", "Code", "Message"], issue_rows)])
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _triple(raw: str, field: str) -> tuple[float, float, float]:
    parts = [part.strip() for part in raw.split(",")]
    if len(parts) != 3:
        raise ValueError(f"--{field} must be 3 comma-separated numbers, got {raw!r}")
    return (float(parts[0]), float(parts[1]), float(parts[2]))


def _find_incoming_dir(session_root: Path, incoming_dir: str | None) -> Path | None:
    if incoming_dir:
        path = (session_root / incoming_dir).resolve()
        return path if path.is_dir() else None
    path = (session_root / "incoming").resolve()
    return path if path.is_dir() else None


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
    write_session(session)
    return load_session(session.session_path)


def _adapt_session_color_from_sources(session: Any) -> Any:
    signatures: set[tuple[str, str, str]] = set()
    for _take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            video_stream = first_stream(probe_media(take.resolve_path(camera.file)).get("streams", []), "video")
            if video_stream is None:
                continue
            signatures.add(
                (
                    str(video_stream.get("color_space") or ""),
                    str(video_stream.get("color_transfer") or ""),
                    str(video_stream.get("color_primaries") or ""),
                )
            )

    if signatures != {("bt709", "bt709", "bt709")}:
        return session

    session.timeline.input_color_space = "Rec.709 Gamma 2.4"
    session.timeline.timeline_color_space = "Rec.709 Gamma 2.4"
    session.timeline.output_color_space = "Rec.709 Gamma 2.4"
    write_session(session)

    for _take_ref, take in iter_session_takes(session):
        take.timeline = TimelineConfig(**asdict(session.timeline))
        take.validation.expected_color_space = "bt709"
        take.validation.expected_color_transfer = "bt709"
        take.validation.expected_color_primaries = "bt709"
        write_take(take)

    return load_session(session.session_path)


def _validation_payload(validation: Any) -> dict[str, Any]:
    payload = session_validation_to_dict(validation)
    payload["summary"] = (
        f"{len(validation.takes)} takes validated"
        if validation.status == "PASS"
        else f"validation produced {len(validation.issues)} issues"
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
        take_order_json_path, take_order_markdown_path, take_order_report = write_take_order_reports(session)
        payload = {
            "status": "PASS",
            "summary": f"session already grouped; {len(session.takes)} takes available",
            "session_config": str(session.session_path),
            "take_count": len(session.takes),
            "take_order_status": take_order_report.status,
            "take_order_path": str(take_order_markdown_path),
            "take_order_json_path": str(take_order_json_path),
        }
        _emit(payload, as_json=args.json)
        return 0

    plan = plan_auto_group(session_root, incoming_dir=args.incoming_dir)
    plan_json_path, plan_markdown_path = write_auto_group_plan_reports(session_root, plan)
    if args.dry_run:
        payload = auto_group_plan_to_dict(plan)
        payload["summary"] = f"planned {len(plan.takes)} takes from {plan.incoming_dir}"
        payload["auto_group_plan_path"] = str(plan_markdown_path)
        payload["auto_group_plan_json_path"] = str(plan_json_path)
        _emit(payload, as_json=args.json)
        return 0 if plan.status == "PASS" else 1

    if plan.status != "PASS":
        payload = auto_group_plan_to_dict(plan)
        payload["summary"] = "grouping plan is not PASS; refusing to apply"
        payload["auto_group_plan_path"] = str(plan_markdown_path)
        payload["auto_group_plan_json_path"] = str(plan_json_path)
        _emit(payload, as_json=args.json)
        return 1

    apply_result = apply_auto_group(session_root, plan=plan, write_reports=True)
    session = _prepare_session_config(
        session_root,
        project_name=args.project_name,
        project_library_name=args.project_library_name,
        project_library_path=args.project_library_path,
    )
    take_order_json_path, take_order_markdown_path, take_order_report = write_take_order_reports(session)
    payload = {
        "status": apply_result.status,
        "summary": f"applied {len(apply_result.takes_created)} takes",
        "session_config": str(session.session_path),
        "takes_created": apply_result.takes_created,
        "excluded_created": apply_result.excluded_created,
        "auto_group_plan_path": str(plan_markdown_path),
        "auto_group_apply_path": str(session_root / "reports" / "auto-group-apply.md"),
        "take_order_status": take_order_report.status,
        "take_order_path": str(take_order_markdown_path),
        "take_order_json_path": str(take_order_json_path),
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
    )
    session = _adapt_session_color_from_sources(session)
    validation = inspect_session_project(session, write_take_reports=False)
    if validation.status == "FAIL":
        payload = _validation_payload(validation)
        write_json_report(session.reports_path("prepare-resolve-session.json"), payload)
        _write_prepare_markdown(session, payload)
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
            "audio proxies ready"
            if audio_result.status == "PASS"
            else f"audio prep emitted {len(audio_result.issues)} issue(s)"
        )
    final_payload["stages"]["audio_prep"] = audio_payload
    final_payload["status"] = _fold_status(final_payload["status"], audio_payload.get("status"))

    if not args.dry_run and final_payload["status"] != "FAIL":
        connection = None
        try:
            storage_payload = ensure_resolve_storage_locations(session_root.parent)
            final_payload["stages"]["resolve_storage"] = storage_payload
            final_payload["status"] = _fold_status(final_payload["status"], storage_payload.get("status"))
            library_payload = ensure_project_library(
                library_name=session.resolve.project_library_name or _default_project_library(session_root)[0],
                library_path=session.resolve.project_library_path or _default_project_library(session_root)[1],
            )
            final_payload["stages"]["project_library"] = library_payload
            final_payload["status"] = _fold_status(final_payload["status"], library_payload.get("status"))
            restart_required = library_payload["status"] == "WARN" or storage_payload.get("restart_required", False)
            connection = ensure_resolve_running(restart=restart_required)
            connector = lambda: connection
            bootstrap_payload = bootstrap_session(
                session,
                connector=connector,
                write_reports=False,
                fresh=args.fresh,
                rebuild_color_prep=args.rebuild_color_prep,
            )
            final_payload["stages"]["bootstrap"] = bootstrap_payload
            final_payload["status"] = _fold_status(final_payload["status"], bootstrap_payload.get("status"))
            if bootstrap_payload.get("status") != "FAIL":
                post_reload_payload = verify_resolve_session_after_reload(session, connector=connector)
                final_payload["stages"]["post_reload_verification"] = post_reload_payload
                final_payload["status"] = _fold_status(final_payload["status"], post_reload_payload.get("status"))
        except (ResolveError, RuntimeError, subprocess.CalledProcessError) as exc:
            failure_payload: dict[str, Any] = {
                "status": "FAIL",
                "summary": f"Resolve bootstrap failed: {exc}",
                "issues": [
                    {
                        "severity": "fail",
                        "code": "resolve_bootstrap_failed",
                        "message": str(exc),
                        "context": {"stage": "bootstrap"},
                    }
                ],
            }
            if connection is not None:
                try:
                    connector = lambda: connection
                    failure_payload["partial_inspection"] = inspect_resolve_session(
                        session,
                        connector=connector,
                        write_reports=True,
                    )
                except Exception as inspect_exc:
                    failure_payload["partial_inspection_error"] = str(inspect_exc)
            final_payload["stages"]["bootstrap"] = failure_payload
            final_payload["status"] = "FAIL"

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
        final_payload["summary"] = f"validated {session.resolve.project_name}; no Resolve or audio files were modified"
    elif final_payload["status"] == "FAIL":
        bootstrap_summary = (final_payload["stages"].get("bootstrap") or {}).get("summary")
        final_payload["summary"] = bootstrap_summary or f"Resolve bootstrap failed for {session.resolve.project_name}"
    else:
        handoff_payload = write_operator_handoff(session)
        final_payload["stages"]["operator_handoff"] = handoff_payload
        final_payload["operator_handoff_path"] = handoff_payload["handoff_path"]
        final_payload["summary"] = (
            f"Resolve prepared for {session.resolve.project_name}; next grade "
            f"{session.resolve.color_prep_timeline_name} with Local Grades"
            + warning_suffix
        )
    write_json_report(session.reports_path("prepare-resolve-session.json"), final_payload)
    _write_prepare_markdown(session, final_payload)
    _emit(final_payload, as_json=args.json)
    return 1 if final_payload["status"] == "FAIL" else 0


def command_inspect_resolve_session(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root
    connection = ensure_resolve_running()
    connector = lambda: connection
    payload = inspect_resolve_session(session, connector=connector, write_reports=True)
    _write_inspect_markdown(session, payload)
    _emit(payload, as_json=args.json)
    return 1 if payload["status"] == "FAIL" else 0


def command_operator_handoff(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root
    payload = write_operator_handoff(session)
    _emit(payload, as_json=args.json)
    return 0


def command_render_stills(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root

    results, issues = render_session_stills(session, width=args.width)
    payload: dict[str, Any] = {
        "session_id": session.session_id,
        "reference_angle": session.reference_angle,
        "review_mode": "all_clips",
        "stills": [
            {
                **asdict(result),
                "is_reference": False,
            }
            for result in results
        ],
        "issues": [asdict(issue) for issue in issues],
        "status": "FAIL"
        if any(issue.severity == "fail" for issue in issues)
        else ("WARN" if issues else "PASS"),
        "summary": (
            f"rendered {len(results)} still(s) to "
            f"{session.resolve_path(f'{session.reports_dir}/stills')}"
            + (f"; {len(issues)} issue(s)" if issues else "")
        ),
        "default_width": DEFAULT_STILL_WIDTH,
        "color_pipeline": "sony_pp10_hlg_rec2100_to_sdr_rec709_gamma24",
    }
    write_json_report(session.reports_path("render-stills.json"), payload)
    _emit(payload, as_json=args.json)
    return 1 if payload["status"] == "FAIL" else 0


def command_review_manifest(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root
    payload = build_review_manifest(session, write_report=True)
    _emit(payload, as_json=args.json)
    return 1 if payload["status"] == "FAIL" else 0


def command_contact_sheet(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root
    out_path = Path(args.out).resolve() if args.out else session.reports_path("color-review-sheet.png")
    payload = write_contact_sheet(session, out_path)
    _emit(payload, as_json=args.json)
    return 1 if payload["status"] == "FAIL" else 0


def command_preview_cdl(args: argparse.Namespace) -> int:
    source_png = Path(args.source_png).resolve()
    if not source_png.exists():
        _print_stderr(f"error: source PNG not found: {source_png}")
        return 1
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


def command_manual_cdl(args: argparse.Namespace) -> int:
    session_root = Path(args.session_root).resolve()
    session = load_session(session_root / "session.yaml")
    session.session_root = session_root
    try:
        slope = _triple(args.slope, "slope")
        offset = _triple(args.offset, "offset")
    except ValueError as exc:
        _print_stderr(f"error: {exc}")
        return 1

    if args.dry_run:
        payload = {
            "status": "PASS",
            "dry_run": True,
            "clip_id": args.clip_id,
            "slope": list(slope),
            "offset": list(offset),
            "version_name": MANUAL_CDL_VERSION_NAME,
            "summary": "dry-run: would apply manual CDL; rerun without --dry-run to commit",
        }
        _emit(payload, as_json=args.json)
        return 0

    connection = ensure_resolve_running()
    connector = lambda: connection
    payload = apply_manual_cdl(
        session,
        clip_id=args.clip_id,
        slope=slope,
        offset=offset,
        connector=connector,
    )
    if payload.get("status") in {"PASS", "WARN"} and payload.get("applied"):
        report_path = append_manual_cdl_report(
            session,
            clip_id=args.clip_id,
            slope=slope,
            offset=offset,
            apply_payload=payload,
        )
        payload["report_path"] = str(report_path)
    _emit(payload, as_json=args.json)
    return 1 if payload.get("status") == "FAIL" else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="piano-guard")
    standard_commands = (
        "group-session,prepare-resolve-session,inspect-resolve-session,"
        "operator-handoff,render-stills,review-manifest,contact-sheet"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar=f"{{{standard_commands}}}",
    )

    group_parser = subparsers.add_parser("group-session", help="group incoming media into takes")
    group_parser.add_argument("session_root")
    group_parser.add_argument("--incoming-dir")
    group_parser.add_argument("--dry-run", action="store_true")
    group_parser.add_argument("--project-name")
    group_parser.add_argument("--project-library-name")
    group_parser.add_argument("--project-library-path")
    group_parser.add_argument("--json", action="store_true")
    group_parser.set_defaults(func=command_group_session)

    prepare_parser = subparsers.add_parser(
        "prepare-resolve-session",
        help="prepare Resolve project, sync media, and build the color prep timeline",
    )
    prepare_parser.add_argument("session_root")
    prepare_parser.add_argument("--project-name")
    prepare_parser.add_argument("--project-library-name")
    prepare_parser.add_argument("--project-library-path")
    prepare_parser.add_argument("--dry-run", action="store_true")
    prepare_parser.add_argument("--skip-edit-audio", action="store_true")
    prepare_parser.add_argument("--fresh", action="store_true")
    prepare_parser.add_argument(
        "--rebuild-color-prep",
        action="store_true",
        help="delete and recreate the color prep timeline; preserves it by default",
    )
    prepare_parser.add_argument("--json", action="store_true")
    prepare_parser.set_defaults(func=command_prepare_resolve_session)

    inspect_resolve_parser = subparsers.add_parser(
        "inspect-resolve-session",
        help="inspect Resolve project, bins, take clips, color prep timeline, and snapshot",
    )
    inspect_resolve_parser.add_argument("session_root")
    inspect_resolve_parser.add_argument("--json", action="store_true")
    inspect_resolve_parser.set_defaults(func=command_inspect_resolve_session)

    handoff_parser = subparsers.add_parser(
        "operator-handoff",
        help="write the manual Resolve operator handoff after Stage B",
    )
    handoff_parser.add_argument("session_root")
    handoff_parser.add_argument("--json", action="store_true")
    handoff_parser.set_defaults(func=command_operator_handoff)

    render_stills_parser = subparsers.add_parser(
        "render-stills",
        help="render one display-ready PNG per take/angle for optional review",
    )
    render_stills_parser.add_argument("session_root")
    render_stills_parser.add_argument("--width", type=int, default=DEFAULT_STILL_WIDTH)
    render_stills_parser.add_argument("--json", action="store_true")
    render_stills_parser.set_defaults(func=command_render_stills)

    manifest_parser = subparsers.add_parser(
        "review-manifest",
        help="write the optional review manifest from rendered stills",
    )
    manifest_parser.add_argument("session_root")
    manifest_parser.add_argument("--json", action="store_true")
    manifest_parser.set_defaults(func=command_review_manifest)

    sheet_parser = subparsers.add_parser(
        "contact-sheet",
        help="write a contact sheet for AI or human color review",
    )
    sheet_parser.add_argument("session_root")
    sheet_parser.add_argument("--out", help="output PNG path; defaults to reports/color-review-sheet.png")
    sheet_parser.add_argument("--json", action="store_true")
    sheet_parser.set_defaults(func=command_contact_sheet)

    preview_parser = subparsers.add_parser(
        "preview-cdl",
        help=argparse.SUPPRESS,
    )
    preview_parser.add_argument("--source-png", required=True)
    preview_parser.add_argument("--slope", required=True)
    preview_parser.add_argument("--offset", required=True)
    preview_parser.add_argument("--out", required=True)
    preview_parser.add_argument("--json", action="store_true")
    preview_parser.set_defaults(func=command_preview_cdl)

    manual_parser = subparsers.add_parser(
        "manual-cdl",
        help=argparse.SUPPRESS,
    )
    manual_parser.add_argument("session_root")
    manual_parser.add_argument("--clip-id", required=True, help='clip identifier, e.g. "take-01/angle-b"')
    manual_parser.add_argument("--slope", required=True, help="comma-separated RGB slope values")
    manual_parser.add_argument("--offset", required=True, help="comma-separated RGB offset values")
    manual_parser.add_argument("--dry-run", action="store_true")
    manual_parser.add_argument("--json", action="store_true")
    manual_parser.set_defaults(func=command_manual_cdl)

    # Keep the legacy CDL commands callable for explicit experiments while
    # keeping the default help focused on the production bootstrap workflow.
    subparsers._choices_actions = [  # type: ignore[attr-defined]
        action
        for action in subparsers._choices_actions  # type: ignore[attr-defined]
        if action.dest not in {"preview-cdl", "manual-cdl"}
    ]

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
