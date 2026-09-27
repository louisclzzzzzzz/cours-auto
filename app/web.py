"""Templates Jinja2 et petits utilitaires pour les routes."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db
from .pipeline import STATUS_LABELS, SUB_LABELS, pipeline
from .textutils import fmt_duration, fmt_time, fmt_ts, fr_date, fr_short_date

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters.update(
    fr_date=fr_date,
    fr_short_date=fr_short_date,
    duration=fmt_duration,
    hhmm=fmt_time,
    ts=fmt_ts,
    status_label=lambda s: STATUS_LABELS.get(s, s),
    sub_label=lambda s: SUB_LABELS.get(s, s),
)
templates.env.globals.update(COURSE_TYPES=config.COURSE_TYPES, pipeline=pipeline)

NAV = [
    ("/", "Enregistrer"),
    ("/enregistrements", "Enregistrements"),
    ("/cours", "Cours"),
    ("/matieres", "Matières"),
    ("/parametres", "Paramètres"),
]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("level", request.query_params.get("level", "ok"))
    path = request.url.path
    ctx["nav"] = [(href, label, path == href or (href != "/" and path.startswith(href))) for href, label in NAV]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str | None = None, level: str = "ok") -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + urlencode({"msg": msg, "level": level})
    return RedirectResponse(url, status_code=303)


def consent_reminder_visible() -> bool:
    return not db.get_bool_setting("consent_reminder_dismissed")
