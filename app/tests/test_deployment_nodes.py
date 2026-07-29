import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from cryptography.fernet import Fernet

from config import Settings
from models import DeploymentNode
from services.deployment_nodes import (
    DeploymentNodeCapacityError,
    DeploymentNodeConfigurationError,
    DeploymentNodeConnectionError,
    DeploymentNodeSafetyError,
    DeploymentNodeService,
)


class Result:
    def __init__(self, *, rows=None, scalars=None, scalar=None):
        self._rows = rows or []
        self._scalars = scalars or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalars(self):
        return self

    def scalar_one(self):
        return self._scalar

    def __iter__(self):
        return iter(self._scalars)


class DeploymentNodeServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = Settings(env="development")
        self.service = DeploymentNodeService(self.settings)

    def test_builds_and_rejects_node_configs(self):
        config = self.service.build_config(
            name="node-us-east",
            endpoint_url="http://node-agent:8787/",
            runtime_host="node-runtime.internal",
            region="us-east",
            max_deployments="12",
            allow_insecure=True,
        )
        self.assertEqual("http://node-agent:8787", config.endpoint_url)
        self.assertEqual(12, config.max_deployments)

        for values in (
            {"name": "bad name"},
            {"runtime_host": "http://node"},
            {"region": "Bad Region"},
            {"max_deployments": 0},
        ):
            with self.subTest(values=values):
                kwargs = {
                    "name": "node",
                    "endpoint_url": "https://node.example.com",
                    "runtime_host": "runtime.example.com",
                    **values,
                }
                with self.assertRaises(DeploymentNodeConfigurationError):
                    self.service.build_config(**kwargs)

        with self.assertRaises(DeploymentNodeConfigurationError):
            self.service.build_token("short")

    async def test_production_rejects_private_endpoint_by_default(self):
        service = DeploymentNodeService(Settings(env="production"))
        service._resolve = lambda hostname, port: {"10.0.0.8"}

        with self.assertRaisesRegex(DeploymentNodeConfigurationError, "private"):
            await service.validate_endpoint_network("https://node.example.com")

    async def test_verify_authenticates_and_checks_protocol_capacity(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.headers["Authorization"].startswith("Bearer "))
            return httpx.Response(
                200,
                json={
                    "protocol_version": 1,
                    "agent_id": "node-agent",
                    "runtime_host": "runtime.example.com",
                    "runtime_scheme": "http",
                    "port_min": 30000,
                    "port_max": 39999,
                    "max_deployments": 20,
                    "docker": "OK",
                    "cpus": 8,
                    "memory_bytes": 16 * 1024**3,
                },
            )

        service = DeploymentNodeService(
            self.settings,
            transport=httpx.MockTransport(handler),
        )
        service.validate_endpoint_network = AsyncMock()
        config = service.build_config(
            name="node",
            endpoint_url="https://node.example.com",
            runtime_host="runtime.example.com",
            max_deployments=10,
        )

        health = await service.verify(config, "x" * 32)

        self.assertEqual(1, health["protocol_version"])
        self.assertEqual(30000, health["port_min"])

    async def test_verify_rejects_agent_runtime_mismatch(self):
        service = DeploymentNodeService(
            self.settings,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "protocol_version": 1,
                        "runtime_host": "other.example.com",
                        "max_deployments": 20,
                        "docker": "OK",
                    },
                )
            ),
        )
        service.validate_endpoint_network = AsyncMock()
        config = service.build_config(
            name="node",
            endpoint_url="https://node.example.com",
            runtime_host="runtime.example.com",
        )

        with self.assertRaisesRegex(DeploymentNodeConnectionError, "runtime host"):
            await service.verify(config, "x" * 32)

    def test_configure_encrypts_token_and_validates_runtime_url(self):
        node = DeploymentNode(
            id="a" * 32,
            name="pending",
            endpoint_url="https://pending.example.com",
            runtime_host="pending.example.com",
            region="global",
            max_deployments=1,
            status="active",
            config={},
        )
        config = self.service.build_config(
            name="node",
            endpoint_url="https://node.example.com",
            runtime_host="runtime.example.com",
            max_deployments=10,
        )
        health = {
            "runtime_scheme": "http",
            "port_min": 30000,
            "port_max": 39999,
        }

        with patch("models.get_fernet", return_value=Fernet(Fernet.generate_key())):
            self.service.configure(node, config, "secret-token-" + "x" * 32, health)
            encrypted = node._token
            token = node.token

        self.assertNotIn(token, encrypted)
        self.assertEqual(
            "http://runtime.example.com:30001",
            self.service.validate_runtime_url(
                node, "http://runtime.example.com:30001"
            ),
        )
        with self.assertRaises(DeploymentNodeConfigurationError):
            self.service.validate_runtime_url(
                node, "http://internal-control-plane:30001"
            )

    async def test_scheduler_selects_least_loaded_healthy_node(self):
        first = SimpleNamespace(
            id="a" * 32,
            name="first",
            max_deployments=10,
        )
        second = SimpleNamespace(
            id="b" * 32,
            name="second",
            max_deployments=10,
        )
        project = SimpleNamespace(id="p" * 32, config={})
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    Result(rows=[]),
                    Result(rows=[first, second]),
                    Result(rows=[(first.id, 8), (second.id, 2)]),
                ]
            )
        )

        selected = await self.service.select_node(db, project, "prod")

        self.assertIs(second, selected)

    async def test_scheduler_falls_back_to_primary_when_nodes_are_full(self):
        node = SimpleNamespace(
            id="a" * 32,
            name="full",
            max_deployments=2,
        )
        project = SimpleNamespace(id="p" * 32, config={})
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    Result(rows=[]),
                    Result(rows=[node]),
                    Result(rows=[(node.id, 2)]),
                ]
            )
        )

        selected = await self.service.select_node(db, project, "prod")

        self.assertIsNone(selected)

    async def test_scheduler_rejects_full_explicit_node(self):
        node = SimpleNamespace(
            id="a" * 32,
            name="full",
            max_deployments=2,
        )
        project = SimpleNamespace(
            id="p" * 32,
            config={"deployment_node": node.id},
        )
        db = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    Result(rows=[]),
                    Result(rows=[node]),
                    Result(rows=[(node.id, 2)]),
                ]
            )
        )

        with self.assertRaisesRegex(
            DeploymentNodeCapacityError,
            "at capacity",
        ):
            await self.service.select_node(db, project, "prod")

    async def test_local_storage_pins_automatic_placement(self):
        association = SimpleNamespace(environment_ids=["prod"])
        storage = SimpleNamespace(type="volume")
        project = SimpleNamespace(id="p" * 32, config={})
        db = SimpleNamespace(
            execute=AsyncMock(return_value=Result(rows=[(association, storage)]))
        )

        selected = await self.service.select_node(db, project, "prod")

        self.assertIsNone(selected)

    async def test_local_storage_rejects_explicit_remote_placement(self):
        association = SimpleNamespace(environment_ids=["prod"])
        storage = SimpleNamespace(type="database")
        project = SimpleNamespace(
            id="p" * 32,
            config={"deployment_node": "a" * 32},
        )
        db = SimpleNamespace(
            execute=AsyncMock(return_value=Result(rows=[(association, storage)]))
        )

        with self.assertRaisesRegex(
            DeploymentNodeCapacityError,
            "must deploy on the primary node",
        ):
            await self.service.select_node(db, project, "prod")

    async def test_node_deletion_is_blocked_by_retained_containers(self):
        node = SimpleNamespace(id="a" * 32)
        db = SimpleNamespace(execute=AsyncMock(return_value=Result(scalar=2)))

        with self.assertRaisesRegex(
            DeploymentNodeSafetyError,
            "2 retained deployment container",
        ):
            await self.service.assert_deletable(db, node)

    async def test_targets_file_contains_only_runtime_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            settings = Settings(
                env="development",
                deployment_node_targets_file=str(Path(root) / "nodes.json"),
            )
            service = DeploymentNodeService(settings)
            node = SimpleNamespace(
                id="a" * 32,
                endpoint_url="https://node.example.com",
                token="x" * 32,
            )
            db = SimpleNamespace(
                execute=AsyncMock(return_value=Result(scalars=[node]))
            )

            await service.write_targets(db)

            payload = json.loads(Path(settings.deployment_node_targets_file).read_text())
            mode = Path(settings.deployment_node_targets_file).parent.stat().st_mode
            self.assertEqual(node.id, payload["targets"][0]["node_id"])
            self.assertEqual(node.token, payload["targets"][0]["token"])
            self.assertNotIn("runtime_host", payload["targets"][0])
            self.assertEqual(0o700, mode & 0o777)


if __name__ == "__main__":
    unittest.main()
