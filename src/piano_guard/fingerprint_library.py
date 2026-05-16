"""Cross-session fingerprint library for Phase 2 Milestone 4.

Solves a real operational problem: the same operator records 20-30 sessions
per year in the same physical room with the same piano and the same camera
rig. Phase 2 Milestone 1-3 re-computes the same clustering and CDL fit
every time, even when the lighting has barely changed since the previous
session. The library caches ``(fingerprint → CDL)`` pairs keyed by the
camera rig hash so repeat sessions can short-circuit the fit step.

Storage: human-inspectable JSON at
``~/Library/Application Support/piano-guard/fingerprint-library.json``
by default. Override via ``session.yaml`` ``library_path`` (future) or
``--library`` on the CLI (future). Operators can delete individual entries
to force re-fit on the next run.

Scope of the cache:

  * Library matching assumes that when a new fingerprint closely matches a
    stored one (cosine similarity ≥ MATCH_THRESHOLD), the stored CDL is
    still valid. This is correct when the overall rig + environment has
    not drifted between the two recordings.
  * The library does NOT validate that the reference fingerprint used at
    library-write time still matches the current session's reference
    state. This is a known simplification — real-world usage (same room,
    same time of day, same lamps) usually stabilizes the reference too.

Schema version v1. Breaking schema changes bump the version and drop
incompatible entries at load time with a warn.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from piano_guard.auto_segment import (
    FEATURE_DIM,
    LightingFingerprint,
    fingerprint_feature_vector,
)
from piano_guard.color_match import CDLResult
from piano_guard.config import SessionProjectConfig


SCHEMA_VERSION = 1
"""Schema version of the on-disk library JSON. Entries with a different
schema_version are dropped at load time (logged to the returned issues)."""

MATCH_THRESHOLD = 0.98
"""Cosine similarity above which two fingerprints are considered the same
lighting state. Tuned empirically: identical regimes at different times
of day score ~0.998; distinct regimes within one session score ~0.90-0.94;
day vs night in the same room scores ~0.80. 0.98 keeps the library
conservative — better to re-fit than to apply a stale CDL."""

REFERENCE_DRIFT_THRESHOLD = 0.98
"""Cosine similarity threshold on the stored ``reference_fingerprint`` vs
the current session's reference-state medoid. When a library entry's
reference has drifted below this (lamp temperature shifted, new bulbs,
seasonal ambient change), reusing the cached CDL would re-target a stale
anchor. We skip the hit and force a cold fit. Use the same threshold as
MATCH_THRESHOLD for now — both are asking the same question (is this
'the same lighting'), just on different observables."""


# --- rig + room identity ---


def compute_rig_hash(session: SessionProjectConfig) -> str:
    """Derive a stable hash of the session's camera rig.

    Matches across sessions with identical angle order, resolution, frame
    rate, and input color space. Truncated to 16 hex chars for human
    readability in the JSON.
    """
    parts = [
        ",".join(session.angles),
        str(session.timeline.frame_rate),
        f"{session.timeline.width}x{session.timeline.height}",
        str(session.timeline.input_color_space),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _compute_entry_id(rig_hash: str, feature_vec: np.ndarray, created_at: str) -> str:
    """Stable short ID for a library entry."""
    payload = f"{rig_hash}|{feature_vec.tolist()}|{created_at}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def default_library_path() -> Path:
    """Default location for the library JSON.

    Honors ``PIANO_GUARD_LIBRARY_PATH`` for tests; otherwise returns the
    macOS Application Support directory. Parent dirs are NOT created — the
    caller is responsible via ``save_library``.
    """
    env_override = os.environ.get("PIANO_GUARD_LIBRARY_PATH")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "piano-guard" / "fingerprint-library.json"


# --- data shapes ---


@dataclass
class LibraryEntry:
    """One cached (fingerprint → CDL) pair.

    ``fingerprint`` is the state medoid at the time this entry was written;
    ``cdl_slope`` / ``cdl_offset`` / ``cdl_power`` are the CDL that brought
    this fingerprint to the reference state used during that original fit.

    ``reference_fingerprint`` captures the target the CDL was fit against.
    At match time we check it drifted within tolerance from the current
    session's reference; if not, we re-fit.
    """

    entry_id: str
    rig_hash: str
    room_id: str                    # operator-chosen label, may be "" when unset
    fingerprint: LightingFingerprint
    reference_fingerprint: LightingFingerprint | None
    cdl_slope: tuple[float, float, float]
    cdl_offset: tuple[float, float, float]
    cdl_power: tuple[float, float, float] = (1.0, 1.0, 1.0)
    cdl_status: str = "pass"
    cdl_code: str = ""
    state_name: str = ""            # optional operator label (e.g., "day-ambient")
    created_at: str = ""            # ISO 8601
    last_used_at: str = ""
    match_count: int = 0

    @property
    def feature_vector(self) -> np.ndarray:
        return fingerprint_feature_vector(self.fingerprint)

    @property
    def reference_feature_vector(self) -> np.ndarray | None:
        if self.reference_fingerprint is None:
            return None
        return fingerprint_feature_vector(self.reference_fingerprint)

    def to_cdl_result(self, *, target_label: str, reference_label: str) -> CDLResult:
        return CDLResult(
            target_angle=target_label,
            reference_angle=reference_label,
            slope_rgb=tuple(self.cdl_slope),  # type: ignore[arg-type]
            offset_rgb=tuple(self.cdl_offset),  # type: ignore[arg-type]
            power_rgb=tuple(self.cdl_power),  # type: ignore[arg-type]
            residuals={},
            status=self.cdl_status,
            code=self.cdl_code,
        )


# --- serialization ---


def _fp_to_dict(fp: LightingFingerprint) -> dict[str, Any]:
    return {
        "white_key_rgb": [float(x) for x in fp.white_key_rgb],
        "piano_body_rgb": [float(x) for x in fp.piano_body_rgb],
        "luma_p05": float(fp.luma_p05),
        "luma_p50": float(fp.luma_p50),
        "luma_p95": float(fp.luma_p95),
        "estimated_cct_kelvin": float(fp.estimated_cct_kelvin),
        "white_key_pixels": int(fp.white_key_pixels),
        "body_pixels": int(fp.body_pixels),
        "window_start_s": float(fp.window_start_s),
        "window_end_s": float(fp.window_end_s),
        "frame_indices": list(fp.frame_indices),
    }


def _fp_from_dict(d: dict[str, Any]) -> LightingFingerprint:
    return LightingFingerprint(
        white_key_rgb=np.array(d["white_key_rgb"], dtype=np.float32),
        piano_body_rgb=np.array(d["piano_body_rgb"], dtype=np.float32),
        luma_p05=float(d["luma_p05"]),
        luma_p50=float(d["luma_p50"]),
        luma_p95=float(d["luma_p95"]),
        estimated_cct_kelvin=float(d["estimated_cct_kelvin"]),
        white_key_pixels=int(d["white_key_pixels"]),
        body_pixels=int(d["body_pixels"]),
        window_start_s=float(d.get("window_start_s", 0.0)),
        window_end_s=float(d.get("window_end_s", 0.0)),
        frame_indices=list(d.get("frame_indices", [])),
    )


def _entry_to_dict(e: LibraryEntry) -> dict[str, Any]:
    return {
        "entry_id": e.entry_id,
        "rig_hash": e.rig_hash,
        "room_id": e.room_id,
        "state_name": e.state_name,
        "fingerprint": _fp_to_dict(e.fingerprint),
        "reference_fingerprint": (
            _fp_to_dict(e.reference_fingerprint) if e.reference_fingerprint is not None else None
        ),
        "cdl_slope": list(e.cdl_slope),
        "cdl_offset": list(e.cdl_offset),
        "cdl_power": list(e.cdl_power),
        "cdl_status": e.cdl_status,
        "cdl_code": e.cdl_code,
        "created_at": e.created_at,
        "last_used_at": e.last_used_at,
        "match_count": int(e.match_count),
    }


def _entry_from_dict(d: dict[str, Any]) -> LibraryEntry:
    ref_fp_raw = d.get("reference_fingerprint")
    return LibraryEntry(
        entry_id=str(d["entry_id"]),
        rig_hash=str(d["rig_hash"]),
        room_id=str(d.get("room_id", "")),
        state_name=str(d.get("state_name", "")),
        fingerprint=_fp_from_dict(d["fingerprint"]),
        reference_fingerprint=_fp_from_dict(ref_fp_raw) if ref_fp_raw is not None else None,
        cdl_slope=tuple(d["cdl_slope"]),  # type: ignore[arg-type]
        cdl_offset=tuple(d["cdl_offset"]),  # type: ignore[arg-type]
        cdl_power=tuple(d.get("cdl_power", (1.0, 1.0, 1.0))),  # type: ignore[arg-type]
        cdl_status=str(d.get("cdl_status", "pass")),
        cdl_code=str(d.get("cdl_code", "")),
        created_at=str(d.get("created_at", "")),
        last_used_at=str(d.get("last_used_at", "")),
        match_count=int(d.get("match_count", 0)),
    )


# --- library container ---


@dataclass
class Library:
    """In-memory library. Use ``load_library`` / ``save_library`` at the
    process boundary.
    """

    schema_version: int = SCHEMA_VERSION
    entries: list[LibraryEntry] = field(default_factory=list)
    load_issues: list[str] = field(default_factory=list)

    def entries_for_rig(
        self, rig_hash: str, *, room_id: str | None = None
    ) -> list[LibraryEntry]:
        """Filter entries to a rig, optionally narrowed by room_id.

        ``room_id=None`` matches everything (legacy behavior); an explicit
        ``room_id=""`` matches only entries written without a room label;
        any other string narrows to that exact label.
        """
        matches = [e for e in self.entries if e.rig_hash == rig_hash]
        if room_id is not None:
            matches = [e for e in matches if e.room_id == room_id]
        return matches

    def find_match(
        self,
        fp: LightingFingerprint,
        *,
        rig_hash: str,
        room_id: str | None = None,
        current_reference_fingerprint: LightingFingerprint | None = None,
        threshold: float = MATCH_THRESHOLD,
        reference_drift_threshold: float = REFERENCE_DRIFT_THRESHOLD,
    ) -> tuple[LibraryEntry, float] | None:
        """Find the best cosine-similarity match within a rig.

        Arguments:

        * ``rig_hash`` — required; only entries matching this rig are
          candidates.
        * ``room_id`` — optional room-label filter. ``None`` (default)
          matches any room for backward compatibility; pass an empty
          string or a specific label to narrow.
        * ``current_reference_fingerprint`` — when provided, entries whose
          stored ``reference_fingerprint`` has drifted below
          ``reference_drift_threshold`` (cosine similarity) from the
          current session's reference are **skipped**. The point of the
          library is to reuse a CDL that brings fingerprint X to reference
          Y; if Y has drifted (new bulbs, seasonal light change), the
          cached CDL would re-target a stale anchor and produce a wrong
          grade. Pass ``None`` to disable the drift check (legacy).

        Returns ``(entry, similarity)`` when a match passes both the
        feature-similarity threshold AND (when applicable) the reference-
        drift check. Ties resolve to the most recently used entry.
        """
        candidates = self.entries_for_rig(rig_hash, room_id=room_id)
        if not candidates:
            return None

        target = fingerprint_feature_vector(fp)
        target_norm = float(np.linalg.norm(target))
        if target_norm < 1e-9:
            return None

        current_ref_vec = None
        current_ref_norm = 0.0
        if current_reference_fingerprint is not None:
            current_ref_vec = fingerprint_feature_vector(current_reference_fingerprint)
            current_ref_norm = float(np.linalg.norm(current_ref_vec))

        best: tuple[LibraryEntry, float] | None = None
        for entry in candidates:
            vec = entry.feature_vector
            denom = target_norm * float(np.linalg.norm(vec))
            if denom < 1e-9:
                continue
            sim = float(np.dot(target, vec) / denom)
            if sim < threshold:
                continue

            # Reference-drift guard: the cached CDL was fit against
            # ``entry.reference_fingerprint``. If the current session's
            # reference-state medoid has drifted, the cached CDL is stale.
            if current_ref_vec is not None and current_ref_norm > 1e-9:
                stored_ref = entry.reference_feature_vector
                if stored_ref is None:
                    # Entry has no stored reference (legacy write) —
                    # conservatively skip so we don't reuse an unanchored CDL.
                    continue
                stored_ref_norm = float(np.linalg.norm(stored_ref))
                if stored_ref_norm < 1e-9:
                    continue
                ref_sim = float(
                    np.dot(current_ref_vec, stored_ref)
                    / (current_ref_norm * stored_ref_norm)
                )
                if ref_sim < reference_drift_threshold:
                    continue

            if best is None or sim > best[1] or (
                sim == best[1] and entry.last_used_at > best[0].last_used_at
            ):
                best = (entry, sim)
        return best

    def add_entry(
        self,
        *,
        rig_hash: str,
        room_id: str,
        fingerprint: LightingFingerprint,
        reference_fingerprint: LightingFingerprint | None,
        cdl: CDLResult,
        state_name: str = "",
    ) -> LibraryEntry:
        now = datetime.now(timezone.utc).isoformat()
        entry_id = _compute_entry_id(rig_hash, fingerprint_feature_vector(fingerprint), now)
        entry = LibraryEntry(
            entry_id=entry_id,
            rig_hash=rig_hash,
            room_id=room_id,
            state_name=state_name,
            fingerprint=fingerprint,
            reference_fingerprint=reference_fingerprint,
            cdl_slope=tuple(cdl.slope_rgb),
            cdl_offset=tuple(cdl.offset_rgb),
            cdl_power=tuple(cdl.power_rgb),
            cdl_status=str(cdl.status),
            cdl_code=str(cdl.code),
            created_at=now,
            last_used_at=now,
            match_count=0,
        )
        self.entries.append(entry)
        return entry

    def touch(self, entry_id: str) -> None:
        """Update ``last_used_at`` and increment ``match_count`` on a hit."""
        now = datetime.now(timezone.utc).isoformat()
        for entry in self.entries:
            if entry.entry_id == entry_id:
                entry.last_used_at = now
                entry.match_count += 1
                return

    def remove(self, entry_id: str) -> bool:
        """Delete an entry by id. Returns True if something was removed."""
        before = len(self.entries)
        self.entries = [e for e in self.entries if e.entry_id != entry_id]
        return len(self.entries) < before


# --- file I/O ---


def load_library(path: Path) -> Library:
    """Load a library from disk. Missing file → empty library (not an error).

    Drops entries with unsupported schema_version and records the reason
    in ``Library.load_issues``.
    """
    if not path.exists():
        return Library(schema_version=SCHEMA_VERSION, entries=[])

    with path.open("r", encoding="utf-8") as handle:
        try:
            raw = json.load(handle)
        except json.JSONDecodeError as exc:
            return Library(
                schema_version=SCHEMA_VERSION,
                entries=[],
                load_issues=[f"library JSON invalid: {exc}"],
            )

    schema = int(raw.get("schema_version", 0))
    issues: list[str] = []
    if schema != SCHEMA_VERSION:
        issues.append(
            f"library schema_version {schema} != expected {SCHEMA_VERSION}; "
            f"dropping all entries (will be rebuilt on next successful fit)"
        )
        return Library(schema_version=SCHEMA_VERSION, entries=[], load_issues=issues)

    entries: list[LibraryEntry] = []
    for idx, entry_raw in enumerate(raw.get("entries") or []):
        try:
            entries.append(_entry_from_dict(entry_raw))
        except (KeyError, ValueError, TypeError) as exc:
            issues.append(f"entry #{idx} invalid ({exc}); skipped")
    return Library(schema_version=SCHEMA_VERSION, entries=entries, load_issues=issues)


def save_library(library: Library, path: Path) -> Path:
    """Write library to disk. Creates parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": library.schema_version,
        "entries": [_entry_to_dict(e) for e in library.entries],
    }
    # Atomic-ish write: write to tmp, rename.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=False)
        handle.write("\n")
    tmp.replace(path)
    return path


__all__ = [
    "SCHEMA_VERSION",
    "MATCH_THRESHOLD",
    "FEATURE_DIM",
    "LibraryEntry",
    "Library",
    "compute_rig_hash",
    "default_library_path",
    "load_library",
    "save_library",
]
