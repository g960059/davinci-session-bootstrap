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

from piano_guard.config import SessionProjectConfig, TakeConfig, TimelineConfig, iter_session_takes
from piano_guard.reports import Issue, issues_to_dict, write_json_report, write_markdown_report


DEFAULT_RESOLVE_SCRIPT_API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
DEFAULT_RESOLVE_SCRIPT_LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
DEFAULT_RESOLVE_DBLIST_CONF = Path.home() / "Library/Preferences/Blackmagic Design/DaVinci Resolve/dblist.conf"


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

    **Color management model (2026-04-15 rewrite):**

    piano-guard targets Resolve's "DaVinci YRGB Color Managed + Automatic +
    SDR Rec.709" preset. Under this preset the operating space at Node 1
    is **Rec.709 (Scene)** = scene-linear BT.709 — which matches
    ``color_match.to_linear_bt709`` exactly, so ``TimelineItem.SetCDL``
    with piano-guard's fitted slope/offset lands on the right signal.

    The prior implementation forced DRCM v2 Custom with
    ``colorSpaceTimeline=DaVinci WG/Intermediate`` which put Node 1 in a
    different primaries + log-ish encoding, silently miscomputing every
    CDL (visible as pink/magenta casts and blown highlights). Fix verified
    live: after switching the project to Automatic SDR Rec.709 the
    previously-computed CDLs render correctly (see
    ``project_resolve_color_management_choice.md`` memory).

    Which keys actually drive Automatic SDR Rec.709:
      * ``colorScienceMode = davinciYRGBColorManagedv2`` (same as before —
        "Automatic" is a flag on top of YRGB Color Managed, not a
        separate science mode)
      * ``isAutoColorManage = 1`` (the toggle — was ``"0"`` in Custom mode)
      * ``rcmPresetMode = SDR`` (SDR grading env — drives Timeline to
        ``Rec.709 (Scene)``)
      * ``separateColorSpaceAndGamma = 0`` (Automatic hides this toggle)
      * ``inputDRT / outputDRT / useInverseDRT`` persist across Automatic
        toggles and still govern HLG → Rec.709 tone mapping.

    Keys we deliberately DO NOT write under Automatic:
      * ``colorSpaceInput`` / ``colorSpaceTimeline`` / ``colorSpaceOutput``
        — Resolve overrides all three to ``Rec.709 (Scene)`` regardless
        of what we pass when Automatic is on. Writing them here has no
        effect but leaves misleading log entries.

    ``timeline.input_color_space`` / ``timeline.timeline_color_space`` /
    ``timeline.output_color_space`` from the session config are retained
    as informational metadata but not applied to the project — the
    preset chooses them. Future work: emit a stage warn if the
    session config disagrees with the preset's implied values.
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
        # Automatic Color Management ON — Resolve derives Input/Timeline/
        # Output color spaces from rcmPresetMode below rather than from
        # explicit colorSpace* writes.
        "isAutoColorManage": _set_setting(
            project, "isAutoColorManage", ["1"], required=False, mismatches=mismatches
        ),
        "separateColorSpaceAndGamma": _set_setting(
            project,
            "separateColorSpaceAndGamma",
            ["0"],
            required=False,
            mismatches=mismatches,
        ),
        # Preset: SDR grading environment → Timeline = Rec.709 (Scene),
        # Output = Rec.709 (Scene). Matches piano-guard's BT.709 linear
        # CDL fit space so SetCDL lands correctly at Node 1.
        "rcmPresetMode": _set_setting(
            project, "rcmPresetMode", ["SDR"], required=False, mismatches=mismatches
        ),
        # DRT keys persist across Automatic mode toggles and still govern
        # the HLG → Rec.709 tone mapping regardless of preset. DaVinci /
        # DaVinci / 1 retains the DaVinci Intelligent Tone Map path.
        "inputDRT": _set_setting(
            project, "inputDRT", ["DaVinci"], required=False, mismatches=mismatches
        ),
        "outputDRT": _set_setting(
            project, "outputDRT", ["DaVinci"], required=False, mismatches=mismatches
        ),
        "useInverseDRT": _set_setting(
            project, "useInverseDRT", ["1"], required=False, mismatches=mismatches
        ),
    }

    # Read back the three colorSpace* fields that Automatic mode is
    # supposed to have auto-driven to "Rec.709 (Scene)". If any value
    # disagrees, surface a mismatch so the operator can diagnose (most
    # likely cause: a stale Custom-mode project that Automatic hasn't
    # fully reset). We OBSERVE but do not SET — writing these under
    # Automatic no-ops or re-introduces Custom mode on some Resolve
    # builds.
    for key in ("colorSpaceInput", "colorSpaceTimeline", "colorSpaceOutput"):
        observed = str(project.GetSetting(key))
        settings[key] = observed
        if observed and observed != "Rec.709 (Scene)":
            mismatches.append(
                {
                    "key": key,
                    "requested": ["Rec.709 (Scene)"],
                    "observed": observed,
                    "set_setting_rc": "not-written (Automatic mode)",
                }
            )

    # Informational: record the current RCM preset mode (not SetSet-managed).
    # Piano-guard drives individual color-space fields rather than binding the
    # project to a preset, so this value typically reads as "Custom" after
    # bootstrap. Captured here so the research/debug workflow can diff it
    # without running a separate probe.
    settings["observed_rcm_preset_mode"] = str(project.GetSetting("rcmPresetMode"))

    return settings, mismatches


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
        resolve.AUDIO_SYNC_RETAIN_EMBEDDED_AUDIO: False,
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


def bootstrap_session(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
    write_reports: bool = True,
    fresh: bool = False,
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
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    folders = _session_folders(media_pool, root, session)
    settings, setting_mismatches = _apply_project_settings(project, session.timeline)

    take_payloads: list[dict[str, Any]] = []
    for take_ref, take in iter_session_takes(session):
        working_folder = _working_take_folder(media_pool, folders["takes"], take)
        imported_working, skipped_working = _ensure_take_source_clips(media_pool, folders["takes"], working_folder, take)
        take_payloads.append(
            {
                "take_id": take_ref.id,
                "working_imported": imported_working,
                "working_skipped": skipped_working,
                "source_dir": str(take.source_dir),
                "editing_audio": str(take.editing_audio_path()),
            }
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
        # timelinePlaybackFrameRate is effectively read-only via the Resolve
        # scripting API (SetSetting returns False regardless of the candidate
        # string). The operator must adjust it via the UI. Elevate the
        # warning into an actionable instruction rather than a generic
        # "drifted" message so the fix path is obvious in the report.
        if mismatch.get("key") == "timelinePlaybackFrameRate":
            issues.append(
                Issue(
                    severity="warn",
                    code="timeline_playback_framerate_locked",
                    message=(
                        f"timelinePlaybackFrameRate is {mismatch['observed']!r} "
                        f"(expected {mismatch['requested']}); Resolve's scripting "
                        f"API cannot change this setting. MANUAL FIX: File → "
                        f"Project Settings (Shift+9) → Master Settings → under "
                        f"'Timeline Format', change 'Playback frame rate' to "
                        f"match the timeline frame rate, then Save. This "
                        f"setting is project-scoped, so once fixed it persists "
                        f"across re-runs."
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

    status = "WARN" if issues else "PASS"
    summary = (
        f"Resolve prepared for {session.resolve.project_name}"
        if status == "PASS"
        else f"Resolve prepared for {session.resolve.project_name} with {len(issues)} warning(s)"
    )

    payload = {
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
        "project_action": "created" if created else "loaded",
        "settings": settings,
        "takes": take_payloads,
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


def apply_color_normalization(
    session: SessionProjectConfig,
    *,
    cdl_by_take_and_angle: dict[str, dict[str, dict[str, Any]]],
    reference_angle: str,
    connector=connect_to_resolve,
    write_reports: bool = True,
) -> dict[str, Any]:
    """Apply per-angle CDL via a scratch timeline + named remote version.

    `cdl_by_take_and_angle[take_id][angle]` is a dict with keys
    `slope`, `offset`, `power` (each a (r, g, b) tuple), `status`
    ("pass"/"warn"/"fail"), and `code`. Angles with `status == "fail"` are
    skipped. The reference_angle is never graded (identity transform).

    Phase 0 probe findings drive the version-handling strategy: SetCDL on
    the default Version 1 does NOT persist across timeline deletion, but
    creating a named remote version via AddVersion(name, 1) + LoadVersionByName
    attaches the grade to the source MediaPoolItem so multicam angle
    sub-clips inherit it.

    Returns a payload with status, issues, summary, and per-angle outcomes.
    """
    connection = connector()
    _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()

    issues: list[Issue] = []

    # Order-of-operations guard: fail hard if multicam clips already exist.
    existing_multicam = _multicam_clips_present(root)
    if existing_multicam:
        summary = (
            f"apply-color-normalization refuses to run: {len(existing_multicam)} multicam "
            f"clip(s) already exist ({', '.join(existing_multicam)}). Re-run this step "
            f"before creating multicam clips, or delete the existing multicam clips and "
            f"rebuild them after applying CDL."
        )
        issues.append(
            Issue(
                severity="fail",
                code="multicam_exists",
                message=summary,
                context={"multicam_clips": existing_multicam},
            )
        )
        payload = {
            "project_name": session.resolve.project_name,
            "status": "FAIL",
            "issues": issues_to_dict(issues),
            "summary": summary,
            "applied": [],
            "skipped": [],
        }
        if write_reports:
            write_json_report(
                session.reports_path("apply-color-normalization.json"), payload
            )
        return payload

    # Ensure Timelines bin exists and create scratch timeline inside it.
    folders = _session_folders(media_pool, root, session)
    timelines_folder = folders["timelines"]
    media_pool.SetCurrentFolder(timelines_folder)
    # Remove any prior scratch timeline from a previous run
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName() == "_pg_calibration_scratch":
            media_pool.DeleteTimelines([tl])
            break
    scratch = media_pool.CreateEmptyTimeline("_pg_calibration_scratch")
    if scratch is None:
        summary = "unable to create scratch timeline _pg_calibration_scratch"
        issues.append(Issue(severity="fail", code="scratch_timeline_create_failed", message=summary))
        payload = {
            "project_name": session.resolve.project_name,
            "status": "FAIL",
            "issues": issues_to_dict(issues),
            "summary": summary,
            "applied": [],
            "skipped": [],
        }
        if write_reports:
            write_json_report(
                session.reports_path("apply-color-normalization.json"), payload
            )
        return payload

    project.SetCurrentTimeline(scratch)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for take_ref, take in iter_session_takes(session):
        take_cdls = cdl_by_take_and_angle.get(take_ref.id, {})
        for camera in take.camera_files:
            angle = camera.label
            if angle == reference_angle:
                continue
            cdl = take_cdls.get(angle)
            if cdl is None:
                skipped.append(
                    {"take_id": take_ref.id, "angle": angle, "reason": "no_cdl"}
                )
                continue
            if cdl.get("status") == "fail":
                skipped.append(
                    {
                        "take_id": take_ref.id,
                        "angle": angle,
                        "reason": "cdl_fail",
                        "code": cdl.get("code"),
                    }
                )
                issues.append(
                    Issue(
                        severity="warn",
                        code="cdl_skipped",
                        message=(
                            f"{take_ref.id}/{angle} CDL skipped "
                            f"(status=fail, code={cdl.get('code')})"
                        ),
                        context={"take_id": take_ref.id, "angle": angle},
                    )
                )
                continue

            # Find the source clip in the media pool
            absolute_path = take.resolve_path(camera.file)
            clip = _find_clip_by_file_path(root, absolute_path)
            if clip is None:
                skipped.append(
                    {
                        "take_id": take_ref.id,
                        "angle": angle,
                        "reason": "clip_not_found",
                        "path": str(absolute_path),
                    }
                )
                issues.append(
                    Issue(
                        severity="warn",
                        code="source_clip_missing",
                        message=(
                            f"{take_ref.id}/{angle} source clip not found in media pool: "
                            f"{absolute_path}"
                        ),
                    )
                )
                continue

            # Append to scratch timeline
            appended = media_pool.AppendToTimeline([clip])
            if not appended:
                issues.append(
                    Issue(
                        severity="warn",
                        code="append_failed",
                        message=f"{take_ref.id}/{angle} failed to append to scratch timeline",
                    )
                )
                skipped.append(
                    {"take_id": take_ref.id, "angle": angle, "reason": "append_failed"}
                )
                continue
            vti = appended[0]

            # Create + load the named remote version (Phase 0 Outcome B).
            # If either call silently fails, SetCDL lands on the default
            # Version 1 instead of our named version, which does NOT persist
            # across timeline deletion or into multicam angle sub-clips.
            # Verify `GetCurrentVersion` after the load and FAIL if the
            # expected version isn't active.
            add_rc = vti.AddVersion(CALIBRATION_VERSION_NAME, 1)
            load_rc = vti.LoadVersionByName(CALIBRATION_VERSION_NAME, 1)
            current = vti.GetCurrentVersion() or {}
            current_name = current.get("versionName") if isinstance(current, dict) else None
            current_type = current.get("versionType") if isinstance(current, dict) else None
            if current_name != CALIBRATION_VERSION_NAME or current_type != 1:
                issues.append(
                    Issue(
                        severity="warn",
                        code="remote_version_load_failed",
                        message=(
                            f"{take_ref.id}/{angle} expected current version "
                            f"{CALIBRATION_VERSION_NAME!r} (remote) after AddVersion+LoadVersionByName "
                            f"but got {current_name!r} (type {current_type}). "
                            f"CDL will NOT persist into multicam angle sub-clips."
                        ),
                        context={
                            "take_id": take_ref.id,
                            "angle": angle,
                            "add_rc": bool(add_rc),
                            "load_rc": bool(load_rc),
                            "current_version": {"name": current_name, "type": current_type},
                        },
                    )
                )
                skipped.append(
                    {
                        "take_id": take_ref.id,
                        "angle": angle,
                        "reason": "remote_version_load_failed",
                    }
                )
                continue

            slope = cdl["slope"]
            offset = cdl["offset"]
            power = cdl.get("power", (1.0, 1.0, 1.0))
            setcdl_dict = {
                "NodeIndex": "1",
                "Slope": f"{slope[0]} {slope[1]} {slope[2]}",
                "Offset": f"{offset[0]} {offset[1]} {offset[2]}",
                "Power": f"{power[0]} {power[1]} {power[2]}",
                "Saturation": "1",
            }
            setcdl_rc = vti.SetCDL(setcdl_dict)

            # Read back: verify the CDL tool was added to node 1
            tools_present: list[str] | None = None
            try:
                graph = vti.GetNodeGraph(1)
                tools = graph.GetToolsInNode(1) or []
                tools_present = list(tools)
            except Exception:
                tools_present = None

            cdl_applied = bool(setcdl_rc) and (
                tools_present is not None and "Primary Balance" in tools_present
            )
            if not cdl_applied:
                issues.append(
                    Issue(
                        severity="warn",
                        code="setcdl_unverified",
                        message=(
                            f"{take_ref.id}/{angle} SetCDL rc={setcdl_rc}; read-back tools="
                            f"{tools_present!r}"
                        ),
                        context={
                            "take_id": take_ref.id,
                            "angle": angle,
                            "setcdl_rc": setcdl_rc,
                            "tools_present": tools_present,
                        },
                    )
                )

            applied.append(
                {
                    "take_id": take_ref.id,
                    "angle": angle,
                    "slope": list(slope),
                    "offset": list(offset),
                    "power": list(power),
                    "setcdl_rc": bool(setcdl_rc),
                    "tools_present": tools_present,
                    "cdl_status": cdl.get("status"),
                    "cdl_code": cdl.get("code"),
                }
            )

    # Clean up scratch timeline
    media_pool.DeleteTimelines([scratch])

    connection.project_manager.SaveProject()
    snapshot_error: str | None = None
    try:
        snapshot_path = _export_project_snapshot(connection.project_manager, session)
    except ResolveError as exc:
        snapshot_path = None
        snapshot_error = str(exc)

    status = "PASS" if not any(i.severity == "warn" or i.severity == "fail" for i in issues) else "WARN"
    if any(i.severity == "fail" for i in issues):
        status = "FAIL"

    summary = (
        f"applied CDL to {len(applied)} angle-clip(s), skipped {len(skipped)}"
        + (f"; {len(issues)} warning(s)" if issues else "")
    )

    payload = {
        "project_name": session.resolve.project_name,
        "status": status,
        "issues": issues_to_dict(issues),
        "summary": summary,
        "applied": applied,
        "skipped": skipped,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
        "reference_angle": reference_angle,
        "version_name": CALIBRATION_VERSION_NAME,
    }
    if write_reports:
        write_json_report(
            session.reports_path("apply-color-normalization.json"), payload
        )
        write_markdown_report(
            session.reports_path("apply-color-normalization.md"),
            title="Apply Color Normalization",
            status=status,
            issues=issues,
            sections={
                "Summary": {
                    "applied_count": len(applied),
                    "skipped_count": len(skipped),
                    "reference_angle": reference_angle,
                    "version_name": CALIBRATION_VERSION_NAME,
                    "project_snapshot": (
                        str(snapshot_path) if snapshot_path is not None else "unavailable"
                    ),
                },
                "Applied": [
                    f"{a['take_id']}/{a['angle']}: slope={a['slope']} offset={a['offset']}"
                    for a in applied
                ],
                "Skipped": [f"{s['take_id']}/{s['angle']}: {s['reason']}" for s in skipped],
            },
        )
    return payload


# Note: RESOLVE_SYSTEM_LUT_DIR_MACOS and _piano_guard_lut_dir were part
# of a v1 implementation of the Milestone 5 escape hatch that wrote
# .cube LUTs and called TimelineItem.SetLUT. The live-verified
# behavior was that SetLUT delivers an OPAQUE LUT node that the
# operator cannot tweak in the Color page UI — violating the "starter
# grade" design principle. v2 replaced LUT delivery with extreme-range
# SetCDL so Node 1 shows up as an editable Primary Balance. The
# lut_io module is retained for a potential future multi-node
# delivery path via ApplyGradeFromDRX (which IS the only scripting
# route to true multi-node grades — Resolve has no AddNode API).


AUTO_STATE_VERSION_PREFIX = "auto-state-"
"""Remote version naming for Phase 2 auto-segment color normalization.

Each MediaPoolItem graded by ``apply_auto_color_normalization`` receives
a version named ``auto-state-NN-v1`` where NN is the zero-padded
dominant lighting-state id for that clip. Keeps Phase 1.5's
``piano-guard-calibration`` version namespace distinct."""


def _auto_state_version_name(state_id: int) -> str:
    return f"{AUTO_STATE_VERSION_PREFIX}{state_id:02d}-v1"


def _auto_state_escape_version_name(state_id: int) -> str:
    """Milestone 5 escape-hatch version name.

    Used when the operator opts a state into ``--escape-transform``.
    The clip ends up with BOTH ``auto-state-NN-v1`` (clipped CDL) and
    ``auto-state-NN-v-escape`` (richer-transform LUT) remote versions
    so the colorist can compare. The escape version is loaded as
    current; the CDL sibling remains available as a fallback.
    """
    return f"{AUTO_STATE_VERSION_PREFIX}{state_id:02d}-v-escape"


def _dominant_state_from_segments(
    segments: list[tuple[float, float, int]],
) -> int:
    """Pick the state_id whose segments sum to the longest total duration.
    Tie-break on lower state_id. Returns -1 when empty.
    """
    if not segments:
        return -1
    duration_by_state: dict[int, float] = {}
    for start_s, end_s, state_id in segments:
        dur = max(0.0, float(end_s) - float(start_s))
        duration_by_state[int(state_id)] = duration_by_state.get(int(state_id), 0.0) + dur
    return min(duration_by_state.items(), key=lambda kv: (-kv[1], kv[0]))[0]


def apply_auto_color_normalization(
    session: SessionProjectConfig,
    *,
    state_assignments: dict[str, list[tuple[float, float, int]]],
    cdl_per_state: dict[int, Any],
    reference_state_id: int,
    richer_transforms_per_state: dict[int, Any] | None = None,
    connector=connect_to_resolve,
    write_reports: bool = True,
) -> dict[str, Any]:
    """Apply per-lighting-state CDL via ``auto-state-NN-v1`` remote versions.

    Inputs (produced by ``auto_segment_pipeline.build_session_plan``):

    * ``state_assignments[clip_id]`` — list of ``(start_s, end_s, state_id)``
      tuples. ``clip_id`` is ``"{take_id}/{angle}"``.
    * ``cdl_per_state[state_id]`` — a ``CDLResult`` (or a dict with the
      equivalent ``slope_rgb`` / ``offset_rgb`` / ``power_rgb`` / ``status``
      / ``code`` fields).
    * ``reference_state_id`` — clips whose dominant state equals this get
      skipped (an identity CDL is unnecessary on the reference).

    Per clip: the dominant state is the one with the longest total duration
    across the clip's segments. The clip gets one SetCDL call via an
    ``auto-state-NN-v1`` remote version. Sub-dominant segments inherit that
    CDL — a Milestone 3 limitation (Resolve versions are clip-scoped); the
    pipeline emits ``multi_state_clip`` warnings upstream. Richer
    per-segment grading is deferred to Milestone 5 (escape hatch).

    Uses the same order-of-operations guard as
    ``apply_color_normalization`` (FAIL if any ``MC_*`` clip exists) and
    the same AddVersion(name, 1) + LoadVersionByName + GetCurrentVersion
    verification contract settled in Phase 0 Probe 1 Outcome B.
    """

    def _as_rgb_triple(raw: Any, *, field: str, state_id: int) -> tuple[float, float, float]:
        """Coerce a CDL field into an (R, G, B) float triple with shape check.

        Accepts a list/tuple of exactly 3 numeric values. Anything else
        raises, which lets the caller emit a skip entry rather than silently
        writing a malformed SetCDL dict that Resolve may misinterpret."""
        try:
            seq = list(raw)
        except TypeError as exc:
            raise ValueError(
                f"state-{state_id} {field}: not iterable ({raw!r})"
            ) from exc
        if len(seq) != 3:
            raise ValueError(
                f"state-{state_id} {field}: expected 3 channels, got {len(seq)} ({raw!r})"
            )
        return (float(seq[0]), float(seq[1]), float(seq[2]))

    def _cdl_slope(obj: Any, *, state_id: int) -> tuple[float, float, float]:
        if isinstance(obj, dict):
            raw = obj.get("slope") or obj.get("slope_rgb") or (1.0, 1.0, 1.0)
        else:
            raw = getattr(obj, "slope_rgb", (1.0, 1.0, 1.0))
        return _as_rgb_triple(raw, field="slope", state_id=state_id)

    def _cdl_offset(obj: Any, *, state_id: int) -> tuple[float, float, float]:
        if isinstance(obj, dict):
            raw = obj.get("offset") or obj.get("offset_rgb") or (0.0, 0.0, 0.0)
        else:
            raw = getattr(obj, "offset_rgb", (0.0, 0.0, 0.0))
        return _as_rgb_triple(raw, field="offset", state_id=state_id)

    def _cdl_power(obj: Any, *, state_id: int) -> tuple[float, float, float]:
        if isinstance(obj, dict):
            raw = obj.get("power") or obj.get("power_rgb") or (1.0, 1.0, 1.0)
        else:
            raw = getattr(obj, "power_rgb", (1.0, 1.0, 1.0))
        return _as_rgb_triple(raw, field="power", state_id=state_id)

    def _cdl_status(obj: Any) -> str:
        if isinstance(obj, dict):
            return str(obj.get("status", "pass"))
        return str(getattr(obj, "status", "pass"))

    def _cdl_code(obj: Any) -> str:
        if isinstance(obj, dict):
            return str(obj.get("code", ""))
        return str(getattr(obj, "code", ""))

    connection = connector()
    _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()

    issues: list[Issue] = []

    # Order-of-operations guard: fail hard if multicam clips already exist.
    existing_multicam = _multicam_clips_present(root)
    if existing_multicam:
        summary = (
            f"auto-color-normalize refuses to run: {len(existing_multicam)} multicam "
            f"clip(s) already exist ({', '.join(existing_multicam)}). Re-run this step "
            f"before creating multicam clips, or delete the existing multicams and "
            f"rebuild them after applying auto CDL."
        )
        issues.append(
            Issue(
                severity="fail",
                code="multicam_exists",
                message=summary,
                context={"multicam_clips": existing_multicam},
            )
        )
        payload = {
            "project_name": session.resolve.project_name,
            "status": "FAIL",
            "issues": issues_to_dict(issues),
            "summary": summary,
            "applied": [],
            "skipped": [],
        }
        if write_reports:
            write_json_report(
                session.reports_path("apply-auto-color-normalization.json"), payload
            )
        return payload

    # Build a clip_id → (take, camera) lookup from the session
    take_camera_by_clip_id: dict[str, tuple[TakeConfig, Any]] = {}
    for take_ref, take in iter_session_takes(session):
        for camera in take.camera_files:
            cid = f"{take_ref.id}/{camera.label}"
            take_camera_by_clip_id[cid] = (take, camera)

    # Scratch timeline inside the Timelines bin
    folders = _session_folders(media_pool, root, session)
    timelines_folder = folders["timelines"]
    media_pool.SetCurrentFolder(timelines_folder)
    scratch_name = "_pg_auto_color_scratch"
    # Remove ALL stale scratch timelines from prior runs (not just the first
    # one). A previous crash could have accumulated multiple copies because
    # the apply loop below creates without de-duplicating.
    stale_to_delete: list[Any] = []
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName() == scratch_name:
            stale_to_delete.append(tl)
    if stale_to_delete:
        media_pool.DeleteTimelines(stale_to_delete)
    scratch = media_pool.CreateEmptyTimeline(scratch_name)
    if scratch is None:
        summary = f"unable to create scratch timeline {scratch_name}"
        issues.append(
            Issue(severity="fail", code="scratch_timeline_create_failed", message=summary)
        )
        payload = {
            "project_name": session.resolve.project_name,
            "status": "FAIL",
            "issues": issues_to_dict(issues),
            "summary": summary,
            "applied": [],
            "skipped": [],
        }
        if write_reports:
            write_json_report(
                session.reports_path("apply-auto-color-normalization.json"), payload
            )
        return payload

    project.SetCurrentTimeline(scratch)

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for clip_id, segments in sorted(state_assignments.items()):
        dominant = _dominant_state_from_segments(segments)
        entry = take_camera_by_clip_id.get(clip_id)
        if entry is None:
            skipped.append(
                {"clip_id": clip_id, "reason": "clip_not_in_session", "dominant_state_id": dominant}
            )
            continue
        take, camera = entry

        if dominant == reference_state_id:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "dominant_is_reference",
                    "dominant_state_id": dominant,
                }
            )
            continue
        if dominant < 0:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "dominant_is_noise",
                    "dominant_state_id": dominant,
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="dominant_is_noise",
                    message=(
                        f"{clip_id}: dominant lighting state is HDBSCAN noise (-1); "
                        f"no CDL to apply. The clip likely has outlier fingerprints "
                        f"not matching any clustered regime."
                    ),
                    context={"clip_id": clip_id},
                )
            )
            continue

        # A Resolve remote version is clip-scoped, not time-range-scoped.
        # If a single clip spans ≥ 2 distinct non-noise lighting states,
        # applying the dominant state's CDL bakes a known-wrong grade into
        # the non-dominant spans and contaminates every later multicam use
        # of that source. Refuse to apply; surface as unresolved so the
        # operator can either accept per-clip manual grading or wait for
        # Milestone 5's per-segment escape hatch.
        distinct_nonnoise_states = {
            int(sid) for _s, _e, sid in segments if int(sid) >= 0
        }
        if len(distinct_nonnoise_states) > 1:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "multi_state_clip_unresolved",
                    "dominant_state_id": dominant,
                    "states": sorted(distinct_nonnoise_states),
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="multi_state_clip_unresolved",
                    message=(
                        f"{clip_id} spans multiple lighting states "
                        f"{sorted(distinct_nonnoise_states)}; remote versions are "
                        f"clip-scoped so we cannot grade per-segment. Skipping "
                        f"auto-apply — grade this clip manually in Resolve, or "
                        f"wait for Milestone 5's richer-transform escape hatch."
                    ),
                    context={
                        "clip_id": clip_id,
                        "dominant_state_id": dominant,
                        "states": sorted(distinct_nonnoise_states),
                        "segments": [
                            {"start_s": s, "end_s": e, "state_id": int(sid)}
                            for s, e, sid in segments
                        ],
                    },
                )
            )
            continue

        cdl = cdl_per_state.get(dominant)
        if cdl is None:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "cdl_missing_for_state",
                    "dominant_state_id": dominant,
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="cdl_missing",
                    message=(
                        f"{clip_id}: no CDL fit for dominant state-{dominant}; skipping."
                    ),
                    context={"clip_id": clip_id, "dominant_state_id": dominant},
                )
            )
            continue
        # Milestone 5: if the operator marked this state for the escape
        # hatch, skip the cdl_fail check — a richer transform may succeed
        # where bounded CDL can't. The cdl_fail path still applies when
        # escape isn't enabled.
        richer = (richer_transforms_per_state or {}).get(dominant)
        use_escape = richer is not None

        cdl_status = _cdl_status(cdl)
        if cdl_status == "fail" and not use_escape:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "cdl_fail",
                    "dominant_state_id": dominant,
                    "code": _cdl_code(cdl),
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="cdl_skipped",
                    message=(
                        f"{clip_id}: dominant state-{dominant} CDL has status=fail "
                        f"(code={_cdl_code(cdl)}); skipping. Pass "
                        f"--escape-transform {dominant} to retry with the richer "
                        f"transform."
                    ),
                    context={"clip_id": clip_id, "dominant_state_id": dominant},
                )
            )
            continue

        absolute_path = take.resolve_path(camera.file)
        clip = _find_clip_by_file_path(root, absolute_path)
        if clip is None:
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "clip_not_found",
                    "dominant_state_id": dominant,
                    "path": str(absolute_path),
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="source_clip_missing",
                    message=f"{clip_id}: source clip not found in media pool: {absolute_path}",
                )
            )
            continue

        appended = media_pool.AppendToTimeline([clip])
        if not appended:
            issues.append(
                Issue(
                    severity="warn",
                    code="append_failed",
                    message=f"{clip_id}: failed to append to scratch timeline",
                )
            )
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "append_failed",
                    "dominant_state_id": dominant,
                }
            )
            continue
        vti = appended[0]

        version_name = (
            _auto_state_escape_version_name(dominant)
            if use_escape
            else _auto_state_version_name(dominant)
        )

        # Re-run robustness: on a second invocation of auto-color-normalize,
        # the source clip often already carries a remote version from the
        # previous run (e.g. auto-state-28-v1 from an older clustering).
        # Resolve's AddVersion call requires the clip's current version to
        # be the default ("Version 1", local, type 0) before it will accept
        # adding a new remote — on a clip that already has a different
        # named remote as current, AddVersion returns False and no new
        # version is created. Verified empirically: live E2E on the test
        # session showed add_rc=False + load_rc=False + current still at
        # "Version 1" because the new name never landed.
        #
        # Robust sequence:
        #   1. Reset to default Version 1 (local) if possible — gives
        #      AddVersion a clean state.
        #   2. If target version is already in the remote-version list
        #      from a prior run, skip AddVersion and just LoadVersionByName.
        #   3. Otherwise AddVersion + re-check the list, because AddVersion
        #      sometimes returns False despite successfully adding (Resolve
        #      API quirk — not documented but reproduced in beta builds).
        try:
            vti.LoadVersionByName("Version 1", 0)
        except Exception:
            # Some builds raise on switching to default when it's already
            # current. Safe to ignore; the subsequent AddVersion handles it.
            pass

        existing_remote = list(vti.GetVersionNameList(1) or [])
        if version_name in existing_remote:
            # Prior run already created this exact name on this clip.
            add_rc = True
        else:
            add_rc = bool(vti.AddVersion(version_name, 1))
            if not add_rc:
                # Re-check the list — AddVersion sometimes lies about its
                # return value. If the name is now present, treat as success.
                if version_name in list(vti.GetVersionNameList(1) or []):
                    add_rc = True

        load_rc = bool(vti.LoadVersionByName(version_name, 1)) if add_rc else False
        current = vti.GetCurrentVersion() or {}
        current_name = current.get("versionName") if isinstance(current, dict) else None
        current_type = current.get("versionType") if isinstance(current, dict) else None
        if current_name != version_name or current_type != 1:
            issues.append(
                Issue(
                    severity="warn",
                    code="remote_version_load_failed",
                    message=(
                        f"{clip_id}: expected current version {version_name!r} "
                        f"(remote) after AddVersion+LoadVersionByName but got "
                        f"{current_name!r} (type {current_type}). CDL will NOT "
                        f"persist into multicam angle sub-clips."
                    ),
                    context={
                        "clip_id": clip_id,
                        "add_rc": bool(add_rc),
                        "load_rc": bool(load_rc),
                        "existing_remote_versions": existing_remote,
                        "current_version": {"name": current_name, "type": current_type},
                    },
                )
            )
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "remote_version_load_failed",
                    "dominant_state_id": dominant,
                }
            )
            continue

        # Note any sub-dominant segments for transparency (used in both
        # CDL and escape applied records below).
        sub_dominant = [
            {"start_s": s, "end_s": e, "state_id": int(sid)}
            for s, e, sid in segments
            if int(sid) != dominant
        ]

        if use_escape:
            # --- Milestone 5 escape path: editable Primary Balance with
            # unbounded CDL values ---
            #
            # v1 of this path wrote a .cube LUT via SetLUT. v2 replaces
            # that with SetCDL using the richer transform's wider-bound
            # gain/offset. Rationale: Resolve's SetCDL accepts slopes far
            # outside the UI's [0.8, 1.25] slider range (verified live:
            # slope=50, offset=-0.3 all land cleanly). CDL on Node 1
            # shows up as editable Primary Balance wheels in the Color
            # page — the operator can tweak the baseline post-hoc, which
            # matches the "starter grade" design principle. LUTs on
            # Node 1 are opaque (only bypass/swap is possible) and would
            # block the downstream tweaking workflow.
            #
            # A future milestone may add a true 3x3 matrix + 1D shaper
            # via ApplyGradeFromDRX (the only scripting path to multi-
            # node grades — Resolve has no AddNode API). Until then the
            # escape hatch is "unbounded CDL delivered as editable
            # Primary Balance".
            #
            # Re-run cleanup: if a v1-era run previously put a LUT on
            # Node 1 of this version, SetCDL below would *add* Primary
            # Balance alongside the stale LUT rather than replace it.
            # Clear the LUT first with SetLUT("") so Node 1 ends up with
            # only Primary Balance.
            try:
                vti.SetLUT(1, "")
            except Exception:
                # On clips that never had a LUT, some Resolve builds
                # raise. Safe to ignore — the SetCDL below will either
                # succeed cleanly or fail loudly.
                pass

            gain = tuple(float(x) for x in getattr(richer, "gain_rgb", (1.0, 1.0, 1.0)))
            off = tuple(float(x) for x in getattr(richer, "offset_rgb", (0.0, 0.0, 0.0)))
            setcdl_dict_escape = {
                "NodeIndex": "1",
                "Slope": f"{gain[0]:.6f} {gain[1]:.6f} {gain[2]:.6f}",
                "Offset": f"{off[0]:.6f} {off[1]:.6f} {off[2]:.6f}",
                "Power": "1.000000 1.000000 1.000000",
                "Saturation": "1",
            }
            setcdl_rc_escape = bool(vti.SetCDL(setcdl_dict_escape))

            tools_present_escape: list[str] | None = None
            try:
                graph = vti.GetNodeGraph(1)
                tools = graph.GetToolsInNode(1) or []
                tools_present_escape = list(tools)
            except Exception:
                tools_present_escape = None

            cdl_applied_escape = bool(setcdl_rc_escape) and (
                tools_present_escape is not None
                and "Primary Balance" in tools_present_escape
            )
            if not cdl_applied_escape:
                issues.append(
                    Issue(
                        severity="warn",
                        code="setcdl_unverified",
                        message=(
                            f"{clip_id}: escape SetCDL rc={setcdl_rc_escape}; "
                            f"read-back tools={tools_present_escape!r}"
                        ),
                        context={
                            "clip_id": clip_id,
                            "setcdl_rc": setcdl_rc_escape,
                            "tools_present": tools_present_escape,
                            "mode": "escape",
                        },
                    )
                )

            applied.append(
                {
                    "clip_id": clip_id,
                    "dominant_state_id": dominant,
                    "version_name": version_name,
                    "mode": "escape",
                    "slope": list(gain),
                    "offset": list(off),
                    "power": [1.0, 1.0, 1.0],
                    "setcdl_rc": setcdl_rc_escape,
                    "tools_present": tools_present_escape,
                    "richer_status": getattr(richer, "status", None),
                    "richer_code": getattr(richer, "code", None),
                    "segment_count": len(segments),
                    "sub_dominant_segments": sub_dominant,
                }
            )
            continue

        # --- CDL path (default) ---
        try:
            slope = _cdl_slope(cdl, state_id=dominant)
            offset = _cdl_offset(cdl, state_id=dominant)
            power = _cdl_power(cdl, state_id=dominant)
        except (ValueError, TypeError) as exc:
            # Malformed CDL shape — refuse to write a bad SetCDL dict that
            # Resolve might silently misinterpret.
            skipped.append(
                {
                    "clip_id": clip_id,
                    "reason": "cdl_malformed",
                    "dominant_state_id": dominant,
                    "detail": str(exc),
                }
            )
            issues.append(
                Issue(
                    severity="warn",
                    code="cdl_malformed",
                    message=f"{clip_id}: malformed CDL ({exc}); skipping.",
                    context={
                        "clip_id": clip_id,
                        "dominant_state_id": dominant,
                    },
                )
            )
            continue
        setcdl_dict = {
            "NodeIndex": "1",
            "Slope": f"{slope[0]:.6f} {slope[1]:.6f} {slope[2]:.6f}",
            "Offset": f"{offset[0]:.6f} {offset[1]:.6f} {offset[2]:.6f}",
            "Power": f"{power[0]:.6f} {power[1]:.6f} {power[2]:.6f}",
            "Saturation": "1",
        }
        setcdl_rc = vti.SetCDL(setcdl_dict)

        tools_present: list[str] | None = None
        try:
            graph = vti.GetNodeGraph(1)
            tools = graph.GetToolsInNode(1) or []
            tools_present = list(tools)
        except Exception:
            tools_present = None

        cdl_applied = bool(setcdl_rc) and (
            tools_present is not None and "Primary Balance" in tools_present
        )
        if not cdl_applied:
            issues.append(
                Issue(
                    severity="warn",
                    code="setcdl_unverified",
                    message=(
                        f"{clip_id}: SetCDL rc={setcdl_rc}; read-back tools={tools_present!r}"
                    ),
                    context={
                        "clip_id": clip_id,
                        "setcdl_rc": setcdl_rc,
                        "tools_present": tools_present,
                    },
                )
            )

        applied.append(
            {
                "clip_id": clip_id,
                "dominant_state_id": dominant,
                "version_name": version_name,
                "mode": "cdl",
                "slope": list(slope),
                "offset": list(offset),
                "power": list(power),
                "setcdl_rc": bool(setcdl_rc),
                "tools_present": tools_present,
                "cdl_status": cdl_status,
                "cdl_code": _cdl_code(cdl),
                "segment_count": len(segments),
                "sub_dominant_segments": sub_dominant,
            }
        )

    # Crash-safe scratch cleanup: DeleteTimelines must run even if the
    # apply loop raised. We don't catch/suppress exceptions here — a mid-
    # run failure still propagates — but we make sure the scratch timeline
    # is gone on the way out. (The startup stale-cleanup block covers
    # crashes where this finally itself didn't reach the DeleteTimelines
    # call, e.g. OOM during the cleanup.)
    try:
        media_pool.DeleteTimelines([scratch])
    except Exception as exc:
        issues.append(
            Issue(
                severity="warn",
                code="scratch_timeline_cleanup_failed",
                message=(
                    f"scratch timeline {scratch_name} could not be deleted: {exc!r}. "
                    f"Next run will attempt stale-cleanup."
                ),
            )
        )

    connection.project_manager.SaveProject()
    snapshot_error: str | None = None
    try:
        snapshot_path = _export_project_snapshot(connection.project_manager, session)
    except ResolveError as exc:
        snapshot_path = None
        snapshot_error = str(exc)

    status = "PASS"
    if any(i.severity == "warn" for i in issues):
        status = "WARN"
    if any(i.severity == "fail" for i in issues):
        status = "FAIL"

    summary = (
        f"applied auto-state CDL to {len(applied)} clip(s), skipped {len(skipped)}"
        + (f"; {len(issues)} warning(s)" if issues else "")
    )

    payload = {
        "project_name": session.resolve.project_name,
        "status": status,
        "issues": issues_to_dict(issues),
        "summary": summary,
        "applied": applied,
        "skipped": skipped,
        "reference_state_id": reference_state_id,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
    }
    if write_reports:
        write_json_report(
            session.reports_path("apply-auto-color-normalization.json"), payload
        )
        write_markdown_report(
            session.reports_path("apply-auto-color-normalization.md"),
            title="Apply Auto Color Normalization",
            status=status,
            issues=issues,
            sections={
                "Summary": {
                    "applied_count": len(applied),
                    "skipped_count": len(skipped),
                    "reference_state_id": reference_state_id,
                    "project_snapshot": (
                        str(snapshot_path) if snapshot_path is not None else "unavailable"
                    ),
                },
                "Applied": [
                    f"{a['clip_id']}: state-{a['dominant_state_id']} "
                    f"slope={a['slope']} offset={a['offset']}"
                    for a in applied
                ],
                "Skipped": [
                    f"{s['clip_id']}: {s['reason']}"
                    + (
                        f" (dominant state-{s['dominant_state_id']})"
                        if "dominant_state_id" in s
                        else ""
                    )
                    for s in skipped
                ],
            },
        )
    return payload


def grab_clip_still(
    session: SessionProjectConfig,
    *,
    clip_id: str,
    out_path: Path,
    version_name: str | None = None,
    version_type: int = 1,
    connector=connect_to_resolve,
) -> dict[str, Any]:
    """Drive Resolve to render a still of the named clip's current grade.

    The skill workflow needs a way to *see* the result of a CDL grade
    after applying it, not just compare ungraded source stills. This
    function:

      1. Finds the clip in the project's media pool from ``clip_id``
         (``"take-01/angle-c"`` form).
      2. Appends it to a throwaway scratch timeline.
      3. (Optionally) loads a specific named version on the timeline
         item.
      4. Calls ``Timeline.GrabStill()`` which captures the current
         viewer frame into a Gallery still.
      5. Exports the still to ``out_path`` via
         ``GalleryStillAlbum.ExportStills``.
      6. Cleans up the still and the scratch timeline.

    Returns a dict with status / out_path / version metadata. Used by
    the ``/color-review`` skill to close its feedback loop without
    requiring the operator to manually screenshot.
    """
    connection = connector()
    _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()

    # Resolve clip_id -> take + camera + source path
    if "/" not in clip_id:
        raise ResolveError(
            f"clip_id {clip_id!r} must be 'take-id/angle-label' format"
        )
    take_id, angle = clip_id.split("/", 1)
    take_camera = None
    for take_ref, take in iter_session_takes(session):
        if take_ref.id != take_id:
            continue
        for camera in take.camera_files:
            if camera.label == angle:
                take_camera = (take, camera)
                break
        break
    if take_camera is None:
        raise ResolveError(f"clip_id {clip_id!r} not found in session")
    take, camera = take_camera
    absolute_path = take.resolve_path(camera.file)
    clip = _find_clip_by_file_path(root, absolute_path)
    if clip is None:
        raise ResolveError(
            f"clip {clip_id!r} not in media pool (run prepare-resolve-session?)"
        )

    folders = _session_folders(media_pool, root, session)
    timelines_folder = folders["timelines"]
    media_pool.SetCurrentFolder(timelines_folder)

    # Use a unique-ish scratch timeline name so concurrent calls don't
    # collide. Cleanup at the end.
    import time as _time

    scratch_name = f"_pg_still_grab_{int(_time.time() * 1000)}"
    # Tidy any stale scratch timelines from prior runs
    for i in range(1, int(project.GetTimelineCount() or 0) + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName().startswith("_pg_still_grab_"):
            media_pool.DeleteTimelines([tl])

    scratch = media_pool.CreateEmptyTimeline(scratch_name)
    if scratch is None:
        raise ResolveError(f"unable to create scratch timeline {scratch_name}")
    project.SetCurrentTimeline(scratch)

    grabbed_still: Any = None
    try:
        appended = media_pool.AppendToTimeline([clip])
        if not appended:
            raise ResolveError(f"failed to append {clip_id} to scratch timeline")
        vti = appended[0]

        # Optional: load a specific named version before grabbing
        loaded_version: dict[str, Any] = {}
        if version_name is not None:
            try:
                vti.LoadVersionByName(version_name, version_type)
            except Exception:
                pass
            current = vti.GetCurrentVersion() or {}
            loaded_version = {
                "requested_name": version_name,
                "requested_type": version_type,
                "current_name": current.get("versionName") if isinstance(current, dict) else None,
                "current_type": current.get("versionType") if isinstance(current, dict) else None,
            }
        else:
            current = vti.GetCurrentVersion() or {}
            loaded_version = {
                "requested_name": None,
                "current_name": current.get("versionName") if isinstance(current, dict) else None,
                "current_type": current.get("versionType") if isinstance(current, dict) else None,
            }

        # Position playhead near the clip mid-point. The default after
        # AppendToTimeline is frame 0 which often shows a slate or the
        # very first frame of a long take — uninformative for color
        # judgment. Use the clip's media frame count for the offset.
        try:
            frames = int(clip.GetClipProperty("Frames") or 0)
        except Exception:
            frames = 0
        if frames > 0:
            mid = frames // 2
            # Convert frames to BCD timecode-ish HH:MM:SS:FF. Use 29.97
            # since session timeline is locked to that.
            fps = 29.97
            sec = mid / fps
            hh = int(sec // 3600)
            mm = int((sec % 3600) // 60)
            ss = int(sec % 60)
            ff = int(round((sec - int(sec)) * fps))
            tc = f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"
            try:
                scratch.SetCurrentTimecode(tc)
            except Exception:
                pass

        # Grab the current viewer frame. Returns a Gallery still.
        grabbed_still = scratch.GrabStill()
        if grabbed_still is None:
            raise ResolveError("Timeline.GrabStill() returned None")

        gallery = project.GetGallery()
        album = gallery.GetCurrentStillAlbum() if gallery else None
        if album is None:
            raise ResolveError("project gallery has no current still album")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # ExportStills(stills_list, folder_path, file_prefix, format)
        # writes one file per still, named "<prefix>_<n>_<extra>.<ext>".
        # We use a unique prefix per call so we can find what was just
        # written, then rename to out_path.
        prefix = f"_pg_grab_{int(_time.time() * 1000)}"
        export_dir = out_path.parent
        rc = album.ExportStills([grabbed_still], str(export_dir), prefix, "png")
        if not rc:
            raise ResolveError(f"ExportStills returned {rc!r}")

        # Find what got written and rename to caller's out_path
        candidates = sorted(export_dir.glob(f"{prefix}*.png"))
        if not candidates:
            raise ResolveError(
                f"ExportStills succeeded but no {prefix}*.png file found in {export_dir}"
            )
        # Resolve writes one .png + sometimes a .drx sidecar. Take the .png.
        written = candidates[0]
        if out_path.exists():
            out_path.unlink()
        written.replace(out_path)
        # Clean up any sidecars (drx) Resolve emitted alongside
        for sidecar in export_dir.glob(f"{prefix}*"):
            try:
                sidecar.unlink()
            except OSError:
                pass

        return {
            "status": "PASS",
            "clip_id": clip_id,
            "out_path": str(out_path),
            "version": loaded_version,
            "frames": frames,
            "mid_frame": frames // 2 if frames > 0 else 0,
        }
    finally:
        # Always clean up the still and the scratch timeline so the
        # gallery and project don't accumulate debris across calls.
        try:
            if grabbed_still is not None:
                gallery = project.GetGallery()
                album = gallery.GetCurrentStillAlbum() if gallery else None
                if album is not None:
                    album.DeleteStills([grabbed_still])
        except Exception:
            pass
        try:
            media_pool.DeleteTimelines([scratch])
        except Exception:
            pass


def sync_session_audio(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
) -> dict[str, Any]:
    connection = connector()
    current_database = _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    folders = _session_folders(media_pool, root, session)

    take_payloads: list[dict[str, Any]] = []
    for take_ref, take in iter_session_takes(session):
        working_folder = _folder_by_name(folders["takes"], take_ref.id)
        video_clip_count, master_audio = _sync_take_clips(project, connection.resolve, working_folder, take)
        take_payloads.append(
            {
                "take_id": take_ref.id,
                "video_clip_count": video_clip_count,
                "master_audio": master_audio,
            }
        )

    connection.project_manager.SaveProject()
    snapshot_path = _export_project_snapshot(connection.project_manager, session)
    payload = {
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "project_snapshot": str(snapshot_path),
        "takes": take_payloads,
        "status": "PASS",
    }
    write_json_report(session.reports_path("sync-session.json"), payload)
    write_markdown_report(
        session.reports_path("sync-session.md"),
        title="Resolve Session Audio Sync",
        status="PASS",
        issues=[],
        sections={
            "Project": {
                "project_name": session.resolve.project_name,
                "project_library": current_database.get("DbName", "current"),
                "project_snapshot": str(snapshot_path),
            },
            "Takes": [f"{item['take_id']}: {item['video_clip_count']} clips" for item in take_payloads],
        },
    )
    return payload


def create_multicam_clips(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
    take_filter: str | None = None,
    write_reports: bool = False,
) -> dict[str, Any]:
    connection = connector()
    current_database = _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    if not connection.resolve.OpenPage("edit"):
        raise ResolveError("unable to switch Resolve to Edit page for multicam creation")

    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    folders = _session_folders(media_pool, root, session)
    takes_root = folders["takes"]

    created: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []

    for take_ref, take in iter_session_takes(session):
        if take_filter and take_ref.id != take_filter:
            continue
        working_folder = _folder_by_name(takes_root, take_ref.id)
        _cleanup_take_temp_folders(media_pool, working_folder)
        multicam_name = f"MC_{take_ref.id}"
        if _direct_clip_by_name(working_folder, multicam_name):
            skipped.append(take_ref.id)
            continue
        _ensure_take_source_clips(media_pool, takes_root, working_folder, take)
        if not _clip_by_name(working_folder, "audio-master"):
            failed.append({"take_id": take_ref.id, "reason": "audio-master missing"})
            continue
        source_folder = None
        source_clips: list[Any] = []
        renamed_clips: list[tuple[Any, str]] = []
        try:
            source_folder, source_clips, renamed_clips = _prepare_multicam_working_folder(media_pool, working_folder, take)
            media_pool.SetCurrentFolder(source_folder)
            time.sleep(0.5)
            known_names = {clip.GetName() for clip in _iter_clips_recursive(takes_root)}
            _create_multicam_clip_ui(multicam_name=multicam_name)

            deadline = time.time() + 30.0
            while time.time() < deadline:
                existing = _direct_clip_by_name(working_folder, multicam_name)
                if existing:
                    created.append(take_ref.id)
                    break
                created_clip = _find_new_multicam_clip(takes_root, known_names=known_names)
                if created_clip is not None:
                    _move_clips_to_folder(media_pool, [created_clip], working_folder)
                    located = _direct_clip_by_name(working_folder, created_clip.GetName())
                    if located is None:
                        raise ResolveError(f"created multicam clip could not be located in {take_ref.id}")
                    _rename_clip(located, multicam_name)
                    created.append(take_ref.id)
                    break
                time.sleep(0.5)
            else:
                failed.append({"take_id": take_ref.id, "reason": f"{multicam_name} did not appear after dialog completion"})
        except ResolveError as exc:
            failed.append({"take_id": take_ref.id, "reason": str(exc)})
        finally:
            if source_folder is not None:
                try:
                    _restore_multicam_working_folder(
                        media_pool,
                        working_folder,
                        source_folder,
                        source_clips,
                        renamed_clips,
                        take,
                    )
                except ResolveError as exc:
                    failed.append({"take_id": take_ref.id, "reason": f"cleanup failed: {exc}"})

    connection.project_manager.SaveProject()
    snapshot_error: str | None = None
    try:
        snapshot_path = _export_project_snapshot(connection.project_manager, session)
    except ResolveError as exc:
        snapshot_path = None
        snapshot_error = str(exc)
    status = "PASS" if not failed else "FAIL"
    payload = {
        "status": status,
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
        "created": created,
        "skipped": skipped,
        "failed": failed,
    }
    if write_reports:
        write_json_report(session.reports_path("multicam-session.json"), payload)
        write_markdown_report(
            session.reports_path("multicam-session.md"),
            title="Resolve Multicam Creation",
            status=status,
            issues=[],
            sections={
                "Project": {
                    "project_name": session.resolve.project_name,
                    "project_library": current_database.get("DbName", "current"),
                    "project_snapshot": str(snapshot_path) if snapshot_path is not None else "unavailable",
                },
                "Created": created or ["none"],
                "Skipped": skipped or ["none"],
                "Failed": [f"{item['take_id']}: {item['reason']}" for item in failed] or ["none"],
            },
        )
    return payload


def build_session_assembly(
    session: SessionProjectConfig,
    *,
    connector=connect_to_resolve,
    gap_seconds: float = 5.0,
    write_reports: bool = False,
) -> dict[str, Any]:
    connection = connector()
    current_database = _select_project_library(connection, session)
    connection.project_manager.GotoRootFolder()
    project, _created = _load_or_create_project(
        connection,
        project_name=session.resolve.project_name,
        media_location=session.session_root,
    )
    media_pool = project.GetMediaPool()
    root = media_pool.GetRootFolder()
    folders = _session_folders(media_pool, root, session)
    takes_root = folders["takes"]
    timelines_folder = folders["timelines"]

    take_items: list[tuple[str, Any]] = []
    missing: list[str] = []
    for take_ref, _take in iter_session_takes(session):
        working_folder = _folder_by_name(takes_root, take_ref.id)
        _cleanup_take_temp_folders(media_pool, working_folder)
        multicam_clip = _direct_clip_by_name(working_folder, f"MC_{take_ref.id}")
        if multicam_clip is None:
            missing.append(take_ref.id)
            continue
        take_items.append((take_ref.id, multicam_clip))

    if not take_items:
        raise ResolveError("no multicam clips available; Session_Assembly cannot be built")

    gap_frames = max(0, int(round(float(session.timeline.frame_rate) * gap_seconds)))
    timeline, appended = _build_session_assembly_timeline(
        project=project,
        media_pool=media_pool,
        timelines_folder=timelines_folder,
        take_items=take_items,
        gap_frames=gap_frames,
        frame_rate=float(session.timeline.frame_rate),
    )
    connection.project_manager.SaveProject()
    snapshot_error: str | None = None
    try:
        snapshot_path = _export_project_snapshot(connection.project_manager, session)
    except ResolveError as exc:
        snapshot_path = None
        snapshot_error = str(exc)
    status = "PASS" if not missing else "WARN"
    payload = {
        "status": status,
        "project_name": session.resolve.project_name,
        "project_library": current_database,
        "project_snapshot": str(snapshot_path) if snapshot_path is not None else None,
        "project_snapshot_warning": snapshot_error,
        "timeline_name": timeline.GetName(),
        "gap_seconds": gap_seconds,
        "appended": appended,
        "missing_multicam": missing,
    }
    if write_reports:
        write_json_report(session.reports_path("session-assembly.json"), payload)
        write_markdown_report(
            session.reports_path("session-assembly.md"),
            title="Session Assembly",
            status=status,
            issues=[],
            sections={
                "Project": {
                    "project_name": session.resolve.project_name,
                    "project_library": current_database.get("DbName", "current"),
                    "project_snapshot": str(snapshot_path) if snapshot_path is not None else "unavailable",
                },
                "Timeline": {"timeline_name": timeline.GetName(), "gap_seconds": gap_seconds},
                "Appended": [
                    f"{item['take_id']}: {item['multicam_clip_name']}"
                    for item in appended
                ],
                "Missing Multicam": missing or ["none"],
            },
        )
    return payload
