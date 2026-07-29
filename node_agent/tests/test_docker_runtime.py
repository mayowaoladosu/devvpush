import os
import tempfile
import unittest
from pathlib import Path

import httpx
from docker_runtime import DockerRuntime, NodeDockerError


class DockerRuntimeDataDirectoryTests(unittest.IsolatedAsyncioTestCase):
    def make_runtime(self, data_dir: Path) -> DockerRuntime:
        return DockerRuntime(
            socket_path="/unused/docker.sock",
            runtime_host="node.internal",
            runtime_scheme="http",
            port_min=30000,
            port_max=30010,
            data_dir=data_dir,
            host_data_dir=Path("/var/lib/devpush-node"),
            max_image_bytes=1024**2,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="OK")
            ),
        )

    async def test_verifies_and_prepares_writable_cache_root(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = self.make_runtime(Path(root))
            try:
                cache_root = runtime.verify_data_directory()
                self.assertEqual(
                    Path(root) / "cache" / "dependencies",
                    cache_root,
                )
                self.assertTrue(cache_root.is_dir())
            finally:
                await runtime.close()

    async def test_rejects_unwritable_data_directory(self):
        with tempfile.TemporaryDirectory() as root:
            data_dir = Path(root) / "data"
            data_dir.mkdir(mode=0o500)
            runtime = self.make_runtime(data_dir)
            try:
                with self.assertRaisesRegex(
                    NodeDockerError,
                    "data directory is not writable",
                ) as context:
                    runtime.verify_data_directory()
                self.assertEqual(503, context.exception.status_code)
            finally:
                os.chmod(data_dir, 0o700)
                await runtime.close()

    async def test_capacity_check_and_creation_are_one_locked_operation(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append((request.method, request.url.path))
            if request.url.path == "/containers/json":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "Labels": {
                                "devpush.deployment_id": "b" * 32,
                            }
                        }
                    ],
                )
            return httpx.Response(500, json={"message": "unexpected request"})

        with tempfile.TemporaryDirectory() as root:
            runtime = DockerRuntime(
                socket_path="/unused/docker.sock",
                runtime_host="node.internal",
                runtime_scheme="http",
                port_min=30000,
                port_max=30010,
                data_dir=Path(root),
                host_data_dir=Path("/var/lib/devpush-node"),
                max_image_bytes=1024**2,
                transport=httpx.MockTransport(handler),
            )
            try:
                with self.assertRaisesRegex(
                    NodeDockerError,
                    "capacity reached",
                ):
                    await runtime.create_runtime(
                        {
                            "deployment_id": "a" * 32,
                            "project_id": "c" * 32,
                            "environment_id": "prod",
                            "node_id": "d" * 32,
                            "image": "ghcr.io/devpushhq/runner-python-3.12:1.0.1",
                        },
                        max_deployments=1,
                    )
            finally:
                await runtime.close()

        self.assertEqual([("GET", "/containers/json")], requests)


if __name__ == "__main__":
    unittest.main()