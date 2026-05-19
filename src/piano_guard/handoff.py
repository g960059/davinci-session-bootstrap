from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from piano_guard.config import SessionProjectConfig, iter_session_takes
from piano_guard.reports import write_json_report


HANDOFF_MARKDOWN = "operator-handoff.md"
HANDOFF_JSON = "operator-handoff.json"


def _take_rows(session: SessionProjectConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for take_ref, take in iter_session_takes(session):
        rows.append(
            {
                "take_id": take_ref.id,
                "take_path": take_ref.take,
                "timeline_frame_rate": take.timeline.frame_rate,
                "master_audio": take.master_audio,
                "edit_audio": take.editing_audio_relative_path(),
                "angles": [
                    {
                        "label": camera.label,
                        "file": camera.file,
                    }
                    for camera in take.camera_files
                ],
            }
        )
    return rows


def build_operator_handoff(session: SessionProjectConfig) -> dict[str, Any]:
    takes = _take_rows(session)
    expected_color_management = {
        "color_science": "DaVinci YRGB Color Managed",
        "automatic_color_management": "off",
        "input_color_space": session.timeline.input_color_space,
        "timeline_color_space": session.timeline.timeline_color_space,
        "output_color_space": session.timeline.output_color_space,
        "input_drt": "DaVinci",
        "output_drt": "DaVinci",
        "timeline_working_luminance": "SDR 100",
        "output_tone_luminance_max": "100",
        "graphics_white_level": "200",
        "use_203_nits_reference_for_rec2100_hdr": "off for PP3/Rec.709 SDR",
        "use_inverse_drt_for_sdr_to_hdr": "off",
        "use_color_space_aware_grading_tools": "on",
    }
    return {
        "status": "PASS",
        "session_id": session.session_id,
        "session_title": session.session_title,
        "session_root": str(session.session_root),
        "resolve_project_name": session.resolve.project_name,
        "resolve_project_library": session.resolve.project_library_name,
        "timeline": asdict(session.timeline),
        "expected_color_management": expected_color_management,
        "take_count": len(takes),
        "takes": takes,
        "reports": {
            "prepare": str(session.reports_path("prepare-resolve-session.json")),
            "inspect": str(session.reports_path("inspect-resolve-session.json")),
            "handoff": str(session.reports_path(HANDOFF_MARKDOWN)),
        },
        "color_prep": {
            "timeline_name": session.resolve.color_prep_timeline_name,
            "gap_seconds": session.resolve.color_prep_gap_seconds,
            "layout": "compact",
        },
        "next_steps": [
            "Open the Resolve project and fix the timeline playback frame rate to 29.97 before editorial/export if Resolve still shows 24.",
            f"Open the {session.resolve.color_prep_timeline_name} timeline and color-match the source angle clips with Local Grades.",
            "Use compact-v tracks as packed rows; grade by each clip item angle label, not by track name.",
            "Use Gallery Stills / Apply Grade to copy a same-angle starting point between takes, then adjust each take independently.",
            "After color prep, duplicate the timeline before manual multicam conversion or downstream editing.",
            "Keep Remote Grades, Shared Nodes, and CDL commands out of the standard workflow unless explicitly experimenting.",
        ],
        "summary": f"operator handoff ready for {session.resolve.project_name}; {len(takes)} take(s)",
    }


def render_operator_handoff_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Operator Handoff",
        "",
        f"- Generated at: {datetime.now(timezone.utc).isoformat()}",
        f"- Session: {payload['session_title']} (`{payload['session_id']}`)",
        f"- Session root: `{payload['session_root']}`",
        f"- Resolve project: `{payload['resolve_project_name']}`",
        f"- Take count: {payload['take_count']}",
        "",
        "## Resolve Project Checks",
        "",
        "- Confirm timeline playback frame rate is `29.97` before editorial assembly or export.",
        "- Confirm project output is SDR Rec.709 for YouTube delivery.",
        f"- Open `{payload['color_prep']['timeline_name']}` for cross-take color prep.",
        "- If Resolve shows cache or stills location warnings, rerun `prepare-resolve-session` after Resolve restarts.",
        "",
        "Expected color management:",
        "",
    ]
    for key, value in payload["expected_color_management"].items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Takes", ""])
    for take in payload["takes"]:
        lines.extend(
            [
                f"### {take['take_id']}",
                "",
                f"- Take config: `{take['take_path']}`",
                f"- Master audio: `{take['master_audio']}`",
                f"- Edit/final audio: `{take['edit_audio']}`",
                "- Angles:",
            ]
        )
        for angle in take["angles"]:
            lines.append(f"  - `{angle['label']}`: `{angle['file']}`")
        lines.append("")
    lines.extend(
        [
            "## Manual Resolve Workflow",
            "",
            f"1. Open `{payload['color_prep']['timeline_name']}`.",
            "2. Confirm the project color management matches the expected Rec.709 / YouTube SDR settings above.",
            "3. Treat `compact-v1`, `compact-v2`, ... as packed rows; grade by each clip item's `angle-*` label.",
            "4. Go to the Color page and grade each source angle timeline item with Local Grades.",
            "5. Match angles within each take first: white keys, black piano finish, gold plate, skin when visible, and window highlights.",
            "6. Grab Gallery Stills for useful same-angle starting points, then Apply Grade to the next take and adjust independently.",
            "7. Do not use Remote Grades, Shared Nodes, or CDL commands for the standard workflow.",
            "8. After color prep, duplicate the timeline before manual multicam conversion or downstream editing.",
            "9. Use final timeline grades only for light take-to-take finishing after the edit is locked.",
            "",
            "## Repo Boundary",
            "",
            "- This repo prepares the session through grouping, Resolve bootstrap, color-prep timeline creation, validation, and handoff.",
            "- Manual color judgment, multicam conversion, editorial decisions, and final export remain Resolve operator work.",
            "- `render-stills`, `review-manifest`, and `contact-sheet` are optional review aids.",
            "- CDL commands are experimental/debug tools and are not part of the standard workflow.",
            "",
        ]
    )
    return "\n".join(lines)


def write_operator_handoff(session: SessionProjectConfig) -> dict[str, Any]:
    payload = build_operator_handoff(session)
    markdown_path = session.reports_path(HANDOFF_MARKDOWN)
    json_path = session.reports_path(HANDOFF_JSON)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text(render_operator_handoff_markdown(payload), encoding="utf-8")
    write_json_report(json_path, payload)
    payload["handoff_path"] = str(markdown_path)
    payload["json_path"] = str(json_path)
    return payload
