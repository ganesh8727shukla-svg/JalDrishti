from __future__ import annotations

import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

import numpy as np

from ml.src.dataset import (
    EventSplitError,
    KuroDatasetError,
    audit_archive_events,
    audit_event_inventory,
    build_example,
    iter_event_split,
    load_split_manifest,
    scan_shard_event_ids,
    validate_event_splits,
)


def npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, array, allow_pickle=False)
    return buffer.getvalue()


def sample_members(event_id: int = 470) -> dict[str, bytes]:
    shape = (3, 4)
    return {
        "sec1_vv.npy": npy_bytes(np.full(shape, 0.0953, dtype=np.float32)),
        "sec1_vh.npy": npy_bytes(np.full(shape, 0.0264, dtype=np.float32)),
        "flood_vv.npy": npy_bytes(np.full(shape, 0.1380, dtype=np.float32)),
        "flood_vh.npy": npy_bytes(np.full(shape, 0.0479, dtype=np.float32)),
        "mask.npy": npy_bytes(np.array([[0, 1, 2, 2]] * 3, dtype=np.uint8)),
        "valid_mask.npy": npy_bytes(np.ones(shape, dtype=np.uint8)),
        "info.json": json.dumps({"actid": event_id, "sample": "fixture"}).encode(),
    }


def append_sample(archive: tarfile.TarFile, sample_id: str, members: dict[str, bytes]) -> None:
    for member_name, contents in members.items():
        info = tarfile.TarInfo(f"{sample_id}.{member_name}")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))


class KuroDatasetTests(unittest.TestCase):
    def test_linear_sigma0_input_order_and_author_normalization(self) -> None:
        example = build_example(sample_members())
        self.assertEqual(example.inputs.shape, (4, 3, 4))
        self.assertEqual(example.labels.shape, (3, 4))
        self.assertEqual(example.event_id, 470)
        self.assertEqual(example.inputs.dtype, np.float32)
        # First two pre-event channels equal the fixed published mean.
        np.testing.assert_allclose(example.inputs[:2], 0.0, atol=1e-6)
        self.assertGreater(float(example.inputs[2, 0, 0]), 0.9)
        self.assertGreater(float(example.inputs[3, 0, 0]), 0.9)
        np.testing.assert_array_equal(example.labels[0], [0, 1, 2, 2])

    def test_linear_sigma0_is_clipped_before_author_normalization(self) -> None:
        members = sample_members()
        vv = np.full((3, 4), 0.0953, dtype=np.float32)
        vv[0, 0] = 0.5
        members["sec1_vv.npy"] = npy_bytes(vv)
        example = build_example(members)
        expected = (0.15 - 0.0953) / 0.0427
        self.assertAlmostEqual(float(example.inputs[0, 0, 0]), expected, places=6)
        self.assertTrue(np.isfinite(example.inputs).all())

    def test_invalid_pixels_are_ignored_across_all_channels(self) -> None:
        members = sample_members()
        valid = np.ones((3, 4), dtype=np.uint8)
        valid[0, 0] = 0
        members["valid_mask.npy"] = npy_bytes(valid)
        example = build_example(members)
        self.assertFalse(example.valid_mask[0, 0])
        self.assertEqual(int(example.labels[0, 0]), 255)
        self.assertTrue(np.all(example.inputs[:, 0, 0] == 0))

    def test_nonpositive_sigma0_pixel_is_ignored(self) -> None:
        members = sample_members()
        vv = np.full((3, 4), 0.0953, dtype=np.float32)
        vv[1, 2] = 0
        members["sec1_vv.npy"] = npy_bytes(vv)
        example = build_example(members)
        self.assertFalse(example.valid_mask[1, 2])
        self.assertEqual(int(example.labels[1, 2]), 255)

    def test_rejects_bad_label_on_declared_valid_pixel(self) -> None:
        members = sample_members()
        mask = np.zeros((3, 4), dtype=np.uint8)
        mask[0, 0] = 3
        members["mask.npy"] = npy_bytes(mask)
        with self.assertRaises(KuroDatasetError):
            build_example(members)

    def test_rejects_band_shape_mismatch(self) -> None:
        members = sample_members()
        members["flood_vh.npy"] = npy_bytes(np.ones((2, 4), dtype=np.float32))
        with self.assertRaises(KuroDatasetError):
            build_example(members)

    def test_split_manifest_has_no_event_leakage(self) -> None:
        manifest = load_split_manifest()
        validate_event_splits(manifest["splits"])
        self.assertEqual(manifest["dataset"]["license"], "CC BY 4.0")
        with self.assertRaises(EventSplitError):
            validate_event_splits({"train": [1], "validation": [1], "test": [2]})

    def test_author_event_ids_override_archive_folder_names(self) -> None:
        splits = {"train": [470], "validation": [514], "test": [321]}
        audit = audit_archive_events([470, 321], "train", splits)
        self.assertEqual(audit["unexpected_event_ids"], [321])
        self.assertFalse(audit["valid"])

    def test_tar_stream_filters_by_event_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shard = Path(tmp) / "samples.tar"
            with tarfile.open(shard, "w") as archive:
                append_sample(archive, "sample-train", sample_members(470))
                append_sample(archive, "sample-test", sample_members(321))
            manifest = {"splits": {"train": [470], "validation": [514], "test": [321]}}
            observed = list(iter_event_split([shard], "train", manifest))
            self.assertEqual([item.event_id for item in observed], [470])

    def test_full_inventory_uses_author_event_ids_despite_folder_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / folder / "shard.tar" for folder in ("train_GRD", "val_GRD", "test_GRD")]
            for path, event_id in zip(paths, (470, 514, 321)):
                path.parent.mkdir()
                with tarfile.open(path, "w") as archive:
                    append_sample(archive, f"sample-{event_id}", sample_members(event_id))
            self.assertEqual(scan_shard_event_ids(paths[0]), {470})
            result = audit_event_inventory(paths, {"train": [470], "validation": [514], "test": [321]})
            self.assertTrue(result["event_filtering_safe"])
            self.assertTrue(result["complete_event_coverage"])
            self.assertEqual(result["missing_event_ids_by_split"], {"train": [], "validation": [], "test": []})

            misplaced = root / "test_GRD" / "misplaced.tar"
            with tarfile.open(misplaced, "w") as archive:
                append_sample(archive, "sample-misplaced", sample_members(470))
            result = audit_event_inventory([*paths, misplaced], {"train": [470], "validation": [514], "test": [321]})
            self.assertTrue(result["event_filtering_safe"])
            self.assertEqual(result["folder_mismatches"][0]["author_split"], "train")

    def test_partial_inventory_is_filterable_but_reports_missing_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "train_GRD" / "shard.tar"
            path.parent.mkdir()
            with tarfile.open(path, "w") as archive:
                append_sample(archive, "sample-train", sample_members(470))
                append_sample(archive, "sample-validation", sample_members(514))
            result = audit_event_inventory(
                [path],
                {"train": [470], "validation": [514], "test": [321]},
            )
            self.assertTrue(result["event_filtering_safe"])
            self.assertFalse(result["complete_event_coverage"])
            self.assertEqual(result["event_ids_by_split"], {"train": [470], "validation": [514], "test": []})
            self.assertEqual(result["unknown_event_ids"], [])


if __name__ == "__main__":
    unittest.main()
