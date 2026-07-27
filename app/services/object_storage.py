"""Encrypted S3-compatible object storage connections."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from config import Settings
from models import Storage


_PROVIDER_NAMES = {
    "aws": "AWS S3",
    "r2": "Cloudflare R2",
    "custom": "S3-compatible",
}
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ACCOUNT_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_NAMESPACE_PATTERN = re.compile(r"[^A-Z0-9]+")


class ObjectStorageConfigurationError(ValueError):
    """Object storage configuration or credentials are invalid."""


class ObjectStorageConnectionError(RuntimeError):
    """The configured bucket could not pass a read/write/delete check."""


@dataclass(frozen=True)
class ObjectStorageCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str | None = None

    def as_dict(self) -> dict[str, str]:
        values = {
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
        }
        if self.session_token:
            values["session_token"] = self.session_token
        return values


@dataclass(frozen=True)
class ObjectStorageConfig:
    provider: str
    bucket: str
    region: str
    endpoint_url: str
    path_style: bool
    public_url: str | None = None
    account_id: str | None = None

    @property
    def provider_name(self) -> str:
        return _PROVIDER_NAMES[self.provider]

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "bucket": self.bucket,
            "region": self.region,
            "endpoint_url": self.endpoint_url,
            "path_style": self.path_style,
            "public_url": self.public_url,
            "account_id": self.account_id,
        }


class ObjectStorageService:
    """Validate, verify, encrypt, and expose S3-compatible connections."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @classmethod
    def build_config(
        cls,
        *,
        provider: str,
        bucket: str,
        region: str | None = None,
        account_id: str | None = None,
        endpoint_url: str | None = None,
        public_url: str | None = None,
        path_style: bool = False,
        allow_insecure: bool = False,
    ) -> ObjectStorageConfig:
        provider = str(provider or "").strip().lower()
        if provider not in _PROVIDER_NAMES:
            raise ObjectStorageConfigurationError("Unsupported object storage provider.")

        bucket = str(bucket or "").strip().lower()
        cls.validate_bucket(bucket)
        region = str(region or "").strip().lower()
        account_id = str(account_id or "").strip().lower()

        if provider == "aws":
            region = region or "us-east-1"
            cls.validate_region(region)
            endpoint_url = f"https://s3.{region}.amazonaws.com"
            path_style = False
        elif provider == "r2":
            if not _ACCOUNT_ID_PATTERN.fullmatch(account_id):
                raise ObjectStorageConfigurationError(
                    "Cloudflare account ID must contain 32 hexadecimal characters."
                )
            region = "auto"
            endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
            path_style = False
        else:
            region = region or "us-east-1"
            cls.validate_region(region)
            endpoint_url = cls.normalize_url(
                endpoint_url,
                field="Endpoint URL",
                allow_insecure=allow_insecure,
            )

        normalized_public_url = None
        if public_url:
            normalized_public_url = cls.normalize_url(
                public_url,
                field="Public URL",
                allow_insecure=allow_insecure,
                allow_path=True,
            )

        return ObjectStorageConfig(
            provider=provider,
            bucket=bucket,
            region=region,
            endpoint_url=str(endpoint_url),
            path_style=bool(path_style),
            public_url=normalized_public_url,
            account_id=account_id or None,
        )

    @classmethod
    def config_from_storage(cls, storage: Storage) -> ObjectStorageConfig:
        config = storage.config or {}
        return cls.build_config(
            provider=str(config.get("provider") or ""),
            bucket=str(config.get("bucket") or ""),
            region=str(config.get("region") or ""),
            account_id=str(config.get("account_id") or ""),
            endpoint_url=str(config.get("endpoint_url") or ""),
            public_url=str(config.get("public_url") or ""),
            path_style=bool(config.get("path_style")),
            allow_insecure=True,
        )

    @staticmethod
    def credentials_from_storage(storage: Storage) -> ObjectStorageCredentials:
        credentials = storage.credentials
        return ObjectStorageService.build_credentials(
            access_key_id=credentials.get("access_key_id"),
            secret_access_key=credentials.get("secret_access_key"),
            session_token=credentials.get("session_token"),
        )

    @staticmethod
    def build_credentials(
        *,
        access_key_id: object,
        secret_access_key: object,
        session_token: object = None,
    ) -> ObjectStorageCredentials:
        access_key = str(access_key_id or "").strip()
        secret_key = str(secret_access_key or "").strip()
        token = str(session_token or "").strip() or None
        if not 3 <= len(access_key) <= 128:
            raise ObjectStorageConfigurationError(
                "Access key ID must contain between 3 and 128 characters."
            )
        if not 8 <= len(secret_key) <= 256:
            raise ObjectStorageConfigurationError(
                "Secret access key must contain between 8 and 256 characters."
            )
        if token and len(token) > 4096:
            raise ObjectStorageConfigurationError(
                "Session token must be 4096 characters or fewer."
            )
        return ObjectStorageCredentials(access_key, secret_key, token)

    async def verify(
        self,
        config: ObjectStorageConfig,
        credentials: ObjectStorageCredentials,
    ) -> None:
        await self.validate_endpoint_network(config)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._verify_sync, config, credentials),
                timeout=30,
            )
        except TimeoutError as exc:
            raise ObjectStorageConnectionError(
                "Bucket verification timed out."
            ) from exc

    def configure(
        self,
        storage: Storage,
        config: ObjectStorageConfig,
        credentials: ObjectStorageCredentials,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        payload = config.as_dict()
        payload["verified_at"] = (
            verified_at or datetime.now(UTC)
        ).isoformat()
        storage.config = payload
        storage.credentials = credentials.as_dict()

    @classmethod
    def runtime_environment(cls, storage: Storage) -> dict[str, str]:
        config = cls.config_from_storage(storage)
        credentials = cls.credentials_from_storage(storage)
        prefix = f"DEVPUSH_OBJECT_{cls.namespace(storage.name)}"
        values = {
            f"{prefix}_PROVIDER": config.provider,
            f"{prefix}_BUCKET": config.bucket,
            f"{prefix}_REGION": config.region,
            f"{prefix}_ENDPOINT_URL": config.endpoint_url,
            f"{prefix}_FORCE_PATH_STYLE": str(config.path_style).lower(),
            f"{prefix}_ACCESS_KEY_ID": credentials.access_key_id,
            f"{prefix}_SECRET_ACCESS_KEY": credentials.secret_access_key,
        }
        if credentials.session_token:
            values[f"{prefix}_SESSION_TOKEN"] = credentials.session_token
        if config.public_url:
            values[f"{prefix}_PUBLIC_URL"] = config.public_url
        return values

    @staticmethod
    def conventional_environment(
        config: ObjectStorageConfig,
        credentials: ObjectStorageCredentials,
    ) -> dict[str, str]:
        values = {
            "AWS_ACCESS_KEY_ID": credentials.access_key_id,
            "AWS_SECRET_ACCESS_KEY": credentials.secret_access_key,
            "AWS_REGION": config.region,
            "AWS_DEFAULT_REGION": config.region,
            "AWS_ENDPOINT_URL": config.endpoint_url,
            "AWS_S3_BUCKET": config.bucket,
            "S3_BUCKET": config.bucket,
            "S3_ENDPOINT_URL": config.endpoint_url,
            "S3_FORCE_PATH_STYLE": str(config.path_style).lower(),
        }
        if credentials.session_token:
            values["AWS_SESSION_TOKEN"] = credentials.session_token
        if config.public_url:
            values["S3_PUBLIC_URL"] = config.public_url
        return values

    @staticmethod
    def namespace(name: str) -> str:
        namespace = _NAMESPACE_PATTERN.sub("_", str(name).upper()).strip("_")
        return namespace or "STORAGE"

    @staticmethod
    def access_key_hint(storage: Storage) -> str:
        value = str(storage.credentials.get("access_key_id") or "")
        if len(value) <= 4:
            return "••••"
        return f"{value[:4]}••••{value[-4:]}"

    async def validate_endpoint_network(self, config: ObjectStorageConfig) -> None:
        parsed = urlparse(config.endpoint_url)
        hostname = parsed.hostname or ""
        allow_insecure = (
            self.settings.env == "development"
            or self.settings.object_storage_allow_insecure_endpoints
        )
        allow_private = (
            self.settings.env == "development"
            or self.settings.object_storage_allow_private_endpoints
        )
        if self.settings.env != "development" and config.provider == "custom":
            suffixes = {
                suffix.strip().lower().lstrip(".")
                for suffix in self.settings.object_storage_allowed_endpoint_suffixes.split(",")
                if suffix.strip()
            }
            if not any(
                hostname.lower() == suffix
                or hostname.lower().endswith(f".{suffix}")
                for suffix in suffixes
            ):
                raise ObjectStorageConfigurationError(
                    "Custom endpoint hostname is not in the operator allowlist."
                )
        if not allow_insecure and parsed.scheme != "https":
            raise ObjectStorageConfigurationError(
                "Object storage endpoints must use HTTPS in production."
            )
        if hostname.casefold() in {
            "localhost",
            "host.docker.internal",
            "docker-proxy",
            "pgsql",
            "redis",
            "prometheus",
        }:
            if not allow_private:
                raise ObjectStorageConfigurationError(
                    "Object storage endpoint cannot target an internal service unless the operator enables private endpoints."
                )
        try:
            addresses = await asyncio.wait_for(
                asyncio.to_thread(self._resolve, hostname, parsed.port),
                timeout=5,
            )
        except (OSError, TimeoutError) as exc:
            raise ObjectStorageConnectionError(
                "Object storage endpoint could not be resolved."
            ) from exc
        if not allow_private and any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise ObjectStorageConfigurationError(
                "Object storage endpoint resolves to a private or reserved address; the operator must explicitly enable private endpoints."
            )

    @staticmethod
    def normalize_url(
        value: object,
        *,
        field: str,
        allow_insecure: bool,
        allow_path: bool = False,
    ) -> str:
        raw = str(value or "").strip()
        parsed = urlparse(raw)
        if parsed.scheme not in ({"https", "http"} if allow_insecure else {"https"}):
            requirement = "HTTP or HTTPS" if allow_insecure else "HTTPS"
            raise ObjectStorageConfigurationError(f"{field} must use {requirement}.")
        if not parsed.hostname or parsed.username or parsed.password:
            raise ObjectStorageConfigurationError(f"{field} is invalid.")
        if parsed.query or parsed.fragment:
            raise ObjectStorageConfigurationError(
                f"{field} cannot include query parameters or fragments."
            )
        path = parsed.path.rstrip("/")
        if path and not allow_path:
            raise ObjectStorageConfigurationError(
                f"{field} cannot include a path."
            )
        normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
        if len(normalized) > 2048:
            raise ObjectStorageConfigurationError(
                f"{field} must be 2048 characters or fewer."
            )
        return normalized

    @staticmethod
    def validate_bucket(bucket: str) -> None:
        if (
            not _BUCKET_PATTERN.fullmatch(bucket)
            or ".." in bucket
            or ".-" in bucket
            or "-." in bucket
        ):
            raise ObjectStorageConfigurationError(
                "Bucket name must be 3–63 lowercase letters, numbers, dots, or hyphens."
            )
        try:
            ipaddress.ip_address(bucket)
        except ValueError:
            return
        raise ObjectStorageConfigurationError(
            "Bucket name cannot be formatted as an IP address."
        )

    @staticmethod
    def validate_region(region: str) -> None:
        if not _REGION_PATTERN.fullmatch(region):
            raise ObjectStorageConfigurationError("Region is invalid.")

    @staticmethod
    def _resolve(hostname: str, port: int | None) -> set[str]:
        results = socket.getaddrinfo(
            hostname,
            port or 443,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
        return {str(result[4][0]) for result in results}

    @staticmethod
    def _verify_sync(
        config: ObjectStorageConfig,
        credentials: ObjectStorageCredentials,
    ) -> None:
        client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region,
            aws_access_key_id=credentials.access_key_id,
            aws_secret_access_key=credentials.secret_access_key,
            aws_session_token=credentials.session_token,
            config=BotoConfig(
                signature_version="s3v4",
                connect_timeout=5,
                read_timeout=10,
                retries={"max_attempts": 2, "mode": "standard"},
                s3={
                    "addressing_style": "path" if config.path_style else "auto"
                },
            ),
        )
        key = f".devpush/connection-tests/{uuid4().hex}.txt"
        payload = uuid4().hex.encode()
        created = False
        try:
            client.head_bucket(Bucket=config.bucket)
            client.put_object(
                Bucket=config.bucket,
                Key=key,
                Body=payload,
                ContentType="text/plain",
            )
            created = True
            response = client.get_object(Bucket=config.bucket, Key=key)
            body = response["Body"]
            try:
                received = body.read()
            finally:
                body.close()
            if received != payload:
                raise ObjectStorageConnectionError(
                    "Bucket read check returned unexpected data."
                )
            client.delete_object(Bucket=config.bucket, Key=key)
            created = False
        except ClientError as exc:
            error = exc.response.get("Error") or {}
            code = str(error.get("Code") or "S3Error")
            status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            detail = f" ({status})" if status and str(status) != code else ""
            raise ObjectStorageConnectionError(
                f"Bucket verification failed: {code}{detail}."
            ) from exc
        except BotoCoreError as exc:
            raise ObjectStorageConnectionError(
                f"Bucket verification failed: {exc.__class__.__name__}."
            ) from exc
        finally:
            if created:
                try:
                    client.delete_object(Bucket=config.bucket, Key=key)
                except Exception:
                    pass
