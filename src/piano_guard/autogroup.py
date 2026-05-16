from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import string
from typing import Any, Iterable

import numpy as np
from scipy.cluster.hierarchy import fcluster, linkage
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import squareform

from piano_guard.config import (
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    initialize_session,
    load_session,
    write_session,
)
from piano_guard.fftools import ExternalToolError, probe_media, run_checked_bytes
from piano_guard.reports import Issue, issues_to_dict, overall_status, write_json_report, write_markdown_report


AUDIO_SAMPLE_RATE = 11_025
SIGNATURE_HZ = 20.0
FRAME_SAMPLE_POSITIONS = (0.15, 0.35, 0.55, 0.75, 0.90)
MAX_LANE_COUNT = 8
NULL_ASSIGNMENT_SCORE = 0.40
MIN_LANE_ASSIGNMENT_SCORE = 0.56
MIN_PRODUCTION_TAKE_SCORE = 0.60
MAX_ASSET_GAP_SECONDS = 240.0


@dataclass
class VideoProposal:
    source: str
    target: str
    angle: str
    lane_id: str
    pair_score: float
    waveform_score: float
    duration_score: float
    duration_delta_seconds: float
    estimated_offset_seconds: float
    visual_score: float | None = None


@dataclass
class TakeProposal:
    take_id: str
    audio_source: str
    audio_target: str
    videos: list[VideoProposal]
    confidence: float
    notes: list[str] = field(default_factory=list)


@dataclass
class MediaBucketEntry:
    source: str
    media_type: str
    reason: str
    confidence: float


@dataclass
class AutoGroupPlan:
    piece_root: str
    incoming_dir: str
    generated_at: str
    status: str
    angles: list[str]
    takes: list[TakeProposal]
    excluded_media: list[MediaBucketEntry]
    unresolved_media: list[MediaBucketEntry]
    issues: list[Issue]


@dataclass
class AutoGroupApplyResult:
    piece_root: str
    proposal_path: str
    takes_created: list[str]
    excluded_created: list[str]
    files_moved: list[dict[str, str]]
    session_config: str
    status: str


@dataclass
class _PairMetrics:
    pair_score: float
    waveform_score: float
    duration_score: float
    duration_delta_seconds: float
    estimated_offset_seconds: float


@dataclass
class _AudioAsset:
    path: Path
    relative_path: str
    duration_seconds: float
    signature: np.ndarray


@dataclass
class _VideoAsset:
    path: Path
    relative_path: str
    duration_seconds: float
    scratch_audio_present: bool
    signature: np.ndarray | None
    descriptor: np.ndarray | None


@dataclass
class _LaneCluster:
    cluster_id: int
    videos: list[_VideoAsset]
    centroid: np.ndarray | None


@dataclass
class _LaneAssignment:
    video: _VideoAsset
    audio: _AudioAsset
    metrics: _PairMetrics
    visual_score: float | None


@dataclass
class _LaneSolveResult:
    lane: _LaneCluster
    assignments: list[_LaneAssignment]
    unassigned_videos: list[tuple[_VideoAsset, float]]


@dataclass
class _CandidatePlan:
    lanes: list[_LaneSolveResult]
    audio_assignments: dict[str, list[_LaneAssignment]]
    excluded_media: list[MediaBucketEntry]
    unresolved_media: list[MediaBucketEntry]
    objective: float
    silhouette: float
    lane_count: int


def _serialize_issue(code: str, message: str, *, severity: str = "warn", context: dict[str, Any] | None = None) -> Issue:
    return Issue(severity, code, message, context or {})


def _group_label(index: int) -> str:
    alphabet = string.ascii_lowercase
    label = ""
    value = index
    while True:
        value, remainder = divmod(value, 26)
        label = alphabet[remainder] + label
        if value == 0:
            break
        value -= 1
    return f"angle-{label}"


def _logical_piece_path(piece_root: Path, relative_path: str) -> Path:
    return Path(os.path.normpath(os.fspath(piece_root / relative_path)))


def _resolve_incoming_dir(piece_root: Path, incoming_dir: str | None) -> tuple[str, Path]:
    if incoming_dir:
        resolved = (piece_root / incoming_dir).resolve()
        if not resolved.is_dir():
            raise FileNotFoundError(f"incoming directory not found: {resolved}")
        return incoming_dir, resolved

    for candidate in ("incoming", "incomings"):
        resolved = (piece_root / candidate).resolve()
        if resolved.is_dir():
            return candidate, resolved
    raise FileNotFoundError(f"no incoming directory found under {piece_root}; expected incoming/ or incomings/")


def _decode_audio_mono(path: Path) -> np.ndarray:
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
            str(AUDIO_SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ]
    )
    samples = np.frombuffer(result.stdout, dtype="<i2")
    if samples.size == 0:
        raise ExternalToolError(f"decoded empty audio stream: {path}")
    return samples.astype(np.float32) / 32768.0


def _rms_envelope(samples: np.ndarray, window_size: int) -> np.ndarray:
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    usable = samples[: samples.size - (samples.size % window_size)]
    if usable.size == 0:
        usable = samples
    reshaped = usable.reshape(-1, window_size) if usable.size >= window_size else usable.reshape(1, -1)
    envelope = np.sqrt(np.mean(np.square(reshaped), axis=1))
    return envelope.astype(np.float32)


def _trim_signal(samples: np.ndarray) -> np.ndarray:
    if samples.size == 0:
        return samples
    window_size = max(256, AUDIO_SAMPLE_RATE // 10)
    envelope = _rms_envelope(samples, window_size)
    if envelope.size == 0:
        return samples
    threshold = max(0.005, float(np.quantile(envelope, 0.70)) * 0.15)
    active = np.where(envelope >= threshold)[0]
    if active.size == 0:
        return samples
    start = max(int(active[0] * window_size) - window_size, 0)
    end = min(int((active[-1] + 1) * window_size) + window_size, samples.size)
    return samples[start:end]


def _normalize_signal(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    centered = values.astype(np.float32) - float(np.mean(values))
    scale = float(np.std(centered))
    if scale < 1e-6:
        return np.zeros_like(centered, dtype=np.float32)
    return centered / scale


def _l2_normalize(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return values.astype(np.float32)
    vector = values.astype(np.float32)
    norm = float(np.linalg.norm(vector))
    if norm < 1e-6:
        return np.zeros_like(vector, dtype=np.float32)
    return vector / norm


def _build_audio_signature(path: Path) -> np.ndarray:
    samples = _decode_audio_mono(path)
    trimmed = _trim_signal(samples)
    window_size = max(256, int(AUDIO_SAMPLE_RATE / SIGNATURE_HZ))
    envelope = _rms_envelope(trimmed, window_size)
    if envelope.size == 0:
        return np.zeros(0, dtype=np.float32)
    compressed = np.log1p(envelope * 20.0)
    return _normalize_signal(compressed)


def _extract_view_descriptor(path: Path, duration_seconds: float) -> np.ndarray | None:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("opencv-python-headless is required for auto-group angle analysis") from exc

    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return None

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(6, 6))
    features: list[np.ndarray] = []
    try:
        for position in FRAME_SAMPLE_POSITIONS:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(duration_seconds * position * 1000.0, 0.0))
            ok, frame = capture.read()
            if not ok or frame is None:
                continue

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            small = cv2.resize(gray, (64, 36), interpolation=cv2.INTER_AREA)
            normalized = clahe.apply(small)
            blurred = cv2.GaussianBlur(normalized, (3, 3), 0)
            normalized_float = blurred.astype(np.float32) / 255.0
            normalized_float = _normalize_signal(normalized_float.reshape(-1)).reshape(normalized_float.shape)

            grad_x = cv2.Sobel(normalized_float, cv2.CV_32F, 1, 0, ksize=3)
            grad_y = cv2.Sobel(normalized_float, cv2.CV_32F, 0, 1, ksize=3)
            magnitude, angle = cv2.cartToPolar(grad_x, grad_y, angleInDegrees=False)
            orientation_hist, _ = np.histogram(angle, bins=8, range=(0.0, 2.0 * np.pi), weights=magnitude)
            orientation_hist = _l2_normalize(orientation_hist.astype(np.float32))

            edge_source = np.clip((normalized_float + 3.0) / 6.0, 0.0, 1.0)
            edges = cv2.Canny((edge_source * 255.0).astype(np.uint8), 48, 144).astype(np.float32) / 255.0
            pooled_intensity = cv2.resize(normalized_float, (16, 9), interpolation=cv2.INTER_AREA).reshape(-1)
            pooled_edges = cv2.resize(edges, (16, 9), interpolation=cv2.INTER_AREA).reshape(-1)
            descriptor = np.concatenate([pooled_intensity, pooled_edges, orientation_hist]).astype(np.float32)
            features.append(_l2_normalize(descriptor))
    finally:
        capture.release()

    if not features:
        return None
    stacked = np.stack(features)
    return _l2_normalize(np.mean(stacked, axis=0))


def _discover_audio_assets(piece_root: Path, incoming_dir: Path, incoming_name: str) -> list[_AudioAsset]:
    assets: list[_AudioAsset] = []
    for path in sorted(incoming_dir.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        probe = probe_media(path)
        duration = float((probe.get("format") or {}).get("duration") or 0.0)
        logical_path = _logical_piece_path(piece_root.resolve(), f"{incoming_name}/{path.name}")
        assets.append(
            _AudioAsset(
                path=logical_path,
                relative_path=str(logical_path.relative_to(piece_root.resolve())),
                duration_seconds=duration,
                signature=_build_audio_signature(path),
            )
        )
    return assets


def _discover_video_assets(piece_root: Path, incoming_dir: Path, incoming_name: str) -> list[_VideoAsset]:
    assets: list[_VideoAsset] = []
    for path in sorted(incoming_dir.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        probe = probe_media(path)
        streams = probe.get("streams") or []
        audio_stream = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        duration = float((probe.get("format") or {}).get("duration") or 0.0)
        scratch_audio_present = audio_stream is not None
        signature = _build_audio_signature(path) if scratch_audio_present else None
        logical_path = _logical_piece_path(piece_root.resolve(), f"{incoming_name}/{path.name}")
        assets.append(
            _VideoAsset(
                path=logical_path,
                relative_path=str(logical_path.relative_to(piece_root.resolve())),
                duration_seconds=duration,
                scratch_audio_present=scratch_audio_present,
                signature=signature,
                descriptor=_extract_view_descriptor(path, duration),
            )
        )
    return assets


def _pair_similarity(left_signature: np.ndarray, right_signature: np.ndarray) -> tuple[float, float]:
    if left_signature.size == 0 or right_signature.size == 0:
        return 0.0, 0.0
    shorter, longer = (
        (left_signature, right_signature)
        if left_signature.size <= right_signature.size
        else (right_signature, left_signature)
    )
    correlation = np.correlate(longer, shorter, mode="valid")
    if correlation.size == 0:
        return 0.0, 0.0
    shorter_norm = float(np.linalg.norm(shorter))
    if shorter_norm < 1e-6:
        return 0.0, 0.0
    window_energies = np.convolve(np.square(longer, dtype=np.float32), np.ones(shorter.size, dtype=np.float32), mode="valid")
    window_norms = np.sqrt(np.maximum(window_energies, 1e-12))
    normalized = np.divide(
        correlation,
        shorter_norm * window_norms,
        out=np.zeros_like(correlation, dtype=np.float32),
        where=window_norms > 1e-6,
    )
    index = int(np.argmax(normalized))
    score = float(np.clip(normalized[index], 0.0, 1.0))
    return score, index / SIGNATURE_HZ


def _audio_video_duration_score(audio_duration: float, video_duration: float) -> tuple[float, float]:
    delta = audio_duration - video_duration
    gap = abs(delta)
    if gap > MAX_ASSET_GAP_SECONDS:
        return 0.0, gap

    if -45.0 <= delta <= 75.0:
        score = 1.0 - min(gap / 120.0, 0.35)
        return max(0.0, score), gap

    if delta > 75.0:
        overflow = delta - 75.0
    else:
        overflow = abs(delta) - 45.0
    score = max(0.0, 0.65 - (overflow / 120.0))
    return score, gap


def _score_audio_video(audio: _AudioAsset, video: _VideoAsset) -> _PairMetrics:
    waveform_score = 0.0
    offset = 0.0
    if video.scratch_audio_present and video.signature is not None:
        waveform_score, offset = _pair_similarity(audio.signature, video.signature)
    duration_score, duration_delta = _audio_video_duration_score(audio.duration_seconds, video.duration_seconds)
    pair_score = (duration_score * 0.65) + (waveform_score * 0.35)
    return _PairMetrics(
        pair_score=float(np.clip(pair_score, 0.0, 1.0)),
        waveform_score=waveform_score,
        duration_score=duration_score,
        duration_delta_seconds=duration_delta,
        estimated_offset_seconds=offset,
    )


def _visual_similarity(left: np.ndarray, right: np.ndarray) -> float:
    return float(np.clip(np.dot(left, right), -1.0, 1.0))


def _distance_matrix(items: list[np.ndarray], similarity_fn) -> np.ndarray:
    size = len(items)
    matrix = np.zeros((size, size), dtype=np.float32)
    for index in range(size):
        for inner in range(index + 1, size):
            similarity = similarity_fn(items[index], items[inner])
            distance = float(np.clip(1.0 - max(similarity, 0.0), 0.0, 1.0))
            matrix[index, inner] = distance
            matrix[inner, index] = distance
    return matrix


def _cluster_by_distance(distance_matrix: np.ndarray, cluster_count: int) -> np.ndarray:
    size = distance_matrix.shape[0]
    if size == 1:
        return np.array([1], dtype=np.int32)
    condensed = squareform(distance_matrix, checks=False)
    tree = linkage(condensed, method="average")
    return fcluster(tree, t=cluster_count, criterion="maxclust")


def _average_silhouette(distance_matrix: np.ndarray, labels: np.ndarray) -> float:
    size = len(labels)
    if size <= 1 or len(set(labels.tolist())) <= 1:
        return 0.0

    silhouettes: list[float] = []
    unique_labels = sorted(set(labels.tolist()))
    for index in range(size):
        own_label = labels[index]
        own_members = [member for member, label in enumerate(labels) if label == own_label and member != index]
        if own_members:
            a = float(np.mean([distance_matrix[index, member] for member in own_members]))
        else:
            silhouettes.append(0.0)
            continue

        b = min(
            float(np.mean([distance_matrix[index, member] for member, label in enumerate(labels) if label == candidate]))
            for candidate in unique_labels
            if candidate != own_label
        )
        denominator = max(a, b)
        if denominator < 1e-6:
            silhouettes.append(0.0)
        else:
            silhouettes.append((b - a) / denominator)
    return float(np.mean(silhouettes)) if silhouettes else 0.0


def _lane_centroid(videos: list[_VideoAsset]) -> np.ndarray | None:
    descriptors = [video.descriptor for video in videos if video.descriptor is not None]
    if not descriptors:
        return None
    return _l2_normalize(np.mean(np.stack(descriptors), axis=0))


def _build_lane_clusters(videos: list[_VideoAsset], labels: np.ndarray) -> list[_LaneCluster]:
    grouped: dict[int, list[_VideoAsset]] = {}
    for video, label in zip(videos, labels):
        grouped.setdefault(int(label), []).append(video)
    lanes = []
    for cluster_id, cluster_videos in grouped.items():
        lanes.append(
            _LaneCluster(
                cluster_id=cluster_id,
                videos=sorted(cluster_videos, key=lambda item: item.relative_path),
                centroid=_lane_centroid(cluster_videos),
            )
        )
    return sorted(lanes, key=lambda lane: lane.cluster_id)


def _assign_lane_videos(lane: _LaneCluster, audio_assets: list[_AudioAsset]) -> _LaneSolveResult:
    videos = lane.videos
    if not videos:
        return _LaneSolveResult(lane=lane, assignments=[], unassigned_videos=[])

    if not audio_assets:
        return _LaneSolveResult(
            lane=lane,
            assignments=[],
            unassigned_videos=[(video, 0.0) for video in videos],
        )

    cost_matrix = np.zeros((len(videos), len(audio_assets) + len(videos)), dtype=np.float32)
    metrics_by_pair: dict[tuple[int, int], _PairMetrics] = {}
    for video_index, video in enumerate(videos):
        for audio_index, audio in enumerate(audio_assets):
            metrics = _score_audio_video(audio, video)
            metrics_by_pair[(video_index, audio_index)] = metrics
            cost_matrix[video_index, audio_index] = 1.0 - metrics.pair_score
        cost_matrix[video_index, len(audio_assets) :] = 1.0 - NULL_ASSIGNMENT_SCORE

    row_ind, col_ind = linear_sum_assignment(cost_matrix)
    assignments: list[_LaneAssignment] = []
    unassigned: list[tuple[_VideoAsset, float]] = []
    for video_index, column_index in zip(row_ind.tolist(), col_ind.tolist()):
        video = videos[video_index]
        if column_index < len(audio_assets):
            metrics = metrics_by_pair[(video_index, column_index)]
            if metrics.pair_score >= MIN_LANE_ASSIGNMENT_SCORE:
                visual_score = None
                if lane.centroid is not None and video.descriptor is not None:
                    visual_score = max(0.0, _visual_similarity(video.descriptor, lane.centroid))
                assignments.append(
                    _LaneAssignment(
                        video=video,
                        audio=audio_assets[column_index],
                        metrics=metrics,
                        visual_score=visual_score,
                    )
                )
                continue
            unassigned.append((video, metrics.pair_score))
        else:
            best_pair = max((metrics_by_pair[(video_index, audio_index)].pair_score for audio_index in range(len(audio_assets))), default=0.0)
            unassigned.append((video, best_pair))
    return _LaneSolveResult(lane=lane, assignments=assignments, unassigned_videos=unassigned)


def _candidate_objective(
    silhouette: float,
    lane_results: list[_LaneSolveResult],
    audio_assignments: dict[str, list[_LaneAssignment]],
    unresolved_count: int,
) -> float:
    production_takes = 0
    partial_takes = 0
    take_confidences: list[float] = []
    for assignments in audio_assignments.values():
        if not assignments:
            continue
        confidence = float(np.mean([assignment.metrics.pair_score for assignment in assignments]))
        take_confidences.append(confidence)
        if 3 <= len(assignments) <= 4 and confidence >= MIN_PRODUCTION_TAKE_SCORE:
            production_takes += 1
        else:
            partial_takes += 1

    unassigned_count = sum(len(result.unassigned_videos) for result in lane_results)
    single_lane_penalty = sum(1 for result in lane_results if len(result.lane.videos) == 1) * 0.35
    lane_size_penalty = sum(max(0, len(result.lane.videos) - len(audio_assignments)) * 0.15 for result in lane_results)
    assignment_scores = [assignment.metrics.pair_score for result in lane_results for assignment in result.assignments]
    average_assignment = float(np.mean(assignment_scores)) if assignment_scores else 0.0
    return (
        (production_takes * 3.0)
        + average_assignment
        + (silhouette * 0.8)
        - (partial_takes * 1.8)
        - (unresolved_count * 1.6)
        - (unassigned_count * 0.4)
        - single_lane_penalty
        - lane_size_penalty
        - (len(lane_results) * 0.18)
    )


def _classify_candidate(
    audio_assets: list[_AudioAsset],
    lane_results: list[_LaneSolveResult],
) -> tuple[dict[str, list[_LaneAssignment]], list[MediaBucketEntry], list[MediaBucketEntry]]:
    audio_assignments: dict[str, list[_LaneAssignment]] = {audio.relative_path: [] for audio in audio_assets}
    for result in lane_results:
        for assignment in result.assignments:
            audio_assignments[assignment.audio.relative_path].append(assignment)

    excluded_media: list[MediaBucketEntry] = []
    unresolved_media: list[MediaBucketEntry] = []
    consumed_videos: set[str] = set()

    for audio in audio_assets:
        assignments = sorted(audio_assignments[audio.relative_path], key=lambda item: item.video.relative_path)
        audio_assignments[audio.relative_path] = assignments
        if 3 <= len(assignments) <= 4 and float(np.mean([item.metrics.pair_score for item in assignments])) >= MIN_PRODUCTION_TAKE_SCORE:
            consumed_videos.update(item.video.relative_path for item in assignments)
            continue

        if not assignments:
            excluded_media.append(
                MediaBucketEntry(
                    source=audio.relative_path,
                    media_type="audio",
                    reason="no production lane assignments; likely preflight or orphan audio",
                    confidence=0.95,
                )
            )
            continue

        confidence = float(np.mean([item.metrics.pair_score for item in assignments]))
        bucket = excluded_media if len(assignments) <= 2 and confidence < MIN_PRODUCTION_TAKE_SCORE else unresolved_media
        reason = (
            "partial audio cluster does not meet production take size"
            if bucket is unresolved_media
            else "audio cluster looks like non-production media"
        )
        bucket.append(
            MediaBucketEntry(
                source=audio.relative_path,
                media_type="audio",
                reason=reason,
                confidence=round(confidence, 4),
            )
        )
        for assignment in assignments:
            consumed_videos.add(assignment.video.relative_path)
            bucket.append(
                MediaBucketEntry(
                    source=assignment.video.relative_path,
                    media_type="video",
                    reason=reason,
                    confidence=round(assignment.metrics.pair_score, 4),
                )
            )

    for result in lane_results:
        for video, best_score in result.unassigned_videos:
            if video.relative_path in consumed_videos:
                continue
            bucket = excluded_media if best_score < MIN_LANE_ASSIGNMENT_SCORE else unresolved_media
            bucket.append(
                MediaBucketEntry(
                    source=video.relative_path,
                    media_type="video",
                    reason="video did not fit any production audio anchor",
                    confidence=round(best_score, 4),
                )
            )

    return audio_assignments, _dedupe_bucket_entries(excluded_media), _dedupe_bucket_entries(unresolved_media)


def _dedupe_bucket_entries(entries: list[MediaBucketEntry]) -> list[MediaBucketEntry]:
    deduped: dict[tuple[str, str], MediaBucketEntry] = {}
    for entry in entries:
        key = (entry.source, entry.media_type)
        if key not in deduped or entry.confidence > deduped[key].confidence:
            deduped[key] = entry
    return sorted(deduped.values(), key=lambda item: (item.media_type, item.source))


def _choose_lane_candidate(audio_assets: list[_AudioAsset], video_assets: list[_VideoAsset]) -> _CandidatePlan | None:
    descriptor_videos = [video for video in video_assets if video.descriptor is not None]
    missing_descriptor_videos = [video for video in video_assets if video.descriptor is None]
    if not descriptor_videos:
        return None

    descriptors = [video.descriptor for video in descriptor_videos if video.descriptor is not None]
    distance_matrix = _distance_matrix(descriptors, _visual_similarity)
    max_lane_count = min(MAX_LANE_COUNT, len(descriptor_videos))
    best_candidate: _CandidatePlan | None = None

    for lane_count in range(1, max_lane_count + 1):
        labels = _cluster_by_distance(distance_matrix, lane_count)
        silhouette = _average_silhouette(distance_matrix, labels)
        lanes = _build_lane_clusters(descriptor_videos, labels)
        next_cluster_id = max((lane.cluster_id for lane in lanes), default=0) + 1
        for video in missing_descriptor_videos:
            lanes.append(_LaneCluster(cluster_id=next_cluster_id, videos=[video], centroid=None))
            next_cluster_id += 1

        lane_results = [_assign_lane_videos(lane, audio_assets) for lane in lanes]
        audio_assignments, excluded_media, unresolved_media = _classify_candidate(audio_assets, lane_results)
        objective = _candidate_objective(silhouette, lane_results, audio_assignments, len(unresolved_media))
        candidate = _CandidatePlan(
            lanes=lane_results,
            audio_assignments=audio_assignments,
            excluded_media=excluded_media,
            unresolved_media=unresolved_media,
            objective=objective,
            silhouette=silhouette,
            lane_count=lane_count,
        )
        if best_candidate is None:
            best_candidate = candidate
            continue
        if candidate.objective > best_candidate.objective + 1e-6:
            best_candidate = candidate
            continue
        if abs(candidate.objective - best_candidate.objective) <= 1e-6 and candidate.lane_count < best_candidate.lane_count:
            best_candidate = candidate

    return best_candidate


def _stable_lane_order(candidate: _CandidatePlan) -> list[_LaneSolveResult]:
    def key(result: _LaneSolveResult) -> tuple[str, str]:
        representative = min(video.relative_path for video in result.lane.videos)
        digest = representative
        if result.lane.centroid is not None:
            digest = hashlib.sha1(np.asarray(result.lane.centroid, dtype=np.float32).tobytes()).hexdigest()
        return digest, representative

    return sorted(candidate.lanes, key=key)


def auto_group_plan_to_dict(plan: AutoGroupPlan) -> dict[str, Any]:
    return {
        "piece_root": plan.piece_root,
        "incoming_dir": plan.incoming_dir,
        "generated_at": plan.generated_at,
        "status": plan.status,
        "angles": plan.angles,
        "takes": [asdict(take) for take in plan.takes],
        "excluded_media": [asdict(entry) for entry in plan.excluded_media],
        "unresolved_media": [asdict(entry) for entry in plan.unresolved_media],
        "issues": issues_to_dict(plan.issues),
    }


def load_auto_group_plan(plan_path: str | Path) -> AutoGroupPlan:
    import json

    path = Path(plan_path).resolve()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    issues = [Issue(**issue) for issue in data.get("issues") or []]
    takes = [
        TakeProposal(
            take_id=item["take_id"],
            audio_source=item["audio_source"],
            audio_target=item["audio_target"],
            videos=[VideoProposal(**video) for video in item.get("videos") or []],
            confidence=float(item["confidence"]),
            notes=list(item.get("notes") or []),
        )
        for item in data.get("takes") or []
    ]
    return AutoGroupPlan(
        piece_root=data["piece_root"],
        incoming_dir=data["incoming_dir"],
        generated_at=data["generated_at"],
        status=data["status"],
        angles=list(data.get("angles") or []),
        takes=takes,
        excluded_media=[MediaBucketEntry(**entry) for entry in data.get("excluded_media") or []],
        unresolved_media=[MediaBucketEntry(**entry) for entry in data.get("unresolved_media") or []],
        issues=issues,
    )


def plan_auto_group(piece_root: str | Path, *, incoming_dir: str | None = None) -> AutoGroupPlan:
    piece_root = Path(piece_root).resolve()
    resolved_incoming_name, incoming = _resolve_incoming_dir(piece_root, incoming_dir)
    audio_assets = _discover_audio_assets(piece_root, incoming, resolved_incoming_name)
    video_assets = _discover_video_assets(piece_root, incoming, resolved_incoming_name)

    issues: list[Issue] = []
    if not audio_assets:
        issues.append(_serialize_issue("incoming_audio_missing", "incoming directory contains no audio files", severity="fail"))
    if not video_assets:
        issues.append(_serialize_issue("incoming_video_missing", "incoming directory contains no video files", severity="fail"))
    if issues:
        return AutoGroupPlan(
            piece_root=str(piece_root),
            incoming_dir=resolved_incoming_name,
            generated_at=datetime.now(timezone.utc).isoformat(),
            status=overall_status(issues),
            angles=[],
            takes=[],
            excluded_media=[],
            unresolved_media=[],
            issues=issues,
        )

    candidate = _choose_lane_candidate(audio_assets, video_assets)
    if candidate is None:
        issues.append(_serialize_issue("lane_clustering_failed", "unable to derive visual lanes from incoming videos", severity="fail"))
        return AutoGroupPlan(
            piece_root=str(piece_root),
            incoming_dir=resolved_incoming_name,
            generated_at=datetime.now(timezone.utc).isoformat(),
            status="FAIL",
            angles=[],
            takes=[],
            excluded_media=[],
            unresolved_media=[],
            issues=issues,
        )

    lane_results = _stable_lane_order(candidate)
    lane_name_map = {result.lane.cluster_id: _group_label(index) for index, result in enumerate(lane_results)}
    video_lane_map = {
        video.relative_path: result.lane.cluster_id
        for result in lane_results
        for video in result.lane.videos
    }
    production_audio_paths: set[str] = set()
    takes: list[TakeProposal] = []

    for audio in audio_assets:
        assignments = candidate.audio_assignments[audio.relative_path]
        if not assignments:
            continue
        confidence = float(np.mean([assignment.metrics.pair_score for assignment in assignments]))
        if not (3 <= len(assignments) <= 4 and confidence >= MIN_PRODUCTION_TAKE_SCORE):
            continue
        production_audio_paths.add(audio.relative_path)

    takes = []
    for take_index, audio in enumerate([asset for asset in audio_assets if asset.relative_path in production_audio_paths], start=1):
        assignments = candidate.audio_assignments[audio.relative_path]
        ordered_assignments = sorted(
            assignments,
            key=lambda item: (
                lane_name_map[video_lane_map[item.video.relative_path]],
                item.video.relative_path,
            ),
        )
        videos = []
        for assignment in ordered_assignments:
            lane_id = video_lane_map[assignment.video.relative_path]
            angle = lane_name_map[lane_id]
            videos.append(
                VideoProposal(
                    source=assignment.video.relative_path,
                    target=f"{angle}{Path(assignment.video.relative_path).suffix.lower()}",
                    angle=angle,
                    lane_id=f"lane-{lane_id}",
                    pair_score=round(assignment.metrics.pair_score, 4),
                    waveform_score=round(assignment.metrics.waveform_score, 4),
                    duration_score=round(assignment.metrics.duration_score, 4),
                    duration_delta_seconds=round(assignment.metrics.duration_delta_seconds, 4),
                    estimated_offset_seconds=round(assignment.metrics.estimated_offset_seconds, 4),
                    visual_score=round(assignment.visual_score, 4) if assignment.visual_score is not None else None,
                )
            )
        take_notes: list[str] = []
        if confidence < 0.72:
            take_notes.append("take confidence is low for a production take")
        takes.append(
            TakeProposal(
                take_id=f"take-{take_index:02d}",
                audio_source=audio.relative_path,
                audio_target=f"audio{Path(audio.relative_path).suffix.lower()}",
                videos=videos,
                confidence=round(confidence, 4),
                notes=take_notes,
            )
        )

    production_video_sources = {
        video.source
        for take in takes
        for video in take.videos
    }

    excluded_media = []
    unresolved_media = []
    for entry in candidate.excluded_media:
        if entry.source in production_audio_paths or entry.source in production_video_sources:
            continue
        excluded_media.append(entry)
    for entry in candidate.unresolved_media:
        if entry.source in production_audio_paths or entry.source in production_video_sources:
            continue
        unresolved_media.append(entry)

    if not takes:
        issues.append(_serialize_issue("grouping_failed", "no production takes were produced", severity="fail"))
    if unresolved_media:
        issues.append(
            _serialize_issue(
                "unresolved_media_present",
                f"{len(unresolved_media)} media items could not be classified confidently",
                context={"files": [entry.source for entry in unresolved_media]},
            )
        )
    for entry in excluded_media:
        issues.append(
            _serialize_issue(
                "excluded_media_detected",
                f"excluded {entry.media_type}: {Path(entry.source).name}",
                severity="info",
                context={"reason": entry.reason, "confidence": entry.confidence},
            )
        )
    for take in takes:
        if not 3 <= len(take.videos) <= 4:
            issues.append(
                _serialize_issue(
                    "take_size_invalid",
                    f"{take.take_id} has {len(take.videos)} videos",
                    severity="warn",
                )
            )
        if take.confidence < MIN_PRODUCTION_TAKE_SCORE:
            issues.append(
                _serialize_issue(
                    "take_confidence_low",
                    f"{take.take_id} confidence {take.confidence:.3f} is below production threshold",
                    severity="warn",
                )
            )

    status = overall_status(issues)
    return AutoGroupPlan(
        piece_root=str(piece_root),
        incoming_dir=resolved_incoming_name,
        generated_at=datetime.now(timezone.utc).isoformat(),
        status=status,
        angles=[lane_name_map[result.lane.cluster_id] for result in lane_results],
        takes=takes,
        excluded_media=sorted(excluded_media, key=lambda item: (item.media_type, item.source)),
        unresolved_media=sorted(unresolved_media, key=lambda item: (item.media_type, item.source)),
        issues=issues,
    )


def write_auto_group_plan_reports(piece_root: str | Path, plan: AutoGroupPlan) -> tuple[Path, Path]:
    piece_root = Path(piece_root).resolve()
    payload = auto_group_plan_to_dict(plan)
    json_path = piece_root / "reports" / "auto-group-plan.json"
    markdown_path = piece_root / "reports" / "auto-group-plan.md"
    write_json_report(json_path, payload)
    write_markdown_report(
        markdown_path,
        title="Auto Group Plan",
        status=plan.status,
        issues=plan.issues,
        sections={
            "Angles": plan.angles or ["not available"],
            "Takes": [
                f"{take.take_id}: {Path(take.audio_source).name} + {len(take.videos)} videos (confidence {take.confidence:.3f})"
                for take in plan.takes
            ],
            "Excluded Media": [
                f"{entry.media_type}: {Path(entry.source).name} ({entry.reason}, confidence {entry.confidence:.3f})"
                for entry in plan.excluded_media
            ]
            or ["none"],
            "Unresolved Media": [
                f"{entry.media_type}: {Path(entry.source).name} ({entry.reason}, confidence {entry.confidence:.3f})"
                for entry in plan.unresolved_media
            ]
            or ["none"],
        },
    )
    return json_path, markdown_path


def _ensure_empty_output_root(root: Path, *, label: str) -> Path:
    if root.exists():
        populated = [entry for entry in root.iterdir() if not entry.name.startswith(".")]
        if populated:
            raise RuntimeError(f"auto-group-apply only supports an empty {label}/ directory")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cleanup_empty_dirs(paths: Iterable[Path]) -> None:
    for path in sorted({item for item in paths}, key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except OSError:
            continue


def _plan_sources(plan: AutoGroupPlan) -> list[str]:
    sources = [take.audio_source for take in plan.takes]
    for take in plan.takes:
        sources.extend(video.source for video in take.videos)
    sources.extend(entry.source for entry in plan.excluded_media)
    sources.extend(entry.source for entry in plan.unresolved_media)
    return sources


def _incoming_media_sources(piece_root: Path, incoming_dir: str) -> set[str]:
    _, incoming = _resolve_incoming_dir(piece_root, incoming_dir)
    media_extensions = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
    sources: set[str] = set()
    for path in sorted(incoming.iterdir()):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in media_extensions:
            continue
        logical_path = _logical_piece_path(piece_root.resolve(), f"{incoming_dir}/{path.name}")
        sources.add(str(logical_path.relative_to(piece_root.resolve())))
    return sources


def _resolve_plan_source(piece_root: Path, incoming_dir: str, incoming_root: Path, relative_path: str) -> Path:
    source = _logical_piece_path(piece_root.resolve(), relative_path)
    incoming_logical = _logical_piece_path(piece_root.resolve(), incoming_dir)
    try:
        source.relative_to(incoming_logical)
    except ValueError as exc:
        raise RuntimeError(f"proposal references media outside {incoming_root}: {relative_path}") from exc
    if source.parent != incoming_logical:
        raise RuntimeError(f"proposal source must be a direct child of {incoming_root.name}/: {relative_path}")
    return source


def _raise_for_duplicates(items: list[str], *, label: str) -> None:
    duplicates = sorted({item for item in items if items.count(item) > 1})
    if duplicates:
        raise RuntimeError(f"proposal contains duplicate {label}: {', '.join(duplicates)}")


def _preflight_auto_group_apply(piece_root: Path, takes_root: Path, excluded_root: Path, plan: AutoGroupPlan) -> None:
    _, incoming_root = _resolve_incoming_dir(piece_root, plan.incoming_dir)
    planned_sources = _plan_sources(plan)
    _raise_for_duplicates(planned_sources, label="source entries")

    current_sources = _incoming_media_sources(piece_root, plan.incoming_dir)
    planned_set = set(planned_sources)
    missing_sources = sorted(planned_set - current_sources)
    unexpected_sources = sorted(current_sources - planned_set)
    if missing_sources or unexpected_sources:
        details: list[str] = []
        if missing_sources:
            details.append(f"missing: {', '.join(missing_sources)}")
        if unexpected_sources:
            details.append(f"unexpected: {', '.join(unexpected_sources)}")
        raise RuntimeError(f"proposal is stale relative to incoming/; rerun auto-group-plan ({'; '.join(details)})")

    final_targets: list[str] = []
    for take in plan.takes:
        if not take.audio_target:
            raise RuntimeError(f"proposal is missing audio_target for {take.take_id}")
        take_targets = [take.audio_target]
        for video in take.videos:
            if not video.target or not video.angle:
                raise RuntimeError(f"proposal is incomplete for {video.source}")
            take_targets.append(video.target)
        _raise_for_duplicates(take_targets, label=f"targets inside {take.take_id}")
        final_targets.extend([str(Path("takes") / take.take_id / target) for target in take_targets])
        for relative_path in [take.audio_source, *[video.source for video in take.videos]]:
            source = _resolve_plan_source(piece_root, plan.incoming_dir, incoming_root, relative_path)
            if not source.exists():
                raise RuntimeError(f"proposal source is missing: {relative_path}")

    excluded_targets = [str(Path("excluded") / Path(entry.source).name) for entry in plan.excluded_media]
    _raise_for_duplicates(final_targets + excluded_targets, label="target paths")
    for relative_target in final_targets:
        destination = piece_root / relative_target
        if destination.exists():
            raise RuntimeError(f"target path already exists: {destination}")
    for relative_target in excluded_targets:
        destination = piece_root / relative_target
        if destination.exists():
            raise RuntimeError(f"target path already exists: {destination}")

    if plan.unresolved_media:
        raise RuntimeError("proposal contains unresolved media; refusing to apply")


def apply_auto_group(
    piece_root: str | Path,
    *,
    proposal_path: str | Path | None = None,
    plan: AutoGroupPlan | None = None,
    write_reports: bool = True,
) -> AutoGroupApplyResult:
    piece_root = Path(piece_root).resolve()
    default_proposal = piece_root / "reports" / "auto-group-plan.json"
    plan = plan or load_auto_group_plan(proposal_path or default_proposal)

    if Path(plan.piece_root).resolve() != piece_root:
        raise RuntimeError("proposal path does not belong to the supplied piece root")
    if plan.status != "PASS":
        raise RuntimeError("proposal status is not PASS; refusing to apply")

    takes_root = piece_root / "takes"
    excluded_root = piece_root / "excluded"
    _preflight_auto_group_apply(piece_root, takes_root, excluded_root, plan)
    takes_root = _ensure_empty_output_root(takes_root, label="takes")
    excluded_root = _ensure_empty_output_root(excluded_root, label="excluded")
    staging_root = takes_root / ".staging"
    staging_root.mkdir(parents=True, exist_ok=True)

    stage_moves: list[tuple[Path, Path]] = []
    final_moves: list[tuple[Path, Path]] = []
    stage_dirs: list[Path] = []
    final_dirs: list[Path] = []
    take_paths = [takes_root / take.take_id / "take.yaml" for take in plan.takes]
    take_backups = {path: path.read_bytes() if path.exists() else None for path in take_paths}
    session_path = piece_root / "session.yaml"
    session_backup = session_path.read_bytes() if session_path.exists() else None

    try:
        for take in plan.takes:
            stage_take_dir = staging_root / take.take_id
            stage_take_dir.mkdir(parents=True, exist_ok=True)
            stage_dirs.append(stage_take_dir)

            audio_source = piece_root / take.audio_source
            audio_target = stage_take_dir / take.audio_target
            audio_source.rename(audio_target)
            stage_moves.append((audio_source, audio_target))

            for video in take.videos:
                source = piece_root / video.source
                target = stage_take_dir / video.target
                source.rename(target)
                stage_moves.append((source, target))

        stage_excluded_dir = staging_root / "excluded"
        if plan.excluded_media:
            stage_excluded_dir.mkdir(parents=True, exist_ok=True)
            stage_dirs.append(stage_excluded_dir)
        for entry in plan.excluded_media:
            source = piece_root / entry.source
            target = stage_excluded_dir / Path(entry.source).name
            source.rename(target)
            stage_moves.append((source, target))

        for take in plan.takes:
            final_take_dir = takes_root / take.take_id
            final_take_dir.mkdir(parents=True, exist_ok=True)
            final_dirs.append(final_take_dir)

            stage_take_dir = staging_root / take.take_id
            for source in sorted(stage_take_dir.iterdir()):
                final_path = final_take_dir / source.name
                source.rename(final_path)
                final_moves.append((source, final_path))

        if stage_excluded_dir.exists():
            final_dirs.append(excluded_root)
            for source in sorted(stage_excluded_dir.iterdir()):
                final_path = excluded_root / source.name
                source.rename(final_path)
                final_moves.append((source, final_path))

        session = initialize_session(piece_root)
        session = load_session(session.session_path)
        session.angles = list(plan.angles)
        write_session(session)

        files_moved = [{"from": str(source), "to": str(target)} for source, target in [*stage_moves, *final_moves]]
        payload = AutoGroupApplyResult(
            piece_root=str(piece_root),
            proposal_path=str((proposal_path or default_proposal)),
            takes_created=[take.take_id for take in plan.takes],
            excluded_created=[Path(entry.source).name for entry in plan.excluded_media],
            files_moved=files_moved,
            session_config=str(session.session_path),
            status="PASS",
        )
        if write_reports:
            write_json_report(piece_root / "reports" / "auto-group-apply.json", asdict(payload))
            write_markdown_report(
                piece_root / "reports" / "auto-group-apply.md",
                title="Auto Group Apply",
                status="PASS",
                issues=[],
                sections={
                    "Takes Created": payload.takes_created,
                    "Excluded Media": payload.excluded_created or ["none"],
                    "Session Config": {"path": payload.session_config},
                },
            )
        _cleanup_empty_dirs([*stage_dirs, staging_root])
        return payload
    except Exception:
        for _, final_target in reversed(final_moves):
            if final_target.exists():
                if final_target.parent == excluded_root:
                    stage_excluded_dir = staging_root / "excluded"
                    stage_excluded_dir.mkdir(parents=True, exist_ok=True)
                    final_target.rename(stage_excluded_dir / final_target.name)
                else:
                    stage_take_dir = staging_root / final_target.parent.name
                    stage_take_dir.mkdir(parents=True, exist_ok=True)
                    final_target.rename(stage_take_dir / final_target.name)
        for source, staged_target in reversed(stage_moves):
            if staged_target.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                staged_target.rename(source)
        for take_path, backup in take_backups.items():
            if backup is None:
                if take_path.exists():
                    take_path.unlink()
            else:
                take_path.parent.mkdir(parents=True, exist_ok=True)
                take_path.write_bytes(backup)
        if session_backup is None:
            if session_path.exists():
                session_path.unlink()
        else:
            session_path.write_bytes(session_backup)
        _cleanup_empty_dirs([*stage_dirs, *final_dirs, staging_root, excluded_root, takes_root])
        raise
