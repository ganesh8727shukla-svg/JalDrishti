"""Deterministic, edge-covering tiles for supervised SAR segmentation."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from ml.src.dataset import IGNORE_INDEX


def _tile_starts(length: int, tile_size: int, stride: int) -> tuple[list[int], int]:
    if length <= tile_size:
        return [0], tile_size
    starts = list(range(0, length - tile_size + 1, stride))
    last = length - tile_size
    if starts[-1] != last:
        starts.append(last)
    return starts, length


def iter_tiles(
    inputs: np.ndarray,
    labels: np.ndarray,
    *,
    tile_size: int = 256,
    overlap: int = 32,
) -> Iterator[tuple[np.ndarray, np.ndarray, tuple[int, int]]]:
    """Yield NCHW-source tiles and labels, including padded edge coverage.

    The inputs are `C×H×W`; labels are `H×W`. Padding is neutral zero in the
    already-normalized input, and ignored in labels. Overlap is allowed for
    evaluation/inference coverage but callers should weight overlap only once
    when aggregating pixel metrics.
    """
    inputs = np.asarray(inputs, dtype=np.float32)
    labels = np.asarray(labels)
    if inputs.ndim != 3 or labels.ndim != 2 or inputs.shape[-2:] != labels.shape:
        raise ValueError("inputs must be C×H×W and align with the H×W labels.")
    if tile_size < 1 or overlap < 0 or overlap >= tile_size:
        raise ValueError("tile_size must be positive and overlap must be in [0, tile_size).")
    if inputs.shape[0] != 4:
        raise ValueError("The model tile input must have exactly four channels.")
    height, width = labels.shape
    stride = tile_size - overlap
    y_starts, padded_height = _tile_starts(height, tile_size, stride)
    x_starts, padded_width = _tile_starts(width, tile_size, stride)
    if padded_height < height:
        padded_height = y_starts[-1] + tile_size
    if padded_width < width:
        padded_width = x_starts[-1] + tile_size
    padded_inputs = np.pad(
        inputs,
        ((0, 0), (0, padded_height - height), (0, padded_width - width)),
        mode="constant",
        constant_values=0,
    )
    padded_labels = np.pad(
        labels.astype(np.uint8, copy=False),
        ((0, padded_height - height), (0, padded_width - width)),
        mode="constant",
        constant_values=IGNORE_INDEX,
    )
    for y in y_starts:
        for x in x_starts:
            yield (
                np.ascontiguousarray(padded_inputs[:, y : y + tile_size, x : x + tile_size]),
                np.ascontiguousarray(padded_labels[y : y + tile_size, x : x + tile_size]),
                (y, x),
            )


def reconstruct_probabilities(
    tile_probabilities: Iterator[tuple[np.ndarray, tuple[int, int]]],
    *,
    height: int,
    width: int,
    tile_size: int = 256,
) -> tuple[np.ndarray, np.ndarray]:
    """Blend overlapping C×tile×tile probabilities with equal pixel weights."""
    if height < 1 or width < 1 or tile_size < 1:
        raise ValueError("height, width, and tile_size must be positive.")
    total = np.zeros((3, height, width), dtype=np.float64)
    weights = np.zeros((height, width), dtype=np.uint32)
    for probabilities, (y, x) in tile_probabilities:
        probabilities = np.asarray(probabilities)
        if probabilities.shape != (3, tile_size, tile_size):
            raise ValueError("Each probability tile must have shape 3×tile_size×tile_size.")
        if not np.all(np.isfinite(probabilities)) or np.any(probabilities < 0) or np.any(probabilities > 1):
            raise ValueError("Probability tiles must be finite and in [0, 1].")
        if y < 0 or x < 0 or y >= height or x >= width:
            raise ValueError("Tile origin is outside the destination raster.")
        h = min(tile_size, height - y)
        w = min(tile_size, width - x)
        total[:, y : y + h, x : x + w] += probabilities[:, :h, :w]
        weights[y : y + h, x : x + w] += 1
    if np.any(weights == 0):
        raise ValueError("Tiles do not cover every output pixel.")
    probabilities = (total / weights[None, :, :]).astype(np.float32)
    return probabilities, weights
