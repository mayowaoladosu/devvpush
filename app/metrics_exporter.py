"""Internal-only Prometheus exporter for deployment container stats."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from services.metrics_exporter import DockerMetricsExporter, RemoteNodeMetricsCollector


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.exporter = DockerMetricsExporter()
    app.state.node_exporter = RemoteNodeMetricsCollector()
    try:
        yield
    finally:
        await app.state.exporter.close()
        await app.state.node_exporter.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
async def health():
    try:
        healthy = await app.state.exporter.health()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Docker stats unavailable") from error
    if not healthy:
        raise HTTPException(status_code=503, detail="Docker stats unavailable")
    return {"status": "ok"}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    try:
        started = time.monotonic()
        local_values = await app.state.exporter.collect()
        remote_values, node_statuses = await app.state.node_exporter.collect()
        payload = app.state.exporter.render(
            [*local_values, *remote_values],
            scrape_duration_seconds=time.monotonic() - started,
        )
        payload += "# HELP devpush_node_metrics_up Whether a remote node metric endpoint responded.\n"
        payload += "# TYPE devpush_node_metrics_up gauge\n"
        for node_id, healthy in sorted(node_statuses.items()):
            escaped = app.state.exporter._escape_label(node_id)
            payload += f'devpush_node_metrics_up{{node_id="{escaped}"}} {1 if healthy else 0}\n'
    except Exception as error:
        raise HTTPException(status_code=503, detail="Metric collection failed") from error
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
