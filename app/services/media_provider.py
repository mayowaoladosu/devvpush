"""Encrypted Cloudinary media provider connections."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote
from uuid import uuid4

import httpx

from config import Settings
from models import Storage


_API_BASE_URLS = {
    "us": "https://api.cloudinary.com",
    "eu": "https://api-eu.cloudinary.com",
    "ap": "https://api-ap.cloudinary.com",
}
_CLOUD_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_NAMESPACE_PATTERN = re.compile(r"[^A-Z0-9]+")
_TEST_IMAGE = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d4944415408d763f8cfc0f01f00050001ff89993d1d0000000049454e44"
    "ae426082"
)


class MediaProviderConfigurationError(ValueError):
    """A media provider configuration or credential is invalid."""


class MediaProviderConnectionError(RuntimeError):
    """A media provider connection could not pass verification."""


@dataclass(frozen=True)
class MediaProviderCredentials:
    api_key: str
    api_secret: str

    def as_dict(self) -> dict[str, str]:
        return {
            "api_key": self.api_key,
            "api_secret": self.api_secret,
        }


@dataclass(frozen=True)
class MediaProviderConfig:
    provider: str
    cloud_name: str
    region: str
    folder: str | None
    api_base_url: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "cloud_name": self.cloud_name,
            "region": self.region,
            "folder": self.folder,
            "api_base_url": self.api_base_url,
        }


class MediaProviderService:
    """Validate, verify, encrypt, and expose Cloudinary connections."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.settings = settings
        self.transport = transport

    @classmethod
    def build_config(
        cls,
        *,
        provider: str = "cloudinary",
        cloud_name: object,
        region: object = "us",
        folder: object = None,
        api_base_url: object = None,
        allow_custom_endpoint: bool = False,
        allow_insecure: bool = False,
    ) -> MediaProviderConfig:
        provider = str(provider or "").strip().lower()
        if provider != "cloudinary":
            raise MediaProviderConfigurationError("Unsupported media provider.")

        cloud_name = str(cloud_name or "").strip().lower()
        if not _CLOUD_NAME_PATTERN.fullmatch(cloud_name):
            raise MediaProviderConfigurationError(
                "Cloud name must contain 1–128 lowercase letters, numbers, hyphens, or underscores."
            )

        region = str(region or "us").strip().lower()
        if region not in _API_BASE_URLS:
            raise MediaProviderConfigurationError("Cloudinary region is invalid.")

        normalized_folder = cls.normalize_folder(folder)
        expected_api_base_url = _API_BASE_URLS[region]
        raw_api_base_url = str(api_base_url or expected_api_base_url).strip()
        parsed = httpx.URL(raw_api_base_url)
        allowed_schemes = {"https", "http"} if allow_insecure else {"https"}
        if (
            parsed.scheme not in allowed_schemes
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise MediaProviderConfigurationError(
                "Cloudinary API base URL is invalid."
            )
        normalized_api_base_url = str(parsed.copy_with(path="")).rstrip("/")
        if (
            normalized_api_base_url != expected_api_base_url
            and not allow_custom_endpoint
        ):
            raise MediaProviderConfigurationError(
                "Cloudinary API base URL must use the selected official region."
            )

        return MediaProviderConfig(
            provider=provider,
            cloud_name=cloud_name,
            region=region,
            folder=normalized_folder,
            api_base_url=normalized_api_base_url,
        )

    @staticmethod
    def build_credentials(
        *, api_key: object, api_secret: object
    ) -> MediaProviderCredentials:
        api_key = str(api_key or "").strip()
        api_secret = str(api_secret or "").strip()
        if not 3 <= len(api_key) <= 128:
            raise MediaProviderConfigurationError(
                "Cloudinary API key must contain between 3 and 128 characters."
            )
        if not 8 <= len(api_secret) <= 256:
            raise MediaProviderConfigurationError(
                "Cloudinary API secret must contain between 8 and 256 characters."
            )
        return MediaProviderCredentials(api_key=api_key, api_secret=api_secret)

    @classmethod
    def config_from_storage(cls, storage: Storage) -> MediaProviderConfig:
        config = storage.config or {}
        return cls.build_config(
            provider=config.get("provider"),
            cloud_name=config.get("cloud_name"),
            region=config.get("region"),
            folder=config.get("folder"),
            api_base_url=config.get("api_base_url"),
            allow_custom_endpoint=True,
            allow_insecure=True,
        )

    @classmethod
    def credentials_from_storage(
        cls, storage: Storage
    ) -> MediaProviderCredentials:
        credentials = storage.credentials
        return cls.build_credentials(
            api_key=credentials.get("api_key"),
            api_secret=credentials.get("api_secret"),
        )

    async def verify(
        self,
        config: MediaProviderConfig,
        credentials: MediaProviderCredentials,
    ) -> None:
        self.validate_endpoint_policy(config)
        try:
            await asyncio.wait_for(
                self._verify_connection(config, credentials),
                timeout=30,
            )
        except TimeoutError as exc:
            raise MediaProviderConnectionError(
                "Cloudinary verification timed out."
            ) from exc

    def validate_endpoint_policy(self, config: MediaProviderConfig) -> None:
        expected = _API_BASE_URLS[config.region]
        if self.settings.env != "development" and config.api_base_url != expected:
            raise MediaProviderConfigurationError(
                "Cloudinary must use the selected official regional endpoint in production."
            )
        if self.settings.env != "development" and not config.api_base_url.startswith(
            "https://"
        ):
            raise MediaProviderConfigurationError(
                "Cloudinary must use HTTPS in production."
            )

    def configure(
        self,
        storage: Storage,
        config: MediaProviderConfig,
        credentials: MediaProviderCredentials,
        *,
        verified_at: datetime | None = None,
    ) -> None:
        payload = config.as_dict()
        payload["verified_at"] = (verified_at or datetime.now(UTC)).isoformat()
        storage.config = payload
        storage.credentials = credentials.as_dict()

    @classmethod
    def runtime_environment(cls, storage: Storage) -> dict[str, str]:
        config = cls.config_from_storage(storage)
        credentials = cls.credentials_from_storage(storage)
        prefix = f"DEVPUSH_MEDIA_{cls.namespace(storage.name)}"
        values = {
            f"{prefix}_PROVIDER": config.provider,
            f"{prefix}_CLOUD_NAME": config.cloud_name,
            f"{prefix}_API_KEY": credentials.api_key,
            f"{prefix}_API_SECRET": credentials.api_secret,
            f"{prefix}_API_BASE_URL": config.api_base_url,
            f"{prefix}_CLOUDINARY_URL": cls.cloudinary_url(config, credentials),
        }
        if config.folder:
            values[f"{prefix}_FOLDER"] = config.folder
        return values

    @classmethod
    def conventional_environment(
        cls,
        config: MediaProviderConfig,
        credentials: MediaProviderCredentials,
    ) -> dict[str, str]:
        values = {
            "CLOUDINARY_URL": cls.cloudinary_url(config, credentials),
            "CLOUDINARY_CLOUD_NAME": config.cloud_name,
            "CLOUDINARY_API_KEY": credentials.api_key,
            "CLOUDINARY_API_SECRET": credentials.api_secret,
            "CLOUDINARY_API_BASE_URL": config.api_base_url,
            "CLOUDINARY_UPLOAD_PREFIX": config.api_base_url,
        }
        if config.folder:
            values["CLOUDINARY_FOLDER"] = config.folder
        return values

    @staticmethod
    def cloudinary_url(
        config: MediaProviderConfig,
        credentials: MediaProviderCredentials,
    ) -> str:
        return (
            f"cloudinary://{quote(credentials.api_key, safe='')}:"
            f"{quote(credentials.api_secret, safe='')}@{config.cloud_name}"
        )

    @staticmethod
    def namespace(name: str) -> str:
        namespace = _NAMESPACE_PATTERN.sub("_", str(name).upper()).strip("_")
        return namespace or "MEDIA"

    @staticmethod
    def api_key_hint(storage: Storage) -> str:
        value = str(storage.credentials.get("api_key") or "")
        if len(value) <= 4:
            return "••••"
        return f"{value[:4]}••••{value[-4:]}"

    @staticmethod
    def normalize_folder(value: object) -> str | None:
        folder = str(value or "").strip().strip("/")
        if not folder:
            return None
        if len(folder) > 255 or not _FOLDER_PATTERN.fullmatch(folder):
            raise MediaProviderConfigurationError(
                "Folder must contain at most 255 letters, numbers, dots, underscores, hyphens, or slashes."
            )
        if any(segment in {"", ".", ".."} for segment in folder.split("/")):
            raise MediaProviderConfigurationError(
                "Folder cannot contain empty, current-directory, or parent-directory segments."
            )
        return folder

    async def _verify_connection(
        self,
        config: MediaProviderConfig,
        credentials: MediaProviderCredentials,
    ) -> None:
        auth = httpx.BasicAuth(credentials.api_key, credentials.api_secret)
        public_id = f"devpush_connection_tests/{uuid4().hex}"
        created_public_id: str | None = None
        base = f"{config.api_base_url}/v1_1/{config.cloud_name}"
        timeout = httpx.Timeout(10, connect=5)
        async with httpx.AsyncClient(
            auth=auth,
            timeout=timeout,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            try:
                upload = await self._request_json(
                    client,
                    "POST",
                    f"{base}/image/upload",
                    data={"public_id": public_id, "overwrite": "false"},
                    files={"file": ("devpush.png", _TEST_IMAGE, "image/png")},
                )
                created_public_id = str(upload.get("public_id") or "")
                if created_public_id != public_id:
                    raise MediaProviderConnectionError(
                        "Cloudinary upload verification returned an unexpected asset."
                    )

                resource = await self._request_json(
                    client,
                    "GET",
                    f"{base}/resources/image/upload/{quote(public_id, safe='')}",
                )
                if str(resource.get("public_id") or "") != public_id:
                    raise MediaProviderConnectionError(
                        "Cloudinary read verification returned an unexpected asset."
                    )

                result = await self._destroy(client, base, public_id)
                if result != "ok":
                    raise MediaProviderConnectionError(
                        "Cloudinary delete verification did not remove the temporary asset."
                    )
                created_public_id = None
            finally:
                if created_public_id:
                    try:
                        await self._destroy(client, base, created_public_id)
                    except Exception:
                        pass

    @classmethod
    async def _destroy(
        cls,
        client: httpx.AsyncClient,
        base: str,
        public_id: str,
    ) -> str:
        payload = await cls._request_json(
            client,
            "POST",
            f"{base}/image/destroy",
            data={"public_id": public_id},
        )
        return str(payload.get("result") or "")

    @staticmethod
    async def _request_json(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs,
    ) -> dict[str, object]:
        try:
            response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise MediaProviderConnectionError(
                f"Cloudinary verification failed with HTTP {exc.response.status_code}."
            ) from exc
        except MediaProviderConnectionError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise MediaProviderConnectionError(
                "Cloudinary verification could not complete."
            ) from exc
        if not isinstance(payload, dict):
            raise MediaProviderConnectionError(
                "Cloudinary verification returned an invalid response."
            )
        return payload
