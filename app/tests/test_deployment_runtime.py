import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from services.deployment import DeploymentService


class DeploymentRuntimeEnvironmentTests(unittest.TestCase):
    def deployment(self, env_vars=None):
        project = SimpleNamespace(
            id="project-id",
            team_id="team-id",
            slug="project-slug",
            get_environment_by_id=lambda _environment_id: None,
        )
        return SimpleNamespace(
            id="deployment-id",
            project=project,
            project_id=project.id,
            env_vars=env_vars or [],
            environment={"slug": "production"},
            environment_id="prod",
            url="http://project.localhost",
            hostname="project.localhost",
            created_at=datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC),
            repo_full_name="owner/repo",
            branch="main",
            commit_sha="abc123",
            commit_meta={},
        )

    def settings(self):
        return SimpleNamespace(
            service_uid=1000,
            service_gid=1000,
            server_ip="127.0.0.1",
            url_scheme="http",
            deploy_domain="localhost",
        )

    def test_standard_container_host_and_port_are_injected(self):
        values = DeploymentService().get_runtime_env_vars(
            self.deployment(), self.settings()
        )

        self.assertEqual("8000", values["PORT"])
        self.assertEqual("0.0.0.0", values["HOST"])
        self.assertEqual("0.0.0.0", values["HOSTNAME"])

    def test_user_host_and_port_overrides_win(self):
        values = DeploymentService().get_runtime_env_vars(
            self.deployment(
                [
                    {"key": "PORT", "value": "5000"},
                    {"key": "HOST", "value": "127.0.0.1"},
                ]
            ),
            self.settings(),
        )

        self.assertEqual("5000", values["PORT"])
        self.assertEqual("127.0.0.1", values["HOST"])
        self.assertEqual("0.0.0.0", values["HOSTNAME"])

    def test_dockerfile_projects_do_not_require_a_runner(self):
        self.assertFalse(
            DeploymentService.requires_runner({"build_strategy": "dockerfile"})
        )
        self.assertTrue(
            DeploymentService.requires_runner({"build_strategy": "zero-config"})
        )
        self.assertTrue(DeploymentService.requires_runner({}))


if __name__ == "__main__":
    unittest.main()
