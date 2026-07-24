import hashlib
import hmac
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from routers.github import _verify_github_webhook, github_webhook


class FakeRequest:
    def __init__(self, payload, headers):
        self.payload = payload
        self.headers = headers
        self._body = json.dumps(payload, separators=(",", ":")).encode()

    async def body(self):
        return self._body

    async def json(self):
        return self.payload


class FakeDb:
    def __init__(self, projects):
        self.projects = projects
        self.rollback = AsyncMock()

    async def execute(self, _query):
        projects = self.projects

        class Scalars:
            def all(self):
                return projects

        return SimpleNamespace(scalars=lambda: Scalars())


class GitHubWebhookSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def signed_request(self, *, delivery_id="delivery-1"):
        payload = {"action": "created"}
        secret = "webhook-secret"
        request = FakeRequest(
            payload,
            {
                "X-Hub-Signature-256": "",
                "X-GitHub-Event": "installation",
                "X-GitHub-Delivery": delivery_id,
            },
        )
        request.headers["X-Hub-Signature-256"] = (
            "sha256=" + hmac.new(secret.encode(), request._body, hashlib.sha256).hexdigest()
        )
        return request, SimpleNamespace(github_app_webhook_secret=secret)

    async def test_webhook_requires_delivery_id_for_replay_safety(self):
        request, settings = self.signed_request(delivery_id="")

        with self.assertRaises(HTTPException) as raised:
            await _verify_github_webhook(request, settings)

        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("Invalid delivery ID", raised.exception.detail)

    async def test_signed_webhook_with_delivery_id_is_accepted(self):
        request, settings = self.signed_request()

        payload, event = await _verify_github_webhook(request, settings)

        self.assertEqual({"action": "created"}, payload)
        self.assertEqual("installation", event)

    async def test_partial_schedule_failure_requests_github_redelivery(self):
        projects = [
            SimpleNamespace(id="project-1", name="first"),
            SimpleNamespace(id="project-2", name="second"),
        ]
        db = FakeDb(projects)
        request = SimpleNamespace(headers={"X-GitHub-Delivery": "delivery-1"})
        payload = {
            "ref": "refs/heads/main",
            "after": "a" * 40,
            "repository": {"id": 123},
            "pusher": {"name": "octocat"},
            "head_commit": {
                "message": "rapid push",
                "timestamp": "2026-07-24T12:00:00Z",
            },
        }
        schedule = AsyncMock(
            side_effect=[SimpleNamespace(id="deployment-1"), RuntimeError("busy")]
        )

        with patch("routers.github.DeploymentService.schedule", schedule):
            response = await github_webhook(
                request=request,
                webhook_data=(payload, "push"),
                db=db,
                redis_client=SimpleNamespace(),
                queue=SimpleNamespace(),
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual(2, schedule.await_count)
        db.rollback.assert_awaited_once()
        for call in schedule.await_args_list:
            self.assertEqual("delivery-1", call.kwargs["commit"]["provider_event_id"])


if __name__ == "__main__":
    unittest.main()
