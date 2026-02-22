"""
VERA FastAPI application — lender-facing NEPA risk tool.

Run from repo root: uvicorn backend.main:app --reload
"""

import threading
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes import global_router, router as api_router
from backend.api.dashboard import router as dashboard_router
from backend.api.radar import router as radar_router
from backend.api.semantic import router as semantic_router
from backend.api import dashboard as _dash

log = logging.getLogger("vera.startup")


def _prewarm_dashboard_cache() -> None:
    """Run in a background thread at startup to fill the dashboard cache."""
    try:
        log.info("Pre-warming dashboard cache…")
        _dash.dashboard_stats()
        _dash.dashboard_by_state()
        _dash.dashboard_by_agency()
        log.info("Dashboard cache warm — subsequent requests will be instant.")
    except Exception as exc:
        log.warning("Dashboard cache pre-warm failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    thread = threading.Thread(target=_prewarm_dashboard_cache, daemon=True)
    thread.start()
    yield


app = FastAPI(
    title="VERA",
    description="Verified Environmental Review & Attestation — NEPA compliance risk tool for lenders",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api")
app.include_router(global_router, prefix="/api")
app.include_router(dashboard_router, prefix="/api")
app.include_router(radar_router, prefix="/api")
app.include_router(semantic_router, prefix="/api")

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
