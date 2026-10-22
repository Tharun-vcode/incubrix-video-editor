from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str
    data_dir: Path
    database_path: Path
    storage_backend: str
    output_dir: Path
    s3_endpoint_url: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_public_url: str
    transcriber: str
    whisper_model: str
    ffmpeg_path: str
    ffprobe_path: str
    ffmpeg_font_file: str
    max_upload_bytes: int
    max_video_seconds: int
    max_url_bytes: int
    download_timeout_seconds: int
    rate_limit_per_minute: int
    job_workers: int
    callback_secret: str
    demo_access_token: str
    log_level: str

    @classmethod
    def from_environment(cls) -> Settings:
        data_dir = Path(os.getenv("DATA_DIR", "data")).expanduser()
        return cls(
            app_name=os.getenv("APP_NAME", "IncuBrix Video Editing Engine"),
            data_dir=data_dir,
            database_path=Path(
                os.getenv("DATABASE_PATH", str(data_dir / "engine.sqlite3"))
            ).expanduser(),
            storage_backend=os.getenv("STORAGE_BACKEND", "filesystem").lower(),
            output_dir=Path(
                os.getenv("OUTPUT_DIR", str(data_dir / "outputs"))
            ).expanduser(),
            s3_endpoint_url=os.getenv("S3_ENDPOINT_URL", ""),
            s3_bucket=os.getenv("S3_BUCKET", "incubrix-results"),
            s3_region=os.getenv("S3_REGION", "us-east-1"),
            s3_access_key=os.getenv("S3_ACCESS_KEY", ""),
            s3_secret_key=os.getenv("S3_SECRET_KEY", ""),
            s3_public_url=os.getenv("S3_PUBLIC_URL", ""),
            transcriber=os.getenv("TRANSCRIBER", "mock").lower(),
            whisper_model=os.getenv("WHISPER_MODEL", "base"),
            ffmpeg_path=os.getenv("FFMPEG_PATH", "ffmpeg"),
            ffprobe_path=os.getenv("FFPROBE_PATH", "ffprobe"),
            ffmpeg_font_file=os.getenv("FFMPEG_FONT_FILE", ""),
            max_upload_bytes=int(os.getenv("MAX_UPLOAD_BYTES", "536870912")),
            max_video_seconds=int(os.getenv("MAX_VIDEO_SECONDS", "3600")),
            max_url_bytes=int(os.getenv("MAX_URL_BYTES", "536870912")),
            download_timeout_seconds=int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "30")),
            rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "120")),
            job_workers=int(os.getenv("JOB_WORKERS", "2")),
            callback_secret=os.getenv("CALLBACK_SECRET", ""),
            demo_access_token=os.getenv("DEMO_ACCESS_TOKEN", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


settings = Settings.from_environment()
