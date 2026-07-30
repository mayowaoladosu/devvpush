"""Standard-library HTTP and credential storage for the LayerRail CLI."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class LayerRailClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class CLIConfig:
    url: str
    token: str


class ConfigStore:
    @staticmethod
    def path() -> Path:
        if os.name == "nt" and os.getenv("APPDATA"):
            return Path(os.environ["APPDATA"]) / "LayerRail" / "config.json"
        root = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
        return root / "layerrail" / "config.json"

    @classmethod
    def load(cls) -> CLIConfig:
        token = os.getenv("LAYERRAIL_TOKEN")
        url = os.getenv("LAYERRAIL_URL")
        if token and url:
            return CLIConfig(url=url.rstrip("/"), token=token)
        path = cls.path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LayerRailClientError(
                "LayerRail is not configured. Run `layerrail config set`."
            ) from exc
        token = str(payload.get("token") or "")
        url = str(payload.get("url") or "").rstrip("/")
        if not token or not url:
            raise LayerRailClientError("Stored LayerRail configuration is invalid.")
        return CLIConfig(url=url, token=token)

    @classmethod
    def save(cls, *, url: str, token: str) -> Path:
        if not url.startswith(("http://", "https://")):
            raise LayerRailClientError("LayerRail URL must use http:// or https://.")
        if not token.startswith(("lr_live_", "lr_test_")):
            raise LayerRailClientError("LayerRail token format is invalid.")
        path = cls.path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"url": url.rstrip("/"), "token": token}, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        temporary.replace(path)
        return path


class LayerRailClient:
    def __init__(self, config: CLIConfig, *, timeout: int = 30):
        self.base_url = config.url.rstrip("/") + "/api/v1"
        self.token = config.token
        self.timeout = max(1, timeout)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = self.base_url + "/" + path.lstrip("/")
        if query:
            clean_query = {key: value for key, value in query.items() if value is not None}
            if clean_query:
                url += "?" + urlencode(clean_query)
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "LayerRail-CLI/1.0",
        }
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return json.loads(body) if body else None
        except HTTPError as exc:
            body = exc.read()
            try:
                detail = json.loads(body).get("detail")
            except (ValueError, AttributeError):
                detail = None
            raise LayerRailClientError(
                str(detail or f"LayerRail returned HTTP {exc.code}."),
                status=exc.code,
            ) from exc
        except URLError as exc:
            raise LayerRailClientError("LayerRail could not be reached.") from exc
