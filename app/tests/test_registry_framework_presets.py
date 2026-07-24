import json
import tempfile
import unittest
from pathlib import Path

from services.registry import RegistryService


class RegistryFrameworkPresetTests(unittest.TestCase):
    def write_registry(self, root: Path, overrides: dict):
        (root / "catalog.json").write_text(
            json.dumps(
                {
                    "meta": {"version": "test", "source": "bundled"},
                    "runners": [
                        {
                            "slug": "node-24",
                            "name": "Node.js 24",
                            "category": "Node.js",
                            "image": "example/node:24",
                        }
                    ],
                    "presets": [],
                }
            )
        )
        (root / "overrides.json").write_text(json.dumps(overrides))

    def test_custom_framework_preset_keeps_detection_and_command_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_registry(
                root,
                {
                    "runners": {"node-24": {"enabled": True}},
                    "presets": {
                        "nextjs": {
                            "enabled": True,
                            "name": "Next.js",
                            "category": "Node.js",
                            "description": "React applications built with Next.js.",
                            "tags": ["node", "server"],
                            "config": {
                                "runner": "node-24",
                                "build_script": "build",
                                "start_script": "start",
                                "output": "server",
                                "modern_runner": "node-24",
                                "bun_runner": "bun-1.3",
                                "detection": {
                                    "priority": 120,
                                    "any_dependencies": ["next"],
                                    "all_dependencies": ["react"],
                                },
                            },
                        }
                    },
                },
            )

            state = RegistryService(root).state
            framework = next(item for item in state.presets if item["slug"] == "nextjs")

            self.assertTrue(framework["enabled"])
            self.assertEqual(
                "React applications built with Next.js.", framework["description"]
            )
            self.assertEqual(["node", "server"], framework["tags"])
            self.assertEqual("build", framework["config"]["build_script"])
            self.assertEqual("start", framework["config"]["start_script"])
            self.assertEqual("server", framework["config"]["output"])
            self.assertEqual(
                ["next"], framework["config"]["detection"]["any_dependencies"]
            )
            self.assertEqual(
                ["react"], framework["config"]["detection"]["all_dependencies"]
            )
            self.assertEqual("", framework["config"]["build_command"])
            self.assertEqual("", framework["config"]["start_command"])

    def test_custom_framework_is_disabled_when_runner_is_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_registry(
                root,
                {
                    "runners": {"node-24": {"enabled": False}},
                    "presets": {
                        "nextjs": {
                            "enabled": True,
                            "name": "Next.js",
                            "category": "Node.js",
                            "config": {
                                "runner": "node-24",
                                "detection": {
                                    "priority": 120,
                                    "any_dependencies": ["next"],
                                },
                            },
                        }
                    },
                },
            )

            state = RegistryService(root).state
            framework = next(item for item in state.presets if item["slug"] == "nextjs")

            self.assertFalse(framework["enabled"])

    def test_custom_runner_keeps_dependency_cache_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.write_registry(
                root,
                {
                    "runners": {
                        "node-24": {
                            "enabled": True,
                            "cache_directory": "/cache",
                        }
                    },
                    "presets": {},
                },
            )

            state = RegistryService(root).state
            runner = next(item for item in state.runners if item["slug"] == "node-24")

            self.assertEqual("/cache", runner["cache_directory"])


if __name__ == "__main__":
    unittest.main()
