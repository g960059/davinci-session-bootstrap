from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def overall_status(issues: list[Issue]) -> str:
    if any(issue.severity == "fail" for issue in issues):
        return "FAIL"
    if any(issue.severity == "warn" for issue in issues):
        return "WARN"
    return "PASS"


def issue_counts(issues: list[Issue]) -> dict[str, int]:
    counts = {"fail": 0, "warn": 0, "info": 0}
    for issue in issues:
        counts.setdefault(issue.severity, 0)
        counts[issue.severity] += 1
    return counts


def report_header(title: str, status: str, generated_at: str) -> list[str]:
    return [
        f"# {title}",
        "",
        f"- Status: {status}",
        f"- Generated at: {generated_at}",
        "",
    ]


def write_json_report(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def write_markdown_report(
    path: Path,
    *,
    title: str,
    status: str,
    issues: list[Issue],
    sections: dict[str, Any],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = report_header(title, status, generated_at)
    counts = issue_counts(issues)
    lines.extend(
        [
            "## Summary",
            "",
            f"- Failures: {counts.get('fail', 0)}",
            f"- Warnings: {counts.get('warn', 0)}",
            f"- Infos: {counts.get('info', 0)}",
            "",
        ]
    )
    if issues:
        lines.append("## Issues")
        lines.append("")
        for issue in issues:
            context = f" ({issue.context})" if issue.context else ""
            lines.append(f"- `{issue.severity.upper()}` `{issue.code}` {issue.message}{context}")
        lines.append("")
    for heading, value in sections.items():
        lines.append(f"## {heading}")
        lines.append("")
        if isinstance(value, list):
            for item in value:
                lines.append(f"- {item}")
        elif isinstance(value, dict):
            for key, item in value.items():
                lines.append(f"- {key}: {item}")
        else:
            lines.append(str(value))
        lines.append("")
    with path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return path


def issues_to_dict(issues: list[Issue]) -> list[dict[str, Any]]:
    return [asdict(issue) for issue in issues]

