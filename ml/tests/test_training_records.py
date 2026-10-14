from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ml.src.training_records import TrainingProgressRecorder


class TrainingProgressRecorderTests(unittest.TestCase):
    def test_epoch_progress_is_atomically_recorded_and_can_finish_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.training.json"
            recorder = TrainingProgressRecorder(path, {"configuration": {"epochs_requested": 4}})
            recorder.start()
            recorder.epoch_completed({"epoch": 1, "validation_loss_per_valid_pixel": 0.4})
            recorder.epoch_completed({"epoch": 2, "validation_loss_per_valid_pixel": 0.5})

            midway = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(midway["run_status"], "running")
            self.assertEqual(midway["epochs_completed"], 2)
            self.assertEqual([item["epoch"] for item in midway["history"]], [1, 2])
            self.assertEqual(list(Path(directory).glob("*.part")), [])

            recorder.finish("interrupted", interruption="KeyboardInterrupt")
            final = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(final["run_status"], "interrupted")
            self.assertEqual(final["interruption"], "KeyboardInterrupt")
            self.assertEqual(final["epochs_completed"], 2)

    def test_refuses_to_overwrite_existing_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.training.json"
            path.write_text('{"existing": true}\n', encoding="utf-8")
            recorder = TrainingProgressRecorder(path, {})
            with self.assertRaises(FileExistsError):
                recorder.start()
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"existing": True})


if __name__ == "__main__":
    unittest.main()
