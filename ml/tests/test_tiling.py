from __future__ import annotations

import unittest

import numpy as np

from ml.src.dataset import IGNORE_INDEX
from ml.src.tiling import iter_tiles, reconstruct_probabilities


class TilingTests(unittest.TestCase):
    def test_tiles_cover_edge_and_pad_labels_as_ignored(self) -> None:
        inputs = np.ones((4, 5, 13), dtype=np.float32)
        labels = np.zeros((5, 13), dtype=np.uint8)
        tiles = list(iter_tiles(inputs, labels, tile_size=8, overlap=3))
        self.assertTrue(tiles)
        self.assertTrue(all(tile.shape == (4, 8, 8) for tile, _, _ in tiles))
        self.assertTrue(any(np.any(target == IGNORE_INDEX) for _, target, _ in tiles))
        reconstructed, weights = reconstruct_probabilities(
            iter((np.full((3, 8, 8), 0.5, dtype=np.float32), origin) for _, _, origin in tiles),
            height=5,
            width=13,
            tile_size=8,
        )
        self.assertEqual(reconstructed.shape, (3, 5, 13))
        self.assertEqual(int(weights.min()), 1)
        np.testing.assert_allclose(reconstructed, 0.5)

    def test_rejects_invalid_shapes_or_incomplete_probability_coverage(self) -> None:
        with self.assertRaises(ValueError):
            list(iter_tiles(np.ones((2, 8, 8)), np.zeros((8, 8)), tile_size=8))
        with self.assertRaisesRegex(ValueError, "do not cover"):
            reconstruct_probabilities(iter(()), height=2, width=2, tile_size=2)


if __name__ == "__main__":
    unittest.main()
