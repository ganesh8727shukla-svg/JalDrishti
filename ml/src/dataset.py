"""Strict readers and split checks for Kuro Siwo GRD webdataset shards.

This module never extracts archive paths to disk. Samples are decoded from
individual TAR members, and the event id (``actid``) controls the split.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
from pathlib import Path
import tarfile
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np


IGNORE_INDEX = 255
NUM_CLASSES = 3
CLASS_NAMES = ("no_water", "permanent_water", "flood")
INPUT_MEMBER_ORDER = (
    "sec1_vv.npy",
    "sec1_vh.npy",
    "flood_vv.npy",
    "flood_vh.npy",
)
CHANNEL_ORDER = ("pre_vv", "pre_vh", "post_vv", "post_vh")
# Pinned Kuro Siwo v2 author configuration. These are linear-Sigma0
# normalization statistics; the arrays are not converted to dB.
POLARIZATION_MEAN = (0.0953, 0.0264)
POLARIZATION_STD = (0.0427, 0.0215)
# Kuro Siwo's published BlackBench setup clips linear SAR inputs before
# standardization. Keep this explicit in the data contract so training and
# future inference cannot silently use different dynamic ranges.
LINEAR_SIGMA0_CLIP_MAX = 0.15
REQUIRED_MEMBERS = frozenset((*INPUT_MEMBER_ORDER, "mask.npy", "valid_mask.npy", "info.json"))


class KuroDatasetError(ValueError):
    """Raised when a Kuro Siwo sample is incomplete or incompatible."""


class EventSplitError(KuroDatasetError):
    """Raised when event IDs overlap or are assigned to an unexpected split."""


@dataclass(frozen=True)
class KuroExample:
    inputs: np.ndarray
    labels: np.ndarray
    valid_mask: np.ndarray
    event_id: int
    info: Mapping[str, Any]


def load_split_manifest(path: Path | str | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().parents[1] / "configs" / "kuro_siwo_splits.json"
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KuroDatasetError("The Kuro Siwo split manifest is unreadable.") from exc
    splits = manifest.get("splits")
    if not isinstance(splits, dict):
        raise KuroDatasetError("The split manifest must contain train, validation, and test event lists.")
    validate_event_splits(splits)
    return manifest


def validate_event_splits(splits: Mapping[str, Sequence[int]]) -> None:
    expected = {"train", "validation", "test"}
    if set(splits) != expected:
        raise EventSplitError("Splits must define exactly train, validation, and test.")
    normalized: dict[str, set[int]] = {}
    for name, event_ids in splits.items():
        try:
            ids = {int(value) for value in event_ids}
        except (TypeError, ValueError) as exc:
            raise EventSplitError(f"The {name} split contains an invalid event ID.") from exc
        if len(ids) != len(event_ids) or not ids:
            raise EventSplitError(f"The {name} split must contain unique, non-empty event IDs.")
        normalized[name] = ids
    for left, right in (("train", "validation"), ("train", "test"), ("validation", "test")):
        overlap = normalized[left] & normalized[right]
        if overlap:
            raise EventSplitError(f"Event leakage between {left} and {right}: {sorted(overlap)}")


def event_split(event_id: int, splits: Mapping[str, Sequence[int]]) -> str | None:
    matches = [name for name, ids in splits.items() if int(event_id) in {int(value) for value in ids}]
    if len(matches) > 1:
        raise EventSplitError(f"Event {event_id} appears in more than one split.")
    return matches[0] if matches else None


def audit_archive_events(
    archive_event_ids: Iterable[int],
    expected_split: str,
    splits: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """Summarize event assignments without assuming folder names are truthful."""
    if expected_split not in {"train", "validation", "test"}:
        raise EventSplitError("expected_split must be train, validation, or test.")
    seen = {int(event_id) for event_id in archive_event_ids}
    assignments = {event_id: event_split(event_id, splits) for event_id in sorted(seen)}
    unexpected = [event_id for event_id, assigned in assignments.items() if assigned != expected_split]
    return {
        "expected_split": expected_split,
        "event_ids": sorted(seen),
        "assignments": {str(key): value for key, value in assignments.items()},
        "unexpected_event_ids": unexpected,
        "valid": not unexpected,
    }


def scan_shard_event_ids(shard_path: Path | str, *, max_info_bytes: int = 1_000_000) -> set[int]:
    """Read only per-sample info.json members and return their event IDs."""
    path = Path(shard_path)
    event_ids: set[int] = set()
    try:
        with tarfile.open(path, mode="r|*") as archive:
            for member in archive:
                if not member.isfile() or not Path(member.name).name.endswith(".info.json"):
                    continue
                if member.size > max_info_bytes:
                    raise KuroDatasetError(f"Oversized info.json metadata in shard {path.name}.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise KuroDatasetError(f"Could not read sample metadata in shard {path.name}.")
                try:
                    info = json.loads(stream.read().decode("utf-8"))
                    event_ids.add(int(info["actid"]))
                except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise KuroDatasetError(f"Invalid sample event metadata in shard {path.name}.") from exc
    except (OSError, tarfile.TarError) as exc:
        raise KuroDatasetError(f"Cannot read Kuro Siwo shard metadata: {path.name}") from exc
    if not event_ids:
        raise KuroDatasetError(f"No sample event IDs found in shard {path.name}.")
    return event_ids


def audit_event_inventory(
    shard_paths: Iterable[Path | str],
    splits: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """Audit all shard event IDs against the author's event-exclusive split."""
    validate_event_splits(splits)
    paths = sorted((Path(path) for path in shard_paths), key=lambda path: path.as_posix())
    if not paths:
        raise EventSplitError("No dataset shards were supplied for the event audit.")
    found_by_shard: dict[str, list[int]] = {}
    all_ids: set[int] = set()
    folder_mismatches: list[dict[str, Any]] = []
    for path in paths:
        ids = scan_shard_event_ids(path)
        found_by_shard[path.as_posix()] = sorted(ids)
        all_ids.update(ids)
        parent = path.parent.name.lower()
        folder_split = "train" if parent.startswith("train") else "test" if parent.startswith("test") else None
        for event_id in sorted(ids):
            assigned = event_split(event_id, splits)
            if folder_split and assigned and folder_split != assigned:
                folder_mismatches.append({
                    "shard": path.as_posix(),
                    "event_id": event_id,
                    "folder_split": folder_split,
                    "author_split": assigned,
                })

    assigned_ids = {int(value) for values in splits.values() for value in values}
    unknown_ids = sorted(all_ids - assigned_ids)
    missing_by_split = {
        name: sorted({int(value) for value in ids} - all_ids)
        for name, ids in splits.items()
    }
    complete = not unknown_ids and all(not missing for missing in missing_by_split.values())
    event_ids_by_split = {
        name: sorted({int(value) for value in ids} & all_ids)
        for name, ids in splits.items()
    }
    return {
        "schema_version": 1,
        "event_filtering_safe": not unknown_ids,
        "complete_event_coverage": complete,
        "event_ids_found": sorted(all_ids),
        "event_ids_by_split": event_ids_by_split,
        "unknown_event_ids": unknown_ids,
        "missing_event_ids_by_split": missing_by_split,
        "folder_mismatches": folder_mismatches,
        "shards": found_by_shard,
        "policy": "Author actid split is authoritative; all shards are read and samples are filtered by actid, never by directory name.",
    }


def _decode_npy(contents: bytes, name: str) -> np.ndarray:
    try:
        array = np.load(io.BytesIO(contents), allow_pickle=False)
    except (ValueError, OSError) as exc:
        raise KuroDatasetError(f"Invalid NumPy member: {name}") from exc
    if array.ndim == 3 and array.shape[0] == 1:
        array = array[0]
    if array.ndim != 2:
        raise KuroDatasetError(f"{name} must be a single-band 2D array.")
    if array.dtype.kind not in "buif":
        raise KuroDatasetError(f"{name} must use a real numeric data type.")
    return np.asarray(array)


def build_example(members: Mapping[str, bytes]) -> KuroExample:
    missing = REQUIRED_MEMBERS - set(members)
    if missing:
        raise KuroDatasetError(f"Sample is missing members: {', '.join(sorted(missing))}")
    try:
        info = json.loads(members["info.json"].decode("utf-8"))
        event_id = int(info["actid"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise KuroDatasetError("Sample info.json must contain a valid integer actid.") from exc

    decoded = {name: _decode_npy(members[name], name) for name in (*INPUT_MEMBER_ORDER, "mask.npy", "valid_mask.npy")}
    shapes = {array.shape for array in decoded.values()}
    if len(shapes) != 1:
        raise KuroDatasetError("All source bands and masks must have the same dimensions.")

    source_arrays = [decoded[name].astype(np.float32, copy=False) for name in INPUT_MEMBER_ORDER]
    declared_valid = decoded["valid_mask.npy"]
    if not np.all(np.isin(declared_valid, (0, 1))):
        raise KuroDatasetError("valid_mask.npy must use 0 for invalid and 1 for valid pixels.")
    valid = declared_valid.astype(bool)
    for array in source_arrays:
        valid &= np.isfinite(array) & (array > 0)

    raw_labels = decoded["mask.npy"]
    label_valid = np.isfinite(raw_labels) & np.isin(raw_labels, (0, 1, 2))
    if np.any(valid & ~label_valid):
        raise KuroDatasetError("Valid labels must be one of no-water (0), permanent-water (1), or flood (2).")

    # Source inspection confirmed the Kuro GRD workflow exports linear Sigma0.
    # The published BlackBench setup clips SAR inputs at 0.15 before
    # standardization; apply that same ceiling and the pinned per-polarization
    # normalization rather than allowing rare high values to dominate inputs.
    # This four-channel JalDrishti contract uses sec1 as pre-event and flood as
    # post-event, deliberately excluding sec2 to match a two-date inference pair.
    channels = np.zeros((len(source_arrays), *next(iter(shapes))), dtype=np.float32)
    for index, array in enumerate(source_arrays):
        polarization_index = index % 2
        mean = POLARIZATION_MEAN[polarization_index]
        standard_deviation = POLARIZATION_STD[polarization_index]
        clipped = np.minimum(array[valid], LINEAR_SIGMA0_CLIP_MAX)
        channels[index, valid] = (clipped - mean) / standard_deviation

    labels = np.full(next(iter(shapes)), IGNORE_INDEX, dtype=np.uint8)
    labels[valid] = raw_labels[valid].astype(np.uint8)
    return KuroExample(
        inputs=np.ascontiguousarray(channels),
        labels=np.ascontiguousarray(labels),
        valid_mask=np.ascontiguousarray(valid),
        event_id=event_id,
        info=info,
    )


def iter_shard_samples(
    shard_path: Path | str,
    *,
    max_member_bytes: int = 128 * 1024 * 1024,
) -> Iterator[KuroExample]:
    """Stream samples from one TAR shard without extracting its members."""
    path = Path(shard_path)
    sample_id: str | None = None
    members: dict[str, bytes] = {}

    def finish_current() -> KuroExample | None:
        if sample_id is None:
            return None
        return build_example(members)

    try:
        archive_context = tarfile.open(path, mode="r|*")
    except (OSError, tarfile.TarError) as exc:
        raise KuroDatasetError(f"Cannot open Kuro Siwo shard: {path.name}") from exc

    try:
        with archive_context as archive:
            for member in archive:
                if not member.isfile():
                    continue
                base_name = Path(member.name).name
                if "." not in base_name:
                    continue
                current_id, member_name = base_name.split(".", 1)
                if sample_id is not None and current_id != sample_id:
                    example = finish_current()
                    if example is not None:
                        yield example
                    members = {}
                sample_id = current_id
                if member_name not in REQUIRED_MEMBERS:
                    continue
                if member.size > max_member_bytes:
                    raise KuroDatasetError(f"Oversized {member_name} member in sample {current_id}.")
                stream = archive.extractfile(member)
                if stream is None:
                    raise KuroDatasetError(f"Could not read TAR member: {base_name}")
                if member_name in members:
                    raise KuroDatasetError(f"Duplicate TAR member in sample {sample_id}: {member_name}")
                members[member_name] = stream.read()
            example = finish_current()
            if example is not None:
                yield example
    except (OSError, tarfile.TarError) as exc:
        raise KuroDatasetError(f"Corrupt or incomplete Kuro Siwo shard: {path.name}") from exc


def iter_event_split(
    shard_paths: Iterable[Path | str],
    split_name: str,
    split_manifest: Mapping[str, Any] | None = None,
) -> Iterator[KuroExample]:
    """Yield only samples whose actid is explicitly assigned to this split."""
    manifest = split_manifest or load_split_manifest()
    splits = manifest["splits"]
    validate_event_splits(splits)
    if split_name not in splits:
        raise EventSplitError(f"Unknown split: {split_name}")
    allowed = {int(value) for value in splits[split_name]}
    observed: set[int] = set()
    for shard_path in shard_paths:
        for example in iter_shard_samples(shard_path):
            if example.event_id not in allowed:
                continue
            observed.add(example.event_id)
            yield example
    if not observed:
        raise EventSplitError(f"No samples matched the configured {split_name} event IDs.")
