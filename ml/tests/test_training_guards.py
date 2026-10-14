from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ml.src.training import resolve_audited_shards


class TrainingGuardTests(unittest.TestCase):
    def test_refuses_training_until_full_split_audit_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "dataset_revision": "pin",
                "shard_root": str(root),
                "event_filtering_safe": False,
                "complete_event_coverage": False,
                "shards": {},
            }))
            with self.assertRaisesRegex(ValueError, "blocked"):
                resolve_audited_shards(audit, root, "pin")

    def test_refuses_training_when_audited_shard_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "dataset_revision": "pin",
                "shard_root": str(root),
                "event_filtering_safe": True,
                "complete_event_coverage": True,
                "shards": {"train_GRD/shard.tar": [470]},
            }))
            with self.assertRaisesRegex(ValueError, "incomplete"):
                resolve_audited_shards(audit, root, "pin")

    def test_explicit_partial_subset_requires_train_and_validation_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = root / "train_GRD" / "shard.tar"
            shard.parent.mkdir()
            shard.write_bytes(b"audited")
            splits = {"train": [470], "validation": [514], "test": [321]}
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "dataset_revision": "pin",
                "author_split_source_revision": "cfg",
                "shard_root": str(root),
                "event_filtering_safe": True,
                "complete_event_coverage": False,
                "audit_scope": "partial_subset",
                "event_ids_found": [470, 514],
                "splits": splits,
                "shards": {"train_GRD/shard.tar": [470, 514]},
                "shard_sizes_bytes": {"train_GRD/shard.tar": shard.stat().st_size},
            }))
            resolved = resolve_audited_shards(
                audit, root, "pin", splits, "cfg",
                allow_partial=True,
                required_splits=("train", "validation"),
            )
            self.assertEqual(resolved, [shard])

    def test_partial_test_only_subset_cannot_be_used_for_training(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = root / "train_GRD" / "shard.tar"
            shard.parent.mkdir()
            shard.write_bytes(b"audited")
            splits = {"train": [470], "validation": [514], "test": [321]}
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "dataset_revision": "pin",
                "shard_root": str(root),
                "event_filtering_safe": True,
                "complete_event_coverage": False,
                "audit_scope": "partial_subset",
                "event_ids_found": [321],
                "splits": splits,
                "shards": {"train_GRD/shard.tar": [321]},
                "shard_sizes_bytes": {"train_GRD/shard.tar": shard.stat().st_size},
            }))
            with self.assertRaisesRegex(ValueError, "lacks required author split events"):
                resolve_audited_shards(
                    audit, root, "pin", splits,
                    allow_partial=True,
                    required_splits=("train", "validation"),
                )


if __name__ == "__main__":
    unittest.main()
