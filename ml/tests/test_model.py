from __future__ import annotations

import unittest

try:
    import torch
except ImportError:  # pragma: no cover - dependency is installed for this milestone
    torch = None

if torch is not None:
    from ml.src.model import UNet


@unittest.skipIf(torch is None, "Install the pinned PyTorch dependency to test the U-Net.")
class UNetTests(unittest.TestCase):
    def test_four_channel_model_preserves_spatial_shape(self) -> None:
        torch.manual_seed(17)
        model = UNet(base_channels=4)
        inputs = torch.randn(2, 4, 33, 35, dtype=torch.float32)
        logits = model(inputs)
        self.assertEqual(tuple(logits.shape), (2, 3, 33, 35))
        loss = logits.square().mean()
        loss.backward()
        self.assertIsNotNone(model.classifier.weight.grad)

    def test_contract_rejects_wrong_channel_count_and_integer_input(self) -> None:
        model = UNet(base_channels=4)
        with self.assertRaises(ValueError):
            model(torch.zeros(1, 3, 16, 16))
        with self.assertRaises(ValueError):
            model(torch.zeros(1, 4, 16, 16, dtype=torch.int64))


if __name__ == "__main__":
    unittest.main()
