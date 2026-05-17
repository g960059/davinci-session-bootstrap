from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
import re
from typing import Any

import yaml


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".mts"}
AUDIO_EXTENSIONS = {".wav", ".aiff", ".aif", ".flac"}


@dataclass
class CameraSource:
    file: str
    label: str


@dataclass
class TimelineConfig:
    frame_rate: str = "29.97"
    width: int = 3840
    height: int = 2160
    input_color_space: str = "Rec.2100 HLG"
    timeline_color_space: str = "Rec.709 Gamma 2.4"
    output_color_space: str = "Rec.709 Gamma 2.4"


@dataclass
class ResolveConfig:
    project_name: str = ""
    project_library_type: str = "Disk"
    project_library_name: str | None = None
    project_library_path: str | None = None
    project_snapshots_dir: str = "resolve"
    takes_bin: str = "Takes"
    timelines_bin: str = "Timelines"


@dataclass
class ValidationConfig:
    expected_color_space: str = "bt2020nc"
    expected_color_transfer: str = "arib-std-b67"
    expected_color_primaries: str = "bt2020"
    frame_rate_tolerance: float = 0.01
    video_duration_warn_seconds: float = 60.0
    video_duration_fail_seconds: float = 180.0
    audio_duration_warn_seconds: float = 60.0
    audio_duration_fail_seconds: float = 180.0


@dataclass
class LogicConfig:
    project_file: str | None = None


@dataclass
class SessionTakeRef:
    id: str
    take: str


@dataclass
class TakeConfig:
    session: str
    date: str
    source_dir: Path
    master_audio: str
    camera_files: list[CameraSource]
    edit_audio: str | None = None
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    resolve: ResolveConfig = field(default_factory=ResolveConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    reports_dir: str = "reports"
    take_file: Path | None = None

    def __post_init__(self) -> None:
        self.source_dir = Path(self.source_dir)
        if not self.resolve.project_name:
            self.resolve.project_name = project_name_for_path(self.source_dir)

    @property
    def take_id(self) -> str:
        return self.source_dir.name

    @property
    def take_path(self) -> Path:
        if self.take_file is None:
            return self.source_dir / "take.yaml"
        return self.take_file

    def resolve_path(self, relative_path: str) -> Path:
        return (self.source_dir / relative_path).resolve()

    def reports_path(self, filename: str) -> Path:
        return self.resolve_path(self.reports_dir) / filename

    def editing_audio_relative_path(self) -> str:
        return self.edit_audio or self.master_audio

    def editing_audio_path(self) -> Path:
        return self.resolve_path(self.editing_audio_relative_path())


@dataclass
class SessionProjectConfig:
    session_id: str
    session_title: str
    date: str
    session_root: Path
    angles: list[str] = field(default_factory=list)
    reference_angle: str | None = None
    logic: LogicConfig = field(default_factory=LogicConfig)
    resolve: ResolveConfig = field(default_factory=ResolveConfig)
    timeline: TimelineConfig = field(default_factory=TimelineConfig)
    takes: list[SessionTakeRef] = field(default_factory=list)
    reports_dir: str = "reports"
    session_file: Path | None = None

    def __post_init__(self) -> None:
        self.session_root = Path(self.session_root)
        if not self.resolve.project_name:
            self.resolve.project_name = self.session_id

    @property
    def session_path(self) -> Path:
        if self.session_file is None:
            return self.session_root / "session.yaml"
        return self.session_file

    def resolve_path(self, relative_path: str) -> Path:
        return (self.session_root / relative_path).resolve()

    def reports_path(self, filename: str) -> Path:
        return self.resolve_path(self.reports_dir) / filename

    def resolve_snapshot_dir(self) -> Path:
        return self.resolve_path(self.resolve.project_snapshots_dir)

    def resolve_snapshot_path(self) -> Path:
        return self.resolve_snapshot_dir() / f"{self.resolve.project_name}.drp"


def project_name_for_path(path: Path) -> str:
    name = path.name.strip()
    cleaned = re.sub(r"[/:]+", "_", name)
    return cleaned or "piano-guard-session"


def derive_date_and_title(folder_name: str) -> tuple[str, str]:
    compact = re.match(r"(?P<date>\d{8})(?P<title>.*)", folder_name)
    dashed = re.match(r"(?P<date>\d{4}-\d{2}-\d{2})(?P<title>.*)", folder_name)
    trailing_compact = re.match(r"(?P<title>.*?)[\s_-]?(?P<date>\d{8})$", folder_name)
    trailing_dashed = re.match(r"(?P<title>.*?)[\s_-]?(?P<date>\d{4}-\d{2}-\d{2})$", folder_name)
    if compact:
        raw_date = compact.group("date")
        title = compact.group("title")
        session_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    elif dashed:
        session_date = dashed.group("date")
        title = dashed.group("title")
    elif trailing_compact:
        raw_date = trailing_compact.group("date")
        title = trailing_compact.group("title")
        session_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
    elif trailing_dashed:
        session_date = trailing_dashed.group("date")
        title = trailing_dashed.group("title")
    else:
        return date.today().isoformat(), folder_name
    return session_date, title.strip(" -_") or folder_name


def infer_session_root(source_dir: Path) -> Path | None:
    source_dir = source_dir.resolve()
    if source_dir.parent.name == "takes":
        return source_dir.parent.parent.resolve()
    return None


def _copy_timeline_config(config: TimelineConfig) -> TimelineConfig:
    return TimelineConfig(**asdict(config))


def _camera_sources_from_list(items: list[dict[str, Any]]) -> list[CameraSource]:
    return [CameraSource(file=item["file"], label=item["label"]) for item in items]


def _session_take_refs_from_list(items: list[dict[str, Any]]) -> list[SessionTakeRef]:
    return [SessionTakeRef(id=item["id"], take=item["take"]) for item in items]


def discover_take(source_dir: Path, *, session_root: Path | None = None) -> TakeConfig:
    source_dir = source_dir.resolve()
    if not source_dir.is_dir():
        raise FileNotFoundError(f"take directory not found: {source_dir}")

    session_root = session_root.resolve() if session_root is not None else infer_session_root(source_dir)
    if session_root is not None:
        session_date, _ = derive_date_and_title(session_root.name)
        session_id = project_name_for_path(session_root)
        resolve = ResolveConfig(project_name=session_id)
    else:
        session_date, _ = derive_date_and_title(source_dir.name)
        session_id = project_name_for_path(source_dir)
        resolve = ResolveConfig(project_name=session_id)

    audio_candidates = sorted(
        [path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS]
    )
    if not audio_candidates:
        raise FileNotFoundError(f"no audio file found in {source_dir}")

    preferred_audio = next(
        (path for path in audio_candidates if path.name.lower() in {"audio.wav", "audio.aiff", "audio.aif"}),
        None,
    )
    master_audio = preferred_audio or max(audio_candidates, key=lambda path: path.stat().st_size)

    video_candidates = sorted(
        [path for path in source_dir.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS]
    )
    if not video_candidates:
        raise FileNotFoundError(f"no video files found in {source_dir}")

    cameras = [CameraSource(file=path.name, label=path.stem) for path in video_candidates]
    return TakeConfig(
        session=session_id,
        date=session_date,
        source_dir=source_dir,
        master_audio=master_audio.name,
        camera_files=cameras,
        resolve=resolve,
    )


def take_to_dict(take: TakeConfig) -> dict[str, Any]:
    payload = asdict(take)
    payload["source_dir"] = str(take.source_dir)
    payload.pop("take_file", None)
    return payload


def write_take(take: TakeConfig, output_path: Path | None = None) -> Path:
    destination = (output_path or take.take_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(take_to_dict(take), handle, allow_unicode=True, sort_keys=False)
    take.take_file = destination
    return destination


def load_take(take_path: str | Path) -> TakeConfig:
    path = Path(take_path)
    if path.is_dir():
        path = path / "take.yaml"
    path = path.resolve()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    source_dir = Path(data.get("source_dir") or path.parent)
    return TakeConfig(
        session=data["session"],
        date=data["date"],
        source_dir=source_dir,
        master_audio=data["master_audio"],
        edit_audio=data.get("edit_audio"),
        camera_files=_camera_sources_from_list(data["camera_files"]),
        timeline=TimelineConfig(**(data.get("timeline") or {})),
        resolve=ResolveConfig(**(data.get("resolve") or {})),
        validation=ValidationConfig(**(data.get("validation") or {})),
        reports_dir=data.get("reports_dir", "reports"),
        take_file=path,
    )


def session_to_dict(session: SessionProjectConfig) -> dict[str, Any]:
    payload = asdict(session)
    payload["session_root"] = str(session.session_root)
    payload.pop("session_file", None)
    return payload


def write_session(session: SessionProjectConfig, output_path: Path | None = None) -> Path:
    destination = (output_path or session.session_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(session_to_dict(session), handle, allow_unicode=True, sort_keys=False)
    session.session_file = destination
    return destination


def load_session(session_path: str | Path) -> SessionProjectConfig:
    path = Path(session_path)
    if path.is_dir():
        path = path / "session.yaml"
    path = path.resolve()
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    session_root = Path(data.get("session_root") or path.parent)
    return SessionProjectConfig(
        session_id=data["session_id"],
        session_title=data["session_title"],
        date=data["date"],
        session_root=session_root,
        angles=list(data.get("angles") or []),
        reference_angle=data.get("reference_angle"),
        logic=LogicConfig(**(data.get("logic") or {})),
        resolve=ResolveConfig(**(data.get("resolve") or {})),
        timeline=TimelineConfig(**(data.get("timeline") or {})),
        takes=_session_take_refs_from_list(data.get("takes") or []),
        reports_dir=data.get("reports_dir", "reports"),
        session_file=path,
    )


def discover_logic_project(session_root: Path) -> str | None:
    candidates: list[Path] = []
    logic_dir = session_root / "logic"
    if logic_dir.exists():
        candidates.extend(sorted([path for path in logic_dir.iterdir() if path.name.endswith(".logicx")]))
    candidates.extend(sorted([path for path in session_root.iterdir() if path.name.endswith(".logicx")]))
    unique_candidates = sorted({path.resolve(): path for path in candidates}.values())
    if len(unique_candidates) > 1:
        raise RuntimeError(f"multiple Logic projects found in {session_root}")
    if not unique_candidates:
        return None
    return str(unique_candidates[0].relative_to(session_root))


def normalize_take_for_session(
    take: TakeConfig,
    *,
    session: SessionProjectConfig,
    take_id: str,
    take_dir: Path,
) -> TakeConfig:
    take.session = session.session_id
    take.date = session.date
    take.source_dir = take_dir.resolve()
    take.resolve.project_name = session.resolve.project_name
    take.timeline = _copy_timeline_config(session.timeline)
    take.take_file = take_dir / "take.yaml"
    return take


def initialize_session(
    session_root: Path,
    *,
    project_name: str | None = None,
    project_library_name: str | None = None,
) -> SessionProjectConfig:
    session_root = session_root.resolve()
    if not session_root.is_dir():
        raise FileNotFoundError(f"session directory not found: {session_root}")

    session_path = session_root / "session.yaml"
    if session_path.exists():
        session = load_session(session_path)
        session.session_root = session_root
    else:
        session_date, session_title = derive_date_and_title(session_root.name)
        session_id = project_name_for_path(session_root)
        session = SessionProjectConfig(
            session_id=session_id,
            session_title=session_title,
            date=session_date,
            session_root=session_root,
            resolve=ResolveConfig(project_name=session_id),
        )

    session.session_id = project_name_for_path(session_root)
    session.date, derived_title = derive_date_and_title(session_root.name)
    if not session.session_title:
        session.session_title = derived_title
    session.resolve.project_name = project_name or session.resolve.project_name or session.session_id
    if project_library_name is not None:
        session.resolve.project_library_name = project_library_name
    session.logic.project_file = discover_logic_project(session_root) or session.logic.project_file
    # This repo is intentionally scoped to Sony α6400 PP10 HLG source and
    # YouTube SDR delivery. Older sessions may still carry the previous
    # DaVinci WG/Intermediate timeline value; migrate them when the session
    # config is normalized so Resolve, still rendering, and CDL preview use
    # one explicit HLG -> Rec.709 path.
    defaults = TimelineConfig()
    session.timeline.input_color_space = defaults.input_color_space
    session.timeline.timeline_color_space = defaults.timeline_color_space
    session.timeline.output_color_space = defaults.output_color_space

    takes_root = session_root / "takes"
    if not takes_root.is_dir():
        raise FileNotFoundError(f"takes directory not found: {takes_root}")

    take_refs: list[SessionTakeRef] = []
    for take_dir in sorted([path for path in takes_root.iterdir() if path.is_dir() and not path.name.startswith(".")]):
        take_path = take_dir / "take.yaml"
        if take_path.exists():
            take = load_take(take_path)
        else:
            take = discover_take(take_dir, session_root=session_root)
        take = normalize_take_for_session(take, session=session, take_id=take_dir.name, take_dir=take_dir)
        write_take(take, take_path)
        take_refs.append(SessionTakeRef(id=take_dir.name, take=str(take_path.relative_to(session_root))))

    if not take_refs:
        raise RuntimeError(f"no take directories found in {takes_root}")

    session.takes = take_refs
    write_session(session, session_path)
    return session


def iter_session_takes(session: SessionProjectConfig) -> list[tuple[SessionTakeRef, TakeConfig]]:
    takes: list[tuple[SessionTakeRef, TakeConfig]] = []
    for take_ref in session.takes:
        take = load_take(session.resolve_path(take_ref.take))
        takes.append((take_ref, take))
    return takes
