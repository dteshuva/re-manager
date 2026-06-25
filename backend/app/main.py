from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.routers import auth, periods, portfolio

settings = get_settings()

app = FastAPI(
    title="Real Estate Portfolio Management API",
    version="0.1.0",
    description="Phase 1: schema, auth, admin-only period unlock, and computed P&L views.",
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


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
