from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import (
    auth,
    categories,
    imports,
    periods,
    portfolio,
    properties,
    records,
    settings as settings_router,
    units,
)

settings = get_settings()

app = FastAPI(
    title="Real Estate Portfolio Management API",
    version="0.1.0",
    description=(
        "Phase 4: CSV/Excel bulk import with column mapping, dry-run preview, row-level "
        "validation, and a structured-row automation seam, plus a 'what's missing' month "
        "view — on top of Phase 3 CRUD/entry and the Phase 2 aggregation endpoints."
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(periods.router)
app.include_router(portfolio.router)
app.include_router(properties.router)
app.include_router(units.router)
app.include_router(categories.router)
app.include_router(records.router)
app.include_router(imports.router)
app.include_router(settings_router.router)


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
