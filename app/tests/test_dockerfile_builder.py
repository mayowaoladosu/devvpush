import asyncio
import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from services.dockerfile_builder import (
    DockerfileBuilder,
    DockerfileBuildError,
    DockerfileBuildSpec,
    is_managed_deployment_image,
    validate_dockerfile_runtime_image,
)


class RecordingBuilder(DockerfileBuilder):
    def __init__(self, work_root: Path):
        super().__init__(
            buildkit_host="unix:///run/buildkit/buildkitd.sock",
            docker_host="tcp://docker-proxy:2375",
            work_root=work_root,
        )
        self.context_directory: Path | None = None
        self.dockerfile_path: str | None = None
        self.loaded_image: str | None = None

    async def _download_source(self, spec, archive_path):
        archive_path.write_bytes(b"archive")

    def _extract_source(self, archive_path, destination):
        repository = destination / "repository"
        app = repository / "apps" / "web"
        app.mkdir(parents=True)
        (app / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
        return repository

    async def _run_buildctl(
        self,
        spec,
        context_directory,
        dockerfile_path,
        image_archive,
        metadata_path,
        on_log,
    ):
        self.context_directory = context_directory
        self.dockerfile_path = dockerfile_path
        image_archive.write_bytes(b"docker-image")
        metadata_path.write_text(
            '{"containerimage.digest":"sha256:abc123"}', encoding="utf-8"
        )
        await self._emit(on_log, "#1 build complete")

    async def _load_image(self, image_archive, image_reference, on_log):
        self.loaded_image = image_reference
        await self._emit(on_log, "Loaded image")


class CancelingBuilder(RecordingBuilder):
    def __init__(self, work_root: Path):
        super().__init__(work_root)
        self.started = asyncio.Event()

    async def _run_buildctl(
        self,
        spec,
        context_directory,
        dockerfile_path,
        image_archive,
        metadata_path,
        on_log,
    ):
        self.started.set()
        await asyncio.Future()


class DockerfileBuilderTests(unittest.IsolatedAsyncioTestCase):
    def spec(self, **overrides):
        values = {
            "deployment_id": "a" * 32,
            "project_id": "b" * 32,
            "repo_full_name": "owner/repository",
            "commit_sha": "c" * 40,
            "source_token": "installation-token",
            "root_directory": "apps/web",
            "dockerfile_path": "Dockerfile",
        }
        values.update(overrides)
        return DockerfileBuildSpec(**values)

    async def test_build_coordinates_context_build_and_image_load(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = RecordingBuilder(Path(directory))
            logs = []

            result = await builder.build(self.spec(), logs.append)

            self.assertEqual(
                "devpush/deployment-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:cccccccccccc",
                result.image_reference,
            )
            self.assertEqual("sha256:abc123", result.digest)
            self.assertEqual("Dockerfile", builder.dockerfile_path)
            self.assertEqual("web", builder.context_directory.name)
            self.assertEqual(result.image_reference, builder.loaded_image)
            self.assertEqual(
                [
                    "Downloading immutable source archive...",
                    "Building Dockerfile with isolated rootless BuildKit...",
                    "#1 build complete",
                    "Loading the built image into the runtime...",
                    "Loaded image",
                    f"Dockerfile image ready ({result.image_reference})",
                ],
                logs,
            )
            self.assertEqual([], list(Path(directory).iterdir()))

    async def test_root_directory_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            builder = RecordingBuilder(Path(directory))

            with self.assertRaisesRegex(
                DockerfileBuildError, "Root directory must stay inside"
            ):
                await builder.build(
                    self.spec(root_directory="../private"), lambda _line: None
                )

            self.assertIsNone(builder.loaded_image)

    async def test_cancellation_removes_build_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            builder = CancelingBuilder(root)
            task = asyncio.create_task(builder.build(self.spec(), lambda _line: None))
            await builder.started.wait()

            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual([], list(root.iterdir()))

    def test_archive_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "source.tar.gz"
            destination = Path(directory) / "source"
            with tarfile.open(archive_path, "w:gz") as archive:
                payload = b"escaped"
                member = tarfile.TarInfo("repository/../../escape.txt")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

            builder = DockerfileBuilder(
                buildkit_host="unix:///run/buildkit/buildkitd.sock",
                docker_host="tcp://docker-proxy:2375",
                work_root=Path(directory),
            )

            with self.assertRaisesRegex(DockerfileBuildError, "unsafe path"):
                builder._extract_source(archive_path, destination)

            self.assertFalse((Path(directory) / "escape.txt").exists())

    def test_safe_archive_extracts_repository_root(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "source.tar.gz"
            destination = Path(directory) / "source"
            with tarfile.open(archive_path, "w:gz") as archive:
                payload = b"FROM scratch\n"
                member = tarfile.TarInfo("repository/Dockerfile")
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

            builder = DockerfileBuilder(
                buildkit_host="unix:///run/buildkit/buildkitd.sock",
                docker_host="tcp://docker-proxy:2375",
                work_root=Path(directory),
            )

            repository = builder._extract_source(archive_path, destination)

            self.assertEqual("repository", repository.name)
            self.assertEqual(
                "FROM scratch\n",
                (repository / "Dockerfile").read_text(encoding="utf-8"),
            )

    def test_archive_member_limit_is_enforced_while_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "source.tar.gz"
            destination = Path(directory) / "source"
            with tarfile.open(archive_path, "w:gz") as archive:
                for name in ("repository/one.txt", "repository/two.txt"):
                    payload = name.encode()
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    archive.addfile(member, io.BytesIO(payload))

            builder = DockerfileBuilder(
                buildkit_host="unix:///run/buildkit/buildkitd.sock",
                docker_host="tcp://docker-proxy:2375",
                work_root=Path(directory),
                max_context_files=1,
            )

            with self.assertRaisesRegex(DockerfileBuildError, "too many files"):
                builder._extract_source(archive_path, destination)

    def test_buildctl_command_uses_project_cache_namespace(self):
        builder = DockerfileBuilder(
            buildkit_host="unix:///run/buildkit/buildkitd.sock",
            docker_host="tcp://docker-proxy:2375",
        )
        spec = self.spec()

        command = builder._buildctl_command(
            spec,
            Path("/tmp/context"),
            "deploy/Dockerfile",
            Path("/tmp/image.tar"),
            Path("/tmp/metadata.json"),
        )

        self.assertEqual("prlimit", command[0])
        self.assertEqual(f"--fsize={builder.max_image_bytes}", command[1])
        self.assertEqual("buildctl", command[3])
        self.assertIn("filename=deploy/Dockerfile", command)
        self.assertIn(
            "build-arg:BUILDKIT_CACHE_MOUNT_NS=devpush-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            command,
        )
        self.assertIn(
            "build-arg:HTTPS_PROXY=http://buildkit-egress:3128",
            command,
        )
        self.assertIn(
            "type=docker,name=devpush/deployment-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:cccccccccccc,dest=/tmp/image.tar",
            command,
        )
        self.assertNotIn(spec.source_token, " ".join(command))
        self.assertNotIn(spec.source_token, repr(spec))

    def test_only_deployment_images_are_managed(self):
        self.assertTrue(
            is_managed_deployment_image("devpush/deployment-abc123:def4567")
        )
        self.assertFalse(
            is_managed_deployment_image("ghcr.io/devpushhq/runner-node-24:1.0.0")
        )

    def test_runtime_image_requires_command_and_non_root_user(self):
        validate_dockerfile_runtime_image(
            {"Config": {"User": "10001:10001", "Cmd": ["./server"]}}
        )
        validate_dockerfile_runtime_image(
            {"Config": {"User": "app", "Entrypoint": ["./server"]}}
        )

        for config in (
            {"User": "", "Cmd": ["./server"]},
            {"User": "root", "Cmd": ["./server"]},
            {"User": "0:0", "Cmd": ["./server"]},
            {"User": "10001:10001"},
        ):
            with self.subTest(config=config), self.assertRaises(DockerfileBuildError):
                validate_dockerfile_runtime_image({"Config": config})


if __name__ == "__main__":
    unittest.main()
