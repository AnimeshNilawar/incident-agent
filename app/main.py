from contextlib import asynccontextmanager

from fastapi import FastAPI

import app.models
import app.models.incident  # Registers the model with Base.metadata
from app.api.grafana import router as grafana_router
from app.api.incidents import router as incidents_router
from app.api.internal import router as internal_router
from app.api.worker import router as worker_router
from app.config import settings
from app.db.base import Base
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Convenient for local development only.
    # Production schema changes will use Alembic migrations.
    if settings.environment == "local":
        Base.metadata.create_all(bind=engine)

    yield


app = FastAPI(
    title="Incident Agent",
    description="Automated incident investigation",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(incidents_router)
app.include_router(grafana_router)
app.include_router(worker_router)
app.include_router(internal_router)

@app.get("/health/live")
def health_live():
    return {"status": "alive"}


@app.get("/")
def root():
    return {
        "service": "incident-agent",
        "status": "running",
    }