"""Pixel metrics for three-class flood mapping with explicit ignored pixels."""

from __future__ import annotations

from typing import Any, Iterable

import numpy as np


def confusion_matrix(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    num_classes: int = 3,
    ignore_index: int = 255,
    valid_mask: np.ndarray | None = None,
) -> np.ndarray:
    truth = np.asarray(y_true)
    prediction = np.asarray(y_pred)
    if truth.shape != prediction.shape:
        raise ValueError("y_true and y_pred must have identical shapes.")
    valid = truth != ignore_index
    if valid_mask is not None:
        mask = np.asarray(valid_mask, dtype=bool)
        if mask.shape != truth.shape:
            raise ValueError("valid_mask must have the same shape as labels.")
        valid &= mask
    if np.any(valid & ((truth < 0) | (truth >= num_classes))):
        raise ValueError("Ground-truth class outside configured range.")
    if np.any(valid & ((prediction < 0) | (prediction >= num_classes))):
        raise ValueError("Predicted class outside configured range.")
    encoded = num_classes * truth[valid].astype(np.int64) + prediction[valid].astype(np.int64)
    return np.bincount(encoded, minlength=num_classes * num_classes).reshape(num_classes, num_classes)


def _scores(tp: int, fp: int, fn: int) -> dict[str, float | int | None]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    union = tp + fp + fn
    iou = tp / union if union else None
    f1 = (2 * tp / (2 * tp + fp + fn)) if (2 * tp + fp + fn) else None
    return {"precision": precision, "recall": recall, "f1": f1, "iou": iou, "tp": tp, "fp": fp, "fn": fn}


def class_metrics(matrix: np.ndarray) -> list[dict[str, float | int | None]]:
    matrix = np.asarray(matrix, dtype=np.int64)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1]:
        raise ValueError("Confusion matrix must be square.")
    scores = []
    for class_id in range(matrix.shape[0]):
        tp = int(matrix[class_id, class_id])
        fp = int(matrix[:, class_id].sum() - tp)
        fn = int(matrix[class_id, :].sum() - tp)
        scores.append(_scores(tp, fp, fn))
    return scores


def bootstrap_event_flood_intervals(
    event_confusion_matrices: dict[int, np.ndarray],
    *,
    seed: int = 17,
    replicates: int = 1000,
) -> dict[str, Any]:
    """Descriptive cluster bootstrap over held-out events, never over pixels."""
    if not event_confusion_matrices or replicates < 1:
        raise ValueError("At least one event matrix and one bootstrap replicate are required.")
    event_ids = sorted(event_confusion_matrices)
    matrices = np.stack([np.asarray(event_confusion_matrices[key], dtype=np.int64) for key in event_ids])
    if matrices.ndim != 3 or matrices.shape[1:] != (2, 2) or np.any(matrices < 0):
        raise ValueError("Each event flood confusion matrix must be a non-negative 2×2 matrix.")
    random = np.random.default_rng(seed)
    samples = {name: [] for name in ("precision", "recall", "f1", "iou")}
    for indices in random.integers(0, len(event_ids), size=(replicates, len(event_ids))):
        matrix = matrices[indices].sum(axis=0)
        tp = int(matrix[1, 1])
        fp = int(matrix[0, 1])
        fn = int(matrix[1, 0])
        scores = _scores(tp, fp, fn)
        for name in samples:
            if scores[name] is not None:
                samples[name].append(float(scores[name]))
    intervals = {
        name: (
            {"lower_95": float(np.quantile(values, 0.025)), "upper_95": float(np.quantile(values, 0.975))}
            if values
            else None
        )
        for name, values in samples.items()
    }
    return {
        "method": "event_cluster_bootstrap_percentile",
        "event_count": len(event_ids),
        "replicates": replicates,
        "seed": seed,
        "flood_metric_intervals": intervals,
        "interpretation": "Descriptive uncertainty across the sampled held-out events at the declared flood threshold; not a calibrated pixel-probability interval or a guarantee of geographic transfer.",
    }


def evaluate_probabilities(
    y_true: np.ndarray,
    class_probabilities: np.ndarray,
    *,
    flood_threshold: float,
    ignore_index: int = 255,
    valid_mask: np.ndarray | None = None,
) -> dict[str, Any]:
    probabilities = np.asarray(class_probabilities)
    truth = np.asarray(y_true)
    if probabilities.shape[:-1] != truth.shape or probabilities.shape[-1] != 3:
        raise ValueError("Probabilities must have shape (*label_shape, 3).")
    if not 0.0 <= flood_threshold <= 1.0:
        raise ValueError("flood_threshold must be in [0, 1].")
    if not np.all(np.isfinite(probabilities)) or np.any(probabilities < 0) or np.any(probabilities > 1):
        raise ValueError("Probabilities must be finite and in [0, 1].")
    multi = np.argmax(probabilities, axis=-1)
    matrix = confusion_matrix(truth, multi, ignore_index=ignore_index, valid_mask=valid_mask)
    binary_truth = np.where(truth == ignore_index, ignore_index, truth == 2).astype(np.int64)
    binary_pred = (probabilities[..., 2] >= flood_threshold).astype(np.uint8)
    binary_matrix = confusion_matrix(
        binary_truth,
        binary_pred,
        num_classes=2,
        ignore_index=ignore_index,
        valid_mask=valid_mask,
    )
    binary_score = class_metrics(binary_matrix)[1]
    return {
        "confusion_matrix": matrix.tolist(),
        "class_metrics": class_metrics(matrix),
        "flood_threshold": float(flood_threshold),
        "flood_binary_confusion_matrix": binary_matrix.tolist(),
        "flood_metrics": binary_score,
        "valid_pixel_count": int(binary_matrix.sum()),
        "ignored_pixel_count": int(
            np.count_nonzero(truth == ignore_index)
            + (0 if valid_mask is None else np.count_nonzero(~np.asarray(valid_mask, dtype=bool) & (truth != ignore_index)))
        ),
        "probability_calibrated": False,
    }


class ThresholdSweep:
    """Streaming validation-only F1 sweep without retaining scene predictions."""

    def __init__(self, thresholds: Iterable[float] = tuple(np.linspace(0.05, 0.95, 19))) -> None:
        self.thresholds = np.asarray(sorted({float(value) for value in thresholds}), dtype=np.float64)
        if self.thresholds.size == 0 or np.any(self.thresholds < 0) or np.any(self.thresholds > 1):
            raise ValueError("Threshold candidates must be non-empty values in [0, 1].")
        self.tp = np.zeros(self.thresholds.size, dtype=np.int64)
        self.fp = np.zeros(self.thresholds.size, dtype=np.int64)
        self.fn = np.zeros(self.thresholds.size, dtype=np.int64)

    def update(
        self,
        labels: np.ndarray,
        flood_probability: np.ndarray,
        valid_mask: np.ndarray | None = None,
    ) -> None:
        truth = np.asarray(labels)
        probability = np.asarray(flood_probability)
        if truth.shape != probability.shape:
            raise ValueError("Validation labels and probability arrays must align.")
        if np.any(~np.isin(truth, (0, 1, 2, 255))):
            raise ValueError("Validation labels must use classes 0, 1, 2, or ignore index 255.")
        if not np.all(np.isfinite(probability)) or np.any(probability < 0) or np.any(probability > 1):
            raise ValueError("Validation flood probabilities must be finite and in [0, 1].")
        valid = truth != 255
        if valid_mask is not None:
            mask = np.asarray(valid_mask, dtype=bool)
            if mask.shape != truth.shape:
                raise ValueError("Validation valid mask must align with labels.")
            valid &= mask
        flood = truth == 2
        for index, threshold in enumerate(self.thresholds):
            predicted = probability >= threshold
            self.tp[index] += np.count_nonzero(valid & flood & predicted)
            self.fp[index] += np.count_nonzero(valid & ~flood & predicted)
            self.fn[index] += np.count_nonzero(valid & flood & ~predicted)

    def result(self) -> dict[str, Any]:
        candidates = []
        for index, threshold in enumerate(self.thresholds):
            scores = _scores(int(self.tp[index]), int(self.fp[index]), int(self.fn[index]))
            candidates.append({"threshold": float(threshold), "f1": scores["f1"], **{key: scores[key] for key in ("tp", "fp", "fn")}})
        best = max(
            candidates,
            key=lambda item: (
                -1.0 if item["f1"] is None else item["f1"],
                -abs(item["threshold"] - 0.5),
                item["threshold"],
            ),
        )
        return {
            "selected_threshold": best["threshold"],
            "validation_f1": best["f1"],
            "candidates": candidates,
            "threshold_source": "validation_only",
        }


def select_threshold_on_validation(
    validation_cases: Iterable[tuple[np.ndarray, np.ndarray, np.ndarray | None]],
    thresholds: Iterable[float] = tuple(np.linspace(0.05, 0.95, 91)),
) -> dict[str, Any]:
    """Choose a flood threshold using validation labels only (max pooled F1)."""
    cases = list(validation_cases)
    if not cases:
        raise ValueError("At least one validation case is required.")
    sweep = ThresholdSweep(thresholds)
    for labels, flood_probability, valid_mask in cases:
        sweep.update(labels, flood_probability, valid_mask)
    return sweep.result()
