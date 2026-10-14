from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import torch
from torch.utils.data import Dataset

from ml.src.training import train_model


class TinyDataset(Dataset):
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __len__(self) -> int:
        return 1

    def __getitem__(self, _index: int):
        return torch.full((4, 16, 16), 0.1), torch.zeros((16, 16), dtype=torch.long), 1


class ControlledValidationModel(torch.nn.Module):
    """Trainable tiny model with a deterministic worse validation loss after epoch 1."""

    def __init__(self) -> None:
        super().__init__()
        self.base_channels = 4
        self.bias = torch.nn.Parameter(torch.tensor(0.0))
        self.validation_passes = 0

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        n, _channels, height, width = inputs.shape
        if self.training:
            foreground = self.bias.expand(n, 1, height, width)
        else:
            score = 5.0 if self.validation_passes == 0 else -5.0
            self.validation_passes += 1
            foreground = torch.full((n, 1, height, width), score, device=inputs.device)
        other = torch.zeros((n, 2, height, width), device=inputs.device)
        return torch.cat((foreground, other), dim=1)


class EarlyStoppingTests(unittest.TestCase):
    def test_validation_plateau_stops_and_retains_best_checkpoint(self) -> None:
        model = ControlledValidationModel()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "candidate.pt"
            shard_path = Path(directory) / "fake.tar"
            shard_path.write_bytes(b"synthetic fixture, never opened")
            with patch("ml.src.training.KuroTileDataset", TinyDataset):
                result = train_model(
                    shard_paths=[shard_path],
                    split_manifest={
                        "dataset": {"id": "fixture", "revision": "rev"},
                        "author_split_source": {"revision": "rev"},
                        "splits": {"train": [1], "validation": [2], "test": [3]},
                    },
                    model=model,  # type: ignore[arg-type]
                    output_path=output,
                    epochs=10,
                    batch_size=1,
                    tile_size=16,
                    overlap=0,
                    early_stopping_patience=2,
                    seed=7,
                )
            self.assertTrue(output.is_file())
            self.assertTrue(result["early_stopped"])
            self.assertEqual(result["epochs_requested"], 10)
            self.assertEqual(result["epochs_completed"], 3)
            self.assertEqual(len(result["history"]), 3)
            checkpoint = torch.load(output, map_location="cpu", weights_only=True)
            self.assertEqual(checkpoint["training_config"]["epoch"], 1)
            self.assertEqual(checkpoint["training_config"]["early_stopping_patience"], 2)


if __name__ == "__main__":
    unittest.main()
