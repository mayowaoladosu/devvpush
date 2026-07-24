"""Repository-aware framework detection and deployment recommendations."""

import fnmatch
import json
import logging
import posixpath
import re
import tomllib
from typing import Any

from services.github import GitHubService

logger = logging.getLogger(__name__)

_IGNORED_PARTS = {
    ".git",
    ".next",
    ".nuxt",
    ".output",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "venv",
}
_CANDIDATE_FILES = {
    "Cargo.toml",
    "Gemfile",
    "Pipfile",
    "angular.json",
    "artisan",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "deno.json",
    "deno.jsonc",
    "go.mod",
    "index.html",
    "manage.py",
    "package.json",
    "pom.xml",
    "poetry.lock",
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "uv.lock",
}
_CONTENT_FILES = {
    ".node-version",
    ".nvmrc",
    "Cargo.toml",
    "Gemfile",
    "Pipfile",
    "angular.json",
    "build.gradle",
    "build.gradle.kts",
    "bun.lock",
    "composer.json",
    "deno.json",
    "deno.jsonc",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "package-lock.json",
    "package.json",
    "pom.xml",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "svelte.config.js",
    "svelte.config.ts",
    "yarn.lock",
}
_LOCKFILES = {
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lock", "bun.lockb"),
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
}
_SERVER_ENV = "HOST=0.0.0.0 HOSTNAME=0.0.0.0 PORT=8000"
_STATIC_SERVER_VERSION = "14.2.4"
_MAX_CONTENT_FILES = 96
_MAX_CONTENT_BYTES = 262_144


class PresetDetector:
    """Detect deployable applications behind one recommendation interface.

    The detector fetches one recursive Git tree and one bounded batch of relevant
    manifests. It evaluates every likely application root, then returns a
    deterministic recommendation plus alternatives and human-readable evidence.
    """

    def __init__(self, presets: list[dict]):
        self.presets_by_slug = {
            preset.get("slug"): preset
            for preset in presets
            if preset.get("slug") and preset.get("enabled") is True
        }
        self.patterns: list[dict[str, Any]] = []
        for preset in self.presets_by_slug.values():
            config = preset.get("config") or {}
            detection = config.get("detection") or {}
            if not self._has_positive_rule(detection):
                continue
            self.patterns.append(self._pattern(preset, detection, {}))
            for variant in detection.get("variants") or []:
                if not isinstance(variant, dict) or not self._has_positive_rule(
                    variant
                ):
                    continue
                self.patterns.append(
                    self._pattern(preset, variant, variant.get("config") or {})
                )

    async def detect(
        self,
        github_service: GitHubService,
        user_access_token: str,
        repo_id: int,
        default_branch: str,
    ) -> dict:
        """Return the best deployment recommendation and viable alternatives."""
        empty = self._empty_result()
        if not self.patterns:
            empty["warnings"].append("No enabled framework detection rules exist.")
            return empty

        try:
            tree = await github_service.get_git_tree(
                user_access_token, repo_id, sha=default_branch, recursive=True
            )
            raw_paths = {
                self._normalize_path(item.get("path", ""))
                for item in tree.get("tree", [])
                if item.get("type") == "blob"
            }
            raw_paths.discard("")
            dockerfile_path = self._find_dockerfile(raw_paths)
            paths = {path for path in raw_paths if not self._is_ignored(path)}
            if not paths:
                return self._empty_result(dockerfile_path=dockerfile_path)

            roots = self._candidate_roots(paths)
            content_paths = self._content_paths(paths)
            contents = await self._fetch_contents(
                github_service,
                user_access_token,
                repo_id,
                default_branch,
                content_paths,
            )

            recommendations = []
            for root in roots:
                recommendation = self._recommend_for_root(root, roots, paths, contents)
                if recommendation:
                    recommendations.append(recommendation)

            recommendations.sort(key=self._recommendation_sort_key)
            if not recommendations:
                result = self._empty_result(dockerfile_path=dockerfile_path)
                if tree.get("truncated"):
                    result["warnings"].append(
                        "GitHub truncated the repository tree; detection may be "
                        "incomplete."
                    )
                return result

            best = self._public_recommendation(recommendations[0])
            best["alternatives"] = [
                self._public_recommendation(item) for item in recommendations[1:]
            ]
            best["dockerfile_path"] = dockerfile_path
            best["build_strategy"] = "zero-config"
            if dockerfile_path:
                best["warnings"].append(
                    f"Detected {dockerfile_path}. Dockerfile builds are not enabled "
                    "yet; "
                    "the generated zero-config recommendation remains selected."
                )
            if tree.get("truncated"):
                best["warnings"].append(
                    "GitHub truncated the repository tree; detection may be incomplete."
                )
            return best
        except Exception:
            logger.exception("Preset detection failed")
            empty["warnings"].append(
                "Framework detection failed; configure the runner and commands "
                "manually."
            )
            return empty

    async def detect_with_commands(
        self,
        github_service: GitHubService,
        user_access_token: str,
        repo_id: int,
        default_branch: str,
    ) -> dict:
        """Compatibility entry point returning the normalized recommendation."""
        return await self.detect(
            github_service, user_access_token, repo_id, default_branch
        )

    def _pattern(self, preset: dict, detection: dict, overrides: dict) -> dict:
        return {
            "preset": preset["slug"],
            "name": preset.get("name") or preset["slug"],
            "category": preset.get("category"),
            "priority": int(
                detection.get(
                    "priority", (preset.get("config") or {}).get("priority", 0)
                )
                or 0
            ),
            "any_files": detection.get("any_files") or [],
            "all_files": detection.get("all_files") or [],
            "any_paths": detection.get("any_paths") or [],
            "none_files": detection.get("none_files") or [],
            "any_dependencies": detection.get("any_dependencies") or [],
            "all_dependencies": detection.get("all_dependencies") or [],
            "package_check": detection.get("package_check"),
            "config_overrides": overrides,
        }

    @staticmethod
    def _has_positive_rule(detection: dict) -> bool:
        return any(
            detection.get(key)
            for key in (
                "any_files",
                "all_files",
                "any_paths",
                "any_dependencies",
                "all_dependencies",
                "package_check",
            )
        )

    def _recommend_for_root(
        self,
        root: str,
        candidate_roots: list[str],
        paths: set[str],
        contents: dict[str, str],
    ) -> dict | None:
        relative_paths = self._relative_paths(paths, root, candidate_roots)
        package_path = self._join(root, "package.json")
        package, package_warning = self._parse_json(contents.get(package_path))
        node_dependencies = self._node_dependencies(package)
        python_dependencies = self._python_dependencies(root, contents)
        composer_dependencies = self._composer_dependencies(root, contents)
        dependencies = node_dependencies | python_dependencies | composer_dependencies

        matches = []
        for pattern in self.patterns:
            evidence = self._match_pattern(pattern, relative_paths, dependencies)
            if evidence is None:
                continue
            matches.append((pattern, evidence))

        if not matches:
            return None
        matches.sort(
            key=lambda item: (
                -item[0]["priority"],
                -len(item[1]),
                item[0]["preset"],
            )
        )
        pattern, evidence = matches[0]

        if self._is_workspace_shell(pattern["preset"], root, package):
            return None

        preset = self.presets_by_slug[pattern["preset"]]
        config = dict(preset.get("config") or {})
        config.pop("detection", None)
        config.update(
            {
                key: value
                for key, value in pattern.get("config_overrides", {}).items()
                if value is not None
            }
        )
        warnings = [package_warning] if package_warning else []
        recommendation = {
            "preset": pattern["preset"],
            "framework_name": pattern["name"],
            "runner": config.get("runner"),
            "root_directory": root,
            "build_command": config.get("build_command") or "",
            "pre_deploy_command": config.get("pre_deploy_command") or "",
            "start_command": config.get("start_command") or "",
            "output": config.get("output") or "server",
            "output_directory": config.get("output_directory"),
            "package_manager": None,
            "lockfile": None,
            "confidence": self._confidence(pattern["priority"], evidence),
            "evidence": evidence,
            "warnings": warnings,
            "_priority": pattern["priority"],
        }

        category = pattern.get("category") or ""
        if category == "Python":
            self._refine_python(recommendation, root, relative_paths, contents)
        elif category == "Go":
            self._refine_go(recommendation, relative_paths)
        elif category in {"Node.js", "Bun"}:
            self._refine_node(
                recommendation, config, root, paths, contents, package or {}
            )

        return recommendation

    def _match_pattern(
        self, pattern: dict, relative_paths: set[str], dependencies: set[str]
    ) -> list[str] | None:
        evidence: list[str] = []

        any_files = pattern.get("any_files") or []
        if any_files:
            hit = self._first_path_match(relative_paths, any_files)
            if not hit:
                return None
            evidence.append(hit)

        all_files = pattern.get("all_files") or []
        for file_pattern in all_files:
            hit = self._first_path_match(relative_paths, [file_pattern])
            if not hit:
                return None
            evidence.append(hit)

        any_paths = pattern.get("any_paths") or []
        if any_paths:
            hit = self._first_path_match(relative_paths, any_paths)
            if not hit:
                return None
            evidence.append(hit)

        none_files = pattern.get("none_files") or []
        if self._first_path_match(relative_paths, none_files):
            return None

        normalized_dependencies = {
            self._normalize_dependency(dep) for dep in dependencies
        }
        any_dependencies = pattern.get("any_dependencies") or []
        if any_dependencies:
            hit = next(
                (
                    dep
                    for dep in any_dependencies
                    if self._normalize_dependency(dep) in normalized_dependencies
                ),
                None,
            )
            if not hit:
                return None
            evidence.append(f"{hit} dependency")

        all_dependencies = pattern.get("all_dependencies") or []
        for dependency in all_dependencies:
            if self._normalize_dependency(dependency) not in normalized_dependencies:
                return None
            evidence.append(f"{dependency} dependency")

        package_check = pattern.get("package_check")
        if package_check:
            normalized = self._normalize_dependency(str(package_check))
            if normalized not in normalized_dependencies:
                return None
            evidence.append(f"{package_check} dependency")

        return list(dict.fromkeys(evidence))

    def _refine_node(
        self,
        recommendation: dict,
        config: dict,
        root: str,
        paths: set[str],
        contents: dict[str, str],
        package: dict,
    ) -> None:
        manager = self._detect_package_manager(root, paths, contents, package)
        recommendation["package_manager"] = manager["name"]
        recommendation["lockfile"] = manager.get("lockfile")
        if manager["name"] == "bun":
            recommendation["runner"] = config.get("bun_runner") or "bun-1.3"
        else:
            recommendation["runner"] = self._node_runner(config, package)

        scripts = (
            package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        )
        dependencies = self._node_dependencies(package)
        slug = recommendation["preset"]
        output = recommendation["output"]
        output_directory = recommendation["output_directory"]

        if slug == "sveltekit":
            if "@sveltejs/adapter-static" in dependencies:
                output = "static"
                output_directory = "build"
                recommendation["evidence"].append("@sveltejs/adapter-static dependency")
            else:
                output = "server"
                output_directory = None
                recommendation["start_command"] = "node build"
                if "@sveltejs/adapter-node" not in dependencies:
                    recommendation["warnings"].append(
                        "SvelteKit adapter-auto detected; install "
                        "@sveltejs/adapter-node or @sveltejs/adapter-static for "
                        "deterministic container output."
                    )
        elif slug == "astro" and "@astrojs/node" in dependencies:
            output = "server"
            output_directory = None
            recommendation["start_command"] = "node dist/server/entry.mjs"
            recommendation["evidence"].append("@astrojs/node dependency")
        elif slug == "angular":
            angular_path = self._join(root, "angular.json")
            angular, warning = self._parse_json(contents.get(angular_path))
            if warning:
                recommendation["warnings"].append(warning)
            project = None
            if angular:
                project = angular.get("defaultProject")
                projects = angular.get("projects")
                if not project and isinstance(projects, dict) and projects:
                    project = next(iter(projects))
            if project:
                output_directory = f"dist/{project}/browser"
                recommendation["evidence"].append(f'angular.json project "{project}"')
        elif slug == "vitepress":
            docs_config = self._join(root, "docs/.vitepress/config.ts")
            if (
                docs_config in paths
                or self._join(root, "docs/.vitepress/config.mts") in paths
            ):
                output_directory = "docs/.vitepress/dist"

        recommendation["output"] = output
        recommendation["output_directory"] = output_directory

        has_package = self._join(root, "package.json") in paths
        build_parts = [manager["install_command"]] if has_package else []
        build_script = config.get("build_script")
        if not build_script:
            build_script = next(
                (key for key in ("build", "compile", "bundle") if key in scripts),
                None,
            )
        if build_script and build_script in scripts:
            build_parts.append(f"{manager['run_prefix']} {build_script}")
        elif build_script:
            recommendation["warnings"].append(
                f'No "{build_script}" script exists in package.json.'
            )

        if output == "static":
            if manager["name"] == "bun":
                recommendation["start_command"] = (
                    f"bunx serve@{_STATIC_SERVER_VERSION} --single "
                    f"{output_directory or '.'} --listen 8000"
                )
            else:
                build_parts.append(
                    f"npm install --global serve@{_STATIC_SERVER_VERSION}"
                )
                recommendation["start_command"] = (
                    f"serve --single {output_directory or '.'} --listen 8000"
                )
        else:
            start_script = config.get("start_script")
            if slug == "nestjs" and "start:prod" in scripts:
                start_script = "start:prod"
            if start_script and start_script in scripts:
                recommendation["start_command"] = (
                    f"{manager['run_prefix']} {start_script}"
                )
            elif "start" in scripts:
                recommendation["start_command"] = f"{manager['run_prefix']} start"
            elif not recommendation["start_command"]:
                main = package.get("main")
                if isinstance(main, str) and main.strip():
                    recommendation["start_command"] = f"node {main.strip()}"
                else:
                    recommendation["start_command"] = f"{manager['run_prefix']} start"
                    recommendation["warnings"].append(
                        "No start script or package.json main entry was found; "
                        "verify the start command."
                    )
            recommendation["start_command"] = self._server_command(
                slug, recommendation["start_command"]
            )

        recommendation["build_command"] = " && ".join(
            part for part in build_parts if part
        )

    def _refine_python(
        self,
        recommendation: dict,
        root: str,
        relative_paths: set[str],
        contents: dict[str, str],
    ) -> None:
        command_prefix = ""
        if "uv.lock" in relative_paths:
            install = "uv sync --frozen --no-dev"
            command_prefix = "uv run "
            recommendation["package_manager"] = "uv"
        else:
            requirement = self._python_requirement_file(root, contents)
            if requirement:
                install = f"pip install -r {requirement}"
            elif self._join(root, "pyproject.toml") in contents:
                install = "pip install ."
            elif self._join(root, "Pipfile") in contents:
                install = "pip install pipenv && pipenv install --deploy --system"
                recommendation["package_manager"] = "pipenv"
            else:
                install = recommendation["build_command"] or "pip install ."
            recommendation["package_manager"] = (
                recommendation["package_manager"] or "pip"
            )

        def install_runtime(package: str) -> None:
            nonlocal install
            if command_prefix:
                install = self._append_command(
                    install,
                    f"uv pip install --python .venv/bin/python {package}",
                )
            else:
                install = self._append_command(install, f"pip install {package}")

        slug = recommendation["preset"]
        if slug == "django":
            install_runtime("gunicorn")
            wsgi = min(
                (
                    path
                    for path in relative_paths
                    if path.endswith("/wsgi.py") and not self._is_ignored(path)
                ),
                key=lambda path: (path.count("/"), path),
                default="config/wsgi.py",
            )
            if wsgi not in relative_paths:
                recommendation["warnings"].append(
                    "No wsgi.py was found; verify the generated Django module path."
                )
            module = wsgi.removesuffix(".py").replace("/", ".")
            recommendation["start_command"] = (
                f"{command_prefix}gunicorn {module}:application --bind 0.0.0.0:8000"
            )
            recommendation["evidence"].append(wsgi)
            if command_prefix and recommendation["pre_deploy_command"]:
                recommendation["pre_deploy_command"] = (
                    command_prefix + recommendation["pre_deploy_command"]
                )
        elif slug in {"fastapi", "starlette"}:
            install_runtime("'uvicorn[standard]'")
            entry = (
                self._first_existing(
                    relative_paths,
                    ("main.py", "app/main.py", "src/main.py", "app.py"),
                )
                or self._shallow_python_entry(relative_paths, ("main.py", "app.py"))
                or "main.py"
            )
            if entry not in relative_paths:
                recommendation["warnings"].append(
                    "No FastAPI entry module was found; verify the generated "
                    "uvicorn module path."
                )
            module = entry.removesuffix(".py").replace("/", ".")
            recommendation["start_command"] = (
                f"{command_prefix}python -m uvicorn {module}:app "
                "--host 0.0.0.0 --port 8000"
            )
            recommendation["evidence"].append(entry)
        elif slug == "flask":
            install_runtime("gunicorn")
            entry = (
                self._first_existing(
                    relative_paths, ("app.py", "wsgi.py", "main.py", "application.py")
                )
                or self._shallow_python_entry(
                    relative_paths, ("app.py", "wsgi.py", "main.py")
                )
                or "app.py"
            )
            if entry not in relative_paths:
                recommendation["warnings"].append(
                    "No Flask entry module was found; verify the generated "
                    "gunicorn module path."
                )
            module = entry.removesuffix(".py").replace("/", ".")
            recommendation["start_command"] = (
                f"{command_prefix}gunicorn {module}:app --bind 0.0.0.0:8000"
            )
            recommendation["evidence"].append(entry)
        elif slug == "streamlit":
            entry = (
                self._first_existing(
                    relative_paths, ("streamlit_app.py", "app.py", "main.py")
                )
                or "streamlit_app.py"
            )
            recommendation["start_command"] = (
                f"{command_prefix}streamlit run {entry} "
                "--server.address 0.0.0.0 --server.port 8000"
            )
        elif slug == "gradio":
            entry = (
                self._first_existing(relative_paths, ("app.py", "main.py")) or "app.py"
            )
            recommendation["start_command"] = (
                "GRADIO_SERVER_NAME=0.0.0.0 GRADIO_SERVER_PORT=8000 "
                f"{command_prefix}python {entry}"
            )
        elif not recommendation["start_command"]:
            entry = self._first_existing(relative_paths, ("main.py", "app.py"))
            recommendation["start_command"] = (
                f"{command_prefix}python {entry or 'main.py'}"
            )
            if not entry:
                recommendation["warnings"].append(
                    "No Python entry point was found; verify the start command."
                )

        recommendation["build_command"] = install

    @staticmethod
    def _refine_go(recommendation: dict, relative_paths: set[str]) -> None:
        target = "."
        if "main.go" not in relative_paths:
            candidates = sorted(
                path
                for path in relative_paths
                if path.startswith("cmd/") and path.endswith("/main.go")
            )
            if candidates:
                target = "./" + posixpath.dirname(candidates[0])
            else:
                recommendation["warnings"].append(
                    "No main.go was found at the module root or under cmd/."
                )
        recommendation["build_command"] = f"go mod download && go build -o app {target}"
        recommendation["start_command"] = "PORT=8000 ./app"
        recommendation["package_manager"] = "go"

    async def _fetch_contents(
        self,
        github_service: GitHubService,
        token: str,
        repo_id: int,
        ref: str,
        paths: list[str],
    ) -> dict[str, str]:
        if not paths:
            return {}
        if hasattr(github_service, "get_file_contents"):
            contents = await github_service.get_file_contents(
                token,
                repo_id,
                paths,
                ref=ref,
                max_bytes=_MAX_CONTENT_BYTES,
            )
        else:
            contents = {}
            for path in paths:
                content = await github_service.get_file_content(
                    token, repo_id, path, ref=ref
                )
                if content is not None:
                    contents[path] = content
        return contents

    @staticmethod
    def _content_paths(paths: set[str]) -> list[str]:
        candidates = []
        for path in paths:
            name = posixpath.basename(path)
            if (
                name in _CONTENT_FILES
                or name in {"bun.lockb", "npm-shrinkwrap.json"}
                or ("requirements/" in path and name.endswith(".txt"))
                or fnmatch.fnmatch(name, "vite.config.*")
                or fnmatch.fnmatch(name, "next.config.*")
            ):
                candidates.append(path)
        candidates.sort(
            key=lambda path: (
                path.count("/"),
                0 if posixpath.basename(path) == "package.json" else 1,
                path,
            )
        )
        return candidates[:_MAX_CONTENT_FILES]

    @staticmethod
    def _candidate_roots(paths: set[str]) -> list[str]:
        roots = {
            posixpath.dirname(path)
            for path in paths
            if posixpath.basename(path) in _CANDIDATE_FILES
            and (
                posixpath.basename(path) != "index.html"
                or PresetDetector._is_static_candidate(path)
            )
        }
        roots.discard(".")
        return sorted(roots, key=lambda root: (root.count("/"), root))

    @staticmethod
    def _is_static_candidate(path: str) -> bool:
        directory = posixpath.dirname(path)
        parts = set(directory.lower().split("/")) if directory else set()
        if parts & {
            "examples",
            "fixtures",
            "public",
            "templates",
            "template",
            "test",
            "tests",
            "views",
        }:
            return False
        return directory.count("/") <= 2

    @staticmethod
    def _relative_paths(
        paths: set[str], root: str, candidate_roots: list[str]
    ) -> set[str]:
        prefix = root + "/" if root else ""
        nested = {
            other[len(prefix) :]
            for other in candidate_roots
            if other != root and other.startswith(prefix) and other[len(prefix) :]
        }
        relative = set()
        for path in paths:
            if prefix and not path.startswith(prefix):
                continue
            value = path[len(prefix) :] if prefix else path
            if any(
                value == boundary or value.startswith(boundary + "/")
                for boundary in nested
            ):
                continue
            relative.add(value)
        return relative

    @staticmethod
    def _recommendation_sort_key(item: dict) -> tuple:
        root = item.get("root_directory") or ""
        return (-item["_priority"], root.count("/"), root, item["preset"])

    @staticmethod
    def _public_recommendation(item: dict) -> dict:
        return {key: value for key, value in item.items() if not key.startswith("_")}

    @staticmethod
    def _confidence(priority: int, evidence: list[str]) -> str:
        if priority >= 90 or len(evidence) >= 2:
            return "high"
        if priority >= 40:
            return "medium"
        return "low"

    @staticmethod
    def _parse_json(content: str | None) -> tuple[dict | None, str | None]:
        if content is None:
            return None, None
        try:
            value = json.loads(content)
            if isinstance(value, dict):
                return value, None
            return None, "A JSON manifest did not contain an object."
        except (json.JSONDecodeError, TypeError):
            return None, "Could not parse package.json; generic defaults were used."

    @staticmethod
    def _node_dependencies(package: dict | None) -> set[str]:
        if not package:
            return set()
        dependencies = set()
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            values = package.get(key)
            if isinstance(values, dict):
                dependencies.update(str(name).lower() for name in values)
        return dependencies

    def _python_dependencies(self, root: str, contents: dict[str, str]) -> set[str]:
        dependencies: set[str] = set()
        prefix = root + "/" if root else ""
        for path, content in contents.items():
            if prefix and not path.startswith(prefix):
                continue
            relative = path[len(prefix) :] if prefix else path
            if relative == "requirements.txt" or (
                relative.startswith("requirements/") and relative.endswith(".txt")
            ):
                for line in content.splitlines():
                    token = line.split("#", 1)[0].strip()
                    if not token or token.startswith(("-", "http://", "https://")):
                        continue
                    match = re.match(r"([A-Za-z0-9_.-]+)", token)
                    if match:
                        dependencies.add(self._normalize_dependency(match.group(1)))
            elif relative == "pyproject.toml":
                dependencies.update(self._pyproject_dependencies(content))
        return dependencies

    def _pyproject_dependencies(self, content: str) -> set[str]:
        dependencies: set[str] = set()
        try:
            data = tomllib.loads(content)
            project = data.get("project") if isinstance(data, dict) else None
            if isinstance(project, dict):
                for entry in project.get("dependencies") or []:
                    match = re.match(r"([A-Za-z0-9_.-]+)", str(entry).strip())
                    if match:
                        dependencies.add(self._normalize_dependency(match.group(1)))
            poetry = (
                ((data.get("tool") or {}).get("poetry") or {})
                if isinstance(data, dict)
                else {}
            )
            if isinstance(poetry, dict):
                values = poetry.get("dependencies") or {}
                if isinstance(values, dict):
                    dependencies.update(
                        self._normalize_dependency(name)
                        for name in values
                        if str(name).lower() != "python"
                    )
        except (tomllib.TOMLDecodeError, AttributeError, TypeError):
            for match in re.finditer(
                r"(?:^|[\"'])\s*([A-Za-z0-9_.-]+)\s*(?:[\"'\s=<>!~\[])",
                content,
                re.MULTILINE,
            ):
                dependencies.add(self._normalize_dependency(match.group(1)))
        return dependencies

    def _composer_dependencies(self, root: str, contents: dict[str, str]) -> set[str]:
        path = self._join(root, "composer.json")
        data, _ = self._parse_json(contents.get(path))
        dependencies = set()
        if data:
            for key in ("require", "require-dev"):
                values = data.get(key)
                if isinstance(values, dict):
                    dependencies.update(
                        self._normalize_dependency(name) for name in values
                    )
        return dependencies

    @staticmethod
    def _detect_package_manager(
        root: str,
        paths: set[str],
        contents: dict[str, str],
        package: dict,
    ) -> dict:
        manifests = [(root, package)]
        current = posixpath.dirname(root) if root else ""
        while root and current != root:
            path = PresetDetector._join(current, "package.json")
            ancestor, _ = PresetDetector._parse_json(contents.get(path))
            if ancestor:
                manifests.append((current, ancestor))
            if not current:
                break
            root, current = current, posixpath.dirname(current)

        for _, manifest in manifests:
            package_manager = manifest.get("packageManager")
            if not isinstance(package_manager, str):
                continue
            match = re.match(r"^(npm|pnpm|yarn|bun)(?:@(.+))?$", package_manager)
            if match:
                name, version = match.groups()
                lockfile = PresetDetector._nearest_lockfile(
                    manifests[0][0], paths, name
                )
                return PresetDetector._manager_info(name, lockfile, version)

        for name in ("pnpm", "yarn", "bun", "npm"):
            lockfile = PresetDetector._nearest_lockfile(manifests[0][0], paths, name)
            if lockfile:
                return PresetDetector._manager_info(name, lockfile, None)
        return PresetDetector._manager_info("npm", None, None)

    @staticmethod
    def _nearest_lockfile(root: str, paths: set[str], manager: str) -> str | None:
        current = root
        while True:
            for name in _LOCKFILES[manager]:
                candidate = PresetDetector._join(current, name)
                if candidate in paths:
                    return candidate
            if not current:
                break
            parent = posixpath.dirname(current)
            current = "" if parent == "." else parent
        return None

    @staticmethod
    def _manager_info(name: str, lockfile: str | None, version: str | None) -> dict:
        if name == "pnpm":
            install = "pnpm install --frozen-lockfile" if lockfile else "pnpm install"
            run = "pnpm run"
        elif name == "yarn":
            major = PresetDetector._major_version(version)
            immutable = "--immutable" if major and major >= 2 else "--frozen-lockfile"
            install = f"yarn install {immutable}" if lockfile else "yarn install"
            run = "yarn"
        elif name == "bun":
            install = "bun install --frozen-lockfile" if lockfile else "bun install"
            run = "bun run"
        else:
            install = "npm ci" if lockfile else "npm install"
            run = "npm run"
        return {
            "name": name,
            "version": version,
            "lockfile": lockfile,
            "install_command": install,
            "run_prefix": run,
        }

    @staticmethod
    def _node_runner(config: dict, package: dict) -> str:
        requested = None
        engines = package.get("engines")
        if isinstance(engines, dict):
            requested = PresetDetector._major_version(str(engines.get("node") or ""))
        if requested and requested >= 24:
            return config.get("modern_runner") or "node-24"
        return config.get("runner") or "node-20"

    @staticmethod
    def _major_version(value: str | None) -> int | None:
        if not value:
            return None
        match = re.search(r"(?<!\d)(\d{1,3})(?:\.\d+)?", value)
        return int(match.group(1)) if match else None

    @staticmethod
    def _server_command(slug: str, command: str) -> str:
        if command.startswith(("HOST=", "PORT=", "NUXT_", "GRADIO_")):
            return command
        if slug == "nuxt":
            return (
                "NUXT_HOST=0.0.0.0 NITRO_HOST=0.0.0.0 "
                "NUXT_PORT=8000 NITRO_PORT=8000 " + command
            )
        return f"{_SERVER_ENV} {command}"

    @staticmethod
    def _python_requirement_file(root: str, contents: dict[str, str]) -> str | None:
        prefix = root + "/" if root else ""
        choices = []
        for path in contents:
            if prefix and not path.startswith(prefix):
                continue
            relative = path[len(prefix) :] if prefix else path
            if relative == "requirements.txt" or (
                relative.startswith("requirements/") and relative.endswith(".txt")
            ):
                choices.append(relative)
        return min(
            choices,
            key=lambda path: (
                0 if path == "requirements.txt" else 1,
                0 if "production" in path else 1,
                path,
            ),
            default=None,
        )

    @staticmethod
    def _first_existing(paths: set[str], candidates: tuple[str, ...]) -> str | None:
        return next((candidate for candidate in candidates if candidate in paths), None)

    @staticmethod
    def _shallow_python_entry(paths: set[str], names: tuple[str, ...]) -> str | None:
        return min(
            (
                path
                for path in paths
                if posixpath.basename(path) in names and path.count("/") <= 2
            ),
            key=lambda path: (path.count("/"), path),
            default=None,
        )

    @staticmethod
    def _append_command(current: str, command: str) -> str:
        return f"{current} && {command}" if current else command

    @staticmethod
    def _first_path_match(paths: set[str], patterns: list[str]) -> str | None:
        for pattern in patterns:
            matches = sorted(path for path in paths if fnmatch.fnmatch(path, pattern))
            if matches:
                return matches[0]
        return None

    @staticmethod
    def _normalize_dependency(value: str) -> str:
        return re.sub(r"[-_.]+", "-", str(value).strip().lower())

    @staticmethod
    def _normalize_path(path: str) -> str:
        return str(path).replace("\\", "/").strip("/")

    @staticmethod
    def _is_ignored(path: str) -> bool:
        return any(part in _IGNORED_PARTS for part in path.split("/"))

    @staticmethod
    def _find_dockerfile(paths: set[str]) -> str | None:
        for root_name in ("Dockerfile", "dockerfile", "Containerfile"):
            if root_name in paths:
                return root_name
        candidates = [
            path
            for path in paths
            if posixpath.basename(path).lower() in {"dockerfile", "containerfile"}
            and not PresetDetector._is_ignored(path)
        ]
        return min(
            candidates,
            key=lambda path: (path.count("/"), path.lower(), path),
            default=None,
        )

    @staticmethod
    def _is_workspace_shell(slug: str, root: str, package: dict | None) -> bool:
        if slug != "nodejs" or not package:
            return False
        scripts = (
            package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
        )
        has_app_script = any(
            key in scripts for key in ("start", "dev", "build", "serve")
        )
        if package.get("workspaces") and not has_app_script:
            return True
        return bool(root and not has_app_script)

    @staticmethod
    def _join(root: str, name: str) -> str:
        return f"{root}/{name}" if root else name

    @staticmethod
    def _empty_result(
        dockerfile_path: str | None = None, warnings: list[str] | None = None
    ) -> dict:
        return {
            "preset": None,
            "framework_name": None,
            "runner": None,
            "root_directory": None,
            "build_command": None,
            "pre_deploy_command": None,
            "start_command": None,
            "output": None,
            "output_directory": None,
            "package_manager": None,
            "lockfile": None,
            "confidence": None,
            "evidence": [],
            "warnings": list(warnings or []),
            "alternatives": [],
            "dockerfile_path": dockerfile_path,
            "build_strategy": "unknown",
        }
