import unittest
from types import SimpleNamespace

from wtforms.validators import ValidationError

from forms.project import (
    validate_dockerfile_path,
    validate_runner,
    validate_start_command,
)


class ProjectBuildFormTests(unittest.TestCase):
    def form(self, strategy: str):
        return SimpleNamespace(
            build_strategy=SimpleNamespace(data=strategy),
            runner=SimpleNamespace(data=""),
            _runners=[],
        )

    def test_dockerfile_strategy_does_not_require_runner_or_start_command(self):
        form = self.form("dockerfile")

        validate_runner(form, form.runner)
        validate_start_command(form, SimpleNamespace(data=""))

    def test_zero_config_strategy_requires_runner_and_start_command(self):
        form = self.form("zero-config")

        with self.assertRaises(ValidationError):
            validate_runner(form, form.runner)
        with self.assertRaises(ValidationError):
            validate_start_command(form, SimpleNamespace(data=""))

    def test_dockerfile_path_rejects_empty_segments_and_directory_paths(self):
        form = self.form("dockerfile")

        for value in ("deploy//Dockerfile", "deploy/", "."):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_dockerfile_path(form, SimpleNamespace(data=value))

    def test_dockerfile_path_is_normalized(self):
        form = self.form("dockerfile")
        field = SimpleNamespace(data="./deploy/Dockerfile")

        validate_dockerfile_path(form, field)

        self.assertEqual("deploy/Dockerfile", field.data)


if __name__ == "__main__":
    unittest.main()
