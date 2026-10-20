import os
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BASE_DIR.parent

if load_dotenv is not None:
    load_dotenv(PROJECT_DIR / ".env", override=False)


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw.strip()))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "Lumo"

    download_dir: Path = PROJECT_DIR / "data" / "downloads"
    file_ttl_seconds: int = 3600
    min_free_disk_mb: int = 1024

    max_concurrent_downloads: int = 2
    max_queued_jobs: int = 8
    max_active_jobs_per_session: int = 2
    max_concurrent_analyses: int = 4
    max_filesize_mb: int = 2048
    max_duration_seconds: int = 4 * 3600
    analyze_timeout_seconds: int = 60
    job_timeout_seconds: int = 30 * 60
    analysis_ttl_seconds: int = 30 * 60

    # per client IP
    analyze_rate_per_minute: int = 15
    jobs_rate_per_hour: int = 30

    ffmpeg_location: str | None = None
    ytdlp_proxy: str | None = None

    cookie_secure: bool = False
    max_request_body_bytes: int = 16 * 1024
    log_level: str = "INFO"

    # dev/tests only
    allow_private_networks: bool = False

    @property
    def max_filesize_bytes(self) -> int:
        return self.max_filesize_mb * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        download_dir = Path(_env_str("DOWNLOAD_DIR", str(cls.download_dir)))
        if not download_dir.is_absolute():
            download_dir = PROJECT_DIR / download_dir
        ffmpeg = os.environ.get("FFMPEG_LOCATION", "").strip() or None
        proxy = os.environ.get("YTDLP_PROXY", "").strip() or None
        return cls(
            app_name=_env_str("APP_NAME", cls.app_name),
            download_dir=download_dir.resolve(),
            file_ttl_seconds=_env_int("FILE_TTL_SECONDS", cls.file_ttl_seconds, 60),
            min_free_disk_mb=_env_int("MIN_FREE_DISK_MB", cls.min_free_disk_mb),
            max_concurrent_downloads=_env_int("MAX_CONCURRENT_DOWNLOADS", cls.max_concurrent_downloads, 1),
            max_queued_jobs=_env_int("MAX_QUEUED_JOBS", cls.max_queued_jobs, 0),
            max_active_jobs_per_session=_env_int(
                "MAX_ACTIVE_JOBS_PER_SESSION", cls.max_active_jobs_per_session, 1
            ),
            max_concurrent_analyses=_env_int("MAX_CONCURRENT_ANALYSES", cls.max_concurrent_analyses, 1),
            max_filesize_mb=_env_int("MAX_FILESIZE_MB", cls.max_filesize_mb, 1),
            max_duration_seconds=_env_int("MAX_DURATION_SECONDS", cls.max_duration_seconds, 1),
            analyze_timeout_seconds=_env_int("ANALYZE_TIMEOUT_SECONDS", cls.analyze_timeout_seconds, 5),
            job_timeout_seconds=_env_int("JOB_TIMEOUT_SECONDS", cls.job_timeout_seconds, 30),
            analysis_ttl_seconds=_env_int("ANALYSIS_TTL_SECONDS", cls.analysis_ttl_seconds, 60),
            analyze_rate_per_minute=_env_int("ANALYZE_RATE_PER_MINUTE", cls.analyze_rate_per_minute, 1),
            jobs_rate_per_hour=_env_int("JOBS_RATE_PER_HOUR", cls.jobs_rate_per_hour, 1),
            ffmpeg_location=ffmpeg,
            ytdlp_proxy=proxy,
            cookie_secure=_env_bool("COOKIE_SECURE", cls.cookie_secure),
            log_level=_env_str("LOG_LEVEL", cls.log_level).upper(),
            allow_private_networks=_env_bool("ALLOW_PRIVATE_NETWORKS", cls.allow_private_networks),
        )

    def resolved_ffmpeg(self) -> str | None:
        return self.ffmpeg_location or shutil.which("ffmpeg")


@lru_cache
def get_settings() -> Settings:
    return Settings.from_env()
