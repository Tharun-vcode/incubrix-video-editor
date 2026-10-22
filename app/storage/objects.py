from __future__ import annotations

from pathlib import Path
import shutil
from typing import Protocol
from urllib.parse import quote

from app.core.config import Settings


class ObjectStorage(Protocol):
    def put(self, key: str, path: Path, content_type: str) -> str: ...

    def download_url(self, key: str) -> str: ...

    def read(self, key: str) -> bytes: ...

    def ready(self) -> bool: ...


class FilesystemStorage:
    def __init__(self, root: Path, public_base_url: str = ""):
        self.root = root.resolve()
        self.public_base_url = public_base_url.rstrip("/")
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, path: Path, content_type: str) -> str:
        del content_type
        destination = self._resolve(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        return key

    def download_url(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{quote(key)}"
        return f"/api/v1/files/{quote(key)}"

    def read(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def ready(self) -> bool:
        return self.root.is_dir()

    def _resolve(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("storage key escapes the configured output directory")
        return path


class S3Storage:
    def __init__(self, settings: Settings):
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError(
                "S3 storage requires boto3; install the project requirements."
            ) from error
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key or None,
            aws_secret_access_key=settings.s3_secret_key or None,
        )
        self.bucket = settings.s3_bucket
        self.public_url = settings.s3_public_url.rstrip("/")
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code != 404:
                raise RuntimeError(
                    f"Could not access configured S3 bucket {self.bucket!r}."
                ) from error
            self.client.create_bucket(Bucket=self.bucket)

    def put(self, key: str, path: Path, content_type: str) -> str:
        self.client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return key

    def download_url(self, key: str) -> str:
        if self.public_url:
            return f"{self.public_url}/{quote(key)}"
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=3600,
        )

    def read(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as error:
            code = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code == 404:
                raise FileNotFoundError(key) from error
            raise RuntimeError("Could not read the requested S3 object.") from error

    def ready(self) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
            return True
        except ClientError as error:
            raise RuntimeError(
                f"Configured S3 bucket {self.bucket!r} is not ready."
            ) from error


def create_storage(settings: Settings) -> ObjectStorage:
    if settings.storage_backend == "filesystem":
        return FilesystemStorage(settings.output_dir, settings.s3_public_url)
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    raise ValueError(
        f"unsupported STORAGE_BACKEND {settings.storage_backend!r}; use filesystem or s3"
    )
