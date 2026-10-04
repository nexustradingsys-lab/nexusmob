import logging
import time
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request

from app.acquisition.kite import ZerodhaMarketDataProvider
from app.auth.zerodha import KiteAuthSession
from app.config import settings
from app.logging_config import configure_logging
from app.routes.mobile import router

configure_logging()
logger = logging.getLogger("nexus_mobile.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.kite_provider = None
    auth_session = KiteAuthSession(settings)
    app.state.kite_auth = auth_session
    if settings.kite_credentials_configured:
        client = auth_session.initialize()
        if client is None:
            logger.warning("Kite authentication unavailable action_required=%s", auth_session.status()["action_required"])
        else:
            try:
                provider = ZerodhaMarketDataProvider(client)
                provider.refresh_instruments()
                app.state.kite_provider = provider
                logger.info("Kite instrument master loaded count=%s", len(provider.instrument_master))
            except Exception as exc:
                logger.error("Kite initialization failed category=%s", type(exc).__name__)
    else:
        logger.warning("Kite API credentials are not configured")
    yield
    app.state.kite_provider = None


app = FastAPI(
    title="Nexus Mobile Raw Acquisition",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error("request_failed request_id=%s endpoint=%s category=%s", request_id, request.url.path, type(exc).__name__)
        raise
    response.headers["X-Request-ID"] = request_id
    stage = request.url.path.split("/")[2] if request.url.path.startswith("/mobile/stage") else "mobile"
    acquisition_status = response.headers.get("X-Acquisition-Status", "FAILED" if response.status_code >= 400 else "HTTP_OK")
    logger.info("request_complete request_id=%s endpoint=%s stage=%s acquisition_status=%s source=%s execution_id=%s status=%s duration_ms=%.2f", request_id, request.url.path, stage, acquisition_status, "Kite+NSE" if stage == "stage2" else "NSE" if stage == "stage1" else "Kite+NSE", response.headers.get("X-Execution-ID", "not-attached"), response.status_code, (time.perf_counter() - started) * 1000)
    return response
