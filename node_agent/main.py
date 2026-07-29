"""Authenticated DevPush remote deployment node agent."""

from __future__ import annotations

import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from docker_runtime import DockerRuntime, NodeDockerError
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

PROTOCOL_VERSION = 1


class AgentSettings:
    def __init__(self):
        self.token = os.environ.get("NODE_AGENT_TOKEN", "")
        self.agent_id = os.environ.get("NODE_AGENT_ID", "node")
        self.runtime_host = os.environ.get("NODE_AGENT_RUNTIME_HOST", "")
        self.runtime_scheme = os.environ.get("NODE_AGENT_RUNTIME_SCHEME", "http")
        self.port_min = int(os.environ.get("NODE_AGENT_PORT_MIN", "30000"))
        self.port_max = int(os.environ.get("NODE_AGENT_PORT_MAX", "39999"))
        self.max_deployments = int(
            os.environ.get("NODE_AGENT_MAX_DEPLOYMENTS", "20")
        )
        self.data_dir = Path(os.environ.get("NODE_AGENT_DATA_DIR", "/data"))
        self.host_data_dir = Path(
            os.environ.get("NODE_AGENT_HOST_DATA_DIR", "/var/lib/devpush-node")
        )
        self.docker_socket = os.environ.get(
            "NODE_AGENT_DOCKER_SOCKET", "/var/run/docker.sock"
        )
        self.max_image_bytes = int(
            os.environ.get("NODE_AGENT_MAX_IMAGE_BYTES", str(2 * 1024**3))
        )
        self.allowed_image_prefixes = tuple(
            value.strip()
            for value in os.environ.get(
                "NODE_AGENT_ALLOWED_IMAGE_PREFIXES",
                "ghcr.io/devpushhq/runner-",
            ).split(",")
            if value.strip()
        )
        self.validate()

    def validate(self) -> None:
        if len(self.token) < 32:
            raise RuntimeError("NODE_AGENT_TOKEN must contain at least 32 characters.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,252}", self.runtime_host):
            raise RuntimeError("NODE_AGENT_RUNTIME_HOST is invalid.")
        if self.runtime_scheme != "http":
            raise RuntimeError(
                "NODE_AGENT_RUNTIME_SCHEME must be http; central Traefik terminates TLS."
            )
        if not 1024 <= self.port_min <= self.port_max <= 65535:
            raise RuntimeError("Node runtime port range is invalid.")
        if not 1 <= self.max_deployments <= 10_000:
            raise RuntimeError("NODE_AGENT_MAX_DEPLOYMENTS is invalid.")
        if not 1024**2 <= self.max_image_bytes <= 16 * 1024**3:
            raise RuntimeError("NODE_AGENT_MAX_IMAGE_BYTES is invalid.")


class ImageRequest(BaseModel):
    image: str = Field(min_length=1, max_length=512)


class CacheRequest(BaseModel):
    project_id: str
    environment_id: str
    namespace: str
    generation: int = Field(ge=1, le=2_147_483_647)


class RuntimeCreateRequest(BaseModel):
    deployment_id: str
    project_id: str
    environment_id: str
    branch: str = Field(default="", max_length=255)
    node_id: str
    node_capacity: int = Field(ge=1, le=10_000)
    image: str = Field(min_length=1, max_length=512)
    environment: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    working_dir: str | None = None
    uses_dockerfile: bool = False
    cpus: float | None = Field(default=None, gt=0, le=256)
    memory_mb: int | None = Field(default=None, ge=16, le=1_048_576)
    pids_limit: int = Field(default=512, ge=16, le=4096)
    storage_ids: list[str] = Field(default_factory=list, max_length=16)
    cache: CacheRequest | None = None

    @field_validator("deployment_id", "project_id", "node_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", value):
            raise ValueError("identifier is invalid")
        return value


settings = AgentSettings()


async def authorize(authorization: str | None = Header(default=None)) -> None:
    expected = f"Bearer {settings.token}"
    if not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Node authorization failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.runtime = DockerRuntime(
        socket_path=settings.docker_socket,
        runtime_host=settings.runtime_host,
        runtime_scheme=settings.runtime_scheme,
        port_min=settings.port_min,
        port_max=settings.port_max,
        data_dir=settings.data_dir,
        host_data_dir=settings.host_data_dir,
        max_image_bytes=settings.max_image_bytes,
    )
    try:
        yield
    finally:
        await app.state.runtime.close()


app = FastAPI(
    title="DevPush Node Agent",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(NodeDockerError)
async def node_docker_error(request: Request, exc: NodeDockerError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/v1/health", dependencies=[Depends(authorize)])
async def health(request: Request):
    docker = await request.app.state.runtime.health()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "agent_id": settings.agent_id,
        "runtime_host": settings.runtime_host,
        "runtime_scheme": settings.runtime_scheme,
        "port_min": settings.port_min,
        "port_max": settings.port_max,
        "max_deployments": settings.max_deployments,
        **docker,
    }


@app.post("/v1/images/inspect", dependencies=[Depends(authorize)])
async def inspect_image(payload: ImageRequest, request: Request):
    return await request.app.state.runtime.inspect_image(payload.image)


@app.post("/v1/images/pull", dependencies=[Depends(authorize)])
async def pull_image(payload: ImageRequest, request: Request):
    await request.app.state.runtime.pull_image(
        payload.image, settings.allowed_image_prefixes
    )
    return {"status": "ready"}


@app.post("/v1/images/load", dependencies=[Depends(authorize)])
async def load_image(
    request: Request,
    image_reference: str = Query(..., min_length=1, max_length=512),
    deployment_id: str = Query(..., pattern=r"^[a-f0-9]{32}$"),
):
    content_length = int(request.headers.get("content-length") or 0)

    async def chunks():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > settings.max_image_bytes:
                raise NodeDockerError("Image archive exceeds the configured limit.")
            yield chunk

    await request.app.state.runtime.load_image(
        chunks(),
        content_length=content_length,
        image_reference=image_reference,
        deployment_id=deployment_id,
    )
    return {"status": "ready", "image": image_reference}


@app.delete("/v1/images", dependencies=[Depends(authorize)])
async def delete_image(
    request: Request,
    image: str = Query(..., min_length=1, max_length=512),
):
    removed = await request.app.state.runtime.delete_image(image)
    return {"removed": removed}


@app.post("/v1/runtimes", dependencies=[Depends(authorize)])
async def create_runtime(payload: RuntimeCreateRequest, request: Request):
    return await request.app.state.runtime.create_runtime(
        payload.model_dump(),
        max_deployments=min(
            settings.max_deployments,
            payload.node_capacity,
        ),
    )


@app.post(
    "/v1/runtimes/{deployment_id}/start",
    dependencies=[Depends(authorize)],
)
async def start_runtime(deployment_id: str, request: Request):
    return await request.app.state.runtime.start_runtime(deployment_id)


@app.get(
    "/v1/runtimes/{deployment_id}",
    dependencies=[Depends(authorize)],
)
async def inspect_runtime(deployment_id: str, request: Request):
    return await request.app.state.runtime.inspect_runtime(deployment_id)


@app.get(
    "/v1/runtimes/{deployment_id}/logs",
    dependencies=[Depends(authorize)],
)
async def runtime_logs(
    deployment_id: str,
    request: Request,
    tail: int = Query(1000, ge=1, le=5000),
):
    lines = await request.app.state.runtime.runtime_logs(deployment_id, tail=tail)
    return {"lines": lines}


@app.post(
    "/v1/runtimes/{deployment_id}/stop",
    dependencies=[Depends(authorize)],
)
async def stop_runtime(deployment_id: str, request: Request):
    await request.app.state.runtime.stop_runtime(deployment_id)
    return {"status": "stopped"}


@app.delete(
    "/v1/runtimes/{deployment_id}",
    dependencies=[Depends(authorize)],
)
async def delete_runtime(
    deployment_id: str,
    request: Request,
    image: str | None = Query(default=None, max_length=512),
):
    await request.app.state.runtime.delete_runtime(deployment_id, image=image)
    return {"status": "removed"}


@app.get("/v1/runtimes", dependencies=[Depends(authorize)])
async def list_runtimes(request: Request):
    return {"runtimes": await request.app.state.runtime.list_runtimes()}


@app.get("/v1/metrics", dependencies=[Depends(authorize)])
async def metrics(request: Request):
    return {"metrics": await request.app.state.runtime.metric_values()}
