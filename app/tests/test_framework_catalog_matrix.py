import json
import unittest
from pathlib import Path

from services.preset_detector import PresetDetector
from tests.test_preset_detector import FakeGitHubService

CATALOG_PATH = Path(__file__).parents[1] / "services" / "framework_catalog.json"

NODE_CASES = {
    "nextjs": "next",
    "nuxt": "nuxt",
    "sveltekit": "@sveltejs/kit",
    "remix": "@remix-run/node",
    "react-router": "@react-router/node",
    "astro": "astro",
    "solidstart": "@solidjs/start",
    "docusaurus": "@docusaurus/core",
    "gatsby": "gatsby",
    "qwik": "@builder.io/qwik",
    "angular": "@angular/core",
    "vitepress": "vitepress",
    "eleventy": "@11ty/eleventy",
    "nestjs": "@nestjs/core",
    "cra": "react-scripts",
    "vue-cli": "@vue/cli-service",
    "storybook": "storybook",
    "ember": "ember-cli",
    "hexo": "hexo",
    "adonisjs": "@adonisjs/core",
    "strapi": "@strapi/strapi",
    "payload": "payload",
    "keystone": "@keystone-6/core",
    "hono": "@hono/node-server",
    "fastify": "fastify",
    "express": "express",
    "vite": "vite",
}


class FrameworkCatalogMatrixTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        data = json.loads(CATALOG_PATH.read_text())
        cls.presets = [{**item, "enabled": True} for item in data["presets"]]

    async def test_every_node_signature_detects_its_framework(self):
        for expected, dependency in NODE_CASES.items():
            with self.subTest(framework=expected):
                files = {
                    "package.json": json.dumps(
                        {
                            "scripts": {
                                "build": "framework build",
                                "build-storybook": "storybook build",
                                "start": "framework start",
                                "start:prod": "node dist/main.js",
                            },
                            "dependencies": {dependency: "1.0.0"},
                        }
                    )
                }
                if expected == "angular":
                    files["angular.json"] = json.dumps(
                        {"projects": {"application": {}}}
                    )
                if expected == "vite":
                    files["vite.config.ts"] = "export default {}"

                result = await PresetDetector(self.presets).detect_with_commands(
                    FakeGitHubService(files), "token", 1, "main"
                )

                self.assertEqual(expected, result["preset"])
                self.assertTrue(result["runner"])
                self.assertTrue(result["build_command"])
                self.assertTrue(result["start_command"])

    async def test_every_python_signature_detects_its_framework(self):
        cases = {
            "streamlit": ("streamlit", "streamlit_app.py"),
            "starlette": ("starlette", "main.py"),
            "gradio": ("gradio", "app.py"),
        }
        for expected, (dependency, entry) in cases.items():
            with self.subTest(framework=expected):
                result = await PresetDetector(self.presets).detect_with_commands(
                    FakeGitHubService(
                        {
                            "requirements.txt": f"{dependency}>=1\n",
                            entry: "app = object()",
                        }
                    ),
                    "token",
                    1,
                    "main",
                )

                self.assertEqual(expected, result["preset"])
                self.assertEqual("python-3.12", result["runner"])
                self.assertTrue(result["build_command"])
                self.assertTrue(result["start_command"])

    async def test_static_fallback_is_deployable(self):
        result = await PresetDetector(self.presets).detect_with_commands(
            FakeGitHubService({"index.html": "<!doctype html>"}),
            "token",
            1,
            "main",
        )

        self.assertEqual("static", result["preset"])
        self.assertEqual("static", result["output"])
        self.assertEqual("node-24", result["runner"])
        self.assertEqual("serve --single . --listen 8000", result["start_command"])

    def test_catalog_has_unique_complete_framework_definitions(self):
        slugs = [preset["slug"] for preset in self.presets]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertGreaterEqual(len(slugs), 30)
        for framework in self.presets:
            with self.subTest(framework=framework["slug"]):
                self.assertTrue(framework.get("name"))
                self.assertTrue(framework.get("description"))
                self.assertTrue(framework.get("category"))
                self.assertTrue(framework.get("config", {}).get("runner"))
                self.assertTrue(
                    framework.get("config", {}).get("detection", {}).get("priority")
                )


if __name__ == "__main__":
    unittest.main()
