"""Templates Jinja2 et petits utilitaires pour les routes."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db
from .pipeline import STATUS_LABELS, SUB_LABELS, pipeline
from .textutils import fmt_duration, fmt_time, fmt_ts, fmt_when, fr_date, fr_short_date

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def static_url(path: str) -> str:
    """URL d'un fichier statique avec sa date de modification : le navigateur recharge les CSS/JS modifiés."""
    try:
        version = int((STATIC_DIR / path).stat().st_mtime)
    except OSError:
        return f"/static/{path}"
    return f"/static/{path}?v={version}"


templates.env.filters.update(
    fr_date=fr_date,
    fr_short_date=fr_short_date,
    duration=fmt_duration,
    hhmm=fmt_time,
    ts=fmt_ts,
    when=fmt_when,
    status_label=lambda s: STATUS_LABELS.get(s, s),
    sub_label=lambda s: SUB_LABELS.get(s, s),
)
templates.env.globals.update(COURSE_TYPES=config.COURSE_TYPES, pipeline=pipeline, static=static_url)

# (lien, libellé, préfixes d'URL qui rendent l'onglet actif)
NAV = [
    ("/", "Enregistrer", ()),
    ("/cours", "Cours", ("/cours", "/matieres")),
    ("/enregistrements", "Historique", ("/enregistrements",)),
    ("/parametres", "Paramètres", ("/parametres",)),
]


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("level", request.query_params.get("level", "ok"))
    path = request.url.path
    ctx["nav"] = [(href, label, path == href or any(path.startswith(p) for p in prefixes))
                  for href, label, prefixes in NAV]
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str | None = None, level: str = "ok") -> RedirectResponse:
    """Redirection 303 avec message facultatif (?msg=…) ; l'ancre éventuelle (#section) reste à la fin."""
    base, sep, anchor = url.partition("#")
    if msg:
        base += ("&" if "?" in base else "?") + urlencode({"msg": msg, "level": level})
    return RedirectResponse(base + sep + anchor, status_code=303)


def consent_reminder_visible() -> bool:
    return not db.get_bool_setting("consent_reminder_dismissed")
