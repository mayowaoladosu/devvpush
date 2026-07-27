import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from config import Settings
from services.storage import (
    MAX_STORAGE_ATTACHMENTS,
    StorageConfigurationError,
    StorageMountUsage,
    StorageSafetyError,
    StorageService,
)
from services.storage_jobs import StorageJobs
from workers.tasks.storage import _ensure_database_path, _ensure_volume_path


class StorageServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(
            data_dir="/data",
            host_data_dir="C:/devpush/data",
            docker_host="http://docker-proxy:2375",
        )
        self.service = StorageService(self.settings)

    @staticmethod
    def storage(
        name="state",
        storage_type="volume",
        storage_id="b" * 32,
        config=None,
        credentials=None,
    ):
        return SimpleNamespace(
            id=storage_id,
            name=name,
            type=storage_type,
            team_id="a" * 32,
            config=config or {},
            credentials=credentials or {},
            status="active",
        )

    @staticmethod
    def association(storage, mount_path=None, environment_ids=None, association_id=None):
        return SimpleNamespace(
            id=association_id or "c" * 32,
            storage_id=storage.id,
            mount_path=mount_path,
            environment_ids=environment_ids or [],
        )

    def test_mount_path_validation_and_defaults(self):
        self.assertEqual(
            "/data/volume/state",
            self.service.default_mount_path("volume", "state"),
        )
        self.assertEqual(
            "/app/state",
            self.service.normalize_mount_path(" /app/state/ "),
        )
        for value in (
            "relative/path",
            "/",
            "/cache",
            "/proc/state",
            "/app",
            "/data",
            "/app//state",
            "/app/../state",
            "/app:state",
        ):
            with self.subTest(value=value):
                with self.assertRaises(StorageConfigurationError):
                    self.service.normalize_mount_path(value)

    def test_paths_and_environment_scopes_detect_conflicts(self):
        self.assertTrue(self.service.paths_overlap("/app/data", "/app/data/cache"))
        self.assertFalse(self.service.paths_overlap("/app/data", "/app/database"))
        self.assertTrue(self.service.environments_overlap([], ["prod"]))
        self.assertTrue(self.service.environments_overlap(["prod"], ["prod"]))
        self.assertFalse(self.service.environments_overlap(["prod"], ["staging"]))

    def test_local_path_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            settings = Settings(data_dir=root, host_data_dir=root)
            service = StorageService(settings)
            storage = self.storage()
            parent = Path(root) / "storage" / storage.team_id / storage.type
            parent.mkdir(parents=True)
            (parent / storage.name).symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(StorageConfigurationError, "escapes"):
                service.local_path(storage)

    async def test_validate_attachment_rejects_overlapping_path(self):
        storage = self.storage()
        other = self.storage(name="other", storage_id="d" * 32)
        association = self.association(
            other,
            mount_path="/app/data",
            environment_ids=["prod"],
        )
        result = SimpleNamespace(all=lambda: [(association, other)])
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[SimpleNamespace(), result])
        )

        with self.assertRaisesRegex(StorageConfigurationError, "conflicts"):
            await self.service.validate_attachment(
                db,
                project_id="project-id",
                storage=storage,
                mount_path="/app/data/files",
                environment_ids=["prod"],
            )

        db.execute.side_effect = [SimpleNamespace(), result]
        path = await self.service.validate_attachment(
            db,
            project_id="project-id",
            storage=storage,
            mount_path="/app/data/files",
            environment_ids=["staging"],
        )
        self.assertEqual("/app/data/files", path)

    async def test_validate_attachment_enforces_project_limit(self):
        attachments = []
        for index in range(MAX_STORAGE_ATTACHMENTS):
            storage = self.storage(
                name=f"storage-{index}",
                storage_id=f"{index:032x}",
            )
            attachments.append(
                (
                    self.association(storage, mount_path=f"/data/storage-{index}"),
                    storage,
                )
            )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(),
                    SimpleNamespace(all=lambda: attachments),
                ]
            )
        )

        with self.assertRaisesRegex(StorageConfigurationError, "at most"):
            await self.service.validate_attachment(
                db,
                project_id="project-id",
                storage=self.storage(name="overflow"),
                mount_path="/app/overflow",
                environment_ids=[],
            )

    async def test_validate_attachment_rejects_object_namespace_collision(self):
        storage = self.storage(name="assets-prod", storage_type="object")
        other = self.storage(
            name="assets.prod", storage_type="object", storage_id="d" * 32
        )
        association = self.association(other, environment_ids=["prod"])
        result = SimpleNamespace(all=lambda: [(association, other)])
        db = SimpleNamespace(
            execute=AsyncMock(side_effect=[SimpleNamespace(), result])
        )

        with self.assertRaisesRegex(StorageConfigurationError, "namespace"):
            await self.service.validate_attachment(
                db,
                project_id="project-id",
                storage=storage,
                mount_path=None,
                environment_ids=["prod"],
            )

    async def test_runtime_returns_only_matching_environment_mounts(self):
        volume = self.storage()
        database = self.storage(
            name="app-db", storage_type="database", storage_id="d" * 32
        )
        rows = [
            (
                self.association(
                    volume,
                    mount_path="/app/state",
                    environment_ids=["prod"],
                ),
                volume,
            ),
            (
                self.association(database, environment_ids=["staging"]),
                database,
            ),
        ]
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[SimpleNamespace(), SimpleNamespace(all=lambda: rows)]
            )
        )
        deployment = SimpleNamespace(project_id="project-id", environment_id="prod")

        runtime = await self.service.runtime(deployment, db, lock=True)

        self.assertEqual([volume.id], runtime.storage_ids)
        self.assertEqual(["C:/devpush/data/storage/" + "a" * 32 + "/volume/state:/app/state"], runtime.binds)
        self.assertEqual("/app/state", runtime.mounts[0].application_path)

    async def test_runtime_rejects_conflicting_legacy_rows(self):
        first = self.storage(name="first")
        second = self.storage(name="second", storage_id="d" * 32)
        rows = [
            (self.association(first, mount_path="/app/data"), first),
            (self.association(second, mount_path="/app/data/nested"), second),
        ]
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
        )

        with self.assertRaisesRegex(StorageConfigurationError, "conflicts"):
            await self.service.runtime(
                SimpleNamespace(project_id="project-id", environment_id="prod"),
                db,
            )

    async def test_runtime_injects_single_object_connection_and_aliases(self):
        storage = self.storage(
            name="assets",
            storage_type="object",
            config={
                "provider": "custom",
                "bucket": "app-assets",
                "region": "us-east-1",
                "endpoint_url": "http://minio-e2e:9000",
                "path_style": True,
                "public_url": None,
            },
            credentials={
                "access_key_id": "access-key",
                "secret_access_key": "secret-key-value",
            },
        )
        rows = [(self.association(storage, environment_ids=["prod"]), storage)]
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
        )

        runtime = await self.service.runtime(
            SimpleNamespace(project_id="project-id", environment_id="prod"),
            db,
        )

        self.assertEqual([], runtime.binds)
        self.assertEqual([storage.id], runtime.storage_ids)
        self.assertEqual("app-assets", runtime.environment["AWS_S3_BUCKET"])
        self.assertEqual(
            "secret-key-value",
            runtime.environment["DEVPUSH_OBJECT_ASSETS_SECRET_ACCESS_KEY"],
        )

    async def test_runtime_omits_conventional_aliases_for_multiple_objects(self):
        def object_storage(name, storage_id, bucket):
            return self.storage(
                name=name,
                storage_type="object",
                storage_id=storage_id,
                config={
                    "provider": "custom",
                    "bucket": bucket,
                    "region": "us-east-1",
                    "endpoint_url": "https://objects.example.com",
                    "path_style": False,
                    "public_url": None,
                },
                credentials={
                    "access_key_id": f"{name}-key",
                    "secret_access_key": f"{name}-secret-value",
                },
            )

        first = object_storage("assets", "d" * 32, "app-assets")
        second = object_storage("backups", "e" * 32, "app-backups")
        rows = [(self.association(first), first), (self.association(second), second)]
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
        )

        runtime = await self.service.runtime(
            SimpleNamespace(project_id="project-id", environment_id="prod"), db
        )

        self.assertNotIn("AWS_ACCESS_KEY_ID", runtime.environment)
        self.assertIn("DEVPUSH_OBJECT_ASSETS_BUCKET", runtime.environment)
        self.assertIn("DEVPUSH_OBJECT_BACKUPS_BUCKET", runtime.environment)

    async def test_runtime_rejects_matching_storage_that_is_not_ready(self):
        storage = self.storage()
        storage.status = "pending"
        rows = [(self.association(storage, mount_path="/app/data"), storage)]
        db = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows))
        )

        with self.assertRaisesRegex(StorageConfigurationError, "not ready"):
            await self.service.runtime(
                SimpleNamespace(project_id="project-id", environment_id="prod"),
                db,
            )

    def test_mount_usage_supports_labels_and_legacy_source_paths(self):
        storage = self.storage()
        containers = [
            {
                "Id": "1" * 64,
                "Names": ["/runner-current"],
                "State": "running",
                "Labels": {
                    "devpush.storage_ids": storage.id,
                    "devpush.deployment_id": "2" * 32,
                    "devpush.project_id": "3" * 32,
                },
                "Mounts": [],
            },
            {
                "Id": "4" * 64,
                "Names": ["/runner-legacy"],
                "State": "exited",
                "Labels": {"devpush.deployment_id": "5" * 32},
                "Mounts": [
                    {
                        "Source": "/run/desktop/mnt/host/c/devpush/data/storage/"
                        + "a" * 32
                        + "/volume/state"
                    }
                ],
            },
            {
                "Id": "6" * 64,
                "Names": ["/runner-unrelated"],
                "State": "running",
                "Labels": {},
                "Mounts": [],
            },
        ]

        usages = self.service.mount_usage_from_containers(storage, containers)

        self.assertEqual(2, len(usages))
        self.assertEqual({"running", "exited"}, {usage.state for usage in usages})
        self.assertIn("2 deployment container", self.service.usage_message(usages))

    async def test_destructive_transitions_are_locked_and_recoverable(self):
        storage = self.storage()
        storage.status = "active"
        storage.error = {"old": True}
        result = SimpleNamespace(scalar_one_or_none=lambda: storage)
        db = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            commit=AsyncMock(),
        )
        self.service.mounted_containers = AsyncMock(return_value=[])

        reset_storage = await self.service.begin_reset(db, storage.id)

        self.assertIs(storage, reset_storage)
        self.assertEqual("resetting", storage.status)
        self.assertIsNone(storage.error)
        db.commit.assert_awaited_once()

        storage.status = "deleted"
        db.commit.reset_mock()
        await self.service.restore_after_queue_failure(
            db,
            storage.id,
            status="active",
            stage="queue_reset",
            message="queue unavailable",
        )

        self.assertEqual("active", storage.status)
        self.assertEqual("queue_reset", storage.error["stage"])
        db.commit.assert_awaited_once()

        storage.status = "pending"
        db.commit.reset_mock()
        deleted_storage, previous_status = await self.service.begin_delete(
            db, storage.id
        )
        self.assertIs(storage, deleted_storage)
        self.assertEqual("pending", previous_status)
        self.assertEqual("deleted", storage.status)

        await self.service.restore_after_queue_failure(
            db,
            storage.id,
            status=previous_status,
            stage="queue_deprovision",
            message="queue unavailable",
        )
        self.assertEqual("pending", storage.status)

    async def test_destructive_transition_rejects_mounted_storage(self):
        storage = self.storage()
        storage.status = "active"
        result = SimpleNamespace(scalar_one_or_none=lambda: storage)
        db = SimpleNamespace(
            execute=AsyncMock(return_value=result),
            commit=AsyncMock(),
        )
        self.service.mounted_containers = AsyncMock(
            return_value=[
                StorageMountUsage(
                    container_id="container",
                    container_name="runner-deployment",
                    deployment_id="d" * 32,
                    project_id="p" * 32,
                    state="running",
                )
            ]
        )

        with self.assertRaisesRegex(StorageSafetyError, "still mounted"):
            await self.service.begin_delete(db, storage.id)

        self.assertEqual("active", storage.status)
        db.commit.assert_not_awaited()

    async def test_disconnect_attachment_uses_project_transaction(self):
        association = SimpleNamespace(id="c" * 32, project_id="project-id")
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(),
                    SimpleNamespace(scalar_one_or_none=lambda: association),
                ]
            ),
            delete=AsyncMock(),
            commit=AsyncMock(),
        )

        removed = await self.service.disconnect_attachment(
            db,
            project_id="project-id",
            association_id=association.id,
        )

        self.assertTrue(removed)
        db.delete.assert_awaited_once_with(association)
        db.commit.assert_awaited_once()


class StorageProvisioningTests(unittest.TestCase):
    @staticmethod
    def storage(name, storage_type):
        return SimpleNamespace(
            id="b" * 32,
            name=name,
            type=storage_type,
            team_id="a" * 32,
            config={},
        )

    def test_database_uses_wal_and_is_group_writable(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(
                data_dir=root,
                host_data_dir=root,
                service_uid=os.getuid(),
                service_gid=os.getgid(),
            )
            storage = self.storage("database", "database")

            _ensure_database_path(settings, storage)

            base = Path(root) / "storage" / storage.team_id / "database" / storage.name
            db_path = base / "db.sqlite"
            with closing(sqlite3.connect(db_path)) as connection:
                journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual("wal", journal_mode)
            self.assertEqual(0o2770, base.stat().st_mode & 0o7777)
            self.assertEqual(0o660, db_path.stat().st_mode & 0o777)

    def test_volume_root_inherits_storage_group(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(
                data_dir=root,
                host_data_dir=root,
                service_uid=os.getuid(),
                service_gid=os.getgid(),
            )
            storage = self.storage("volume", "volume")

            _ensure_volume_path(settings, storage)

            base = Path(root) / "storage" / storage.team_id / "volume" / storage.name
            self.assertEqual(0o2770, base.stat().st_mode & 0o7777)


class StorageJobsTests(unittest.IsolatedAsyncioTestCase):
    async def test_transition_jobs_are_deterministic_and_action_specific(self):
        queue = SimpleNamespace(enqueue_job=AsyncMock(return_value="job"))
        changed_at = datetime(2026, 7, 26, 12, 30, tzinfo=UTC)

        for status, function, action in (
            ("pending", "provision_storage", "provision"),
            ("resetting", "reset_storage", "reset"),
            ("deleted", "deprovision_storage", "deprovision"),
        ):
            with self.subTest(status=status):
                queue.enqueue_job.reset_mock()
                result = await StorageJobs.enqueue(
                    queue,
                    storage_id="a" * 32,
                    status=status,
                    updated_at=changed_at,
                )
                self.assertEqual("job", result)
                queue.enqueue_job.assert_awaited_once_with(
                    function,
                    "a" * 32,
                    _job_id=StorageJobs.job_id("a" * 32, action, changed_at),
                )


if __name__ == "__main__":
    unittest.main()
