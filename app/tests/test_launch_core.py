import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from config import Settings
from models import ApiToken
from routers.api import ProjectCreateRequest, _authorize_repository_installation
from services.api_tokens import ApiTokenError, ApiTokenService
from services.audit import AuditService
from services.deployment_policy import DeploymentPolicyService
from services.notifications import (
    NotificationConfigurationError,
    NotificationService,
)
from services.project_config import ProjectConfigError, ProjectConfigService
from services.rate_limit import RateLimiter


class Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeDB:
    def __init__(self, result=None):
        self.items = []
        self.result = result
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    def add(self, item):
        self.items.append(item)

    async def execute(self, query):
        return Result(self.result)


class LaunchCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_token_is_returned_once_and_only_digest_is_persisted(self):
        db = FakeDB()
        team = SimpleNamespace(id="a" * 32)
        user = SimpleNamespace(id=7)
        with patch("services.api_tokens.secrets.token_urlsafe") as token_urlsafe:
            token_urlsafe.side_effect = ["prefix12", "s" * 43]
            token, raw = await ApiTokenService.create(
                db,
                team=team,
                user=user,
                name="CI deploys",
                scopes=["deployments:write", "deployments:read"],
                expires_in_days=30,
                mode="test",
            )
        self.assertEqual("lr_test_prefix12_" + "s" * 43, raw)
        self.assertNotIn(raw, token.token_hash)
        self.assertEqual(ApiTokenService.digest(raw), token.token_hash)
        self.assertEqual(["deployments:read", "deployments:write"], token.scopes)
        db.commit.assert_awaited_once()

    async def test_api_token_rejects_an_explicit_empty_scope_list(self):
        db = FakeDB()
        with self.assertRaisesRegex(ApiTokenError, "at least one valid API scope"):
            await ApiTokenService.create(
                db,
                team=SimpleNamespace(id="a" * 32),
                user=SimpleNamespace(id=7),
                name="No access",
                scopes=[],
            )
        self.assertEqual([], db.items)
        db.commit.assert_not_awaited()

    async def test_api_token_auth_rejects_revoked_or_expired_tokens(self):
        token = ApiToken(
            id="b" * 32,
            team_id="a" * 32,
            name="revoked",
            prefix="prefix12",
            token_hash="x" * 64,
            scopes=["projects:read"],
            revoked_at=SimpleNamespace(),
        )
        db = FakeDB(result=token)
        raw = "lr_test_prefix12_" + "s" * 43
        token.token_hash = ApiTokenService.digest(raw)
        with self.assertRaisesRegex(ApiTokenError, "invalid or expired"):
            await ApiTokenService.authenticate(db, raw)

    def test_audit_redaction_removes_nested_secret_values(self):
        redacted = AuditService.redact(
            {
                "branch": "main",
                "authorization": "Bearer secret",
                "nested": {"api_key": "sensitive", "count": 2},
            }
        )
        self.assertEqual("main", redacted["branch"])
        self.assertEqual("[redacted]", redacted["authorization"])
        self.assertEqual("[redacted]", redacted["nested"]["api_key"])
        self.assertEqual(2, redacted["nested"]["count"])

    def test_deployment_policy_filters_branch_author_and_message(self):
        project = SimpleNamespace(
            config={
                "deployment_policy": {
                    "allowed_branches": ["main", "release/*"],
                    "ignored_branches": ["release/wip-*"],
                    "ignored_authors": ["dependabot[bot]"],
                    "skip_message_tokens": ["[skip layerrail]"],
                    "max_concurrent": 2,
                }
            }
        )
        policy = DeploymentPolicyService.from_project(project)
        self.assertTrue(
            policy.decide(branch="release/v1", author="mayowa", message="ship").allowed
        )
        self.assertFalse(
            policy.decide(
                branch="release/wip-a",
                author="mayowa",
                message="ship",
            ).allowed
        )
        self.assertFalse(
            policy.decide(
                branch="main",
                author="DEPENDABOT[BOT]",
                message="update",
            ).allowed
        )
        self.assertFalse(
            policy.decide(
                branch="main",
                author="mayowa",
                message="docs [skip layerrail]",
            ).allowed
        )
        self.assertEqual(2, policy.max_concurrent)

    def test_project_config_parses_and_exports_public_contract(self):
        document = {
            "version": "1",
            "build": {
                "strategy": "dockerfile",
                "rootDirectory": "apps/api",
                "dockerfile": "deploy/Dockerfile",
            },
            "resources": {"cpus": 2, "memoryMb": 1024},
            "deployment": {
                "allowedBranches": ["main"],
                "maxConcurrent": 3,
                "supersedeOlder": True,
            },
        }
        result = ProjectConfigService.parse(json.dumps(document))
        self.assertEqual("dockerfile", result.values["build_strategy"])
        self.assertEqual("apps/api", result.values["root_directory"])
        self.assertEqual(3, result.values["deployment_policy"]["max_concurrent"])
        project = SimpleNamespace(config=result.values)
        exported = ProjectConfigService.export(project)
        self.assertEqual("dockerfile", exported["build"]["strategy"])
        self.assertEqual(3, exported["deployment"]["maxConcurrent"])

    def test_project_config_rejects_escape_and_unknown_keys(self):
        with self.assertRaises(ProjectConfigError):
            ProjectConfigService.parse(
                json.dumps(
                    {
                        "version": "1",
                        "build": {"rootDirectory": "../secrets"},
                    }
                )
            )
        with self.assertRaises(ProjectConfigError):
            ProjectConfigService.parse(json.dumps({"version": "1", "secrets": {}}))

    def test_project_config_runtime_validation_matches_schema_types(self):
        invalid_documents = (
            {"version": "1", "build": []},
            {"version": "1", "build": {"runner": 24}},
            {"version": "1", "resources": {"cpus": True}},
            {"version": "1", "resources": {"memoryMb": 1024.5}},
            {"version": "1", "deployment": {"allowedBranches": "main"}},
            {"version": "1", "deployment": {"maxConcurrent": 1.5}},
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(ProjectConfigError):
                    ProjectConfigService.parse(json.dumps(document))

        result = ProjectConfigService.parse(
            json.dumps({"version": "1", "resources": None})
        )
        self.assertEqual({}, result.values)

    def test_project_config_put_replaces_public_values_only(self):
        project = SimpleNamespace(
            config={
                "runner": "python-3.12",
                "build_command": "old build",
                "deployment_policy": {"max_concurrent": 5},
                "dependency_cache": True,
                "dependency_cache_generation": 3,
            }
        )
        ProjectConfigService.apply_to_project(
            project,
            json.dumps(
                {
                    "version": "1",
                    "build": {"strategy": "dockerfile"},
                }
            ),
        )
        self.assertEqual("dockerfile", project.config["build_strategy"])
        self.assertEqual("api", project.config["layerrail_config_source"])
        self.assertNotIn("runner", project.config)
        self.assertNotIn("build_command", project.config)
        self.assertNotIn("deployment_policy", project.config)
        self.assertTrue(project.config["dependency_cache"])
        self.assertEqual(3, project.config["dependency_cache_generation"])

    async def test_rate_limiter_returns_retry_after_from_atomic_result(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value=[6, 42]))
        result = await RateLimiter.check(
            redis,
            bucket="api",
            identity="token",
            limit=5,
            window_seconds=60,
        )
        self.assertFalse(result.allowed)
        self.assertEqual(0, result.remaining)
        self.assertEqual(42, result.retry_after)

    async def test_webhook_policy_rejects_private_production_target(self):
        settings = Settings(env="production")
        with patch.object(NotificationService, "_resolve", return_value={"127.0.0.1"}):
            with self.assertRaisesRegex(
                NotificationConfigurationError,
                "public addresses",
            ):
                await NotificationService.validate_url(
                    "https://hooks.example.com/layerrail",
                    settings,
                )

    async def test_webhook_delivery_pins_the_validated_address(self):
        settings = Settings(env="production")
        with patch.object(
            NotificationService,
            "_resolve",
            return_value={"93.184.216.34"},
        ):
            raw, parsed, addresses = await NotificationService.resolve_url(
                "https://hooks.example.com:8443/layerrail?source=test",
                settings,
            )
        self.assertEqual(
            "https://hooks.example.com:8443/layerrail?source=test",
            raw,
        )
        self.assertEqual(("93.184.216.34",), addresses)
        self.assertEqual(
            "https://93.184.216.34:8443/layerrail?source=test",
            NotificationService.pinned_url(parsed, addresses[0]),
        )
        self.assertEqual("hooks.example.com:8443", NotificationService.host_header(parsed))

    async def test_webhook_invalid_port_is_a_configuration_error(self):
        with self.assertRaises(NotificationConfigurationError):
            await NotificationService.validate_url(
                "https://hooks.example.com:not-a-port/layerrail",
                Settings(env="production"),
            )

    async def test_webhook_queue_outage_leaves_delivery_recoverable(self):
        queue = SimpleNamespace(
            enqueue_job=AsyncMock(side_effect=RuntimeError("redis unavailable"))
        )
        queued = await NotificationService.enqueue_webhook(queue, "delivery-id")
        self.assertFalse(queued)
        queue.enqueue_job.assert_awaited_once_with(
            "deliver_webhook",
            "delivery-id",
            _job_id="webhook:delivery-id",
        )

    async def test_api_project_creation_binds_user_and_app_installation(self):
        payload = ProjectCreateRequest(
            name="secure-project",
            repo_id=123,
            repo_full_name="layerrail/private-app",
            github_installation_id=456,
        )
        principal = SimpleNamespace(user=SimpleNamespace(status="active"))
        github_service = SimpleNamespace(
            get_repository=AsyncMock(
                return_value={"id": 123, "full_name": "layerrail/private-app"}
            ),
            get_repository_installation=AsyncMock(return_value={"id": 456}),
        )
        installation = SimpleNamespace(token="installation-token")
        installation_service = SimpleNamespace(
            get_or_refresh_installation=AsyncMock(return_value=installation)
        )
        with patch(
            "routers.api.get_user_github_token",
            new=AsyncMock(return_value="user-oauth-token"),
        ):
            result, repository = await _authorize_repository_installation(
                payload=payload,
                principal=principal,
                db=SimpleNamespace(),
                github_service=github_service,
                installation_service=installation_service,
            )
        self.assertIs(installation, result)
        self.assertEqual(123, repository["id"])
        github_service.get_repository.assert_awaited_once_with(
            "user-oauth-token",
            123,
        )
        github_service.get_repository_installation.assert_awaited_once_with(
            "layerrail/private-app"
        )

        github_service.get_repository_installation.return_value = {"id": 999}
        with patch(
            "routers.api.get_user_github_token",
            new=AsyncMock(return_value="user-oauth-token"),
        ):
            with self.assertRaises(HTTPException) as error:
                await _authorize_repository_installation(
                    payload=payload,
                    principal=principal,
                    db=SimpleNamespace(),
                    github_service=github_service,
                    installation_service=installation_service,
                )
        self.assertEqual(403, error.exception.status_code)

    def test_webhook_events_are_bounded_and_known(self):
        selected = NotificationService.normalize_events(
            ["deployment.failed", "deployment.succeeded", "unknown"]
        )
        self.assertEqual(
            ["deployment.failed", "deployment.succeeded"],
            selected,
        )


if __name__ == "__main__":
    unittest.main()
