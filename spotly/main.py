import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import available_timezones

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from . import models  # noqa: F401
from .api import check_key, router
from .config import Settings
from .db import database
from .photo_api import router as photo_router

HERE = Path(__file__).parent
COMMON = {
    "Europe/Kaliningrad": "Калининград (UTC+2)",
    "Europe/Moscow": "Москва (UTC+3)",
    "Europe/Samara": "Самара (UTC+4)",
    "Asia/Yekaterinburg": "Екатеринбург (UTC+5)",
    "Asia/Omsk": "Омск (UTC+6)",
    "Asia/Krasnoyarsk": "Красноярск (UTC+7)",
    "Asia/Irkutsk": "Иркутск (UTC+8)",
    "Asia/Yakutsk": "Якутск (UTC+9)",
    "Asia/Vladivostok": "Владивосток (UTC+10)",
    "Asia/Magadan": "Магадан (UTC+11)",
    "Asia/Kamchatka": "Камчатка (UTC+12)",
    "UTC": "Всемирное время (UTC)",
}


def build_app(settings=None):
    settings = settings or Settings()
    engine, sessions = database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        yield
        await engine.dispose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.engine, app.state.sessions = settings, engine, sessions
    app.state.image_semaphore = asyncio.Semaphore(1)
    app.include_router(router)
    app.include_router(photo_router)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    zones = list(COMMON.items()) + [
        (z, z.replace("_", " "))
        for z in sorted(available_timezones())
        if z not in COMMON and not z.startswith("Etc/")
    ]

    @app.middleware("http")
    async def security(request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlsplit

            if urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"message": "Недопустимый источник запроса"}, status_code=403)
        try:
            async with asyncio.timeout(180 if "/photos" in request.url.path else 15):
                response = await call_next(request)
        except TimeoutError:
            return JSONResponse(
                {"message": "Сервер не успел обработать запрос. Попробуйте ещё раз"},
                status_code=503,
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return '<html lang="ru"><meta charset="utf-8"><title>Spotly</title><p>Для создания мероприятия откройте вашу персональную ссылку Spotly.</p></html>'

    @app.get("/create/{key}", response_class=HTMLResponse)
    async def form(key: str, request: Request):
        check_key(key)
        return templates.TemplateResponse(
            request=request, name="form.html", context={"key": key, "zones": zones}
        )

    @app.get("/health")
    async def health():
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "revision": settings.app_revision}

    return app
