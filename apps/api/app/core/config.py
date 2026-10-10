from dataclasses import dataclass, field
import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_download_bytes: int
    max_attempts: int
    connect_timeout_seconds: float
    read_timeout_seconds: float
    backoff_seconds: float
    max_backoff_seconds: float
    cdse_username: str | None = field(repr=False)
    cdse_password: str | None = field(repr=False)

    @classmethod
    def from_environment(cls) -> "Settings":
        configured_data_dir = os.getenv("JALDRISHTI_DATA_DIR", "data/raw/sentinel1").strip()
        data_dir = Path(configured_data_dir).expanduser()
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir

        username = os.getenv("CDSE_USERNAME", "").strip() or None
        password = os.getenv("CDSE_PASSWORD", "") or None
        return cls(
            data_dir=data_dir,
            max_download_bytes=_positive_int("JALDRISHTI_MAX_DOWNLOAD_BYTES", 5 * 1024**3),
            max_attempts=min(_positive_int("CDSE_DOWNLOAD_MAX_ATTEMPTS", 3), 5),
            connect_timeout_seconds=_positive_float("CDSE_CONNECT_TIMEOUT_SECONDS", 15.0),
            read_timeout_seconds=_positive_float("CDSE_READ_TIMEOUT_SECONDS", 120.0),
            backoff_seconds=_positive_float("CDSE_RETRY_BACKOFF_SECONDS", 1.0),
            max_backoff_seconds=_positive_float("CDSE_MAX_RETRY_WAIT_SECONDS", 30.0),
            cdse_username=username,
            cdse_password=password,
        )

    @property
    def has_cdse_credentials(self) -> bool:
        return bool(self.cdse_username and self.cdse_password)
