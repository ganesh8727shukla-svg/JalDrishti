from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

import torch

from ml.src.evaluation import _load_checkpoint_safely


class SafeCheckpointLoadTests(unittest.TestCase):
    def test_loads_project_checkpoint_with_torch_version_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.pt"
            torch.save(
                {
                    "model_state_dict": {"weight": torch.ones((1,))},
                    "torch_version": torch.__version__,
                },
                checkpoint_path,
            )

            checkpoint = _load_checkpoint_safely(checkpoint_path)

        self.assertEqual(checkpoint["torch_version"], str(torch.__version__))
        self.assertTrue(torch.equal(checkpoint["model_state_dict"]["weight"], torch.ones((1,))))

    def test_rejects_non_dictionary_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            checkpoint_path = Path(temporary_directory) / "checkpoint.pt"
            torch.save([torch.ones((1,))], checkpoint_path)

            with self.assertRaisesRegex(ValueError, "must contain a dictionary"):
                _load_checkpoint_safely(checkpoint_path)


if __name__ == "__main__":
    unittest.main()
