import unittest
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet

from config import Settings
from models import Storage
from services.object_storage import (
    ObjectStorageConfigurationError,
    ObjectStorageCredentials,
    ObjectStorageService,
)


class ObjectStorageServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(env="development")
        self.service = ObjectStorageService(self.settings)

    def test_builds_aws_r2_and_custom_configs(self):
        aws = self.service.build_config(
            provider="aws",
            bucket="app-assets",
            region="us-west-2",
        )
        self.assertEqual("https://s3.us-west-2.amazonaws.com", aws.endpoint_url)
        self.assertFalse(aws.path_style)

        r2 = self.service.build_config(
            provider="r2",
            bucket="app-assets",
            account_id="a" * 32,
        )
        self.assertEqual(
            f"https://{'a' * 32}.r2.cloudflarestorage.com",
            r2.endpoint_url,
        )
        self.assertEqual("auto", r2.region)

        custom = self.service.build_config(
            provider="custom",
            bucket="app-assets",
            region="us-east-1",
            endpoint_url="http://minio-e2e:9000/",
            path_style=True,
            allow_insecure=True,
        )
        self.assertEqual("http://minio-e2e:9000", custom.endpoint_url)
        self.assertTrue(custom.path_style)

    def test_rejects_invalid_bucket_urls_and_credentials(self):
        invalid_buckets = ("ab", "127.0.0.1", "bad..dots")
        for bucket in invalid_buckets:
            with self.subTest(bucket=bucket):
                with self.assertRaises(ObjectStorageConfigurationError):
                    self.service.build_config(
                        provider="aws", bucket=bucket, region="us-east-1"
                    )

        normalized = self.service.build_config(
            provider="aws", bucket="UPPERCASE", region="us-east-1"
        )
        self.assertEqual("uppercase", normalized.bucket)

        with self.assertRaises(ObjectStorageConfigurationError):
            self.service.build_config(
                provider="custom",
                bucket="valid-bucket",
                endpoint_url="http://objects.example.com",
            )
        with self.assertRaises(ObjectStorageConfigurationError):
            self.service.build_credentials(
                access_key_id="ok-key",
                secret_access_key="short",
            )

    async def test_production_endpoint_rejects_private_resolution(self):
        service = ObjectStorageService(Settings(env="production"))
        service._resolve = lambda hostname, port: {"127.0.0.1"}

        with self.assertRaisesRegex(ObjectStorageConfigurationError, "private"):
            await service.validate_endpoint_network(
                ObjectStorageService.build_config(
                    provider="aws", bucket="valid-bucket", region="us-east-1"
                )
            )

    async def test_operator_can_explicitly_allow_private_http_endpoint(self):
        service = ObjectStorageService(
            Settings(
                env="production",
                object_storage_allow_private_endpoints=True,
                object_storage_allow_insecure_endpoints=True,
                object_storage_allowed_endpoint_suffixes="minio.internal",
            )
        )
        service._resolve = lambda hostname, port: {"10.0.0.8"}

        config = service.build_config(
            provider="custom",
            bucket="valid-bucket",
            endpoint_url="http://minio.internal:9000",
            allow_insecure=True,
        )
        await service.validate_endpoint_network(config)

    async def test_production_custom_endpoint_requires_operator_allowlist(self):
        service = ObjectStorageService(Settings(env="production"))
        config = service.build_config(
            provider="custom",
            bucket="valid-bucket",
            endpoint_url="https://objects.example.com",
        )

        with self.assertRaisesRegex(ObjectStorageConfigurationError, "allowlist"):
            await service.validate_endpoint_network(config)

    async def test_verify_checks_network_before_signed_bucket_operations(self):
        config = self.service.build_config(
            provider="custom",
            bucket="app-assets",
            endpoint_url="http://minio-e2e:9000",
            path_style=True,
            allow_insecure=True,
        )
        credentials = ObjectStorageCredentials("access-key", "secret-key-value")
        self.service.validate_endpoint_network = AsyncMock(return_value=None)

        with patch.object(self.service, "_verify_sync") as verify_sync:
            await self.service.verify(config, credentials)

        self.service.validate_endpoint_network.assert_awaited_once_with(config)
        verify_sync.assert_called_once_with(config, credentials)

    def test_credentials_are_encrypted_and_runtime_values_are_namespaced(self):
        storage = Storage(
            id="b" * 32,
            name="assets-prod",
            type="object",
            status="active",
            team_id="a" * 32,
            config={},
        )
        config = self.service.build_config(
            provider="custom",
            bucket="app-assets",
            endpoint_url="http://minio-e2e:9000",
            path_style=True,
            allow_insecure=True,
        )
        credentials = ObjectStorageCredentials("access-key", "secret-key-value")

        with patch("models.get_fernet", return_value=Fernet(Fernet.generate_key())):
            self.service.configure(storage, config, credentials)
            encrypted = storage._credentials
            runtime = self.service.runtime_environment(storage)
            hint = self.service.access_key_hint(storage)

        self.assertNotIn("access-key", encrypted)
        self.assertNotIn("secret-key-value", encrypted)
        self.assertEqual(
            "app-assets", runtime["DEVPUSH_OBJECT_ASSETS_PROD_BUCKET"]
        )
        self.assertEqual(
            "secret-key-value",
            runtime["DEVPUSH_OBJECT_ASSETS_PROD_SECRET_ACCESS_KEY"],
        )
        self.assertEqual("acce••••-key", hint)

    def test_conventional_aliases_cover_standard_aws_sdks(self):
        config = self.service.build_config(
            provider="aws", bucket="app-assets", region="us-east-1"
        )
        credentials = ObjectStorageCredentials("access-key", "secret-key-value")

        values = self.service.conventional_environment(config, credentials)

        self.assertEqual("access-key", values["AWS_ACCESS_KEY_ID"])
        self.assertEqual("app-assets", values["AWS_S3_BUCKET"])
        self.assertEqual("us-east-1", values["AWS_REGION"])
        self.assertEqual(
            "ASSETS_PROD", self.service.namespace("assets--prod")
        )


if __name__ == "__main__":
    unittest.main()
