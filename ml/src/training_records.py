"""Atomic JSON run records that retain epoch progress across interruptions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


class TrainingProgressRecorder:
    """Persist run state after every completed epoch without overwriting runs."""

    def __init__(self, path: Path, initial_record: dict[str, Any]) -> None:
        self.path = Path(path)
        self.record = dict(initial_record)
        self.record.setdefault("schema_version", 1)
        self.record.setdefault("run_status", "queued")
        self.record.setdefault("epochs_completed", 0)
        self.record.setdefault("history", [])

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".part",
                delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump(self.record, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def start(self) -> None:
        if self.path.exists():
            raise FileExistsError(f"Refusing to overwrite training record: {self.path}")
        self.record["run_status"] = "running"
        self._write()

    def epoch_completed(self, record: dict[str, Any]) -> None:
        history = list(self.record.get("history", []))
        history.append(dict(record))
        self.record["history"] = history
        self.record["epochs_completed"] = len(history)
        self.record["run_status"] = "running"
        self.record.pop("error_type", None)
        self._write()

    def finish(self, run_status: str, **details: Any) -> None:
        self.record["run_status"] = run_status
        self.record.update(details)
        self._write()
