from __future__ import annotations

from dataclasses import dataclass
import hashlib
import shutil
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

import numpy as np
from scipy.signal import correlate, correlation_lags

from piano_guard.config import SessionProjectConfig, TakeConfig, TimelineConfig, iter_session_takes
from piano_guard.fftools import run_checked_bytes
from piano_guard.review import MANUAL_CDL_VERSION_NAME
from piano_guard.reports import Issue, issues_to_dict, write_json_report, write_markdown_report


DEFAULT_RESOLVE_SCRIPT_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
DEFAULT_RESOLVE_SCRIPT_LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
DEFAULT_RESOLVE_DBLIST_CONF = Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve/dblist.conf"
DEFAULT_RESOLVE_CONFIG_DAT = Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve/config.dat"
RESOLVE_CACHE_DIR_NAME = "CacheClip"
RESOLVE_GALLERY_DIR_NAME = ".gallery"
RESOLVE_PROJECT_BACKUPS_DIR_NAME = "Resolve Project Backups"


class ResolveError(RuntimeError):
    pass


@dataclass
class ResolveConnection:
    resolve: Any
    project_manager: Any


DEFAULT_DISK_LIBRARY_TEMPLATE = (
    Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Resolve Project Library"
)


def _format_disk_library_entry(library_name: str, library_path: Path) -> str:
    return f"{library_name}:{library_path}:*:::DISK"


def _resolve_storage_dirs(storage_root: Path) -> dict[str, Path]:
    return {
        "cache": storage_root / RESOLVE_CACHE_DIR_NAME,
        "gallery": storage_root / RESOLVE_GALLERY_DIR_NAME,
        "project_backups": storage_root / RESOLVE_PROJECT_BACKUPS_DIR_NAME,
    }


def _read_resolve_config(config_dat_path: Path) -> tuple[list[str], dict[str, str]]:
    lines = config_dat_path.read_text(encoding="utf-8").splitlines() if config_dat_path.exists() else []
    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    return lines, values


def _write_resolve_config_value(lines: list[str], key: str, value: str) -> tuple[list[str], bool]:
    desired = f"{key} = {value}"
    updated = False
    found = False
    next_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("//") and "=" in stripped:
            existing_key = stripped.split("=", 1)[0].strip()
            if existing_key == key:
                found = True
                if line != desired:
                    updated = True
                next_lines.append(desired)
                continue
        next_lines.append(line)
    if not found:
        if next_lines and next_lines[-1] != "":
            next_lines.append("")
        next_lines.append(desired)
        updated = True
    return next_lines, updated


def ensure_resolve_storage_locations(
    storage_root: str | Path,
    *,
    config_dat_path: str | Path = DEFAULT_RESOLVE_CONFIG_DAT,
) -> dict[str, Any]:
    """Keep Resolve cache/stills paths on an online disk used by this session.

    Resolve derives cache and gallery stills locations from `config.dat`:
    `Site.1.FS.1.Root` + `RenderCaching.CacheDir` / `System.Gallery.Folder`.
    If the old root points to an offline external disk, Resolve opens modal
    warnings before the AI operator can continue. This helper preflights and
    repairs those global paths before scripting starts.
    """
    target_root = Path(storage_root).expanduser().resolve()
    config_path = Path(config_dat_path).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    dirs = _resolve_storage_dirs(target_root)
    created_dirs: list[str] = []
    for path in dirs.values():
        existed = path.exists()
        path.mkdir(parents=True, exist_ok=True)
        if not existed:
            created_dirs.append(str(path))

    lines, values = _read_resolve_config(config_path)
    original_root = values.get("Site.1.FS.1.Root")
    original_cache_dir = values.get("RenderCaching.CacheDir")
    original_gallery_dir = values.get("System.Gallery.Folder")
    original_cache_path = (
        Path(original_root) / (original_cache_dir or RESOLVE_CACHE_DIR_NAME)
        if original_root
        else None
    )
    original_gallery_path = (
        Path(original_root) / (original_gallery_dir or RESOLVE_GALLERY_DIR_NAME)
        if original_root
        else None
    )

    desired_values = {
        "Site.1.FS.1.Root": str(target_root),
        "RenderCaching.FsNo": "1",
        "RenderCaching.CacheDir": RESOLVE_CACHE_DIR_NAME,
        "System.Gallery.DtMgr.FileSys": "1",
        "System.Gallery.Folder": RESOLVE_GALLERY_DIR_NAME,
    }
    updated = False
    next_lines = lines
    for key, value in desired_values.items():
        next_lines, changed = _write_resolve_config_value(next_lines, key, value)
        updated = updated or changed

    backup_path: Path | None = None
    if updated:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            backup_path = config_path.with_name(f"{config_path.name}.codex-backup-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(config_path, backup_path)
        content = "\n".join(next_lines)
        if content:
            content += "\n"
        config_path.write_text(content, encoding="utf-8")

    running = _is_resolve_running()
    issues: list[dict[str, Any]] = []
    if original_root and not Path(original_root).exists():
        issues.append(
            {
                "severity": "warn",
                "code": "resolve_storage_root_offline",
                "message": f"Resolve storage root was offline or inaccessible: {original_root}",
                "context": {"old_root": original_root, "new_root": str(target_root)},
            }
        )
    status = "WARN" if updated and running else "PASS"
    return {
        "status": status,
        "config_dat_path": str(config_path),
        "config_updated": updated,
        "backup_path": str(backup_path) if backup_path is not None else None,
        "restart_required": bool(updated and running),
        "storage_root": str(target_root),
        "previous_storage_root": original_root,
        "previous_cache_path": str(original_cache_path) if original_cache_path is not None else None,
        "previous_gallery_path": str(original_gallery_path) if original_gallery_path is not None else None,
        "cache_path": str(dirs["cache"]),
        "gallery_path": str(dirs["gallery"]),
        "project_backups_path": str(dirs["project_backups"]),
        "created_dirs": created_dirs,
        "issues": issues,
        "message": (
            f"updated Resolve storage root to {target_root}; restart Resolve to reload it"
            if updated and running
            else f"Resolve storage paths are available under {target_root}"
        ),
    }


def _parse_dblist_line(line: str) -> dict[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split(":")
    if len(parts) < 3:
        return None
    return {
        "name": parts[0],
        "path": parts[1],
        "db_type": parts[-1],
        "raw": stripped,
    }


def _register_disk_library_in_dblist(
    *,
    library_name: str,
    library_path: Path,
    dblist_conf_path: Path,
) -> dict[str, Any]:
    desired_line = _format_disk_library_entry(library_name, library_path)
    existing_lines = dblist_conf_path.read_text(encoding="utf-8").splitlines() if dblist_conf_path.exists() else []

    kept_lines: list[str] = []
    removed_lines: list[str] = []
    exact_match = False
    normalized_target = _normalize_media_path(library_path)

    for line in existing_lines:
        parsed = _parse_dblist_line(line)
        if parsed and parsed["db_type"] == "DISK":
            same_name = parsed["name"] == library_name
            same_path = _normalize_media_path(parsed["path"]) == normalized_target
            if same_name or same_path:
                if parsed["raw"] == desired_line and not exact_match:
                    exact_match = True
                    kept_lines.append(desired_line)
                else:
                    removed_lines.append(parsed["raw"])
                continue
        kept_lines.append(line)

    if not exact_match:
        kept_lines.append(desired_line)

    updated = kept_lines != existing_lines
    if updated:
        dblist_conf_path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(kept_lines)
        if content:
            content += "\n"
        dblist_conf_path.write_text(content, encoding="utf-8")

    return {
        "dblist_conf_path": str(dblist_conf_path),
        "entry": desired_line,
        "updated": updated,
        "removed_entries": removed_lines,
    }


def connect_to_resolve() -> ResolveConnection:
    os.environ.setdefault("RESOLVE_SCRIPT_API", DEFAULT_RESOLVE_SCRIPT_API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", DEFAULT_RESOLVE_SCRIPT_LIB)
    module_path = Path(os.environ["RESOLVE_SCRIPT_API"]) / "Modules"
    if str(module_path) not in sys.path:
        sys.path.append(str(module_path))

    try:
        import DaVinciResolveScript as dvr  # type: ignore
    except ImportError as exc:
        raise ResolveError("DaVinci Resolve scripting module is not available") from exc

    resolve = dvr.scriptapp("Resolve")
    if not resolve:
        raise ResolveError("DaVinci Resolve is not running or scripting is disabled")
    return ResolveConnection(resolve=resolve, project_manager=resolve.GetProjectManager())


def _is_resolve_running() -> bool:
    result = subprocess.run(["pgrep", "-x", "Resolve"], capture_output=True, text=True, check=False)
    return result.returncode == 0


def _launch_resolve() -> None:
    subprocess.run(["open", "-a", "DaVinci Resolve"], check=True)


def _quit_resolve() -> None:
    subprocess.run(
        ["osascript", "-e", 'tell application "DaVinci Resolve" to quit'],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _force_quit_resolve() -> None:
    subprocess.run(["pkill", "-9", "Resolve"], check=False, capture_output=True, text=True)


def _wait_for_resolve_state(*, running: bool, timeout_seconds: float = 60.0) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if _is_resolve_running() == running:
            return
        time.sleep(1.0)
    state = "start" if running else "quit"
    raise ResolveError(f"DaVinci Resolve did not {state} within {timeout_seconds:.0f}s")


def ensure_resolve_running(*, restart: bool = False, timeout_seconds: float = 180.0) -> ResolveConnection:
    if restart and _is_resolve_running():
        try:
            connection = connect_to_resolve()
            if connection.project_manager.GetCurrentProject() is not None:
                connection.project_manager.SaveProject()
        except ResolveError:
            pass
        _quit_resolve()
        try:
            _wait_for_resolve_state(running=False, timeout_seconds=20.0)
        except ResolveError:
            _force_quit_resolve()
            _wait_for_resolve_state(running=False, timeout_seconds=20.0)

    if not _is_resolve_running():
        _launch_resolve()

    deadline = time.time() + timeout_seconds
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return connect_to_resolve()
        except ResolveError as exc:
            last_error = exc
            time.sleep(2.0)
    raise ResolveError(f"DaVinci Resolve did not become scriptable within {timeout_seconds:.0f}s: {last_error}")


def _load_or_create_project(connection: ResolveConnection, *, project_name: str, media_location: Path) -> tuple[Any, bool]:
    project = connection.project_manager.LoadProject(project_name)
    if project:
        return project, False
    project = connection.project_manager.CreateProject(project_name, str(media_location))
    if not project:
        raise ResolveError(f"unable to create project {project_name}")
    return project, True


def _project_exists(project_manager: Any, project_name: str) -> bool:
    return project_name in (project_manager.GetProjectListInCurrentFolder() or [])


def _delete_project_if_exists(connection: ResolveConnection, *, project_name: str) -> bool:
    if not _project_exists(connection.project_manager, project_name):
        return False
    current_project = connection.project_manager.GetCurrentProject()
    if current_project and current_project.GetName() == project_name:
        # Save before closing to prevent a GUI "Save changes?" dialog that
        # blocks the scripting thread indefinitely.  Without this call,
        # CloseProject on a project with unsaved changes triggers a modal
        # dialog in the Resolve UI and the script hangs until the operator
        # manually dismisses it.
        connection.project_manager.SaveProject()
        if not connection.project_manager.CloseProject(current_project):
            raise ResolveError(f"unable to close existing Resolve project {project_name}")
    if not connection.project_manager.DeleteProject(project_name):
        raise ResolveError(f"unable to delete existing Resolve project {project_name}")
    return True


def _select_project_library(connection: ResolveConnection, session: SessionProjectConfig) -> dict[str, Any]:
    requested_name = session.resolve.project_library_name
    requested_type = session.resolve.project_library_type
    if not requested_name:
        return connection.project_manager.GetCurrentDatabase() or {}

    databases = connection.project_manager.GetDatabaseList() or []
    for entry in databases:
        if entry.get("DbType") == requested_type and entry.get("DbName") == requested_name:
            current = connection.project_manager.GetCurrentDatabase() or {}
            if current != entry and not connection.project_manager.SetCurrentDatabase(entry):
                raise ResolveError(f"unable to switch Resolve to project library {requested_name}")
            return entry

    dblist_entry = _register_disk_library_in_dblist(
        library_name=requested_name,
        library_path=Path(session.resolve.project_library_path or session.session_root / "Resolve Project Library"),
        dblist_conf_path=DEFAULT_RESOLVE_DBLIST_CONF,
    )
    raise ResolveError(
        f"Resolve project library is not registered: {requested_name}. "
        f"Registered entries are stored in {dblist_entry['dblist_conf_path']}. "
        "If Resolve is already running, restart it once and rerun."
    )


def _export_project_snapshot(project_manager: Any, session: SessionProjectConfig) -> Path:
    snapshot_path = session.resolve_snapshot_path()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists():
        snapshot_path.unlink()
    result = project_manager.ExportProject(session.resolve.project_name, str(snapshot_path), True)
    if result is False or not snapshot_path.exists():
        raise ResolveError(f"unable to export Resolve project snapshot to {snapshot_path}")
    return snapshot_path


def _ensure_folder(media_pool: Any, parent: Any, folder_name: str) -> Any:
    for folder in parent.GetSubFolderList():
        if folder.GetName() == folder_name:
            return folder
    folder = media_pool.AddSubFolder(parent, folder_name)
    if not folder:
        raise ResolveError(f"unable to create Resolve media pool folder: {folder_name}")
    return folder


def _folder_by_name(parent: Any, name: str) -> Any:
    for folder in parent.GetSubFolderList():
        if folder.GetName() == name:
            return folder
    raise ResolveError(f"Resolve folder not found: {name}")


def _clip_file_path(clip: Any) -> str:
    properties = clip.GetClipProperty() or {}
    return properties.get("File Path") or properties.get("FilePath") or ""


def _normalize_media_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def _iter_clips_recursive(folder: Any) -> list[Any]:
    clips = list(folder.GetClipList() or [])
    for child in folder.GetSubFolderList() or []:
        clips.extend(_iter_clips_recursive(child))
    return clips


def _ensure_imported(media_pool: Any, folder: Any, paths: list[Path]) -> tuple[list[str], list[str]]:
    requested_paths = [_normalize_media_path(path) for path in paths]
    existing_paths = {
        _normalize_media_path(_clip_file_path(clip))
        for clip in _iter_clips_recursive(folder)
        if _clip_file_path(clip)
    }
    pending = [path for path in requested_paths if path not in existing_paths]
    skipped = [path for path in requested_paths if path in existing_paths]
    if pending:
        media_pool.SetCurrentFolder(folder)
        imported = media_pool.ImportMedia(pending)
        if imported is None:
            raise ResolveError(f"failed to import media into {folder.GetName()}")
    return pending, skipped


def _clips_for_paths(folder: Any, paths: list[Path]) -> list[Any]:
    normalized_paths = [_normalize_media_path(path) for path in paths]
    clip_map = {
        _normalize_media_path(_clip_file_path(clip)): clip
        for clip in _iter_clips_recursive(folder)
        if _clip_file_path(clip)
    }
    missing = [path for path in normalized_paths if path not in clip_map]
    if missing:
        raise ResolveError(f"expected imported clips are missing from {folder.GetName()}: {', '.join(missing)}")
    return [clip_map[path] for path in normalized_paths]


def _clip_by_name(folder: Any, clip_name: str) -> Any | None:
    for clip in _iter_clips_recursive(folder):
        if clip.GetName() == clip_name:
            return clip
    return None


def _direct_clip_by_name(folder: Any, clip_name: str) -> Any | None:
    for clip in folder.GetClipList() or []:
        if clip.GetName() == clip_name:
            return clip
    return None


def _clip_frame_count(clip: Any, *, frame_rate: float | None = None) -> int:
    properties = clip.GetClipProperty() or {}
    candidates = [properties.get("Frames"), properties.get("frames")]
    for candidate in candidates:
        if candidate in (None, ""):
            continue
        try:
            return max(1, int(float(candidate)))
        except (TypeError, ValueError):
            continue
    duration_candidates = [properties.get("Duration"), properties.get("duration")]
    for candidate in duration_candidates:
        if candidate in (None, ""):
            continue
        text = str(candidate).strip()
        if frame_rate is not None and ":" in text:
            parts = text.replace(";", ":").split(":")
            if len(parts) == 4:
                try:
                    hours, minutes, seconds, frames = [int(part) for part in parts]
                    base_frames = int(round(((hours * 3600) + (minutes * 60) + seconds) * frame_rate))
                    return max(1, base_frames + frames)
                except ValueError:
                    pass
        if frame_rate is not None:
            try:
                return max(1, int(round(float(text) * frame_rate)))
            except (TypeError, ValueError):
                pass
    raise ResolveError(f"unable to determine frame count for clip {clip.GetName()}")


def _rename_clip(clip: Any, target_name: str) -> None:
    if clip.GetName() == target_name:
        return
    if hasattr(clip, "SetName") and clip.SetName(target_name):
        return
    if hasattr(clip, "SetClipProperty") and clip.SetClipProperty("Clip Name", target_name):
        return
    raise ResolveError(f"unable to rename Resolve clip to {target_name}")


def _set_setting(
    project: Any,
    key: str,
    candidates: list[str],
    *,
    required: bool = True,
    mismatches: list[dict[str, Any]] | None = None,
) -> str:
    """Write a Resolve project setting and verify via GetSetting read-back.

    GetSetting is the only source of truth. The SetSetting return value is
    captured only for error diagnostics — it never drives control flow.
    A candidate list lets callers pass aliases (e.g. legacy vs current value
    strings) and accept the first one that round-trips cleanly.

    When none of the candidates round-trips:
      - required=True raises ResolveError with key, tried candidates, observed
        value, and the last SetSetting return code.
      - required=False returns the observed value and, if `mismatches` is
        provided, appends a drift record so the caller can surface it as a
        stage warning. Callers that do not care about drift surfacing may
        leave `mismatches=None`.
    """
    last_rc: Any = None
    for candidate in candidates:
        target = str(candidate)
        last_rc = project.SetSetting(key, target)
        current = str(project.GetSetting(key))
        if current == target:
            return current

    final = str(project.GetSetting(key))
    if required:
        tried = ", ".join(repr(str(c)) for c in candidates)
        raise ResolveError(
            f"unable to set Resolve project setting {key}: "
            f"tried [{tried}], final value={final!r}, last SetSetting rc={last_rc!r}"
        )
    if mismatches is not None:
        mismatches.append(
            {
                "key": key,
                "requested": [str(c) for c in candidates],
                "observed": final,
                "set_setting_rc": last_rc,
            }
        )
    return final


def _apply_project_settings(
    project: Any, timeline: TimelineConfig
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Apply piano-guard's target project settings via `_set_setting`.

    Returns a `(settings, mismatches)` tuple:
      - `settings` is the observed read-back value per key (source of truth).
      - `mismatches` captures any `required=False` keys that failed to
        round-trip so the caller can surface them as stage warnings.

    **Color management model (2026-05-17 rewrite):**

    Source is Sony α6400 PP10 HLG, tagged as Rec.2020 / ARIB STD-B67.
    The production target for this repo is YouTube SDR, so Resolve is
    configured explicitly as DaVinci YRGB Color Managed **Custom**:

      * Input color space: ``Rec.2100 HLG``.
      * Timeline color space: ``Rec.709 Gamma 2.4``.
      * Output color space: ``Rec.709 Gamma 2.4``.
      * Input / output DRT: ``DaVinci``.

    Automatic Color Management previously let Resolve report
    ``colorSpaceInput = Rec.709 Gamma 2.4`` on HLG clips, which made the
    HLG→SDR transform ambiguous and produced unpleasant warm/dark results.
    DRT=None was also observed to clip HLG highlights badly in the Resolve
    viewer. The explicit DaVinci DRT path keeps Node 1 / ``SetCDL`` in the
    same Rec.709 display-review space used by ``preview-cdl``.
    """
    width = str(timeline.width)
    height = str(timeline.height)
    frame_rate = str(timeline.frame_rate)
    monitor_format = f"UHD 2160p {timeline.frame_rate}"
    mismatches: list[dict[str, Any]] = []

    settings: dict[str, str] = {
        "timelineFrameRate": _set_setting(
            project, "timelineFrameRate", [frame_rate], mismatches=mismatches
        ),
        # timelinePlaybackFrameRate is effectively read-only via the Resolve
        # scripting API: SetSetting returns False regardless of candidate
        # string ("29.97", "30", numeric strings, etc.) and the value
        # stays at whatever the UI default is (often "24" on a fresh
        # project). useCustomTimelinePlaybackFrameRate is also locked.
        # The only way to change it is via UI Project Settings. We still
        # call SetSetting so the mismatch is surfaced as a
        # timeline_playback_framerate_locked WARN with the manual fix
        # instructions — treating this as required=True would cause the
        # bootstrap to FAIL on every run and block legitimate work.
        "timelinePlaybackFrameRate": _set_setting(
            project,
            "timelinePlaybackFrameRate",
            [frame_rate],
            required=False,
            mismatches=mismatches,
        ),
        "timelineResolutionWidth": _set_setting(
            project, "timelineResolutionWidth", [width], mismatches=mismatches
        ),
        "timelineResolutionHeight": _set_setting(
            project, "timelineResolutionHeight", [height], mismatches=mismatches
        ),
        "timelineOutputResolutionWidth": _set_setting(
            project, "timelineOutputResolutionWidth", [width], mismatches=mismatches
        ),
        "timelineOutputResolutionHeight": _set_setting(
            project, "timelineOutputResolutionHeight", [height], mismatches=mismatches
        ),
        "timelineDropFrameTimecode": _set_setting(
            project, "timelineDropFrameTimecode", ["1"], mismatches=mismatches
        ),
        "videoMonitorFormat": _set_setting(
            project,
            "videoMonitorFormat",
            [monitor_format],
            required=False,
            mismatches=mismatches,
        ),
        "colorScienceMode": _set_setting(
            project,
            "colorScienceMode",
            [
                "davinciYRGBColorManagedv2",
                "davinciYRGBColorManaged",
                "DaVinci YRGB Color Managed",
            ],
            mismatches=mismatches,
        ),
        # Custom Color Management. Keep "separate color space and gamma" off
        # because Resolve's scripting API accepts the combined names used here
        # ("Rec.2100 HLG", "Rec.709 Gamma 2.4") in that mode.
        "isAutoColorManage": _set_setting(
            project, "isAutoColorManage", ["0"], required=False, mismatches=mismatches
        ),
        "separateColorSpaceAndGamma": _set_setting(
            project,
            "separateColorSpaceAndGamma",
            ["0"],
            required=False,
            mismatches=mismatches,
        ),
        "colorSpaceInput": _set_setting(
            project,
            "colorSpaceInput",
            [timeline.input_color_space, "Rec.2100 HLG"],
            mismatches=mismatches,
        ),
        "colorSpaceTimeline": _set_setting(
            project,
            "colorSpaceTimeline",
            [timeline.timeline_color_space, "Rec.709 Gamma 2.4"],
            mismatches=mismatches,
        ),
        "colorSpaceOutput": _set_setting(
            project,
            "colorSpaceOutput",
            [timeline.output_color_space, "Rec.709 Gamma 2.4"],
            mismatches=mismatches,
        ),
        # Use Resolve's DaVinci DRT for the HLG -> SDR rolloff. Leaving this
        # at None clips HLG highlights in the viewer and makes the AI stills
        # materially disagree with the operator's Resolve review.
        "inputDRT": _set_setting(
            project, "inputDRT", ["DaVinci"], mismatches=mismatches
        ),
        "outputDRT": _set_setting(
            project, "outputDRT", ["DaVinci"], mismatches=mismatches
        ),
        "useInverseDRT": _set_setting(
            project, "useInverseDRT", ["0"], required=False, mismatches=mismatches
        ),
    }

    # Informational: record the current RCM preset mode (not SetSet-managed).
    # Piano-guard drives individual color-space fields rather than binding the
    # project to a preset, so this value typically reads as "Custom" after
    # bootstrap. Captured here so the research/debug workflow can diff it
    # without running a separate probe.
    settings["observed_rcm_preset_mode"] = str(project.GetSetting("rcmPresetMode"))

    return settings, mismatches


def _read_project_color_settings(project: Any) -> dict[str, str]:
    return {
        "colorScienceMode": str(project.GetSetting("colorScienceMode")),
        "isAutoColorManage": str(project.GetSetting("isAutoColorManage")),
        "separateColorSpaceAndGamma": str(project.GetSetting("separateColorSpaceAndGamma")),
        "colorSpaceInput": str(project.GetSetting("colorSpaceInput")),
        "colorSpaceTimeline": str(project.GetSetting("colorSpaceTimeline")),
        "colorSpaceOutput": str(project.GetSetting("colorSpaceOutput")),
        "inputDRT": str(project.GetSetting("inputDRT")),
        "outputDRT": str(project.GetSetting("outputDRT")),
        "useInverseDRT": str(project.GetSetting("useInverseDRT")),
    }


def _expected_project_color_settings(timeline: TimelineConfig) -> dict[str, str]:
    return {
        "colorScienceMode": "davinciYRGBColorManagedv2",
        "isAutoColorManage": "0",
        "separateColorSpaceAndGamma": "0",
        "colorSpaceInput": timeline.input_color_space,
        "colorSpaceTimeline": timeline.timeline_color_space,
        "colorSpaceOutput": timeline.output_color_space,
        "inputDRT": "DaVinci",
        "outputDRT": "DaVinci",
        "useInverseDRT": "0",
    }


def _project_color_setting_mismatches(project: Any, timeline: TimelineConfig) -> list[dict[str, str]]:
    settings = _read_project_color_settings(project)
    expected = _expected_project_color_settings(timeline)
    return [
        {"key": key, "expected": value, "observed": settings.get(key, "")}
        for key, value in expected.items()
        if settings.get(key) != value
    ]


def _session_folders(media_pool: Any, root: Any, session: SessionProjectConfig) -> dict[str, Any]:
    return {
        "takes": _ensure_folder(media_pool, root, session.resolve.takes_bin),
        "timelines": _ensure_folder(media_pool, root, session.resolve.timelines_bin),
    }


def _seed_project_library_template(library_root: Path) -> None:
    if (library_root / "Resolve Projects").exists():
        return
    if not DEFAULT_DISK_LIBRARY_TEMPLATE.exists():
        raise ResolveError(f"default Resolve Project Library template not found: {DEFAULT_DISK_LIBRARY_TEMPLATE}")

    users_root = DEFAULT_DISK_LIBRARY_TEMPLATE / "Resolve Projects/Users"
    guest_root = users_root / "guest"
    target_guest = library_root / "Resolve Projects/Users/guest"
    target_guest.mkdir(parents=True, exist_ok=True)
    (target_guest / "Projects").mkdir(parents=True, exist_ok=True)
    (target_guest / "ProjectMetadataCache").mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        DEFAULT_DISK_LIBRARY_TEMPLATE / "Resolve Projects/Settings",
        library_root / "Resolve Projects/Settings",
        dirs_exist_ok=True,
    )
    shutil.copytree(
        guest_root / "Configs",
        target_guest / "Configs",
        dirs_exist_ok=True,
    )
    source = guest_root / "User.db"
    target = target_guest / "User.db"
    if source.exists() and not target.exists():
        shutil.copy2(source, target)


def list_project_libraries(*, connector=connect_to_resolve) -> dict[str, Any]:
    connection = connector()
    return {
        "current_database": connection.project_manager.GetCurrentDatabase() or {},
        "databases": connection.project_manager.GetDatabaseList() or [],
    }


def ensure_project_library(
    *,
    library_name: str,
    library_path: str | Path,
    connector=connect_to_resolve,
    dblist_conf_path: str | Path = DEFAULT_RESOLVE_DBLIST_CONF,
) -> dict[str, Any]:
    target_path = Path(library_path).expanduser().resolve()
    target_path.mkdir(parents=True, exist_ok=True)
    _seed_project_library_template(target_path)
    dblist_conf = Path(dblist_conf_path).expanduser().resolve()
    registration = _register_disk_library_in_dblist(
        library_name=library_name,
        library_path=target_path,
        dblist_conf_path=dblist_conf,
    )

    payload = {
        "library_name": library_name,
        "library_path": str(target_path),
        "dblist_conf_path": registration["dblist_conf_path"],
        "dblist_updated": registration["updated"],
    }

    try:
        connection = connector()
    except ResolveError:
        payload["status"] = "PASS"
        payload["message"] = (
            f"registered disk project library in {registration['dblist_conf_path']}: {library_name}. "
            "Launch Resolve to use it."
        )
        return payload

    databases = connection.project_manager.GetDatabaseList() or []
    for entry in databases:
        if entry.get("DbType") == "Disk" and entry.get("DbName") == library_name:
            payload["status"] = "PASS"
            payload["message"] = f"project library already registered: {library_name}"
            return payload

    payload["status"] = "WARN"
    payload["message"] = (
        f"registered disk project library in {registration['dblist_conf_path']}, but the running Resolve instance "
        f"has not reloaded it yet: {library_name}. Restart Resolve once, then rerun."
    )
    return payload


def _take_media_paths(take: TakeConfig) -> tuple[list[Path], Path]:
    video_paths = [take.resolve_path(camera.file) for camera in take.camera_files]
    audio_path = take.editing_audio_path()
    return video_paths, audio_path


def _working_take_folder(media_pool: Any, takes_root: Any, take: TakeConfig) -> Any:
    return _ensure_folder(media_pool, takes_root, take.take_id)


def _clips_by_normalized_path(folder: Any) -> dict[str, Any]:
    clip_map: dict[str, Any] = {}
    for clip in _iter_clips_recursive(folder):
        file_path = _clip_file_path(clip)
        if not file_path:
            continue
        clip_map[_normalize_media_path(file_path)] = clip
    return clip_map


def _move_clips_to_folder(media_pool: Any, clips: list[Any], folder: Any) -> None:
    if not clips:
        return
    moved = media_pool.MoveClips(clips, folder)
    if moved:
        return
    current_paths = {
        _normalize_media_path(_clip_file_path(clip))
        for clip in _iter_clips_recursive(folder)
        if _clip_file_path(clip)
    }
    target_paths = {
        _normalize_media_path(_clip_file_path(clip))
        for clip in clips
        if _clip_file_path(clip)
    }
    if target_paths.issubset(current_paths):
        return
    raise ResolveError(f"unable to move Resolve clips into {folder.GetName()}")


def _delete_folder_if_exists(media_pool: Any, parent: Any, folder_name: str) -> None:
    for folder in parent.GetSubFolderList() or []:
        if folder.GetName() != folder_name:
            continue
        remaining = [clip.GetName() for clip in folder.GetClipList() or []]
        if remaining:
            raise ResolveError(f"cannot delete non-empty Resolve media pool folder {folder_name}: {', '.join(remaining)}")
        if not media_pool.DeleteFolders([folder]):
            raise ResolveError(f"unable to delete Resolve media pool folder: {folder_name}")
        return


def _cleanup_take_temp_folders(media_pool: Any, working_folder: Any) -> None:
    for folder_name in ("__mc_source", "__mc_hold"):
        for folder in list(working_folder.GetSubFolderList() or []):
            if folder.GetName() != folder_name:
                continue
            clips = list(folder.GetClipList() or [])
            if clips:
                _move_clips_to_folder(media_pool, clips, working_folder)
            _delete_folder_if_exists(media_pool, working_folder, folder_name)


def _ensure_take_source_clips(media_pool: Any, takes_root: Any, working_folder: Any, take: TakeConfig) -> tuple[list[str], list[str]]:
    video_paths, audio_path = _take_media_paths(take)
    requested = [*video_paths, audio_path]
    imported_paths, skipped_paths = _ensure_imported(media_pool, working_folder, requested)
    clip_map = _clips_by_normalized_path(takes_root)
    missing_paths = [path for path in requested if _normalize_media_path(path) not in clip_map]
    if missing_paths:
        missing = ", ".join(str(path) for path in missing_paths)
        raise ResolveError(f"expected imported take media is missing for {take.take_id}: {missing}")
    matched_clips = [clip_map[_normalize_media_path(path)] for path in requested]
    _move_clips_to_folder(media_pool, matched_clips, working_folder)
    _ensure_take_clip_names(working_folder, take)
    return imported_paths, skipped_paths


def _rename_stale_audio_master(folder: Any, current_audio_path: Path) -> list[tuple[str, str]]:
    """Rename stale `audio-master` clips in `folder` whose path differs from
    `current_audio_path`.

    Prior bootstraps may have imported a different audio source (e.g. the
    raw `audio.aif` master) and renamed it to `audio-master`. When a later
    bootstrap generates `audio-edit.wav` and imports that instead, the old
    clip survives in the bin with the same `audio-master` name. Resolve
    then has two clips named `audio-master` per take, and any name-based
    lookup (including multicam clip creation) picks whichever one Resolve
    returns first.

    This helper scans the folder's direct children non-recursively and
    renames each stale `audio-master` clip to `<filename>-stale` — where
    `filename` is the full basename of the clip's file path, extension
    included — so operators can see the debris at a glance without losing
    the breadcrumb of what the old source was. On collision with an
    existing clip, a numeric suffix is appended.

    Scanning `__mc_source` / `__mc_hold` sub-folders is deliberately
    avoided: those are temporary multicam-creation scratch areas whose
    contents get moved back and renamed by `_restore_multicam_working_folder`.

    Returns a list of `(old_name, new_name)` tuples for reporting.
    """
    renamed: list[tuple[str, str]] = []
    normalized_current = _normalize_media_path(current_audio_path)
    all_clips = list(folder.GetClipList() or [])
    existing_names = {clip.GetName() for clip in all_clips}
    for clip in all_clips:
        if clip.GetName() != "audio-master":
            continue
        clip_path = _clip_file_path(clip)
        if clip_path and _normalize_media_path(clip_path) == normalized_current:
            continue  # this is the target clip — not stale
        filename = Path(clip_path).name if clip_path else "audio-master"
        base_name = f"{filename}-stale"
        new_name = base_name
        suffix = 2
        while new_name in existing_names:
            new_name = f"{base_name}-{suffix}"
            suffix += 1
        _rename_clip(clip, new_name)
        existing_names.discard("audio-master")
        existing_names.add(new_name)
        renamed.append(("audio-master", new_name))
    return renamed


def _ensure_take_clip_names(folder: Any, take: TakeConfig) -> None:
    video_paths, audio_path = _take_media_paths(take)
    for clip, camera in zip(_clips_for_paths(folder, video_paths), take.camera_files):
        _rename_clip(clip, camera.label)
    _rename_stale_audio_master(folder, audio_path)
    audio_clip = _clips_for_paths(folder, [audio_path])[0]
    _rename_clip(audio_clip, "audio-master")


def _take_angle_clips(folder: Any, take: TakeConfig) -> list[Any]:
    video_paths, _audio_path = _take_media_paths(take)
    return _clips_for_paths(folder, video_paths)


def _sync_take_clips(project: Any, resolve: Any, folder: Any, take: TakeConfig) -> tuple[int, str]:
    video_paths, audio_path = _take_media_paths(take)
    video_clips = _clips_for_paths(folder, video_paths)
    audio_clip = _clips_for_paths(folder, [audio_path])[0]
    if not video_clips:
        raise ResolveError(f"no video clips available for sync in {folder.GetName()}")

    sync_settings = {
        resolve.AUDIO_SYNC_MODE: resolve.AUDIO_SYNC_WAVEFORM,
        resolve.AUDIO_SYNC_CHANNEL_NUMBER: resolve.AUDIO_SYNC_CHANNEL_MIX,
        resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO: True,
        resolve.AUDIO_SYNC_RETAIN_VIDEO_METADATA: True,
    }
    result = project.GetMediaPool().AutoSyncAudio([*video_clips, audio_clip], sync_settings)
    if not result:
        raise ResolveError(f"Resolve waveform sync failed for take {take.take_id}")
    _ensure_take_clip_names(folder, take)
    return len(video_clips), str(audio_path)


def _run_osascript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-"],
        input=script.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        raise ResolveError(stderr or stdout or "AppleScript UI automation failed")
    return result.stdout.decode("utf-8", errors="replace").strip()


AX_MULTICAM_HELPER_SOURCE = r"""
import AppKit
import ApplicationServices
import Foundation

func attr<T>(_ element: AXUIElement, _ name: String, as type: T.Type = T.self) -> T? {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    guard err == .success, let v = value else { return nil }
    return (v as! T)
}

func title(_ element: AXUIElement) -> String { attr(element, kAXTitleAttribute as String) ?? "" }
func role(_ element: AXUIElement) -> String { attr(element, kAXRoleAttribute as String) ?? "" }

func press(_ element: AXUIElement) {
    _ = AXUIElementPerformAction(element, kAXPressAction as CFString)
}

func setStringValue(_ element: AXUIElement, _ value: String) {
    _ = AXUIElementSetAttributeValue(element, kAXValueAttribute as CFString, value as CFTypeRef)
}

func cgPoint(_ value: AXValue?) -> CGPoint? {
    guard let value else { return nil }
    var point = CGPoint.zero
    if AXValueGetType(value) != .cgPoint { return nil }
    if AXValueGetValue(value, .cgPoint, &point) { return point }
    return nil
}

func cgSize(_ value: AXValue?) -> CGSize? {
    guard let value else { return nil }
    var size = CGSize.zero
    if AXValueGetType(value) != .cgSize { return nil }
    if AXValueGetValue(value, .cgSize, &size) { return size }
    return nil
}

func frame(_ element: AXUIElement) -> CGRect? {
    guard let position = cgPoint(attr(element, kAXPositionAttribute as String, as: AXValue.self)),
          let size = cgSize(attr(element, kAXSizeAttribute as String, as: AXValue.self)) else { return nil }
    return CGRect(origin: position, size: size)
}

func collectMenus(_ element: AXUIElement, depth: Int = 0, maxDepth: Int = 8, out: inout [AXUIElement]) {
    if role(element) == kAXMenuRole as String { out.append(element) }
    guard depth < maxDepth, let children: [AXUIElement] = attr(element, kAXChildrenAttribute as String, as: [AXUIElement].self) else { return }
    for child in children { collectMenus(child, depth: depth + 1, maxDepth: maxDepth, out: &out) }
}

func findWindow(_ element: AXUIElement, title windowTitle: String, depth: Int = 0, maxDepth: Int = 8) -> AXUIElement? {
    if role(element) == kAXWindowRole as String, title(element) == windowTitle { return element }
    guard depth < maxDepth, let children: [AXUIElement] = attr(element, kAXChildrenAttribute as String, as: [AXUIElement].self) else { return nil }
    for child in children {
        if let found = findWindow(child, title: windowTitle, depth: depth + 1, maxDepth: maxDepth) { return found }
    }
    return nil
}

func mainWindow(_ app: AXUIElement) -> AXUIElement? {
    if let focused: AXUIElement = attr(app, kAXFocusedWindowAttribute as String, as: AXUIElement.self) {
        let focusedTitle = title(focused)
        if !focusedTitle.isEmpty && focusedTitle != "New Multicam Clip" && focusedTitle != "New Timeline Properties" {
            return focused
        }
    }
    if let main: AXUIElement = attr(app, kAXMainWindowAttribute as String, as: AXUIElement.self) {
        let mainTitle = title(main)
        if !mainTitle.isEmpty && mainTitle != "New Multicam Clip" && mainTitle != "New Timeline Properties" {
            return main
        }
    }
    guard let windows: [AXUIElement] = attr(app, kAXWindowsAttribute as String, as: [AXUIElement].self) else { return nil }
    for window in windows {
        let windowTitle = title(window)
        if windowTitle.isEmpty { continue }
        if windowTitle == "New Multicam Clip" || windowTitle == "New Timeline Properties" { continue }
        return window
    }
    return windows.first
}

func click(_ x: Double, _ y: Double) {
    let point = CGPoint(x: x, y: y)
    let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)
    let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func rightClick(_ x: Double, _ y: Double) {
    let point = CGPoint(x: x, y: y)
    let down = CGEvent(mouseEventSource: nil, mouseType: .rightMouseDown, mouseCursorPosition: point, mouseButton: .right)
    let up = CGEvent(mouseEventSource: nil, mouseType: .rightMouseUp, mouseCursorPosition: point, mouseButton: .right)
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func parent(_ element: AXUIElement) -> AXUIElement? {
    attr(element, kAXParentAttribute as String, as: AXUIElement.self)
}

func showMenu(_ element: AXUIElement) -> Bool {
    AXUIElementPerformAction(element, "AXShowMenu" as CFString) == .success
}

func showMenuChain(from element: AXUIElement?) -> Bool {
    var current = element
    var depth = 0
    while let candidate = current, depth < 8 {
        if showMenu(candidate) { return true }
        current = parent(candidate)
        depth += 1
    }
    return false
}

func key(_ code: CGKeyCode, flags: CGEventFlags = []) {
    let source = CGEventSource(stateID: .combinedSessionState)
    let down = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: true)
    let up = CGEvent(keyboardEventSource: source, virtualKey: code, keyDown: false)
    down?.flags = flags
    up?.flags = flags
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
}

func element(at point: CGPoint) -> AXUIElement? {
    let system = AXUIElementCreateSystemWide()
    var found: AXUIElement?
    let err = AXUIElementCopyElementAtPosition(system, Float(point.x), Float(point.y), &found)
    guard err == .success else { return nil }
    return found
}

func waitForWindow(_ app: AXUIElement, title: String, timeout: TimeInterval) -> AXUIElement? {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if let window = findWindow(app, title: title) { return window }
        Thread.sleep(forTimeInterval: 0.1)
    }
    return nil
}

func waitForWindowClose(_ app: AXUIElement, title: String, timeout: TimeInterval) -> Bool {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if findWindow(app, title: title) == nil { return true }
        Thread.sleep(forTimeInterval: 0.1)
    }
    return false
}

func closeIfOpen(_ app: AXUIElement, _ windowTitle: String) {
    guard let window = findWindow(app, title: windowTitle) else { return }
    guard let children: [AXUIElement] = attr(window, kAXChildrenAttribute as String, as: [AXUIElement].self) else { return }
    if let cancel = children.first(where: { role($0) == kAXButtonRole as String && title($0) == "Cancel" }) {
        press(cancel)
    }
}

func collectDescendants(_ element: AXUIElement, out: inout [AXUIElement], depth: Int = 0, maxDepth: Int = 12) {
    out.append(element)
    guard depth < maxDepth else { return }
    guard let children: [AXUIElement] = attr(element, kAXChildrenAttribute as String, as: [AXUIElement].self) else { return }
    for child in children { collectDescendants(child, out: &out, depth: depth + 1, maxDepth: maxDepth) }
}

func mediaPoolPane(in window: AXUIElement) -> CGRect? {
    guard let windowFrame = frame(window) else { return nil }
    var descendants: [AXUIElement] = []
    collectDescendants(window, out: &descendants)
    let candidates = descendants.compactMap { element -> (CGRect, AXUIElement)? in
        guard let candidateFrame = frame(element) else { return nil }
        let candidateRole = role(element)
        guard candidateRole == kAXGroupRole as String || candidateRole == kAXSplitGroupRole as String else { return nil }
        guard candidateFrame.width >= 250.0, candidateFrame.width <= windowFrame.width * 0.45 else { return nil }
        guard candidateFrame.height >= windowFrame.height * 0.45 else { return nil }
        guard candidateFrame.minX <= windowFrame.minX + 24.0 else { return nil }
        guard candidateFrame.minY >= windowFrame.minY + 20.0 else { return nil }
        return (candidateFrame, element)
    }
    return candidates
        .sorted { lhs, rhs in
            if lhs.0.minX != rhs.0.minX { return lhs.0.minX < rhs.0.minX }
            if lhs.0.minY != rhs.0.minY { return lhs.0.minY < rhs.0.minY }
            return lhs.0.width * lhs.0.height > rhs.0.width * rhs.0.height
        }
        .first?.0
}

func choose(_ window: AXUIElement, comboTitlePrefix: String, optionTitle: String) throws {
    let children: [AXUIElement] = attr(window, kAXChildrenAttribute as String, as: [AXUIElement].self) ?? []
    guard let combo = children.first(where: { role($0) == kAXComboBoxRole as String && title($0).hasPrefix(comboTitlePrefix) }) else {
        throw NSError(domain: "piano_guard", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing combo \(comboTitlePrefix)"])
    }
    let comboChildren: [AXUIElement] = attr(combo, kAXChildrenAttribute as String, as: [AXUIElement].self) ?? []
    guard let list = comboChildren.first else {
        throw NSError(domain: "piano_guard", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing option list for \(comboTitlePrefix)"])
    }
    let options: [AXUIElement] = attr(list, kAXChildrenAttribute as String, as: [AXUIElement].self) ?? []
    guard let option = options.first(where: { title($0) == optionTitle }) else {
        throw NSError(domain: "piano_guard", code: 1, userInfo: [NSLocalizedDescriptionKey: "missing option \(optionTitle) for \(comboTitlePrefix)"])
    }
    press(option)
}

let env = ProcessInfo.processInfo.environment
let multicamName = env["MC_NAME"] ?? "MC_take"

let pid = NSRunningApplication.runningApplications(withBundleIdentifier: "com.blackmagic-design.DaVinciResolve").first?.processIdentifier ?? 0
if pid == 0 {
    fputs("Resolve process not found\n", stderr)
    exit(1)
}
let app = AXUIElementCreateApplication(pid)
NSRunningApplication(processIdentifier: pid)?.activate(options: [])
Thread.sleep(forTimeInterval: 0.3)

closeIfOpen(app, "New Multicam Clip")
closeIfOpen(app, "New Timeline Properties")
Thread.sleep(forTimeInterval: 0.2)

guard let window = mainWindow(app) else {
    fputs("Resolve main window not found\n", stderr)
    exit(1)
}
guard let mediaPoolFrame = mediaPoolPane(in: window) else {
    fputs("Resolve Media Pool pane not found\n", stderr)
    exit(1)
}
let candidatePoints = [
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.50, y: mediaPoolFrame.minY + 120.0),
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.65, y: mediaPoolFrame.minY + 120.0),
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.50, y: mediaPoolFrame.minY + 170.0),
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.65, y: mediaPoolFrame.minY + 170.0),
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.50, y: mediaPoolFrame.minY + 230.0),
    CGPoint(x: mediaPoolFrame.minX + mediaPoolFrame.width * 0.65, y: mediaPoolFrame.minY + 230.0),
]

var multicamTriggered = false
var lastCandidateDescription = "none"
for point in candidatePoints {
    guard let target = element(at: point) else { continue }
    let targetRole = role(target)
    if targetRole == kAXMenuButtonRole as String || targetRole == kAXButtonRole as String {
        continue
    }
    lastCandidateDescription = "x=\(Int(point.x)),y=\(Int(point.y)) role=\(targetRole)"
    if !showMenuChain(from: target) {
        rightClick(Double(point.x), Double(point.y))
    }
    Thread.sleep(forTimeInterval: 0.2)

    var menus: [AXUIElement] = []
    collectMenus(app, out: &menus)
    for menu in menus {
        guard let items: [AXUIElement] = attr(menu, kAXChildrenAttribute as String, as: [AXUIElement].self) else { continue }
        if let item = items.first(where: { title($0).hasPrefix("Create New Multicam Clip Using Selected Bin") }) {
            _ = AXUIElementPerformAction(item, kAXPressAction as CFString)
            multicamTriggered = true
            break
        }
    }
    if multicamTriggered {
        break
    }
    key(53) // escape
    Thread.sleep(forTimeInterval: 0.1)
}

if !multicamTriggered {
    fputs("Create New Multicam Clip Using Selected Bin menu item not found (last candidate \(lastCandidateDescription))\n", stderr)
    exit(1)
}

guard let dialog = waitForWindow(app, title: "New Multicam Clip", timeout: 5.0) else {
    fputs("New Multicam Clip dialog did not appear\n", stderr)
    exit(1)
}

let dialogChildren: [AXUIElement] = attr(dialog, kAXChildrenAttribute as String, as: [AXUIElement].self) ?? []
if let moveSource = dialogChildren.first(where: { role($0) == kAXCheckBoxRole as String && title($0).contains("Move source clips") }) {
    let current = (attr(moveSource, kAXValueAttribute as String, as: NSNumber.self)?.intValue) ?? 0
    if current == 1 { press(moveSource) }
}
if let textField = dialogChildren.first(where: { role($0) == kAXTextFieldRole as String }) {
    setStringValue(textField, multicamName)
}

do {
    try choose(dialog, comboTitlePrefix: "Source Audio Channels", optionTitle: "Reference Audio/Angle 1")
    try choose(dialog, comboTitlePrefix: "Sequential", optionTitle: "Clip Name")
    try choose(dialog, comboTitlePrefix: "Timecode", optionTitle: "Sound")
    try choose(dialog, comboTitlePrefix: "1", optionTitle: "Mix")
} catch {
    closeIfOpen(app, "New Multicam Clip")
    fputs("\(error.localizedDescription)\n", stderr)
    exit(1)
}

guard let createButton = dialogChildren.first(where: { role($0) == kAXButtonRole as String && title($0) == "Create" }) else {
    fputs("Create button missing\n", stderr)
    exit(1)
}
press(createButton)
if !waitForWindowClose(app, title: "New Multicam Clip", timeout: 10.0) {
    fputs("New Multicam Clip dialog did not close\n", stderr)
    exit(1)
}
"""


def _ax_helper_dir() -> Path:
    return Path.home() / "Library/Caches/piano-guard"


def _ensure_ax_multicam_helper() -> Path:
    helper_dir = _ax_helper_dir()
    helper_dir.mkdir(parents=True, exist_ok=True)
    source_hash = hashlib.sha256(AX_MULTICAM_HELPER_SOURCE.encode("utf-8")).hexdigest()[:12]
    source_path = helper_dir / f"resolve_multicam_helper_{source_hash}.swift"
    binary_path = helper_dir / f"resolve_multicam_helper_{source_hash}"
    if not source_path.exists():
        source_path.write_text(AX_MULTICAM_HELPER_SOURCE, encoding="utf-8")
    if binary_path.exists():
        return binary_path
    result = subprocess.run(
        ["swiftc", str(source_path), "-o", str(binary_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ResolveError(result.stderr.strip() or result.stdout.strip() or "failed to compile Resolve multicam helper")
    return binary_path


def _create_multicam_clip_ui(*, multicam_name: str) -> None:
    helper_path = _ensure_ax_multicam_helper()
    env = os.environ.copy()
    env["MC_NAME"] = multicam_name
    result = subprocess.run([str(helper_path)], capture_output=True, text=True, env=env, check=False)
    if result.returncode != 0:
        raise ResolveError(result.stderr.strip() or result.stdout.strip() or "Resolve multicam helper failed")


def _find_new_multicam_clip(takes_root: Any, *, known_names: set[str]) -> Any | None:
    candidates = [
        clip
        for clip in _iter_clips_recursive(takes_root)
        if clip.GetName() not in known_names and ("Multicam" in clip.GetName() or clip.GetName().startswith("MC_"))
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda clip: clip.GetName())
    return candidates[0]


def _prepare_multicam_working_folder(
    media_pool: Any,
    working_folder: Any,
    take: TakeConfig,
) -> tuple[Any, list[Any], list[tuple[Any, str]]]:
    source_folder = _ensure_folder(media_pool, working_folder, "__mc_source")
    angle_clips = _take_angle_clips(working_folder, take)
    if len(angle_clips) < 2:
        raise ResolveError(f"fewer than 2 angle clips available in {take.take_id}")
    audio_clip = _direct_clip_by_name(working_folder, "audio-master")
    if audio_clip is None:
        raise ResolveError(f"audio-master missing from {take.take_id}")
    moved_clips = [audio_clip, *angle_clips]
    _move_clips_to_folder(media_pool, moved_clips, source_folder)

    renamed_clips: list[tuple[Any, str]] = []
    renamed_clips.append((audio_clip, audio_clip.GetName()))
    _rename_clip(audio_clip, "00_audio-master")
    for index, clip in enumerate(angle_clips, start=1):
        original_name = clip.GetName()
        renamed_clips.append((clip, original_name))
        _rename_clip(clip, f"{index:02d}_{original_name}")
    return source_folder, moved_clips, renamed_clips


def _restore_multicam_working_folder(
    media_pool: Any,
    working_folder: Any,
    hidden_folder: Any,
    hidden_clips: list[Any],
    renamed_clips: list[tuple[Any, str]],
    take: TakeConfig,
) -> None:
    _move_clips_to_folder(media_pool, hidden_clips, working_folder)
    for clip, original_name in renamed_clips:
        _rename_clip(clip, original_name)
    _ensure_take_clip_names(working_folder, take)
    _delete_folder_if_exists(media_pool, working_folder, hidden_folder.GetName())


def _timeline_by_name(project: Any, timeline_name: str) -> Any | None:
    for index in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline and timeline.GetName() == timeline_name:
            return timeline
    return None


def _delete_timeline_if_exists(project: Any, media_pool: Any, timeline_name: str) -> None:
    timeline = _timeline_by_name(project, timeline_name)
    if timeline and not media_pool.DeleteTimelines([timeline]):
        raise ResolveError(f"unable to delete existing timeline {timeline_name}")


def _build_session_assembly_timeline(
    *,
    project: Any,
    media_pool: Any,
    timelines_folder: Any,
    take_items: list[tuple[str, Any]],
    gap_frames: int,
    frame_rate: float,
) -> tuple[Any, list[dict[str, Any]]]:
    _delete_timeline_if_exists(project, media_pool, "Session_Assembly")
    media_pool.SetCurrentFolder(timelines_folder)
    timeline = media_pool.CreateEmptyTimeline("Session_Assembly")
    if not timeline:
        raise ResolveError("unable to create Session_Assembly timeline")
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError("unable to set Session_Assembly as current timeline")

    record_frame = 0
    appended_payload: list[dict[str, Any]] = []
    for take_id, multicam_clip in take_items:
        timeline.AddMarker(record_frame, "Blue", take_id, "", 1, "")
        multicam_frames = _clip_frame_count(multicam_clip, frame_rate=frame_rate)
        segment_frames = multicam_frames
        video_items = media_pool.AppendToTimeline(
            [
                {
                    "mediaPoolItem": multicam_clip,
                    "startFrame": 0,
                    "endFrame": multicam_frames,
                    "recordFrame": record_frame,
                }
            ]
        )
        if not video_items:
            raise ResolveError(f"unable to append {multicam_clip.GetName()} to Session_Assembly")
        appended_payload.append(
            {
                "take_id": take_id,
                "multicam_clip_name": multicam_clip.GetName(),
                "record_frame": record_frame,
                "multicam_frames": multicam_frames,
                "segment_frames": segment_frames,
            }
        )
        record_frame += segment_frames + gap_frames
    return timeline, appended_payload


COLOR_PREP_MARKER_PREFIX = "piano_guard:color_prep:"
VIDEO_ONLY_MEDIA_TYPE = 1
AUDIO_ONLY_MEDIA_TYPE = 2
COLOR_PREP_SYNC_SAMPLE_RATE = 11_025
COLOR_PREP_SYNC_HZ = 20.0
COLOR_PREP_START_TIMECODE = "00:00:00;00"
COLOR_PREP_LAYOUT = "compact"
COLOR_PREP_SYNC_CONFIDENCE_WARN = 0.70


def _timeline_frame_rate_value(timeline: TimelineConfig) -> float:
    value = str(timeline.frame_rate)
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    return float(value)


def _decode_color_prep_sync_audio(path: Path) -> np.ndarray:
    result = run_checked_bytes(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(COLOR_PREP_SYNC_SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = np.frombuffer(result.stdout, dtype="<i2")
    if samples.size == 0:
        raise ResolveError(f"decoded empty sync audio stream: {path}")
    return samples.astype(np.float32) / 32768.0


def _color_prep_sync_envelope(path: Path) -> np.ndarray:
    samples = _decode_color_prep_sync_audio(path)
    window_size = max(256, int(COLOR_PREP_SYNC_SAMPLE_RATE / COLOR_PREP_SYNC_HZ))
    usable = samples[: samples.size - (samples.size % window_size)]
    if usable.size == 0:
        usable = samples
    reshaped = usable.reshape(-1, window_size) if usable.size >= window_size else usable.reshape(1, -1)
    envelope = np.sqrt(np.mean(np.square(reshaped), axis=1)).astype(np.float32)
    envelope = np.log1p(envelope * 20.0)
    centered = envelope - float(np.mean(envelope))
    scale = float(np.std(centered))
    if scale < 1e-6:
        return np.zeros_like(centered, dtype=np.float32)
    return centered / scale


def _estimate_color_prep_sync_offset(
    master_audio_path: Path,
    video_path: Path,
    *,
    frame_rate: float,
) -> dict[str, Any]:
    """Return the video record-frame offset relative to the master audio.

    Positive offsets mean the video starts later than master audio and should
    be placed later on the color-prep timeline. Negative offsets mean the video
    starts before master audio.
    """
    master = _color_prep_sync_envelope(master_audio_path)
    scratch = _color_prep_sync_envelope(video_path)
    if master.size == 0 or scratch.size == 0:
        raise ResolveError(f"unable to build sync envelope for {video_path}")

    corr = correlate(master, scratch, mode="full", method="fft")
    lags = correlation_lags(master.size, scratch.size, mode="full")
    index = int(np.argmax(corr))
    lag_windows = int(lags[index])
    denominator = float(np.linalg.norm(master) * np.linalg.norm(scratch))
    confidence = float(corr[index] / denominator) if denominator > 1e-6 else 0.0
    offset_seconds = lag_windows / COLOR_PREP_SYNC_HZ
    offset_frames = int(round(offset_seconds * frame_rate))
    return {
        "offset_frames": offset_frames,
        "offset_seconds": offset_seconds,
        "confidence": confidence,
    }


def _color_prep_angles(session: SessionProjectConfig) -> list[str]:
    angles: list[str] = []
    seen: set[str] = set()
    for angle in session.angles:
        if angle not in seen:
            angles.append(angle)
            seen.add(angle)
    for _take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            if camera.label not in seen:
                angles.append(camera.label)
                seen.add(camera.label)
    return angles


def _ordered_take_cameras(take: TakeConfig, angle_order: dict[str, int]) -> list[Any]:
    return sorted(
        take.camera_files,
        key=lambda camera: (
            angle_order.get(camera.label, len(angle_order)),
            camera.label,
            camera.file,
        ),
    )


def _ensure_track_count(timeline: Any, track_type: str, count: int) -> None:
    while int(timeline.GetTrackCount(track_type) or 0) < count:
        if track_type == "audio":
            try:
                added = timeline.AddTrack("audio", "stereo")
            except TypeError:
                added = timeline.AddTrack("audio")
        else:
            added = timeline.AddTrack(track_type)
        if not added:
            raise ResolveError(f"unable to add {track_type} track for color prep timeline")


def _set_track_name_if_possible(timeline: Any, track_type: str, index: int, name: str) -> None:
    if hasattr(timeline, "SetTrackName") and not timeline.SetTrackName(track_type, index, name):
        raise ResolveError(f"unable to set {track_type} track {index} name to {name!r}")


def _color_prep_timeline_items(timeline: Any, track_type: str, track_index: int) -> list[Any]:
    if not hasattr(timeline, "GetItemListInTrack"):
        return []
    return list(timeline.GetItemListInTrack(track_type, track_index) or [])


def _timeline_item_clip_name(item: Any) -> str:
    if hasattr(item, "GetName"):
        return str(item.GetName())
    return ""


def _timeline_item_start(item: Any) -> int | None:
    if not hasattr(item, "GetStart"):
        return None
    try:
        return int(round(float(item.GetStart(False))))
    except (TypeError, ValueError):
        return None


def _append_color_prep_clip(
    media_pool: Any,
    *,
    clip: Any,
    media_type: int,
    track_index: int,
    record_frame: int,
    frame_count: int,
) -> None:
    appended = media_pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": clip,
                "startFrame": 0,
                "endFrame": frame_count,
                "mediaType": media_type,
                "trackIndex": track_index,
                "recordFrame": record_frame,
            }
        ]
    )
    if not appended:
        raise ResolveError(f"unable to append {clip.GetName()} to color prep timeline")


def _build_color_prep_timeline(
    *,
    project: Any,
    media_pool: Any,
    takes_root: Any,
    timelines_folder: Any,
    session: SessionProjectConfig,
    rebuild: bool = False,
) -> dict[str, Any]:
    timeline_name = session.resolve.color_prep_timeline_name
    existing = _timeline_by_name(project, timeline_name)
    if existing is not None and not rebuild:
        issue = Issue(
            severity="warn",
            code="color_prep_existing_preserved",
            message=f"color prep timeline {timeline_name!r} already exists; preserving manual grades",
            context={"timeline": timeline_name, "rebuild_flag": "--rebuild-color-prep"},
        )
        return {
            "status": "WARN",
            "action": "preserved-existing",
            "timeline_name": timeline_name,
            "issues": issues_to_dict([issue]),
            "summary": f"preserved existing color prep timeline {timeline_name}",
        }
    if existing is not None:
        _delete_timeline_if_exists(project, media_pool, timeline_name)

    angles = _color_prep_angles(session)
    if not angles:
        raise ResolveError("cannot build color prep timeline without angle labels")

    media_pool.SetCurrentFolder(timelines_folder)
    timeline = media_pool.CreateEmptyTimeline(timeline_name)
    if not timeline:
        raise ResolveError(f"unable to create color prep timeline {timeline_name}")
    if hasattr(timeline, "SetStartTimecode") and not timeline.SetStartTimecode(COLOR_PREP_START_TIMECODE):
        raise ResolveError(f"unable to set {timeline_name} start timecode to {COLOR_PREP_START_TIMECODE}")
    if not project.SetCurrentTimeline(timeline):
        raise ResolveError(f"unable to set {timeline_name} as current timeline")

    max_video_tracks = max((len(take.camera_files) for _take_ref, take in iter_session_takes(session)), default=0)
    if max_video_tracks <= 0:
        raise ResolveError("cannot build color prep timeline without camera files")

    _ensure_track_count(timeline, "video", max_video_tracks)
    _ensure_track_count(timeline, "audio", 1)
    for index in range(1, max_video_tracks + 1):
        _set_track_name_if_possible(timeline, "video", index, f"compact-v{index}")
    _set_track_name_if_possible(timeline, "audio", 1, "master-audio")

    frame_rate = _timeline_frame_rate_value(session.timeline)
    gap_frames = int(round(frame_rate * float(session.resolve.color_prep_gap_seconds)))
    record_frame = 0
    take_payloads: list[dict[str, Any]] = []
    issues: list[Issue] = []
    angle_order = {angle: index for index, angle in enumerate(angles)}
    video_track_map = {f"compact-v{index}": index for index in range(1, max_video_tracks + 1)}

    for take_ref, take in iter_session_takes(session):
        folder = _folder_by_name(takes_root, take_ref.id)
        ordered_cameras = _ordered_take_cameras(take, angle_order)
        appended_angles: list[dict[str, Any]] = []
        missing_angles = [angle for angle in angles if angle not in {camera.label for camera in take.camera_files}]
        take_frame_counts: list[int] = []
        sync_by_angle: dict[str, dict[str, Any]] = {}
        for camera in take.camera_files:
            sync_by_angle[camera.label] = _estimate_color_prep_sync_offset(
                take.editing_audio_path(),
                take.resolve_path(camera.file),
                frame_rate=frame_rate,
            )
        timeline_zero_shift = max(
            0,
            -min((sync["offset_frames"] for sync in sync_by_angle.values()), default=0),
        )
        audio_record_frame = record_frame + timeline_zero_shift

        for track_index, camera in enumerate(ordered_cameras, start=1):
            angle = camera.label
            clip = _clips_for_paths(folder, [take.resolve_path(camera.file)])[0]
            frame_count = _clip_frame_count(clip, frame_rate=frame_rate)
            sync = sync_by_angle[angle]
            sync_confidence = float(sync["confidence"])
            clip_record_frame = audio_record_frame + int(sync["offset_frames"])
            _append_color_prep_clip(
                media_pool,
                clip=clip,
                media_type=VIDEO_ONLY_MEDIA_TYPE,
                track_index=track_index,
                record_frame=clip_record_frame,
                frame_count=frame_count,
            )
            take_frame_counts.append((clip_record_frame - record_frame) + frame_count)
            if sync_confidence < COLOR_PREP_SYNC_CONFIDENCE_WARN:
                issues.append(
                    Issue(
                        severity="warn",
                        code="color_prep_sync_confidence_low",
                        message=(
                            f"{take_ref.id}/{angle}: audio sync confidence "
                            f"{sync_confidence:.3f} is below {COLOR_PREP_SYNC_CONFIDENCE_WARN:.2f}"
                        ),
                        context={
                            "take_id": take_ref.id,
                            "angle": angle,
                            "track_index": track_index,
                            "sync_confidence": round(sync_confidence, 4),
                            "threshold": COLOR_PREP_SYNC_CONFIDENCE_WARN,
                        },
                    )
                )
            appended_angles.append(
                {
                    "angle": angle,
                    "clip_name": clip.GetName(),
                    "track_index": track_index,
                    "frame_count": frame_count,
                    "record_frame": clip_record_frame,
                    "sync_offset_frames": int(sync["offset_frames"]),
                    "sync_offset_seconds": round(float(sync["offset_seconds"]), 4),
                    "sync_confidence": round(sync_confidence, 4),
                }
            )

        audio_clip = _clips_for_paths(folder, [take.editing_audio_path()])[0]
        audio_frame_count = _clip_frame_count(audio_clip, frame_rate=frame_rate)
        _append_color_prep_clip(
            media_pool,
            clip=audio_clip,
            media_type=AUDIO_ONLY_MEDIA_TYPE,
            track_index=1,
            record_frame=audio_record_frame,
            frame_count=audio_frame_count,
        )
        take_frame_counts.append((audio_record_frame - record_frame) + audio_frame_count)

        marker_custom_data = f"{COLOR_PREP_MARKER_PREFIX}{take_ref.id}"
        if not timeline.AddMarker(audio_record_frame, "Blue", take_ref.id, "", 1, marker_custom_data):
            raise ResolveError(f"unable to add color prep marker for {take_ref.id}")
        segment_frames = max(take_frame_counts)
        take_payloads.append(
            {
                "take_id": take_ref.id,
                "record_frame": record_frame,
                "marker_frame": audio_record_frame,
                "segment_frames": segment_frames,
                "gap_frames_after": gap_frames,
                "timeline_zero_shift_frames": timeline_zero_shift,
                "angles": appended_angles,
                "missing_angles": missing_angles,
                "audio": {
                    "clip_name": audio_clip.GetName(),
                    "track_index": 1,
                    "frame_count": audio_frame_count,
                    "record_frame": audio_record_frame,
                },
                "marker_custom_data": marker_custom_data,
            }
        )
        record_frame += segment_frames + gap_frames

    return {
        "status": "PASS",
        "action": "rebuilt" if rebuild else "created",
        "timeline_name": timeline_name,
        "start_timecode": COLOR_PREP_START_TIMECODE,
        "layout": COLOR_PREP_LAYOUT,
        "angles": angles,
        "video_track_map": video_track_map,
        "max_video_tracks": max_video_tracks,
        "audio_track": "master-audio",
        "gap_seconds": float(session.resolve.color_prep_gap_seconds),
        "gap_frames": gap_frames,
        "take_count": len(take_payloads),
        "takes": take_payloads,
        "issues": issues_to_dict(issues),
        "summary": f"created color prep timeline {timeline_name} for {len(take_payloads)} take(s)",
    }


def _inspect_color_prep_timeline(project: Any, session: SessionProjectConfig) -> tuple[dict[str, Any], list[Issue]]:
    timeline_name = session.resolve.color_prep_timeline_name
    angles = _color_prep_angles(session)
    payload: dict[str, Any] = {
        "timeline_name": timeline_name,
        "exists": False,
        "start_timecode": None,
        "start_frame": None,
        "layout": COLOR_PREP_LAYOUT,
        "expected_angles": angles,
        "expected_video_track_count": max((len(take.camera_files) for _take_ref, take in iter_session_takes(session)), default=0),
        "video_track_count": 0,
        "audio_track_count": 0,
        "video_track_names": [],
        "audio_track_names": [],
        "take_count": len(session.takes),
        "markers": {},
        "takes": [],
    }
    issues: list[Issue] = []
    timeline = _timeline_by_name(project, timeline_name)
    if timeline is None:
        issues.append(
            Issue(
                severity="fail",
                code="color_prep_timeline_missing",
                message=f"color prep timeline {timeline_name!r} is missing",
                context={"timeline": timeline_name},
            )
        )
        return payload, issues

    payload["exists"] = True
    if hasattr(timeline, "GetStartTimecode"):
        payload["start_timecode"] = timeline.GetStartTimecode()
        if payload["start_timecode"] != COLOR_PREP_START_TIMECODE:
            issues.append(
                Issue(
                    severity="fail",
                    code="color_prep_start_timecode_mismatch",
                    message=(
                        f"color prep timeline starts at {payload['start_timecode']!r}, "
                        f"expected {COLOR_PREP_START_TIMECODE!r}; early recordFrame items may not persist"
                    ),
                    context={"expected": COLOR_PREP_START_TIMECODE, "observed": payload["start_timecode"]},
                )
            )
    if hasattr(timeline, "GetStartFrame"):
        try:
            payload["start_frame"] = int(timeline.GetStartFrame())
        except (TypeError, ValueError):
            payload["start_frame"] = None
    video_count = int(timeline.GetTrackCount("video") or 0)
    audio_count = int(timeline.GetTrackCount("audio") or 0)
    payload["video_track_count"] = video_count
    payload["audio_track_count"] = audio_count
    payload["video_track_names"] = [
        str(timeline.GetTrackName("video", index)) if hasattr(timeline, "GetTrackName") else ""
        for index in range(1, video_count + 1)
    ]
    payload["audio_track_names"] = [
        str(timeline.GetTrackName("audio", index)) if hasattr(timeline, "GetTrackName") else ""
        for index in range(1, audio_count + 1)
    ]
    expected_video_track_count = int(payload["expected_video_track_count"])
    if video_count < expected_video_track_count:
        issues.append(
            Issue(
                severity="fail",
                code="color_prep_video_tracks_missing",
                message=f"color prep timeline has {video_count} video tracks, expected at least {expected_video_track_count}",
                context={"expected": expected_video_track_count, "observed": video_count},
            )
        )
    if audio_count < 1:
        issues.append(
            Issue(
                severity="fail",
                code="color_prep_audio_track_missing",
                message="color prep timeline has no audio track",
            )
        )

    for index in range(1, expected_video_track_count + 1):
        expected_name = f"compact-v{index}"
        observed = payload["video_track_names"][index - 1] if index <= len(payload["video_track_names"]) else ""
        if observed and observed != expected_name:
            issues.append(
                Issue(
                    severity="warn",
                    code="color_prep_track_name_mismatch",
                    message=f"video track {index} is named {observed!r}, expected {expected_name!r}",
                    context={"track_type": "video", "track_index": index, "expected": expected_name, "observed": observed},
                )
            )
    if payload["audio_track_names"] and payload["audio_track_names"][0] not in {"", "master-audio"}:
        issues.append(
            Issue(
                severity="warn",
                code="color_prep_track_name_mismatch",
                message=f"audio track 1 is named {payload['audio_track_names'][0]!r}, expected 'master-audio'",
                context={
                    "track_type": "audio",
                    "track_index": 1,
                    "expected": "master-audio",
                    "observed": payload["audio_track_names"][0],
                },
            )
        )

    markers = timeline.GetMarkers() or {}
    payload["markers"] = markers
    marker_frames_by_custom_data = {
        str(marker.get("customData") or ""): int(round(float(frame)))
        for frame, marker in markers.items()
        if isinstance(marker, dict)
    }
    video_items_by_track: dict[int, list[Any]] = {}
    for track_index in range(1, expected_video_track_count + 1):
        video_items_by_track[track_index] = sorted(
            _color_prep_timeline_items(timeline, "video", track_index),
            key=lambda item: _timeline_item_start(item) if _timeline_item_start(item) is not None else -1,
        )
    audio_items = sorted(
        [
            item
            for item in _color_prep_timeline_items(timeline, "audio", 1)
            if _timeline_item_clip_name(item) == "audio-master"
        ],
        key=lambda item: _timeline_item_start(item) if _timeline_item_start(item) is not None else -1,
    )

    angle_order = {angle: index for index, angle in enumerate(angles)}
    for take_ref, take in iter_session_takes(session):
        marker_custom_data = f"{COLOR_PREP_MARKER_PREFIX}{take_ref.id}"
        marker_frame = marker_frames_by_custom_data.get(marker_custom_data)
        ordered_cameras = _ordered_take_cameras(take, angle_order)

        take_payload = {
            "take_id": take_ref.id,
            "marker_frame": marker_frame,
            "marker_exists": marker_frame is not None,
            "angles": [],
            "audio_exists": False,
            "audio_start": None,
        }
        if not take_payload["marker_exists"]:
            issues.append(
                Issue(
                    severity="fail",
                    code="color_prep_take_marker_missing",
                    message=f"{take_ref.id}: color prep take marker is missing",
                    context={"take_id": take_ref.id},
                )
            )

        for track_index in range(1, expected_video_track_count + 1):
            expected = ordered_cameras[track_index - 1] if track_index <= len(ordered_cameras) else None
            if expected is None:
                take_payload["angles"].append({"track_index": track_index, "expected": False, "exists": False})
                continue
            angle = expected.label
            item = video_items_by_track.get(track_index, []).pop(0) if video_items_by_track.get(track_index) else None
            item_start = _timeline_item_start(item) if item is not None else None
            observed_name = _timeline_item_clip_name(item) if item is not None else None
            exists = item is not None and observed_name == angle
            take_payload["angles"].append(
                {
                    "angle": angle,
                    "expected": True,
                    "exists": exists,
                    "track_index": track_index,
                    "start_frame": item_start,
                    "observed_name": observed_name,
                }
            )
            if not exists:
                issues.append(
                    Issue(
                        severity="fail",
                        code="color_prep_angle_item_missing",
                        message=f"{take_ref.id}/{angle}: color prep timeline item is missing",
                        context={
                            "take_id": take_ref.id,
                            "angle": angle,
                            "track_index": track_index,
                            "observed_name": observed_name,
                        },
                    )
                )

        audio_item = audio_items.pop(0) if audio_items else None
        take_payload["audio_exists"] = audio_item is not None
        take_payload["audio_start"] = _timeline_item_start(audio_item) if audio_item is not None else None
        if take_payload["audio_exists"] and marker_frame is not None and take_payload["audio_start"] != marker_frame:
            issues.append(
                Issue(
                    severity="fail",
                    code="color_prep_audio_marker_mismatch",
                    message=f"{take_ref.id}: audio item starts at {take_payload['audio_start']}, marker is at {marker_frame}",
                    context={
                        "take_id": take_ref.id,
                        "audio_start": take_payload["audio_start"],
                        "marker_frame": marker_frame,
                    },
                )
            )
        if not take_payload["audio_exists"]:
            issues.append(
                Issue(
                    severity="fail",
                    code="color_prep_audio_item_missing",
                    message=f"{take_ref.id}: color prep audio item is missing",
                    context={"take_id": take_ref.id, "track_index": 1},
                )
            )
        payload["takes"].append(take_payload)

    return payload, issues


def bootstrap_session(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
    write_reports: bool = True,
    fresh: bool = False,
    rebuild_color_prep: bool = False,
) -> dict[str, Any]:
    connection = connector()
    current_database = _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    if fresh:
        _delete_project_if_exists(connection, project_name=session.resolve.project_name)
    project, created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    if not connection.resolve.OpenPage("media"):
        raise ResolveError("unable to switch Resolve to the Media page before media pool setup")
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    folders = _session_folders(media_pool, root, session)
    settings, setting_mismatches = _apply_project_settings(project, session.timeline)

    take_payloads: list[dict[str, Any]] = []
    for take_ref, take in iter_session_takes(session):
        working_folder = _working_take_folder(media_pool, folders["takes"], take)
        imported_working, skipped_working = _ensure_take_source_clips(media_pool, folders["takes"], working_folder, take)
        synced_video_count, sync_audio_path = _sync_take_clips(project, connection.resolve, working_folder, take)
        take_payloads.append(
            {
                "take_id": take_ref.id,
                "working_imported": imported_working,
                "working_skipped": skipped_working,
                "synced_video_count": synced_video_count,
                "sync_audio": sync_audio_path,
                "sync_retain_embedded_audio": True,
                "expected_audio_streams_after_sync": len(take.camera_files) + 1,
                "audio_layout": "video embedded scratch audio retained, plus audio-master",
                "source_dir": str(take.source_dir),
                "editing_audio": str(take.editing_audio_path()),
            }
        )

    color_prep_payload = _build_color_prep_timeline(
        project=project,
        media_pool=media_pool,
        takes_root=folders["takes"],
        timelines_folder=folders["timelines"],
        session=session,
        rebuild=rebuild_color_prep,
    )

    connection.project_manager.SaveProject()
    snapshot_error: str | None = None
    try:
        snapshot_path = _export_project_snapshot(connection.project_manager, session)
    except ResolveError as exc:
        snapshot_path = None
        snapshot_error = str(exc)

    current_timeline = project.GetCurrentTimeline()

    # Bin drift detection. Inspection-only — never delete anything here
    # because user-created Multicam clips and custom timelines may live in
    # legacy bins and destructive cleanup would lose work. Operators who
    # want a clean slate can run with --fresh.
    known_bin_names = {session.resolve.takes_bin, session.resolve.timelines_bin}
    stale_top_level_bins = sorted(
        folder.GetName()
        for folder in (root.GetSubFolderList() or [])
        if folder.GetName() not in known_bin_names
    )
    stale_top_level_items = sorted(
        clip.GetName() for clip in (root.GetClipList() or [])
    )

    # Timelines living outside the Timelines/ bin are also drift.
    timelines_folder = folders["timelines"]
    expected_timeline_names = {
        clip.GetName() for clip in (timelines_folder.GetClipList() or [])
    }
    stray_timelines: list[str] = []
    try:
        timeline_count = int(project.GetTimelineCount() or 0)
    except (TypeError, ValueError):
        timeline_count = 0
    for index in range(1, timeline_count + 1):
        timeline = project.GetTimelineByIndex(index)
        if timeline is None:
            continue
        name = timeline.GetName()
        if name not in expected_timeline_names:
            stray_timelines.append(name)
    stray_timelines.sort()

    # Translate mismatches and stale lists into stage-level Issue records.
    issues: list[Issue] = []
    for mismatch in setting_mismatches:
        if mismatch.get("key") == "timelinePlaybackFrameRate":
            issues.append(
                Issue(
                    severity="warn",
                    code="timeline_playback_framerate_mismatch",
                    message=(
                        f"timelinePlaybackFrameRate is {mismatch['observed']!r} "
                        f"(expected {mismatch['requested']}); Resolve's scripting "
                        f"API cannot change this setting. Fix before editorial/export: File → "
                        f"Project Settings (Shift+9) → Master Settings → under "
                        f"'Timeline Format', change 'Playback frame rate' to "
                        f"match the timeline frame rate, then Save."
                    ),
                    context=dict(mismatch),
                )
            )
            continue
        issues.append(
            Issue(
                severity="warn",
                code="resolve_setting_drift",
                message=(
                    f"{mismatch['key']} drifted to {mismatch['observed']!r} "
                    f"(requested {mismatch['requested']})"
                ),
                context=dict(mismatch),
            )
        )
    for bin_name in stale_top_level_bins:
        issues.append(
            Issue(
                severity="warn",
                code="stale_top_level_bin",
                message=f"root-level media pool bin {bin_name!r} is not expected",
                context={"bin": bin_name},
            )
        )
    for item_name in stale_top_level_items:
        issues.append(
            Issue(
                severity="warn",
                code="stale_top_level_item",
                message=f"root-level media pool item {item_name!r} is not expected",
                context={"item": item_name},
            )
        )
    for timeline_name in stray_timelines:
        issues.append(
            Issue(
                severity="warn",
                code="stray_timeline",
                message=(
                    f"timeline {timeline_name!r} lives outside the "
                    f"{session.resolve.timelines_bin} bin"
                ),
                context={"timeline": timeline_name},
            )
        )
    for issue in color_prep_payload.get("issues", []) or []:
        issues.append(Issue(**issue))

    status = "FAIL" if any(issue.severity == "fail" for issue in issues) else ("WARN" if issues else "PASS")
    summary = (
        f"Resolve prepared for {session.resolve.project_name}"
        if status == "PASS"
        else f"Resolve prepared for {session.resolve.project_name} with {len(issues)} issue(s)"
    )

    payload = {
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
        "project_action": "created" if created else "loaded",
        "settings": settings,
        "takes": take_payloads,
        "color_prep": color_prep_payload,
        "current_timeline": current_timeline.GetName() if current_timeline else None,
        "top_level_bins": [session.resolve.takes_bin, session.resolve.timelines_bin],
        "stale_top_level_bins": stale_top_level_bins,
        "stale_top_level_items": stale_top_level_items,
        "stray_timelines": stray_timelines,
        "status": status,
        "issues": issues_to_dict(issues),
        "summary": summary,
    }
    if write_reports:
        write_json_report(session.reports_path("bootstrap-session.json"), payload)
        write_markdown_report(
            session.reports_path("bootstrap-session.md"),
            title="Resolve Session Bootstrap",
            status=status,
            issues=issues,
            sections={
                "Project": {
                    "project_name": session.resolve.project_name,
                    "project_library": current_database.get("DbName", "current"),
                    "project_snapshot": str(snapshot_path) if snapshot_path is not None else "unavailable",
                    "current_timeline": payload["current_timeline"] or "unset",
                },
                "Top-Level Bins": payload["top_level_bins"],
                "Stale Top-Level Bins": stale_top_level_bins or ["(none)"],
                "Stale Top-Level Items": stale_top_level_items or ["(none)"],
                "Stray Timelines": stray_timelines or ["(none)"],
                "Takes": [f"{item['take_id']}: imported {len(item['working_imported'])}" for item in take_payloads],
            },
        )
    return payload


def inspect_resolve_session(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
    write_reports: bool = True,
) -> dict[str, Any]:
    connection = connector()
    current_database = _select_project_library(connection, session)
    project = connection.project_manager.LoadProject(session.resolve.project_name)

    snapshot_path = session.resolve_snapshot_path()
    payload: dict[str, Any] = {
        "session_id": session.session_id,
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "current_page": connection.resolve.GetCurrentPage(),
        "project_exists": bool(project),
        "project_loaded_name": project.GetName() if project else None,
        "project_snapshot": str(snapshot_path),
        "project_snapshot_exists": snapshot_path.is_file(),
        "project_snapshot_size_bytes": snapshot_path.stat().st_size if snapshot_path.is_file() else 0,
        "settings": {},
        "expected_top_level_bins": [session.resolve.takes_bin, session.resolve.timelines_bin],
        "top_level_bins": [],
        "missing_top_level_bins": [],
        "stale_top_level_bins": [],
        "take_bins": [],
        "timeline_names": [],
        "color_prep_timeline": {},
        "current_timeline": None,
    }

    issues: list[Issue] = []
    if project is None:
        issues.append(
            Issue(
                severity="fail",
                code="resolve_project_missing",
                message=f"Resolve project {session.resolve.project_name!r} was not found",
                context={"project_name": session.resolve.project_name},
            )
        )
    else:
        media_pool = project.GetMediaPool()
        root = media_pool.GetRootFolder()
        expected_frame_rate = str(session.timeline.frame_rate)
        settings = {
            "timelineFrameRate": str(project.GetSetting("timelineFrameRate")),
            "timelinePlaybackFrameRate": str(project.GetSetting("timelinePlaybackFrameRate")),
            "timelineDropFrameTimecode": str(project.GetSetting("timelineDropFrameTimecode")),
            "videoMonitorFormat": str(project.GetSetting("videoMonitorFormat")),
            **_read_project_color_settings(project),
        }
        payload["settings"] = settings
        if settings["timelineFrameRate"] != expected_frame_rate:
            issues.append(
                Issue(
                    severity="fail",
                    code="timeline_framerate_mismatch",
                    message=(
                        f"timelineFrameRate is {settings['timelineFrameRate']!r} "
                        f"(expected {expected_frame_rate!r})"
                    ),
                    context={"key": "timelineFrameRate", "expected": expected_frame_rate, "observed": settings["timelineFrameRate"]},
                )
            )
        if settings["timelinePlaybackFrameRate"] != expected_frame_rate:
            issues.append(
                Issue(
                    severity="warn",
                    code="timeline_playback_framerate_mismatch",
                    message=(
                        f"timelinePlaybackFrameRate is {settings['timelinePlaybackFrameRate']!r} "
                        f"(expected {expected_frame_rate!r}). Resolve scripting cannot set this; "
                        f"fix it in Project Settings before editorial/export."
                    ),
                    context={
                        "key": "timelinePlaybackFrameRate",
                        "expected": expected_frame_rate,
                        "observed": settings["timelinePlaybackFrameRate"],
                        "manual_fix": "File -> Project Settings -> Master Settings -> Timeline Format -> Playback frame rate",
                    },
                )
            )
        expected_color_settings = _expected_project_color_settings(session.timeline)
        for key, expected in expected_color_settings.items():
            observed = settings.get(key)
            if observed != expected:
                issues.append(
                    Issue(
                        severity="fail",
                        code="resolve_color_management_mismatch",
                        message=f"{key} is {observed!r} (expected {expected!r})",
                        context={"key": key, "expected": expected, "observed": observed},
                    )
                )
        top_level = {folder.GetName(): folder for folder in (root.GetSubFolderList() or [])}
        expected_bins = {session.resolve.takes_bin, session.resolve.timelines_bin}
        payload["top_level_bins"] = sorted(top_level)
        payload["missing_top_level_bins"] = sorted(expected_bins - set(top_level))
        payload["stale_top_level_bins"] = sorted(set(top_level) - expected_bins)

        for bin_name in payload["missing_top_level_bins"]:
            issues.append(
                Issue(
                    severity="fail",
                    code="resolve_bin_missing",
                    message=f"Resolve media pool bin {bin_name!r} is missing",
                    context={"bin": bin_name},
                )
            )
        for bin_name in payload["stale_top_level_bins"]:
            issues.append(
                Issue(
                    severity="warn",
                    code="stale_top_level_bin",
                    message=f"root-level media pool bin {bin_name!r} is not expected",
                    context={"bin": bin_name},
                )
            )

        takes_root = top_level.get(session.resolve.takes_bin)
        take_folders = {folder.GetName(): folder for folder in (takes_root.GetSubFolderList() or [])} if takes_root else {}
        for take_ref, take in iter_session_takes(session):
            expected_clips = sorted([camera.label for camera in take.camera_files] + ["audio-master"])
            folder = take_folders.get(take_ref.id)
            actual_clips = sorted(clip.GetName() for clip in (folder.GetClipList() or [])) if folder else []
            missing_clips = [name for name in expected_clips if name not in actual_clips]
            extra_clips = [name for name in actual_clips if name not in expected_clips]
            take_payload = {
                "take_id": take_ref.id,
                "bin_exists": folder is not None,
                "expected_clips": expected_clips,
                "clips": actual_clips,
                "missing_clips": missing_clips,
                "extra_clips": extra_clips,
                "expected_audio_streams_after_sync": len(take.camera_files) + 1,
                "audio_layout": "video embedded scratch audio retained, plus audio-master",
            }
            payload["take_bins"].append(take_payload)
            if folder is None:
                issues.append(
                    Issue(
                        severity="fail",
                        code="take_bin_missing",
                        message=f"{take_ref.id}: Resolve take bin is missing",
                        context={"take_id": take_ref.id},
                    )
                )
            elif missing_clips:
                issues.append(
                    Issue(
                        severity="fail",
                        code="take_clip_missing",
                        message=f"{take_ref.id}: missing Resolve clip(s): {', '.join(missing_clips)}",
                        context={"take_id": take_ref.id, "missing_clips": missing_clips},
                    )
                )
            if extra_clips:
                issues.append(
                    Issue(
                        severity="warn",
                        code="take_extra_clip",
                        message=f"{take_ref.id}: unexpected Resolve clip(s): {', '.join(extra_clips)}",
                        context={"take_id": take_ref.id, "extra_clips": extra_clips},
                    )
                )

        try:
            timeline_count = int(project.GetTimelineCount() or 0)
        except (TypeError, ValueError):
            timeline_count = 0
        timeline_names = []
        for index in range(1, timeline_count + 1):
            timeline = project.GetTimelineByIndex(index)
            if timeline is not None:
                timeline_names.append(timeline.GetName())
        payload["timeline_names"] = sorted(timeline_names)
        current_timeline = project.GetCurrentTimeline()
        payload["current_timeline"] = current_timeline.GetName() if current_timeline else None
        color_prep_payload, color_prep_issues = _inspect_color_prep_timeline(project, session)
        payload["color_prep_timeline"] = color_prep_payload
        issues.extend(color_prep_issues)

    if not payload["project_snapshot_exists"]:
        issues.append(
            Issue(
                severity="warn",
                code="project_snapshot_missing",
                message=f"Resolve project snapshot was not found at {snapshot_path}",
                context={"project_snapshot": str(snapshot_path)},
            )
        )

    status = "FAIL" if any(issue.severity == "fail" for issue in issues) else ("WARN" if issues else "PASS")
    payload["status"] = status
    payload["issues"] = issues_to_dict(issues)
    payload["summary"] = (
        f"Resolve project {session.resolve.project_name} inspection passed"
        if status == "PASS"
        else f"Resolve project {session.resolve.project_name} inspection found {len(issues)} issue(s)"
    )
    if write_reports:
        write_json_report(session.reports_path("inspect-resolve-session.json"), payload)
    return payload


def verify_resolve_session_after_reload(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
) -> dict[str, Any]:
    connection = connector()
    current_project = connection.project_manager.GetCurrentProject()
    closed_project = False
    if current_project is not None:
        connection.project_manager.SaveProject()
        if current_project.GetName() == session.resolve.project_name:
            closed_project = bool(connection.project_manager.CloseProject(current_project))
            if not closed_project:
                return {
                    "status": "FAIL",
                    "summary": f"unable to close Resolve project {session.resolve.project_name} for reload verification",
                    "closed_project": False,
                    "reloaded_project": False,
                    "issues": [
                        {
                            "severity": "fail",
                            "code": "post_reload_close_failed",
                            "message": f"unable to close Resolve project {session.resolve.project_name}",
                            "context": {"project_name": session.resolve.project_name},
                        }
                    ],
                }

    reloaded = connection.project_manager.LoadProject(session.resolve.project_name)
    if not reloaded:
        return {
            "status": "FAIL",
            "summary": f"unable to reload Resolve project {session.resolve.project_name}",
            "closed_project": closed_project,
            "reloaded_project": False,
            "issues": [
                {
                    "severity": "fail",
                    "code": "post_reload_load_failed",
                    "message": f"unable to reload Resolve project {session.resolve.project_name}",
                    "context": {"project_name": session.resolve.project_name},
                }
            ],
        }

    inspection = inspect_resolve_session(session, connector=lambda: connection, write_reports=False)
    color_prep = inspection.get("color_prep_timeline") or {}
    payload = {
        "status": inspection.get("status", "FAIL"),
        "summary": (
            f"post-reload verification passed for {session.resolve.project_name}"
            if inspection.get("status") == "PASS"
            else f"post-reload verification found {len(inspection.get('issues') or [])} issue(s)"
        ),
        "closed_project": closed_project,
        "reloaded_project": True,
        "project_loaded_name": inspection.get("project_loaded_name"),
        "color_prep_timeline": color_prep,
        "issues": inspection.get("issues", []),
    }
    connection.project_manager.SaveProject()
    return payload


CALIBRATION_VERSION_NAME = "piano-guard-calibration"
"""Named remote color version created on every graded source MediaPoolItem.

Resolve's default Version 1 is local-scoped by default (timeline-item
grades don't persist when the timeline is deleted). Creating a named
remote version via AddVersion(name, 1) + LoadVersionByName(name, 1) and
applying SetCDL there makes the grade live on the MediaPoolItem. Operators
can switch back to Version 1 in the Color page's versions panel to see
the ungraded source at any time."""


def _find_clip_by_file_path(root: Any, absolute_path: Path) -> Any | None:
    """Walk the media pool recursively for a clip whose File Path matches."""
    target = _normalize_media_path(absolute_path)
    for clip in _iter_clips_recursive(root):
        fp = _clip_file_path(clip)
        if fp and _normalize_media_path(fp) == target:
            return clip
    return None


def _multicam_clips_present(root: Any, prefix: str = "MC_") -> list[str]:
    """Return names of any media-pool clips whose name starts with `prefix`.

    Default prefix is `MC_` (not `MC_take-`) to match the broader naming
    convention used elsewhere in the codebase (see `_create_multicam_clip_ui`).
    Take IDs are not guaranteed to start with `take-` — operators may use
    `piece-A`, `movement-1`, etc., and the multicam clip becomes `MC_<take_id>`
    following `create_multicam_clips`.
    """
    present: list[str] = []
    for clip in _iter_clips_recursive(root):
        name = clip.GetName()
        if name and name.startswith(prefix):
            present.append(name)
    return sorted(present)


def _resolve_session_clip(session: SessionProjectConfig, clip_id: str) -> tuple[TakeConfig, Any, Path]:
    if "/" not in clip_id:
        raise ResolveError(f"clip_id {clip_id!r} must be 'take-id/angle-label' format")
    take_id, angle = clip_id.split("/", 1)
    for take_ref, take in iter_session_takes(session):
        if take_ref.id != take_id:
            continue
        for camera in take.camera_files:
            if camera.label == angle:
                return take, camera, take.resolve_path(camera.file)
    raise ResolveError(f"clip_id {clip_id!r} not found in session")


def apply_manual_cdl(
    session: SessionProjectConfig,
    *,
    clip_id: str,
    slope: tuple[float, float, float],
    offset: tuple[float, float, float],
    connector=connect_to_resolve,
) -> dict[str, Any]:
    """Apply one AI-reviewed CDL to one source clip's remote version."""
    connection = connector()
    _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )

    color_mismatches = _project_color_setting_mismatches(project, session.timeline)
    if color_mismatches:
        summary = (
            f"manual-cdl refuses to run: Resolve project color management does not match "
            f"the required Sony PP10 HLG -> SDR Rec.709 path. Run "
            f"`prepare-resolve-session` again before applying {MANUAL_CDL_VERSION_NAME}."
        )
        return {
            "status": "FAIL",
            "clip_id": clip_id,
            "version_name": MANUAL_CDL_VERSION_NAME,
            "summary": summary,
            "settings": _read_project_color_settings(project),
            "issues": [
                {
                    "severity": "fail",
                    "code": "resolve_color_management_mismatch",
                    "message": (
                        f"{mismatch['key']} is {mismatch['observed']!r} "
                        f"(expected {mismatch['expected']!r})"
                    ),
                    "context": mismatch,
                }
                for mismatch in color_mismatches
            ],
            "applied": [],
            "skipped": [{"clip_id": clip_id, "reason": "resolve_color_management_mismatch"}],
        }

    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()

    existing_multicam = _multicam_clips_present(root)
    if existing_multicam:
        summary = (
            f"manual-cdl refuses to run: {len(existing_multicam)} multicam clip(s) "
            f"already exist ({', '.join(existing_multicam)}). Run color review before "
            "creating multicam clips."
        )
        return {
            "status": "FAIL",
            "clip_id": clip_id,
            "version_name": MANUAL_CDL_VERSION_NAME,
            "summary": summary,
            "issues": [
                {
                    "severity": "fail",
                    "code": "multicam_exists",
                    "message": summary,
                    "context": {"multicam_clips": existing_multicam},
                }
            ],
            "applied": [],
            "skipped": [{"clip_id": clip_id, "reason": "multicam_exists"}],
        }

    _take, _camera, absolute_path = _resolve_session_clip(session, clip_id)
    clip = _find_clip_by_file_path(root, absolute_path)
    if clip is None:
        summary = f"{clip_id}: source clip not found in media pool: {absolute_path}"
        return {
            "status": "FAIL",
            "clip_id": clip_id,
            "version_name": MANUAL_CDL_VERSION_NAME,
            "summary": summary,
            "issues": [
                {
                    "severity": "fail",
                    "code": "clip_not_found",
                    "message": summary,
                    "context": {"clip_id": clip_id, "path": str(absolute_path)},
                }
            ],
            "applied": [],
            "skipped": [{"clip_id": clip_id, "reason": "clip_not_found", "path": str(absolute_path)}],
        }

    folders = _session_folders(media_pool, root, session)
    media_pool.SetCurrentFolder(folders["timelines"])
    scratch_name = "_pg_manual_cdl_scratch"
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        timeline = project.GetTimelineByIndex(i)
        if timeline is not None and timeline.GetName() == scratch_name:
            media_pool.DeleteTimelines([timeline])
            break
    scratch = media_pool.CreateEmptyTimeline(scratch_name)
    if scratch is None:
        raise ResolveError(f"unable to create scratch timeline {scratch_name}")
    project.SetCurrentTimeline(scratch)

    try:
        appended = media_pool.AppendToTimeline([clip])
        if not appended:
            raise ResolveError(f"failed to append {clip_id} to scratch timeline")
        vti = appended[0]

        add_rc = vti.AddVersion(MANUAL_CDL_VERSION_NAME, 1)
        load_rc = vti.LoadVersionByName(MANUAL_CDL_VERSION_NAME, 1)
        current = vti.GetCurrentVersion() or {}
        current_name = current.get("versionName") if isinstance(current, dict) else None
        current_type = current.get("versionType") if isinstance(current, dict) else None
        if current_name != MANUAL_CDL_VERSION_NAME or current_type != 1:
            summary = (
                f"{clip_id}: expected current remote version {MANUAL_CDL_VERSION_NAME!r}, "
                f"got {current_name!r} type {current_type!r}"
            )
            return {
                "status": "FAIL",
                "clip_id": clip_id,
                "version_name": MANUAL_CDL_VERSION_NAME,
                "summary": summary,
                "issues": [
                    {
                        "severity": "fail",
                        "code": "remote_version_load_failed",
                        "message": summary,
                        "context": {"add_rc": bool(add_rc), "load_rc": bool(load_rc), "current_version": current},
                    }
                ],
                "applied": [],
                "skipped": [{"clip_id": clip_id, "reason": "remote_version_load_failed"}],
            }

        setcdl_dict = {
            "NodeIndex": "1",
            "Slope": f"{slope[0]} {slope[1]} {slope[2]}",
            "Offset": f"{offset[0]} {offset[1]} {offset[2]}",
            "Power": "1 1 1",
            "Saturation": "1",
        }
        setcdl_rc = vti.SetCDL(setcdl_dict)

        tools_present: list[str] | None = None
        try:
            graph = vti.GetNodeGraph(1)
            tools_present = list(graph.GetToolsInNode(1) or [])
        except Exception:
            tools_present = None

        issue_payloads: list[dict[str, Any]] = []
        status = "PASS"
        if not bool(setcdl_rc):
            status = "WARN"
            issue_payloads.append(
                {
                    "severity": "warn",
                    "code": "setcdl_false",
                    "message": f"{clip_id}: SetCDL returned {setcdl_rc!r}",
                    "context": {"clip_id": clip_id},
                }
            )

        connection.project_manager.SaveProject()
        snapshot_error: str | None = None
        try:
            snapshot_path = _export_project_snapshot(connection.project_manager, session)
        except ResolveError as exc:
            snapshot_path = None
            snapshot_error = str(exc)

        return {
            "status": status,
            "clip_id": clip_id,
            "version_name": MANUAL_CDL_VERSION_NAME,
            "slope": list(slope),
            "offset": list(offset),
            "setcdl_rc": bool(setcdl_rc),
            "tools_present": tools_present,
            "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
            "project_snapshot_warning": snapshot_error,
            "issues": issue_payloads,
            "applied": [
                {
                    "clip_id": clip_id,
                    "slope": list(slope),
                    "offset": list(offset),
                    "version_name": MANUAL_CDL_VERSION_NAME,
                    "setcdl_rc": bool(setcdl_rc),
                    "tools_present": tools_present,
                }
            ],
            "skipped": [],
            "summary": f"applied manual CDL to {clip_id} via {MANUAL_CDL_VERSION_NAME}",
        }
    finally:
        try:
            media_pool.DeleteTimelines([scratch])
        except Exception:
            pass
