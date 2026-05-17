from __future__ import annotations

import contextlib
import io
import json
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
from piano_guard.config import load_session
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
