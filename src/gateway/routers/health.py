"""Probes and metrics endpoint.

/healthz (liveness): the process is up and serving. Never checks dependencies; otherwise
    a database outage would make Kubernetes restart every healthy gateway pod.
/readyz (readiness): can we do useful work right now? Checks the database. Not ready means
    "take me out of the load balancer", not "restart me". Ollama is deliberately not
    checked: one slow backend shouldn't take the whole gateway (and other backends) offline.
"""

import asyncio

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from gateway.db import Database

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    db: Database = request.app.state.db
    try:
        async with asyncio.timeout(2), db.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # any failure means "not ready"; the reason goes in the body
        return JSONResponse({"status": "not ready", "database": type(exc).__name__}, 503)
    return JSONResponse({"status": "ready", "database": "ok"})


@router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
