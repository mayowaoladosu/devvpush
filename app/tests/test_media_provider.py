import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from cryptography.fernet import Fernet

from config import Settings
from models import Storage
from services.media_provider import (
    MediaProviderConfigurationError,
    MediaProviderConnectionError,
    MediaProviderCredentials,
    MediaProviderService,
)


class MediaProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(env="development")
        self.service = MediaProviderService(self.settings)

    def test_builds_regional_and_development_configs(self):
        config = self.service.build_config(
            cloud_name="my-cloud",
            region="eu",
            folder="apps/production",
        )
        self.assertEqual("cloudinary", config.provider)
        self.assertEqual("https://api-eu.cloudinary.com", config.api_base_url)
        self.assertEqual("apps/production", config.folder)

        local = self.service.build_config(
            cloud_name="local-cloud",
            region="us",
            api_base_url="http://cloudinary-e2e:8080/",
            allow_custom_endpoint=True,
            allow_insecure=True,
        )
        self.assertEqual("http://cloudinary-e2e:8080", local.api_base_url)

    def test_rejects_invalid_config_and_credentials(self):
        for cloud_name in ("", "UPPER CASE", "bad.example.com"):
            with self.subTest(cloud_name=cloud_name):
                with self.assertRaises(MediaProviderConfigurationError):
                    self.service.build_config(cloud_name=cloud_name)

        for folder in ("../assets", "assets//images", "assets?private"):
            with self.subTest(folder=folder):
                with self.assertRaises(MediaProviderConfigurationError):
                    self.service.build_config(
                        cloud_name="valid-cloud",
                        folder=folder,
                    )

        with self.assertRaisesRegex(
            MediaProviderConfigurationError, "official region"
        ):
            self.service.build_config(
                cloud_name="valid-cloud",
                api_base_url="https://api.example.com",
            )
        with self.assertRaises(MediaProviderConfigurationError):
            self.service.build_credentials(
                api_key="key",
                api_secret="short",
            )

    def test_production_rejects_persisted_custom_endpoint(self):
        service = MediaProviderService(Settings(env="production"))
        config = service.build_config(
            cloud_name="valid-cloud",
            api_base_url="https://api.example.com",
            allow_custom_endpoint=True,
        )

        with self.assertRaisesRegex(
            MediaProviderConfigurationError, "official regional endpoint"
        ):
            service.validate_endpoint_policy(config)

    async def test_verify_uploads_reads_and_deletes_temporary_asset(self):
        public_id = f"devpush_connection_tests/{'a' * 32}"
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            self.assertTrue(request.headers["Authorization"].startswith("Basic "))
            if request.url.path.endswith("/image/upload"):
                return httpx.Response(200, json={"public_id": public_id})
            if "/resources/image/upload/" in request.url.path:
                return httpx.Response(200, json={"public_id": public_id})
            if request.url.path.endswith("/image/destroy"):
                return httpx.Response(200, json={"result": "ok"})
            return httpx.Response(404)

        service = MediaProviderService(
            self.settings,
            transport=httpx.MockTransport(handler),
        )
        config = service.build_config(
            cloud_name="valid-cloud",
            api_base_url="https://api.example.com",
            allow_custom_endpoint=True,
        )
        credentials = MediaProviderCredentials("api-key", "api-secret-value")

        with patch(
            "services.media_provider.uuid4",
            return_value=SimpleNamespace(hex="a" * 32),
        ):
            await service.verify(config, credentials)

        self.assertEqual(
            [
                ("POST", "/v1_1/valid-cloud/image/upload"),
                (
                    "GET",
                    f"/v1_1/valid-cloud/resources/image/upload/{public_id}",
                ),
                ("POST", "/v1_1/valid-cloud/image/destroy"),
            ],
            calls,
        )

    async def test_failed_read_still_removes_temporary_asset(self):
        public_id = f"devpush_connection_tests/{'b' * 32}"
        destroyed = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal destroyed
            if request.url.path.endswith("/image/upload"):
                return httpx.Response(200, json={"public_id": public_id})
            if request.url.path.endswith("/image/destroy"):
                destroyed = True
                return httpx.Response(200, json={"result": "ok"})
            return httpx.Response(503, json={"error": {"message": "unavailable"}})

        service = MediaProviderService(
            self.settings,
            transport=httpx.MockTransport(handler),
        )
        config = service.build_config(
            cloud_name="valid-cloud",
            api_base_url="https://api.example.com",
            allow_custom_endpoint=True,
        )
        credentials = MediaProviderCredentials("api-key", "api-secret-value")

        with patch(
            "services.media_provider.uuid4",
            return_value=SimpleNamespace(hex="b" * 32),
        ):
            with self.assertRaisesRegex(MediaProviderConnectionError, "HTTP 503"):
                await service.verify(config, credentials)

        self.assertTrue(destroyed)

    async def test_http_authentication_failure_reports_safe_status(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"error": {"message": "credential detail must stay hidden"}},
            )

        service = MediaProviderService(
            self.settings,
            transport=httpx.MockTransport(handler),
        )
        config = service.build_config(
            cloud_name="valid-cloud",
            api_base_url="https://api.example.com",
            allow_custom_endpoint=True,
        )

        with self.assertRaisesRegex(MediaProviderConnectionError, "HTTP 401") as ctx:
            await service.verify(
                config,
                MediaProviderCredentials("api-key", "api-secret-value"),
            )

        self.assertNotIn("credential detail", str(ctx.exception))

    async def test_verify_checks_policy_before_remote_calls(self):
        config = self.service.build_config(
            cloud_name="valid-cloud",
        )
        credentials = MediaProviderCredentials("api-key", "api-secret-value")
        self.service.validate_endpoint_policy = Mock()
        self.service._verify_connection = AsyncMock()

        await self.service.verify(config, credentials)

        self.service.validate_endpoint_policy.assert_called_once_with(config)
        self.service._verify_connection.assert_awaited_once_with(config, credentials)

    def test_credentials_are_encrypted_and_runtime_values_are_namespaced(self):
        storage = Storage(
            id="b" * 32,
            name="product-media",
            type="media",
            status="active",
            team_id="a" * 32,
            config={},
        )
        config = self.service.build_config(
            cloud_name="valid-cloud",
            folder="apps/production",
        )
        credentials = MediaProviderCredentials("api-key", "api-secret-value")

        with patch("models.get_fernet", return_value=Fernet(Fernet.generate_key())):
            self.service.configure(storage, config, credentials)
            encrypted = storage._credentials
            runtime = self.service.runtime_environment(storage)
            hint = self.service.api_key_hint(storage)

        self.assertNotIn("api-key", encrypted)
        self.assertNotIn("api-secret-value", encrypted)
        self.assertEqual(
            "valid-cloud",
            runtime["DEVPUSH_MEDIA_PRODUCT_MEDIA_CLOUD_NAME"],
        )
        self.assertEqual(
            "api-secret-value",
            runtime["DEVPUSH_MEDIA_PRODUCT_MEDIA_API_SECRET"],
        )
        self.assertEqual("api-••••-key", hint)

    def test_conventional_environment_supports_cloudinary_sdks(self):
        config = self.service.build_config(
            cloud_name="valid-cloud",
            region="ap",
            folder="apps/production",
        )
        credentials = MediaProviderCredentials("api:key", "secret/value")

        values = self.service.conventional_environment(config, credentials)

        self.assertEqual("valid-cloud", values["CLOUDINARY_CLOUD_NAME"])
        self.assertEqual("api:key", values["CLOUDINARY_API_KEY"])
        self.assertEqual("apps/production", values["CLOUDINARY_FOLDER"])
        self.assertEqual(
            "https://api-ap.cloudinary.com",
            values["CLOUDINARY_UPLOAD_PREFIX"],
        )
        self.assertEqual(
            "cloudinary://api%3Akey:secret%2Fvalue@valid-cloud",
            values["CLOUDINARY_URL"],
        )
        self.assertEqual("PRODUCT_MEDIA", self.service.namespace("product--media"))


if __name__ == "__main__":
    unittest.main()
