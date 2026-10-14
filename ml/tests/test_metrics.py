from __future__ import annotations

import unittest

import numpy as np

from ml.src.metrics import (
    class_metrics,
    confusion_matrix,
    evaluate_probabilities,
    select_threshold_on_validation,
    ThresholdSweep,
    bootstrap_event_flood_intervals,
)


class SegmentationMetricsTests(unittest.TestCase):
    def test_confusion_matrix_ignores_unlabeled_and_invalid_pixels(self) -> None:
        truth = np.array([[0, 1, 2, 255], [2, 0, 1, 2]])
        pred = np.array([[0, 2, 2, 0], [1, 0, 1, 2]])
        valid = np.array([[1, 1, 1, 1], [1, 0, 1, 1]], dtype=bool)
        matrix = confusion_matrix(truth, pred, valid_mask=valid)
        np.testing.assert_array_equal(matrix, [[1, 0, 0], [0, 1, 1], [0, 1, 2]])

    def test_class_metrics_define_empty_denominators_as_none(self) -> None:
        scores = class_metrics(np.zeros((3, 3), dtype=np.int64))
        self.assertIsNone(scores[0]["precision"])
        self.assertIsNone(scores[0]["recall"])
        self.assertIsNone(scores[0]["iou"])

    def test_flood_probability_metrics_report_uncalibrated_scores(self) -> None:
        truth = np.array([[0, 1, 2, 2, 255]])
        probabilities = np.array(
            [[
                [0.8, 0.1, 0.1],
                [0.1, 0.8, 0.1],
                [0.2, 0.2, 0.6],
                [0.3, 0.3, 0.4],
                [0.0, 0.0, 1.0],
            ]],
            dtype=np.float32,
        )
        result = evaluate_probabilities(truth, probabilities, flood_threshold=0.5)
        self.assertEqual(result["valid_pixel_count"], 4)
        self.assertEqual(result["ignored_pixel_count"], 1)
        self.assertAlmostEqual(result["flood_metrics"]["iou"], 0.5)
        self.assertFalse(result["probability_calibrated"])

    def test_validation_threshold_uses_only_given_validation_cases(self) -> None:
        labels = np.array([[0, 2, 2, 255]])
        probability = np.array([[0.15, 0.45, 0.85, 0.99]])
        result = select_threshold_on_validation(
            [(labels, probability, None)], thresholds=[0.4, 0.5, 0.9]
        )
        self.assertEqual(result["selected_threshold"], 0.4)
        self.assertEqual(result["validation_f1"], 1.0)

    def test_threshold_sweep_streams_cases_without_retain_pixels(self) -> None:
        sweep = ThresholdSweep([0.25, 0.5, 0.75])
        sweep.update(np.array([[0, 2]]), np.array([[0.1, 0.8]]))
        sweep.update(np.array([[2, 255]]), np.array([[0.6, 0.9]]))
        result = sweep.result()
        self.assertEqual(result["selected_threshold"], 0.5)
        self.assertEqual(result["threshold_source"], "validation_only")

    def test_event_bootstrap_is_deterministic_and_clustered_by_event(self) -> None:
        event_matrices = {
            10: np.array([[8, 1], [1, 5]]),
            20: np.array([[4, 2], [2, 2]]),
        }
        first = bootstrap_event_flood_intervals(event_matrices, seed=4, replicates=100)
        second = bootstrap_event_flood_intervals(event_matrices, seed=4, replicates=100)
        self.assertEqual(first, second)
        self.assertEqual(first["event_count"], 2)
        self.assertLessEqual(
            first["flood_metric_intervals"]["f1"]["lower_95"],
            first["flood_metric_intervals"]["f1"]["upper_95"],
        )

    def test_invalid_probabilities_and_shapes_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            evaluate_probabilities(np.zeros((2, 2)), np.zeros((2, 2, 2)), flood_threshold=0.5)
        probabilities = np.zeros((2, 2, 3))
        probabilities[0, 0, 0] = 2.0
        with self.assertRaises(ValueError):
            evaluate_probabilities(np.zeros((2, 2)), probabilities, flood_threshold=0.5)


if __name__ == "__main__":
    unittest.main()
