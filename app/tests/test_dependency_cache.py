import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from services.dependency_cache import (
    DependencyCacheError,
    DependencyCacheService,
)


class DependencyCacheTests(unittest.TestCase):
    def settings(self, root: Path):
        return SimpleNamespace(
            data_dir=str(root),
            host_data_dir="C:\\devpush\\data",
        )

    def runners(self):
        return [
            {
                "slug": "node-24",
                "image": "ghcr.io/devpushhq/runner-node-24:1.0.1",
                "enabled": True,
            },
            {
                "slug": "custom",
                "image": "example/custom:latest",
                "enabled": True,
            },
            {
                "slug": "custom-cache",
                "image": "example/custom-cache:latest",
                "cache_directory": "/cache",
                "enabled": True,
            },
        ]

    def deployment(
        self,
        *,
        project_id="project-id",
        environment_id="prod",
        runner="node-24",
        generation=1,
        enabled=True,
        build_strategy="zero-config",
    ):
        return SimpleNamespace(
            project_id=project_id,
            environment_id=environment_id,
            config={
                "build_strategy": build_strategy,
                "runner": runner,
                "dependency_cache": enabled,
                "dependency_cache_generation": generation,
            },
        )

    def test_official_runner_cache_moves_from_cold_to_warm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = DependencyCacheService(self.settings(root), self.runners())
            deployment = self.deployment()

            cold = service.prepare(deployment)

            self.assertIsNotNone(cold)
            self.assertFalse(cold.warm)
            self.assertEqual("/cache", cold.target)
            self.assertEqual(
                "C:/devpush/data/cache/dependencies/project-id/"
                f"generation-1/prod/{cold.namespace}:/cache",
                cold.bind,
            )
            self.assertTrue(cold.runtime_path.is_dir())

            (cold.runtime_path / ".devpush-ready").touch()
            warm = service.prepare(deployment)

            self.assertTrue(warm.warm)
            self.assertEqual(cold.bind, warm.bind)

    def test_cache_is_isolated_by_project_environment_runner_and_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DependencyCacheService(
                self.settings(Path(directory)), self.runners()
            )
            mounts = [
                service.prepare(self.deployment()),
                service.prepare(self.deployment(project_id="other-project")),
                service.prepare(self.deployment(environment_id="preview")),
                service.prepare(self.deployment(runner="custom-cache")),
                service.prepare(self.deployment(generation=2)),
            ]

            paths = {mount.runtime_path for mount in mounts}
            self.assertEqual(5, len(paths))

    def test_cache_is_disabled_for_dockerfiles_disabled_projects_and_unaware_runners(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DependencyCacheService(
                self.settings(Path(directory)), self.runners()
            )

            self.assertIsNone(
                service.prepare(self.deployment(build_strategy="dockerfile"))
            )
            self.assertIsNone(service.prepare(self.deployment(enabled=False)))
            self.assertIsNone(service.prepare(self.deployment(runner="custom")))
            self.assertIsNotNone(
                service.prepare(self.deployment(runner="custom-cache"))
            )

    def test_generation_rotation_never_mutates_the_previous_path(self):
        config = {
            "dependency_cache": True,
            "dependency_cache_generation": 7,
            "runner": "node-24",
        }

        rotated = DependencyCacheService.rotate_config(config)

        self.assertEqual(7, config["dependency_cache_generation"])
        self.assertEqual(8, rotated["dependency_cache_generation"])
        self.assertTrue(rotated["dependency_cache"])

    def test_prune_preserves_current_and_mounted_generations(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DependencyCacheService(
                self.settings(Path(directory)), self.runners()
            )
            project_id = "project-id"
            for generation in (1, 2, 3, 4):
                service.prepare(
                    self.deployment(project_id=project_id, generation=generation)
                )

            removed = service.prune(project_id, {2, 4})

            self.assertEqual(
                {"generation-1", "generation-3"},
                {path.name for path in removed},
            )
            root = Path(directory) / "cache" / "dependencies" / project_id
            self.assertEqual(
                {"generation-2", "generation-4"},
                {path.name for path in root.iterdir()},
            )

    def test_project_deletion_removes_every_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DependencyCacheService(
                self.settings(Path(directory)), self.runners()
            )
            service.prepare(self.deployment(generation=1))
            service.prepare(self.deployment(generation=2))

            self.assertTrue(service.delete_project("project-id"))
            self.assertFalse(service.delete_project("project-id"))

    def test_identifiers_cannot_escape_the_cache_root(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DependencyCacheService(
                self.settings(Path(directory)), self.runners()
            )

            with self.assertRaisesRegex(DependencyCacheError, "Project identifier"):
                service.prepare(self.deployment(project_id="../escape"))
            with self.assertRaisesRegex(
                DependencyCacheError, "Environment identifier"
            ):
                service.prepare(self.deployment(environment_id="../escape"))


if __name__ == "__main__":
    unittest.main()
