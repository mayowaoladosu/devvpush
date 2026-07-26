"""Internal-only Prometheus exporter for deployment container stats."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from services.metrics_exporter import DockerMetricsExporter


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.exporter = DockerMetricsExporter()
    try:
        yield
    finally:
        await app.state.exporter.close()


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
        payload = await app.state.exporter.export()
    except Exception as error:
        raise HTTPException(status_code=503, detail="Metric collection failed") from error
    return PlainTextResponse(
        payload,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
