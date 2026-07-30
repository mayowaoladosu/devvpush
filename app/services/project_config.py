"""Validated `.layerrail.json` project configuration as code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from models import Project
from services.deployment_policy import DeploymentPolicyService

_CONFIG_FILES = (".layerrail.json", "layerrail.json", "devpush.json")
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9_./-]*$")


class ProjectConfigError(ValueError):
    """A repository or API project configuration is invalid."""


@dataclass(frozen=True)
class ProjectConfigResult:
    values: dict[str, object]
    source: str


class ProjectConfigService:
    """Parse, validate, merge, and export the public LayerRail config contract."""

    MAX_BYTES = 65_536
    PUBLIC_INTERNAL_KEYS = frozenset(
        {
            "build_strategy",
            "preset",
            "runner",
            "image",
            "root_directory",
            "dockerfile_path",
            "build_command",
            "pre_deploy_command",
            "start_command",
            "cpus",
            "memory",
            DeploymentPolicyService.KEY,
            "layerrail_config_source",
        }
    )

    @classmethod
    def parse(cls, content: object, *, source: str = ".layerrail.json") -> ProjectConfigResult:
        if isinstance(content, bytes):
            raw = content.decode("utf-8")
        else:
            raw = str(content or "")
        if not raw or len(raw.encode("utf-8")) > cls.MAX_BYTES:
            raise ProjectConfigError("LayerRail configuration is empty or too large.")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProjectConfigError(
                f"LayerRail configuration contains invalid JSON at line {exc.lineno}."
            ) from exc
        if not isinstance(payload, dict):
            raise ProjectConfigError("LayerRail configuration must be a JSON object.")
        version = str(payload.get("version") or "1")
        if version != "1":
            raise ProjectConfigError("LayerRail configuration version is not supported.")
        unknown = set(payload) - {
            "$schema",
            "version",
            "build",
            "resources",
            "deployment",
        }
        if unknown:
            raise ProjectConfigError(
                f"Unknown LayerRail configuration key: {sorted(unknown)[0]}."
            )
        values: dict[str, object] = {}
        schema_url = payload.get("$schema")
        if schema_url is not None and (
            not isinstance(schema_url, str) or len(schema_url) > 2_048
        ):
            raise ProjectConfigError("$schema must be a string.")
        build_value = payload.get("build")
        build = {} if build_value is None else build_value
        if not isinstance(build, dict):
            raise ProjectConfigError("build must be an object.")
        build_keys = {
            "strategy",
            "preset",
            "runner",
            "rootDirectory",
            "dockerfile",
            "buildCommand",
            "preDeployCommand",
            "startCommand",
        }
        if set(build) - build_keys:
            raise ProjectConfigError(
                f"Unknown build key: {sorted(set(build) - build_keys)[0]}."
            )
        strategy = str(build.get("strategy") or "").strip()
        if strategy:
            if strategy not in {"zero-config", "dockerfile"}:
                raise ProjectConfigError("build.strategy must be zero-config or dockerfile.")
            values["build_strategy"] = strategy
        mapping = {
            "preset": "preset",
            "runner": "runner",
            "rootDirectory": "root_directory",
            "dockerfile": "dockerfile_path",
            "buildCommand": "build_command",
            "preDeployCommand": "pre_deploy_command",
            "startCommand": "start_command",
        }
        for public, internal in mapping.items():
            if public not in build:
                continue
            if not isinstance(build.get(public), str):
                raise ProjectConfigError(f"build.{public} must be a string.")
            value = str(build.get(public) or "").strip()
            max_length = (
                255
                if public in {"preset", "runner", "rootDirectory", "dockerfile"}
                else 2_000
            )
            if len(value) > max_length:
                raise ProjectConfigError(f"build.{public} is too long.")
            if public in {"rootDirectory", "dockerfile"}:
                cls.validate_path(value, f"build.{public}")
            values[internal] = value

        resources_value = payload.get("resources")
        resources = {} if resources_value is None else resources_value
        if not isinstance(resources, dict) or set(resources) - {"cpus", "memoryMb"}:
            raise ProjectConfigError("resources accepts only cpus and memoryMb.")
        if "cpus" in resources and resources["cpus"] is not None:
            if isinstance(resources["cpus"], bool) or not isinstance(
                resources["cpus"], (int, float)
            ):
                raise ProjectConfigError("resources.cpus must be a number.")
            cpus = float(resources["cpus"])
            if not 0 < cpus <= 256:
                raise ProjectConfigError("resources.cpus must be between 0 and 256.")
            values["cpus"] = cpus
        if "memoryMb" in resources and resources["memoryMb"] is not None:
            if isinstance(resources["memoryMb"], bool) or not isinstance(
                resources["memoryMb"], int
            ):
                raise ProjectConfigError("resources.memoryMb must be an integer.")
            memory = resources["memoryMb"]
            if not 16 <= memory <= 1_048_576:
                raise ProjectConfigError(
                    "resources.memoryMb must be between 16 and 1048576."
                )
            values["memory"] = memory

        deployment_value = payload.get("deployment")
        deployment = {} if deployment_value is None else deployment_value
        if not isinstance(deployment, dict):
            raise ProjectConfigError("deployment must be an object.")
        deployment_keys = {
            "webhookEnabled",
            "allowedBranches",
            "ignoredBranches",
            "ignoredAuthors",
            "skipMessageTokens",
            "maxConcurrent",
            "supersedeOlder",
        }
        if set(deployment) - deployment_keys:
            raise ProjectConfigError(
                f"Unknown deployment key: {sorted(set(deployment) - deployment_keys)[0]}."
            )
        if deployment:
            for name in ("webhookEnabled", "supersedeOlder"):
                if name in deployment and not isinstance(deployment[name], bool):
                    raise ProjectConfigError(f"deployment.{name} must be a boolean.")
            for name in (
                "allowedBranches",
                "ignoredBranches",
                "ignoredAuthors",
                "skipMessageTokens",
            ):
                if name not in deployment:
                    continue
                patterns = deployment[name]
                if (
                    not isinstance(patterns, list)
                    or len(patterns) > 50
                    or any(
                        not isinstance(pattern, str)
                        or not pattern.strip()
                        or len(pattern) > 255
                        for pattern in patterns
                    )
                ):
                    raise ProjectConfigError(
                        f"deployment.{name} must contain up to 50 non-empty strings."
                    )
            max_concurrent_value = deployment.get("maxConcurrent", 1)
            if isinstance(max_concurrent_value, bool) or not isinstance(
                max_concurrent_value, int
            ):
                raise ProjectConfigError(
                    "deployment.maxConcurrent must be an integer."
                )
            max_concurrent = max_concurrent_value
            if not 1 <= max_concurrent <= 50:
                raise ProjectConfigError(
                    "deployment.maxConcurrent must be between 1 and 50."
                )
            policy_values = {
                "webhook_enabled": bool(deployment.get("webhookEnabled", True)),
                "allowed_branches": list(
                    DeploymentPolicyService.patterns(deployment.get("allowedBranches"))
                ),
                "ignored_branches": list(
                    DeploymentPolicyService.patterns(deployment.get("ignoredBranches"))
                ),
                "ignored_authors": list(
                    DeploymentPolicyService.patterns(deployment.get("ignoredAuthors"))
                ),
                "skip_message_tokens": list(
                    DeploymentPolicyService.patterns(deployment.get("skipMessageTokens"))
                ),
                "max_concurrent": max_concurrent,
                "supersede_older": bool(deployment.get("supersedeOlder", True)),
            }
            values[DeploymentPolicyService.KEY] = policy_values
        return ProjectConfigResult(values=values, source=source)

    @staticmethod
    def validate_path(value: str, label: str) -> None:
        if (
            len(value) > 255
            or value.startswith(("/", "\\"))
            or ".." in value.replace("\\", "/").split("/")
            or not _PATH_PATTERN.fullmatch(value)
        ):
            raise ProjectConfigError(f"{label} must stay inside the repository.")

    @classmethod
    async def load_from_github(
        cls,
        github_service,
        token: str,
        repo_id: int,
        ref: str,
        root_directory: str = "",
    ) -> ProjectConfigResult | None:
        root = str(root_directory or "").strip().strip("/")
        candidates = list(_CONFIG_FILES)
        if root:
            cls.validate_path(root, "root directory")
            candidates = [f"{root}/{path}" for path in _CONFIG_FILES] + candidates
        contents = await github_service.get_file_contents(
            token,
            repo_id,
            candidates,
            ref=ref,
            max_bytes=cls.MAX_BYTES,
            concurrency=3,
        )
        for path in candidates:
            if path in contents:
                return cls.parse(contents[path], source=path)
        return None

    @staticmethod
    def merge(base: dict | None, override: ProjectConfigResult | None) -> dict:
        values = dict(base or {})
        if override:
            values.update(override.values)
            values["layerrail_config_source"] = override.source
        return values

    @classmethod
    def apply_to_project(cls, project: Project, content: object) -> ProjectConfigResult:
        result = cls.parse(content, source="api")
        internal = {
            key: value
            for key, value in (project.config or {}).items()
            if key not in cls.PUBLIC_INTERNAL_KEYS
        }
        project.config = cls.merge(internal, result)
        return result

    @classmethod
    def export(cls, project: Project) -> dict[str, object]:
        config = project.config or {}
        policy = DeploymentPolicyService.from_project(project)
        return {
            "version": "1",
            "build": {
                "strategy": config.get("build_strategy") or "zero-config",
                "preset": config.get("preset") or "",
                "runner": config.get("runner") or config.get("image") or "",
                "rootDirectory": config.get("root_directory") or "",
                "dockerfile": config.get("dockerfile_path") or "Dockerfile",
                "buildCommand": config.get("build_command") or "",
                "preDeployCommand": config.get("pre_deploy_command") or "",
                "startCommand": config.get("start_command") or "",
            },
            "resources": {
                "cpus": config.get("cpus"),
                "memoryMb": config.get("memory"),
            },
            "deployment": {
                "webhookEnabled": policy.webhook_enabled,
                "allowedBranches": list(policy.allowed_branches),
                "ignoredBranches": list(policy.ignored_branches),
                "ignoredAuthors": list(policy.ignored_authors),
                "skipMessageTokens": list(policy.skip_message_tokens),
                "maxConcurrent": policy.max_concurrent,
                "supersedeOlder": policy.supersede_older,
            },
        }
