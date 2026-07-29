import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import aiodocker
import httpx

from config import Settings
from services.node_runtime import NodeDockerClient, NodeRuntimeError


class NodeRuntimeAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deployment = SimpleNamespace(
            id="a" * 32,
            project_id="b" * 32,
            environment_id="prod",
            branch="main",
        )
        self.node = SimpleNamespace(
            id="c" * 32,
            endpoint_url="https://agent.example.com",
            runtime_host="runtime.example.com",
            token="x" * 32,
            status="active",
            max_deployments=10,
            config={
                "runtime_scheme": "http",
                "port_min": 30000,
                "port_max": 39999,
            },
        )
        self.settings = Settings(env="development")
        self.requests = []

    def client(self, handler):
        return NodeDockerClient(
            self.deployment,
            self.node,
            self.settings,
            transport=httpx.MockTransport(handler),
        )

    async def test_create_translates_docker_config_to_constrained_runtime(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            self.assertTrue(request.headers["Authorization"].startswith("Bearer "))
            payload = json.loads(request.content)
            self.assertNotIn("traefik.enable", payload)
            self.assertEqual(self.deployment.id, payload["deployment_id"])
            self.assertEqual(10, payload["node_capacity"])
            self.assertEqual(
                "namespace123456789ab",
                payload["cache"]["namespace"],
            )
            self.assertEqual(["d" * 32], payload["storage_ids"])
            return httpx.Response(
                200,
                json={
                    "container_id": "e" * 64,
                    "host_port": 30123,
                    "runtime_url": "http://malicious.internal:1",
                    "cache_warm": True,
                },
            )

        client = self.client(handler)
        config = {
            "Image": "ghcr.io/devpushhq/runner-python-3.12:1.0.1",
            "Env": ["PORT=8000", "SECRET=value"],
            "Cmd": ["/bin/sh", "-c", "python app.py"],
            "WorkingDir": "/app",
            "Labels": {
                "traefik.enable": "true",
                "devpush.storage_ids": "d" * 32,
                "devpush.cache_generation": "2",
                "devpush.cache_namespace": "namespace123456789ab",
            },
            "HostConfig": {
                "Binds": ["C:/cache:/cache"],
                "CpuQuota": 50_000,
                "CpuPeriod": 100_000,
                "Memory": 256 * 1024 * 1024,
                "PidsLimit": 128,
            },
        }

        container = await client.containers.create_or_replace(
            name=f"runner-{self.deployment.id}", config=config
        )
        await client.close()

        self.assertEqual("e" * 64, container.id)
        self.assertEqual("http://runtime.example.com:30123", container.runtime_url)
        self.assertTrue(container.cache_warm)

    async def test_create_rejects_non_cache_host_bind(self):
        client = self.client(
            lambda request: httpx.Response(500, json={"detail": "unused"})
        )
        config = {
            "Image": "ghcr.io/devpushhq/runner-python-3.12:1.0.1",
            "Env": [],
            "Cmd": ["true"],
            "Labels": {},
            "HostConfig": {"Binds": ["/host/data:/app/data"]},
        }

        with self.assertRaisesRegex(NodeRuntimeError, "Local persistent storage"):
            await client.containers.create_or_replace(
                name=f"runner-{self.deployment.id}", config=config
            )
        await client.close()

    async def test_container_show_and_logs_match_worker_contract(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/logs"):
                return httpx.Response(
                    200,
                    json={"lines": ["2026-01-01T00:00:00Z ready"]},
                )
            return httpx.Response(
                200,
                json={
                    "container_id": "e" * 64,
                    "status": "running",
                    "exit_code": 0,
                    "started_at": "2026-01-01T00:00:00Z",
                    "host_port": 30123,
                },
            )

        client = self.client(handler)
        container = await client.containers.get("e" * 64)
        info = await container.show()
        lines = await container.log(tail=10, timestamps=True)
        await client.close()

        self.assertEqual("running", info["State"]["Status"])
        self.assertEqual(["2026-01-01T00:00:00Z ready"], lines)

    async def test_not_found_maps_to_docker_error(self):
        client = self.client(
            lambda request: httpx.Response(404, json={"detail": "not found"})
        )

        with self.assertRaises(aiodocker.DockerError) as ctx:
            await client.images.inspect("missing:image")
        await client.close()

        self.assertEqual(404, ctx.exception.status)

    async def test_image_archive_streams_with_bounded_metadata(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["length"] = request.headers.get("Content-Length")
            seen["body"] = request.content
            seen["query"] = dict(request.url.params)
            return httpx.Response(200, json={"status": "ready"})

        client = self.client(handler)
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / "image.tar"
            archive.write_bytes(b"bounded-image")
            await client.images.load_archive(
                archive,
                f"devpush/deployment-{self.deployment.id}:abcdef123456",
                self.deployment.id,
            )
        await client.close()

        self.assertEqual("13", seen["length"])
        self.assertEqual(b"bounded-image", seen["body"])
        self.assertEqual(self.deployment.id, seen["query"]["deployment_id"])


if __name__ == "__main__":
    unittest.main()
