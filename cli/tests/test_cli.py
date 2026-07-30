import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from layerrail_cli.client import CLIConfig, ConfigStore, LayerRailClient
from layerrail_cli.main import parser


class Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return self.payload


class LayerRailCLITests(unittest.TestCase):
    def test_client_sends_bearer_token_to_versioned_api(self):
        seen = {}

        def open_request(request, timeout):
            seen["url"] = request.full_url
            seen["authorization"] = request.headers["Authorization"]
            seen["timeout"] = timeout
            return Response({"status": "ok"})

        client = LayerRailClient(
            CLIConfig("https://deploy.layerrail.com", "lr_live_prefix12_" + "s" * 43)
        )
        with patch("layerrail_cli.client.urlopen", side_effect=open_request):
            result = client.request("GET", "whoami")

        self.assertEqual({"status": "ok"}, result)
        self.assertEqual(
            "https://deploy.layerrail.com/api/v1/whoami",
            seen["url"],
        )
        self.assertTrue(seen["authorization"].startswith("Bearer lr_live_"))

    def test_config_store_never_writes_token_to_stdout_contract(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "config.json"
            with patch.object(ConfigStore, "path", return_value=path):
                ConfigStore.save(
                    url="https://deploy.layerrail.com/",
                    token="lr_test_prefix12_" + "s" * 43,
                )
                loaded = ConfigStore.load()
        self.assertEqual("https://deploy.layerrail.com", loaded.url)
        self.assertTrue(loaded.token.startswith("lr_test_"))

    def test_deploy_command_accepts_branch_and_commit(self):
        args = parser().parse_args(
            [
                "deployments",
                "create",
                "project-id",
                "--branch",
                "main",
                "--commit",
                "a" * 40,
            ]
        )
        self.assertEqual("create", args.deployments_command)
        self.assertEqual("main", args.branch)
        self.assertEqual("a" * 40, args.commit)


if __name__ == "__main__":
    unittest.main()
