"""Application FastAPI : pages (Jinja2 + HTMX) et API d'enregistrement. Écoute sur 127.0.0.1 uniquement."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import calendar_ics, config, db, supports
from .pipeline import pipeline
from .routes import courses, record, recordings, settings, subjects
from .web import STATIC_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    pipeline.start()
    supports.resume_pending()  # supports de cours dont la lecture a été interrompue
    calendar_ics.refresh_in_background()  # rafraîchissement de l'EDT à l'ouverture de l'app
    yield
    pipeline.stop()


app = FastAPI(title="Cours auto", lifespan=lifespan, docs_url=None, redoc_url=None)

ALLOWED_HOSTS = {
    f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}", "127.0.0.1", "localhost", "testserver",
}


@app.middleware("http")
async def local_only(request: Request, call_next):
    """Protège l'app locale : refuse les hôtes inattendus (DNS rebinding) et les requêtes
    d'écriture venant d'autres sites (CSRF)."""
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return PlainTextResponse("Hôte non autorisé.", status_code=400)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != "null" and urlparse(origin).netloc not in ALLOWED_HOSTS:
            return PlainTextResponse("Origine non autorisée.", status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
for module in (record, recordings, courses, subjects, settings):
    app.include_router(module.router)


def run() -> None:
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
