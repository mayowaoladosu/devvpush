import json
import unittest

from services.preset_detector import PresetDetector


class FakeGitHubService:
    def __init__(self, files: dict[str, str]):
        self.files = files
        self.batch_requests: list[list[str]] = []

    async def get_git_tree(self, _token, _repo_id, sha="HEAD", recursive=True):
        return {
            "sha": sha,
            "truncated": False,
            "tree": [
                {"path": path, "type": "blob", "sha": f"sha-{index}"}
                for index, path in enumerate(self.files)
            ],
        }

    async def get_file_contents(
        self, _token, _repo_id, paths, ref="HEAD", max_bytes=262_144
    ):
        requested = list(paths)
        self.batch_requests.append(requested)
        return {
            path: self.files[path]
            for path in requested
            if path in self.files and len(self.files[path].encode()) <= max_bytes
        }


def preset(
    slug: str,
    name: str,
    *,
    category: str,
    runner: str,
    priority: int,
    any_dependencies: list[str] | None = None,
    all_dependencies: list[str] | None = None,
    any_files: list[str] | None = None,
    all_files: list[str] | None = None,
    none_files: list[str] | None = None,
    package_check: str | None = None,
    build_script: str | None = None,
    start_script: str | None = None,
    output: str = "server",
    output_directory: str | None = None,
    build_command: str = "",
    pre_deploy_command: str = "",
    start_command: str = "",
):
    return {
        "slug": slug,
        "name": name,
        "category": category,
        "enabled": True,
        "config": {
            "runner": runner,
            "build_command": build_command,
            "pre_deploy_command": pre_deploy_command,
            "start_command": start_command,
            "build_script": build_script,
            "start_script": start_script,
            "output": output,
            "output_directory": output_directory,
            "logo": "",
            "detection": {
                "priority": priority,
                "any_dependencies": any_dependencies or [],
                "all_dependencies": all_dependencies or [],
                "any_files": any_files or [],
                "all_files": all_files or [],
                "any_paths": [],
                "none_files": none_files or [],
                "package_check": package_check,
                "variants": [],
            },
        },
    }


PRESETS = [
    preset(
        "nextjs",
        "Next.js",
        category="Node.js",
        runner="node-24",
        priority=120,
        any_dependencies=["next"],
        build_script="build",
        start_script="start",
    ),
    preset(
        "sveltekit",
        "SvelteKit",
        category="Node.js",
        runner="node-24",
        priority=120,
        any_dependencies=["@sveltejs/kit"],
        build_script="build",
    ),
    preset(
        "astro",
        "Astro",
        category="Node.js",
        runner="node-24",
        priority=110,
        any_dependencies=["astro"],
        build_script="build",
        output="static",
        output_directory="dist",
    ),
    preset(
        "angular",
        "Angular",
        category="Node.js",
        runner="node-24",
        priority=105,
        any_dependencies=["@angular/core"],
        all_files=["angular.json"],
        build_script="build",
        output="static",
        output_directory="dist",
    ),
    preset(
        "nestjs",
        "NestJS",
        category="Node.js",
        runner="node-24",
        priority=100,
        any_dependencies=["@nestjs/core"],
        build_script="build",
        start_script="start:prod",
    ),
    preset(
        "vite",
        "Vite",
        category="Node.js",
        runner="node-24",
        priority=80,
        any_dependencies=["vite"],
        any_files=["vite.config.*"],
        build_script="build",
        output="static",
        output_directory="dist",
    ),
    preset(
        "express",
        "Express",
        category="Node.js",
        runner="node-24",
        priority=70,
        any_dependencies=["express"],
        start_script="start",
    ),
    preset(
        "nodejs",
        "Node.js",
        category="Node.js",
        runner="node-20",
        priority=40,
        any_files=["package.json"],
        none_files=["bun.lock", "bun.lockb"],
    ),
    preset(
        "django",
        "Django",
        category="Python",
        runner="python-3.12",
        priority=110,
        any_files=["manage.py"],
        package_check="django",
        pre_deploy_command="python manage.py migrate",
    ),
    preset(
        "fastapi",
        "FastAPI",
        category="Python",
        runner="python-3.12",
        priority=100,
        package_check="fastapi",
    ),
    preset(
        "flask",
        "Flask",
        category="Python",
        runner="python-3.12",
        priority=95,
        package_check="flask",
    ),
    preset(
        "python",
        "Python",
        category="Python",
        runner="python-3.12",
        priority=30,
        any_files=["requirements.txt", "pyproject.toml"],
    ),
    preset(
        "laravel",
        "Laravel",
        category="PHP",
        runner="frankenphp-8.3",
        priority=110,
        all_files=["artisan", "composer.json"],
        build_command=(
            "composer install --no-dev --optimize-autoloader "
            "--no-interaction --no-progress"
        ),
        pre_deploy_command="php artisan migrate --force",
        start_command=(
            "frankenphp run --config /etc/caddy/Caddyfile --adapter caddyfile"
        ),
    ),
    preset(
        "go",
        "Go",
        category="Go",
        runner="go-1.25",
        priority=100,
        any_files=["go.mod"],
    ),
    preset(
        "static",
        "Static site",
        category="Node.js",
        runner="node-24",
        priority=10,
        any_files=["index.html"],
        output="static",
        output_directory=".",
    ),
]


class PresetDetectorTests(unittest.IsolatedAsyncioTestCase):
    async def detect(self, files: dict[str, str], presets=None):
        github = FakeGitHubService(files)
        result = await PresetDetector(presets or PRESETS).detect_with_commands(
            github, "token", 42, "main"
        )
        return result, github

    async def test_nextjs_beats_vite_and_uses_declared_pnpm(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "name": "web",
                        "packageManager": "pnpm@10.0.0",
                        "scripts": {"build": "next build", "start": "next start"},
                        "dependencies": {"next": "16.0.0"},
                        "devDependencies": {"vite": "7.0.0"},
                    }
                ),
                "pnpm-lock.yaml": "lockfileVersion: '9.0'",
                "vite.config.ts": "export default {}",
            }
        )

        self.assertEqual("nextjs", result["preset"])
        self.assertEqual("pnpm", result["package_manager"])
        self.assertEqual("node-24", result["runner"])
        self.assertEqual(
            "pnpm install --frozen-lockfile && pnpm run build",
            result["build_command"],
        )
        self.assertEqual(
            "HOST=0.0.0.0 HOSTNAME=0.0.0.0 PORT=8000 pnpm run start",
            result["start_command"],
        )
        self.assertEqual("high", result["confidence"])

    async def test_nested_workspace_detects_root_and_shared_lockfile(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {"name": "root", "private": True, "workspaces": ["apps/*"]}
                ),
                "pnpm-lock.yaml": "lockfileVersion: '9.0'",
                "apps/web/package.json": json.dumps(
                    {
                        "name": "web",
                        "scripts": {"build": "next build", "start": "next start"},
                        "dependencies": {"next": "16.0.0"},
                    }
                ),
                "apps/web/next.config.ts": "export default {}",
                "packages/ui/package.json": json.dumps(
                    {"name": "@acme/ui", "main": "index.js"}
                ),
            }
        )

        self.assertEqual("nextjs", result["preset"])
        self.assertEqual("apps/web", result["root_directory"])
        self.assertEqual("pnpm", result["package_manager"])
        self.assertEqual("pnpm-lock.yaml", result["lockfile"])
        self.assertNotIn(
            "packages/ui",
            [item["root_directory"] for item in result["alternatives"]],
        )

    async def test_workspace_inherits_modern_yarn_from_root_manifest(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "private": True,
                        "packageManager": "yarn@4.9.2",
                        "workspaces": ["apps/*"],
                    }
                ),
                "yarn.lock": "",
                "apps/web/package.json": json.dumps(
                    {
                        "scripts": {"build": "next build", "start": "next start"},
                        "dependencies": {"next": "16"},
                    }
                ),
            }
        )

        self.assertEqual("nextjs", result["preset"])
        self.assertEqual("yarn", result["package_manager"])
        self.assertEqual(
            "yarn install --immutable && yarn build", result["build_command"]
        )

    async def test_workspace_shell_cannot_absorb_nested_django_files(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {"private": True, "workspaces": ["frontend", "backend"]}
                ),
                "frontend/package.json": json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "devDependencies": {"vite": "7"},
                    }
                ),
                "frontend/vite.config.ts": "export default {}",
                "backend/requirements.txt": "django>=5\n",
                "backend/manage.py": "",
                "backend/project/settings.py": "",
                "backend/project/wsgi.py": "",
            }
        )

        self.assertEqual("django", result["preset"])
        self.assertEqual("backend", result["root_directory"])
        roots = [result["root_directory"]] + [
            item["root_directory"] for item in result["alternatives"]
        ]
        self.assertNotIn("", roots)

    async def test_multiple_apps_returns_deterministic_alternatives(self):
        result, _ = await self.detect(
            {
                "apps/web/package.json": json.dumps(
                    {
                        "scripts": {"build": "next build", "start": "next start"},
                        "dependencies": {"next": "16"},
                    }
                ),
                "apps/api/package.json": json.dumps(
                    {
                        "scripts": {"start": "node server.js"},
                        "dependencies": {"express": "5"},
                    }
                ),
                "apps/api/server.js": "",
            }
        )

        self.assertEqual("nextjs", result["preset"])
        self.assertEqual("apps/web", result["root_directory"])
        self.assertEqual(1, len(result["alternatives"]))
        self.assertEqual("express", result["alternatives"][0]["preset"])
        self.assertEqual("apps/api", result["alternatives"][0]["root_directory"])

    async def test_sveltekit_static_adapter_gets_static_server(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "devDependencies": {
                            "@sveltejs/kit": "2",
                            "@sveltejs/adapter-static": "3",
                            "vite": "7",
                        },
                    }
                ),
                "package-lock.json": "{}",
            }
        )

        self.assertEqual("sveltekit", result["preset"])
        self.assertEqual("static", result["output"])
        self.assertEqual("build", result["output_directory"])
        self.assertIn("npm install --global serve@14.2.4", result["build_command"])
        self.assertEqual("serve --single build --listen 8000", result["start_command"])

    async def test_sveltekit_node_adapter_gets_node_entrypoint(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "scripts": {"build": "vite build"},
                        "devDependencies": {
                            "@sveltejs/kit": "2",
                            "@sveltejs/adapter-node": "5",
                        },
                    }
                )
            }
        )

        self.assertEqual("server", result["output"])
        self.assertEqual(
            "HOST=0.0.0.0 HOSTNAME=0.0.0.0 PORT=8000 node build",
            result["start_command"],
        )

    async def test_astro_node_adapter_changes_static_default(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "scripts": {"build": "astro build"},
                        "dependencies": {"astro": "5", "@astrojs/node": "9"},
                    }
                )
            }
        )

        self.assertEqual("astro", result["preset"])
        self.assertEqual("server", result["output"])
        self.assertIsNone(result["output_directory"])
        self.assertTrue(result["start_command"].endswith("node dist/server/entry.mjs"))

    async def test_angular_reads_default_project_output(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "scripts": {"build": "ng build"},
                        "dependencies": {"@angular/core": "20"},
                    }
                ),
                "angular.json": json.dumps(
                    {"defaultProject": "console", "projects": {"console": {}}}
                ),
            }
        )

        self.assertEqual("angular", result["preset"])
        self.assertEqual("dist/console/browser", result["output_directory"])

    async def test_nestjs_beats_express_and_uses_start_prod(self):
        result, _ = await self.detect(
            {
                "package.json": json.dumps(
                    {
                        "scripts": {
                            "build": "nest build",
                            "start:prod": "node dist/main.js",
                        },
                        "dependencies": {"@nestjs/core": "11", "express": "5"},
                    }
                )
            }
        )

        self.assertEqual("nestjs", result["preset"])
        self.assertTrue(result["start_command"].endswith("npm run start:prod"))

    async def test_django_discovers_wsgi_module_and_nested_root(self):
        result, _ = await self.detect(
            {
                "backend/requirements.txt": "Django>=5.2\ngunicorn\n",
                "backend/manage.py": "",
                "backend/mysite/settings.py": "",
                "backend/mysite/wsgi.py": "",
            }
        )

        self.assertEqual("django", result["preset"])
        self.assertEqual("backend", result["root_directory"])
        self.assertEqual(
            "gunicorn mysite.wsgi:application --bind 0.0.0.0:8000",
            result["start_command"],
        )
        self.assertIn("pip install -r requirements.txt", result["build_command"])

    async def test_django_without_wsgi_warns_about_fallback_module(self):
        result, _ = await self.detect(
            {"requirements.txt": "django>=5\n", "manage.py": ""}
        )

        self.assertEqual("django", result["preset"])
        self.assertEqual(
            "gunicorn config.wsgi:application --bind 0.0.0.0:8000",
            result["start_command"],
        )
        self.assertTrue(any("wsgi.py" in warning for warning in result["warnings"]))

    async def test_fastapi_discovers_nested_python_module(self):
        result, _ = await self.detect(
            {
                "requirements.txt": "fastapi==0.116\n",
                "app/main.py": "from fastapi import FastAPI\napp = FastAPI()",
            }
        )

        self.assertEqual("fastapi", result["preset"])
        self.assertEqual(
            "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000",
            result["start_command"],
        )
        self.assertIn("pip install 'uvicorn[standard]'", result["build_command"])

    async def test_fastapi_discovers_shallow_custom_module(self):
        result, _ = await self.detect(
            {
                "requirements.txt": "fastapi\n",
                "backend/main.py": "app = object()",
            }
        )

        self.assertEqual("fastapi", result["preset"])
        self.assertEqual(
            "python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000",
            result["start_command"],
        )
        self.assertFalse(result["warnings"])

    async def test_python_framework_wins_commands_in_polyglot_app_root(self):
        result, _ = await self.detect(
            {
                "app/pyproject.toml": "[project]\ndependencies = ['fastapi']\n",
                "app/package.json": json.dumps(
                    {"scripts": {"build": "tailwindcss -i input.css -o output.css"}}
                ),
                "app/main.py": "app = object()",
            }
        )

        self.assertEqual("fastapi", result["preset"])
        self.assertEqual("app", result["root_directory"])
        self.assertEqual("pip", result["package_manager"])
        self.assertEqual(
            "pip install . && pip install 'uvicorn[standard]'",
            result["build_command"],
        )
        self.assertEqual(
            "python -m uvicorn main:app --host 0.0.0.0 --port 8000",
            result["start_command"],
        )

    async def test_uv_lock_uses_reproducible_environment_and_uv_run(self):
        result, _ = await self.detect(
            {
                "pyproject.toml": "[project]\ndependencies = ['fastapi']\n",
                "uv.lock": "version = 1\n",
                "main.py": "app = object()",
            }
        )

        self.assertEqual("fastapi", result["preset"])
        self.assertEqual("uv", result["package_manager"])
        self.assertEqual(
            "uv sync --frozen --no-dev && uv pip install --python "
            ".venv/bin/python 'uvicorn[standard]'",
            result["build_command"],
        )
        self.assertEqual(
            "uv run python -m uvicorn main:app --host 0.0.0.0 --port 8000",
            result["start_command"],
        )

    async def test_python_dependency_matching_is_exact_and_normalized(self):
        result, _ = await self.detect(
            {
                "requirements.txt": "not-fastapi-wrapper==1\nFlask[async]>=3\n",
                "app.py": "app = object()",
            }
        )

        self.assertEqual("flask", result["preset"])

    async def test_laravel_keeps_registry_commands(self):
        result, _ = await self.detect(
            {
                "services/api/composer.json": json.dumps(
                    {"require": {"laravel/framework": "^12"}}
                ),
                "services/api/artisan": "",
            }
        )

        self.assertEqual("laravel", result["preset"])
        self.assertEqual("services/api", result["root_directory"])
        self.assertIn("composer install", result["build_command"])
        self.assertIn("frankenphp", result["start_command"])

    async def test_go_discovers_cmd_build_target(self):
        result, _ = await self.detect(
            {
                "services/billing/go.mod": "module example.com/billing\n",
                "services/billing/cmd/server/main.go": "package main",
            }
        )

        self.assertEqual("go", result["preset"])
        self.assertEqual("services/billing", result["root_directory"])
        self.assertEqual(
            "go mod download && go build -o app ./cmd/server",
            result["build_command"],
        )
        self.assertEqual("PORT=8000 ./app", result["start_command"])

    async def test_pure_static_site_is_lowest_priority_fallback(self):
        result, _ = await self.detect(
            {"index.html": "<!doctype html>", "assets/app.css": ""}
        )

        self.assertEqual("static", result["preset"])
        self.assertEqual("static", result["output"])
        self.assertEqual("npm install --global serve@14.2.4", result["build_command"])
        self.assertEqual("serve --single . --listen 8000", result["start_command"])

    async def test_root_dockerfile_is_reported_but_zero_config_remains_usable(self):
        result, _ = await self.detect(
            {
                "Dockerfile": "FROM node:24",
                "package.json": json.dumps(
                    {
                        "scripts": {"start": "node index.js"},
                        "dependencies": {"express": "5"},
                    }
                ),
                "index.js": "",
                "node_modules/pkg/Dockerfile": "FROM scratch",
            }
        )

        self.assertEqual("Dockerfile", result["dockerfile_path"])
        self.assertEqual("zero-config", result["build_strategy"])
        self.assertTrue(any("Dockerfile" in warning for warning in result["warnings"]))

    async def test_shallowest_nested_dockerfile_is_selected(self):
        result, _ = await self.detect(
            {
                "apps/web/Dockerfile": "FROM node:24",
                "apps/web/deploy/Dockerfile": "FROM node:24",
                "apps/web/package.json": json.dumps(
                    {"scripts": {"start": "node index.js"}, "main": "index.js"}
                ),
            }
        )

        self.assertEqual("apps/web/Dockerfile", result["dockerfile_path"])

    async def test_malformed_package_json_falls_back_without_raising(self):
        result, _ = await self.detect({"package.json": "{not-json", "index.js": ""})

        self.assertEqual("nodejs", result["preset"])
        self.assertTrue(
            any("package.json" in warning for warning in result["warnings"])
        )

    async def test_disabled_presets_do_not_participate(self):
        disabled_next = {**PRESETS[0], "enabled": False}
        result, _ = await self.detect(
            {"package.json": json.dumps({"dependencies": {"next": "16"}})},
            [disabled_next, *PRESETS[1:]],
        )

        self.assertEqual("nodejs", result["preset"])

    async def test_empty_repository_returns_normalized_empty_result(self):
        result, github = await self.detect({})

        self.assertIsNone(result["preset"])
        self.assertEqual([], result["alternatives"])
        self.assertIsNone(result["dockerfile_path"])
        self.assertEqual("unknown", result["build_strategy"])
        self.assertEqual([], github.batch_requests)

    async def test_manifest_content_is_fetched_in_one_batch(self):
        _, github = await self.detect(
            {
                "package.json": json.dumps({"dependencies": {"express": "5"}}),
                "apps/web/package.json": json.dumps({"dependencies": {"next": "16"}}),
                "apps/web/next.config.ts": "export default {}",
            }
        )

        self.assertEqual(1, len(github.batch_requests))
        self.assertIn("package.json", github.batch_requests[0])
        self.assertIn("apps/web/package.json", github.batch_requests[0])

    async def test_backend_template_index_is_not_a_static_application(self):
        result, github = await self.detect(
            {
                "requirements.txt": "fastapi\n",
                "main.py": "app = object()",
                "templates/project/index.html": "<html></html>",
                "templates/admin/index.html": "<html></html>",
            }
        )

        self.assertEqual("fastapi", result["preset"])
        self.assertEqual([], result["alternatives"])
        self.assertNotIn("templates/project/index.html", github.batch_requests[0])


if __name__ == "__main__":
    unittest.main()
