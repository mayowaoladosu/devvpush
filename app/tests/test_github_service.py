import base64
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from services.github import GitHubService


class FakeResponse:
    def __init__(self, status_code: int, *, headers=None, data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._data = data or {}
        self.request = httpx.Request("GET", "https://api.github.test/file")

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(  # noqa: TRY003
                "request failed", request=self.request, response=self
            )


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


class GitHubServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_manifest_batch_retries_rate_limit_then_decodes_file(self):
        content = '{"name": "app"}'
        client = FakeAsyncClient(
            [
                FakeResponse(429, headers={"Retry-After": "0"}),
                FakeResponse(
                    200,
                    data={
                        "type": "file",
                        "size": len(content),
                        "content": base64.b64encode(content.encode()).decode(),
                    },
                ),
            ]
        )

        with (
            patch("services.github.httpx.AsyncClient", return_value=client),
            patch("services.github.asyncio.sleep", new=AsyncMock()) as sleep,
        ):
            result = await GitHubService("", "", "", "").get_file_contents(
                "token", 1, ["package.json"]
            )

        self.assertEqual({"package.json": content}, result)
        self.assertEqual(2, client.calls)
        sleep.assert_awaited_once_with(0.1)

    async def test_manifest_batch_omits_missing_file(self):
        client = FakeAsyncClient([FakeResponse(404)])

        with patch("services.github.httpx.AsyncClient", return_value=client):
            result = await GitHubService("", "", "", "").get_file_contents(
                "token", 1, ["missing.json"]
            )

        self.assertEqual({}, result)


if __name__ == "__main__":
    unittest.main()
