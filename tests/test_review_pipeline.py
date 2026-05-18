from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import cv2
import numpy as np
import yaml

from piano_guard import cli
from piano_guard import autogroup
from piano_guard import resolve_ops
from piano_guard.handoff import write_operator_handoff
from piano_guard.review import MANUAL_CDL_VERSION_NAME, build_review_manifest, write_contact_sheet
from piano_guard.config import iter_session_takes, load_session
from piano_guard.stills import preview_cdl_on_still


def _write_session(root: Path, *, reference_angle: str | None, takes: dict[str, list[str]] | None = None) -> None:
    takes = takes or {"take-01": ["angle-b", "angle-c"]}
    all_angles = sorted({angle for angles in takes.values() for angle in angles})
    take_refs = []

    session = {
        "session_id": "test-session",
        "session_title": "test-session",
        "date": "2026-05-16",
        "session_root": str(root),
        "angles": all_angles,
        "reference_angle": reference_angle,
        "logic": {"project_file": None},
        "resolve": {"project_name": "test-session"},
        "timeline": {},
        "takes": take_refs,
        "reports_dir": "reports",
    }
    for take_id, angles in takes.items():
        take_dir = root / "takes" / take_id
        take_dir.mkdir(parents=True)
        (take_dir / "audio.wav").write_bytes(b"")
        for angle in angles:
            (take_dir / f"{angle}.mp4").write_bytes(b"")
        take_refs.append({"id": take_id, "take": f"takes/{take_id}/take.yaml"})
        take = {
            "session": "test-session",
            "date": "2026-05-16",
            "source_dir": str(take_dir),
            "master_audio": "audio.wav",
            "camera_files": [{"file": f"{angle}.mp4", "label": angle} for angle in angles],
            "timeline": {},
            "resolve": {"project_name": "test-session"},
            "validation": {},
            "reports_dir": "reports",
        }
        (take_dir / "take.yaml").write_text(yaml.safe_dump(take, sort_keys=False), encoding="utf-8")

    (root / "session.yaml").write_text(yaml.safe_dump(session, sort_keys=False), encoding="utf-8")


def _write_stills(root: Path, takes: dict[str, list[str]]) -> None:
    for take_id, angles in takes.items():
        still_dir = root / "reports" / "stills" / take_id
        still_dir.mkdir(parents=True, exist_ok=True)
        for index, angle in enumerate(angles):
            color = 40 + index * 40
            image = np.full((24, 36, 3), color, dtype=np.uint8)
            cv2.imwrite(str(still_dir / f"{angle}.png"), image)


class FakeResolveClip:
    def __init__(self, name: str, path: Path, frames: int = 100) -> None:
        self._name = name
        self._path = str(path.resolve())
        self._frames = frames

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self) -> dict[str, str]:
        return {"File Path": self._path, "Frames": str(self._frames)}


class FakeTimelineItem:
    def __init__(self, name: str, start: int) -> None:
        self._name = name
        self._start = start

    def GetName(self) -> str:
        return self._name

    def GetStart(self, _subframe_precision: bool = False) -> int:
        return self._start


class FakeResolveFolder:
    def __init__(
        self,
        name: str,
        *,
        clips: list[FakeResolveClip] | None = None,
        subfolders: list["FakeResolveFolder"] | None = None,
    ) -> None:
        self._name = name
        self._clips = clips or []
        self._subfolders = subfolders or []

    def GetName(self) -> str:
        return self._name

    def GetClipList(self) -> list[FakeResolveClip]:
        return list(self._clips)

    def GetSubFolderList(self) -> list["FakeResolveFolder"]:
        return list(self._subfolders)


class FakeResolveTimeline:
    def __init__(self, name: str) -> None:
        self._name = name
        self._track_counts = {"video": 1, "audio": 1}
        self._track_names: dict[tuple[str, int], str] = {}
        self._items: dict[tuple[str, int], list[FakeTimelineItem]] = {}
        self._markers: dict[int, dict[str, str | int]] = {}
        self._start_timecode = "01:00:00;00"

    def GetName(self) -> str:
        return self._name

    def SetStartTimecode(self, timecode: str) -> bool:
        self._start_timecode = timecode
        return True

    def GetStartTimecode(self) -> str:
        return self._start_timecode

    def GetStartFrame(self) -> int:
        return 0 if self._start_timecode == resolve_ops.COLOR_PREP_START_TIMECODE else 107892

    def GetTrackCount(self, track_type: str) -> int:
        return self._track_counts.get(track_type, 0)

    def AddTrack(self, track_type: str, *_args) -> bool:
        self._track_counts[track_type] = self._track_counts.get(track_type, 0) + 1
        return True

    def SetTrackName(self, track_type: str, index: int, name: str) -> bool:
        self._track_names[(track_type, index)] = name
        return True

    def GetTrackName(self, track_type: str, index: int) -> str:
        return self._track_names.get((track_type, index), "")

    def AddMarker(self, frame: int, color: str, name: str, note: str, duration: int, custom_data: str) -> bool:
        self._markers[frame] = {
            "color": color,
            "name": name,
            "note": note,
            "duration": duration,
            "customData": custom_data,
        }
        return True

    def GetMarkers(self) -> dict[int, dict[str, str | int]]:
        return dict(self._markers)

    def add_item(self, track_type: str, track_index: int, item: FakeTimelineItem) -> None:
        self._items.setdefault((track_type, track_index), []).append(item)

    def GetItemListInTrack(self, track_type: str, index: int) -> list[FakeTimelineItem]:
        return list(self._items.get((track_type, index), []))


class FakeResolveProject:
    def __init__(self) -> None:
        self.timelines: list[FakeResolveTimeline] = []
        self.current_timeline: FakeResolveTimeline | None = None

    def SetCurrentTimeline(self, timeline: FakeResolveTimeline) -> bool:
        self.current_timeline = timeline
        return True

    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    def GetTimelineByIndex(self, index: int) -> FakeResolveTimeline | None:
        return self.timelines[index - 1] if 1 <= index <= len(self.timelines) else None


class FakeResolveMediaPool:
    def __init__(self, project: FakeResolveProject) -> None:
        self.project = project
        self.current_folder: FakeResolveFolder | None = None
        self.append_calls: list[dict[str, object]] = []

    def SetCurrentFolder(self, folder: FakeResolveFolder) -> bool:
        self.current_folder = folder
        return True

    def CreateEmptyTimeline(self, name: str) -> FakeResolveTimeline:
        timeline = FakeResolveTimeline(name)
        self.project.timelines.append(timeline)
        return timeline

    def DeleteTimelines(self, timelines: list[FakeResolveTimeline]) -> bool:
        for timeline in timelines:
            if timeline in self.project.timelines:
                self.project.timelines.remove(timeline)
        return True

    def AppendToTimeline(self, clip_infos: list[dict[str, object]]) -> list[FakeTimelineItem]:
        timeline = self.project.current_timeline
        assert timeline is not None
        items = []
        for info in clip_infos:
            self.append_calls.append(dict(info))
            clip = info["mediaPoolItem"]
            assert isinstance(clip, FakeResolveClip)
            media_type = int(info["mediaType"])
            track_type = "video" if media_type == resolve_ops.VIDEO_ONLY_MEDIA_TYPE else "audio"
            track_index = int(info["trackIndex"])
            item = FakeTimelineItem(clip.GetName(), int(info["recordFrame"]))
            timeline.add_item(track_type, track_index, item)
            items.append(item)
        return items


def _fake_takes_root(session) -> FakeResolveFolder:
    take_folders = []
    for take_ref, take in iter_session_takes(session):
        clips = [
            FakeResolveClip(camera.label, take.resolve_path(camera.file), frames=100 + index * 10)
            for index, camera in enumerate(take.camera_files)
        ]
        clips.append(FakeResolveClip("audio-master", take.editing_audio_path(), frames=130))
        take_folders.append(FakeResolveFolder(take_ref.id, clips=clips))
    return FakeResolveFolder("Takes", subfolders=take_folders)


class ReviewPipelineTests(unittest.TestCase):
    def test_preview_cdl_on_still_applies_slope_and_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.png"
            out = root / "out.png"
            rgb = np.array([[[100, 150, 200], [10, 20, 30]]], dtype=np.uint8)
            cv2.imwrite(str(source), cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))

            preview_cdl_on_still(source, (1.1, 0.5, 2.0), (0.0, 0.1, -0.1), out)

            actual = cv2.cvtColor(cv2.imread(str(out)), cv2.COLOR_BGR2RGB)
            expected = np.clip((rgb.astype(np.float32) / 255.0) * np.array([1.1, 0.5, 2.0]) + np.array([0.0, 0.1, -0.1]), 0, 1)
            expected = (expected * 255.0).astype(np.uint8)
            np.testing.assert_array_equal(actual, expected)

    def test_review_manifest_does_not_require_reference_angle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            still_dir = root / "reports" / "stills" / "take-01"
            still_dir.mkdir(parents=True)
            (still_dir / "angle-b.png").write_bytes(b"png")
            (still_dir / "angle-c.png").write_bytes(b"png")
            session = load_session(root / "session.yaml")

            payload = build_review_manifest(session)

            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["review_mode"], "all_clips")

    def test_review_manifest_classifies_all_clips_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle="angle-b")
            _write_stills(root, {"take-01": ["angle-b", "angle-c"]})
            session = load_session(root / "session.yaml")

            payload = build_review_manifest(session)

            self.assertEqual(payload["status"], "PASS")
            actions = {clip["clip_id"]: clip["action"] for clip in payload["clips"]}
            self.assertEqual(actions["take-01/angle-b"], "review")
            self.assertEqual(actions["take-01/angle-c"], "review")
            self.assertFalse(any(clip["is_reference"] for clip in payload["clips"]))

    def test_review_manifest_summarizes_angle_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            takes = {
                "take-01": ["angle-b", "angle-c"],
                "take-02": ["angle-b", "angle-d"],
            }
            _write_session(root, reference_angle=None, takes=takes)
            _write_stills(root, takes)
            session = load_session(root / "session.yaml")

            payload = build_review_manifest(session)

            self.assertEqual(payload["status"], "PASS")
            summary = payload["angle_summary"]
            self.assertEqual(summary["all_angles"], ["angle-b", "angle-c", "angle-d"])
            self.assertEqual(summary["common_angles"], ["angle-b"])
            self.assertEqual(summary["missing_by_take"]["take-01"], ["angle-d"])
            self.assertEqual(summary["missing_by_take"]["take-02"], ["angle-c"])

    def test_contact_sheet_uses_fixed_angle_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            takes = {
                "take-01": ["angle-b", "angle-c"],
                "take-02": ["angle-b", "angle-d"],
            }
            _write_session(root, reference_angle=None, takes=takes)
            _write_stills(root, takes)
            session = load_session(root / "session.yaml")
            out = root / "sheet.png"

            payload = write_contact_sheet(
                session,
                out,
                cell_width=80,
                cell_height=50,
                label_height=18,
                header_height=24,
                take_label_width=42,
            )

            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["angle_columns"], ["angle-b", "angle-c", "angle-d"])
            image = cv2.imread(str(out))
            self.assertIsNotNone(image)
            self.assertEqual(image.shape[0], 24 + 2 * (50 + 18))
            self.assertEqual(image.shape[1], 42 + 3 * 80)

    def test_review_manifest_fails_when_still_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            session = load_session(root / "session.yaml")

            payload = build_review_manifest(session)

            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual({issue["code"] for issue in payload["issues"]}, {"still_missing"})

    def test_manual_cdl_dry_run_returns_auto_state_99(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle="angle-b")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = cli.main(
                    [
                        "manual-cdl",
                        str(root),
                        "--clip-id",
                        "take-01/angle-c",
                        "--slope",
                        "1,1,1",
                        "--offset",
                        "0,0,0",
                        "--dry-run",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["version_name"], MANUAL_CDL_VERSION_NAME)
            self.assertEqual(payload["clip_id"], "take-01/angle-c")
            self.assertEqual(payload["slope"], [1.0, 1.0, 1.0])
            self.assertEqual(payload["offset"], [0.0, 0.0, 0.0])

    def test_operator_handoff_writes_manual_workflow_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            session = load_session(root / "session.yaml")

            payload = write_operator_handoff(session)

            handoff_path = Path(payload["handoff_path"])
            self.assertTrue(handoff_path.is_file())
            text = handoff_path.read_text(encoding="utf-8")
            self.assertIn("Manual Resolve Workflow", text)
            self.assertIn("Local Grades", text)
            self.assertIn("CDL commands are experimental", text)
            self.assertEqual(payload["take_count"], 1)
            self.assertEqual(payload["takes"][0]["edit_audio"], "audio.wav")

    def test_take_order_report_uses_same_angle_file_times(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            takes = {
                "take-01": ["angle-a", "angle-b"],
                "take-02": ["angle-a", "angle-b"],
                "take-03": ["angle-a", "angle-b"],
            }
            _write_session(root, reference_angle=None, takes=takes)
            order_times = {"take-02": 1000.0, "take-03": 2000.0, "take-01": 3000.0}
            for take_id, timestamp in order_times.items():
                take_dir = root / "takes" / take_id
                for filename in ["audio.wav", "angle-a.mp4", "angle-b.mp4"]:
                    os.utime(take_dir / filename, (timestamp, timestamp))
            session = load_session(root / "session.yaml")

            _json_path, markdown_path, report = autogroup.write_take_order_reports(session)

            self.assertEqual([entry.take_id for entry in report.inferred_order], ["take-02", "take-03", "take-01"])
            self.assertTrue(markdown_path.is_file())
            self.assertIn("take-02 -> take-03 -> take-01", markdown_path.read_text(encoding="utf-8"))

    def test_cli_help_shows_standard_pipeline_and_hides_cdl_commands(self) -> None:
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stdout(stdout):
                cli.build_parser().parse_args(["--help"])

        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        self.assertIn("group-session", help_text)
        self.assertIn("prepare-resolve-session", help_text)
        self.assertIn("inspect-resolve-session", help_text)
        self.assertIn("operator-handoff", help_text)
        self.assertIn("render-stills", help_text)
        self.assertIn("contact-sheet", help_text)
        self.assertNotIn("manual-cdl", help_text)
        self.assertNotIn("preview-cdl", help_text)

    def test_prepare_help_shows_rebuild_color_prep_flag(self) -> None:
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with contextlib.redirect_stdout(stdout):
                cli.build_parser().parse_args(["prepare-resolve-session", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--rebuild-color-prep", stdout.getvalue())

    def test_color_prep_timeline_uses_compact_tracks_and_record_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            takes = {
                "take-01": ["angle-b", "angle-c"],
                "take-02": ["angle-b", "angle-d"],
            }
            _write_session(root, reference_angle=None, takes=takes)
            session = load_session(root / "session.yaml")
            project = FakeResolveProject()
            media_pool = FakeResolveMediaPool(project)
            takes_root = _fake_takes_root(session)
            timelines_folder = FakeResolveFolder("Timelines")

            def fake_offset(_audio_path, video_path, *, frame_rate):
                offsets = {"angle-b": 0, "angle-c": 10, "angle-d": -5}
                return {
                    "offset_frames": offsets[Path(video_path).stem],
                    "offset_seconds": offsets[Path(video_path).stem] / frame_rate,
                    "confidence": 0.9,
                }

            with mock.patch.object(resolve_ops, "_estimate_color_prep_sync_offset", side_effect=fake_offset):
                payload = resolve_ops._build_color_prep_timeline(
                    project=project,
                    media_pool=media_pool,
                    takes_root=takes_root,
                    timelines_folder=timelines_folder,
                    session=session,
                    rebuild=False,
                )

            self.assertEqual(payload["status"], "PASS")
            self.assertEqual(payload["start_timecode"], "00:00:00;00")
            self.assertEqual(payload["layout"], "compact")
            self.assertEqual(payload["angles"], ["angle-b", "angle-c", "angle-d"])
            self.assertEqual(payload["video_track_map"], {"compact-v1": 1, "compact-v2": 2})
            self.assertEqual(payload["max_video_tracks"], 2)
            self.assertEqual(payload["gap_frames"], 150)
            self.assertEqual([take["record_frame"] for take in payload["takes"]], [0, 280])
            self.assertEqual([take["marker_frame"] for take in payload["takes"]], [0, 285])
            self.assertEqual(payload["takes"][0]["audio"]["record_frame"], 0)
            self.assertEqual(payload["takes"][1]["audio"]["record_frame"], 285)
            self.assertEqual(payload["takes"][1]["timeline_zero_shift_frames"], 5)

            calls = media_pool.append_calls
            self.assertEqual(len(calls), 6)
            self.assertEqual(
                [(call["mediaType"], call["trackIndex"], call["recordFrame"]) for call in calls],
                [
                    (resolve_ops.VIDEO_ONLY_MEDIA_TYPE, 1, 0),
                    (resolve_ops.VIDEO_ONLY_MEDIA_TYPE, 2, 10),
                    (resolve_ops.AUDIO_ONLY_MEDIA_TYPE, 1, 0),
                    (resolve_ops.VIDEO_ONLY_MEDIA_TYPE, 1, 285),
                    (resolve_ops.VIDEO_ONLY_MEDIA_TYPE, 2, 280),
                    (resolve_ops.AUDIO_ONLY_MEDIA_TYPE, 1, 285),
                ],
            )

            timeline = project.timelines[0]
            self.assertEqual(timeline.GetStartTimecode(), "00:00:00;00")
            self.assertEqual(timeline.GetTrackName("video", 1), "compact-v1")
            self.assertEqual(timeline.GetTrackName("video", 2), "compact-v2")
            self.assertEqual(timeline.GetTrackName("audio", 1), "master-audio")
            self.assertIn("piano_guard:color_prep:take-01", {marker["customData"] for marker in timeline.GetMarkers().values()})
            self.assertEqual(
                {marker["customData"]: frame for frame, marker in timeline.GetMarkers().items()},
                {"piano_guard:color_prep:take-01": 0, "piano_guard:color_prep:take-02": 285},
            )

            inspected, issues = resolve_ops._inspect_color_prep_timeline(project, session)
            self.assertEqual(inspected["start_timecode"], "00:00:00;00")
            self.assertEqual(inspected["start_frame"], 0)
            self.assertEqual(inspected["layout"], "compact")
            self.assertEqual(inspected["expected_video_track_count"], 2)
            self.assertEqual([take["marker_frame"] for take in inspected["takes"]], [0, 285])
            self.assertEqual(inspected["takes"][1]["audio_start"], 285)
            self.assertEqual(
                [
                    angle["start_frame"]
                    for angle in inspected["takes"][1]["angles"]
                    if angle["angle"] == "angle-d"
                ],
                [280],
            )
            self.assertEqual(issues, [])

    def test_inspect_color_prep_timeline_fails_on_default_start_timecode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            session = load_session(root / "session.yaml")
            project = FakeResolveProject()
            timeline = FakeResolveTimeline(session.resolve.color_prep_timeline_name)
            project.timelines.append(timeline)

            _take_ref, take = list(iter_session_takes(session))[0]
            for track_index, camera in enumerate(take.camera_files, start=1):
                timeline.add_item("video", track_index, FakeTimelineItem(camera.label, 0))
            timeline.add_item("audio", 1, FakeTimelineItem("audio-master", 0))
            timeline.AddMarker(0, "Blue", "take-01", "", 1, "piano_guard:color_prep:take-01")

            _payload, issues = resolve_ops._inspect_color_prep_timeline(project, session)

            self.assertIn("color_prep_start_timecode_mismatch", {issue.code for issue in issues})

    def test_color_prep_timeline_preserves_existing_unless_rebuild_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            session = load_session(root / "session.yaml")
            project = FakeResolveProject()
            existing = FakeResolveTimeline(session.resolve.color_prep_timeline_name)
            project.timelines.append(existing)
            media_pool = FakeResolveMediaPool(project)
            takes_root = _fake_takes_root(session)
            timelines_folder = FakeResolveFolder("Timelines")

            preserved = resolve_ops._build_color_prep_timeline(
                project=project,
                media_pool=media_pool,
                takes_root=takes_root,
                timelines_folder=timelines_folder,
                session=session,
                rebuild=False,
            )

            self.assertEqual(preserved["status"], "WARN")
            self.assertEqual(preserved["action"], "preserved-existing")
            self.assertEqual(len(project.timelines), 1)
            self.assertEqual(media_pool.append_calls, [])

            with mock.patch.object(
                resolve_ops,
                "_estimate_color_prep_sync_offset",
                return_value={"offset_frames": 0, "offset_seconds": 0.0, "confidence": 1.0},
            ):
                rebuilt = resolve_ops._build_color_prep_timeline(
                    project=project,
                    media_pool=media_pool,
                    takes_root=takes_root,
                    timelines_folder=timelines_folder,
                    session=session,
                    rebuild=True,
                )

            self.assertEqual(rebuilt["status"], "PASS")
            self.assertEqual(rebuilt["action"], "rebuilt")
            self.assertEqual(len(project.timelines), 1)
            self.assertIsNot(project.timelines[0], existing)
            self.assertTrue(media_pool.append_calls)

    def test_inspect_color_prep_timeline_reports_missing_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_session(root, reference_angle=None)
            session = load_session(root / "session.yaml")
            project = FakeResolveProject()
            timeline = FakeResolveTimeline(session.resolve.color_prep_timeline_name)
            project.timelines.append(timeline)

            payload, issues = resolve_ops._inspect_color_prep_timeline(project, session)

            self.assertTrue(payload["exists"])
            self.assertIn("color_prep_take_marker_missing", {issue.code for issue in issues})
            self.assertIn("color_prep_angle_item_missing", {issue.code for issue in issues})
            self.assertIn("color_prep_audio_item_missing", {issue.code for issue in issues})

    def test_color_prep_sync_offset_is_signed(self) -> None:
        master = np.zeros(30, dtype=np.float32)
        master[10] = 1.0
        late_video = np.zeros(20, dtype=np.float32)
        late_video[7] = 1.0
        early_video = np.zeros(20, dtype=np.float32)
        early_video[12] = 1.0

        with mock.patch.object(resolve_ops, "_color_prep_sync_envelope", side_effect=[master, late_video]):
            late = resolve_ops._estimate_color_prep_sync_offset(Path("audio.aif"), Path("angle-a.mp4"), frame_rate=20.0)
        with mock.patch.object(resolve_ops, "_color_prep_sync_envelope", side_effect=[master, early_video]):
            early = resolve_ops._estimate_color_prep_sync_offset(Path("audio.aif"), Path("angle-b.mp4"), frame_rate=20.0)

        self.assertEqual(late["offset_frames"], 3)
        self.assertEqual(early["offset_frames"], -2)

    def test_resolve_storage_locations_repoint_offline_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.dat"
            missing_root = root / "missing-disk"
            storage_root = root / "online-disk"
            config.write_text(
                "\n".join(
                    [
                        "Site.1.FS.Count = 1",
                        f"Site.1.FS.1.Root = {missing_root}",
                        "RenderCaching.FsNo = 1",
                        "RenderCaching.CacheDir = CacheClip",
                        "System.Gallery.DtMgr.FileSys = 1",
                        "System.Gallery.Folder = .gallery",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(resolve_ops, "_is_resolve_running", return_value=True):
                payload = resolve_ops.ensure_resolve_storage_locations(
                    storage_root,
                    config_dat_path=config,
                )

            self.assertEqual(payload["status"], "WARN")
            self.assertTrue(payload["config_updated"])
            self.assertTrue(payload["restart_required"])
            self.assertTrue(Path(payload["backup_path"]).is_file())
            self.assertEqual(payload["previous_storage_root"], str(missing_root))
            self.assertTrue((storage_root / "CacheClip").is_dir())
            self.assertTrue((storage_root / ".gallery").is_dir())
            self.assertTrue((storage_root / "Resolve Project Backups").is_dir())
            updated = config.read_text(encoding="utf-8")
            self.assertIn(f"Site.1.FS.1.Root = {storage_root.resolve()}", updated)

    def test_resolve_storage_locations_is_idempotent_when_paths_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.dat"
            storage_root = root / "online-disk"
            (storage_root / "CacheClip").mkdir(parents=True)
            (storage_root / ".gallery").mkdir()
            (storage_root / "Resolve Project Backups").mkdir()
            config.write_text(
                "\n".join(
                    [
                        f"Site.1.FS.1.Root = {storage_root.resolve()}",
                        "RenderCaching.FsNo = 1",
                        "RenderCaching.CacheDir = CacheClip",
                        "System.Gallery.DtMgr.FileSys = 1",
                        "System.Gallery.Folder = .gallery",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(resolve_ops, "_is_resolve_running", return_value=True):
                payload = resolve_ops.ensure_resolve_storage_locations(
                    storage_root,
                    config_dat_path=config,
                )

            self.assertEqual(payload["status"], "PASS")
            self.assertFalse(payload["config_updated"])
            self.assertFalse(payload["restart_required"])
            self.assertEqual(payload["issues"], [])

    def test_default_incoming_directory_is_canonical_incoming_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "incomings").mkdir()

            self.assertIsNone(cli._find_incoming_dir(root, None))
            with self.assertRaisesRegex(FileNotFoundError, "expected incoming/"):
                autogroup._resolve_incoming_dir(root, None)

            (root / "incoming").mkdir()
            self.assertEqual(cli._find_incoming_dir(root, None), (root / "incoming").resolve())
            name, path = autogroup._resolve_incoming_dir(root, None)
            self.assertEqual(name, "incoming")
            self.assertEqual(path, (root / "incoming").resolve())


if __name__ == "__main__":
    unittest.main()
