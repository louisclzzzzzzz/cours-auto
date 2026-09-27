"""Petites fonctions de mise en forme (dates FR, durées, slugs)."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text or "sans-titre"


def safe_filename(text: str, max_len: int = 120) -> str:
    """Nom de fichier lisible (accents conservés) sans caractères interdits."""
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:max_len] or "sans titre"


def parse_date(value: str | date | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fr_date(value: str | date | None, with_weekday: bool = True) -> str:
    d = parse_date(value)
    if d is None:
        return ""
    s = f"{d.day} {MOIS[d.month - 1]} {d.year}"
    return f"{JOURS[d.weekday()]} {s}" if with_weekday else s


def fr_short_date(value: str | date | None) -> str:
    d = parse_date(value)
    return d.strftime("%d/%m/%Y") if d else ""


def fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def fmt_ts(seconds: float | None) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_time(value: str | datetime | None) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%H:%M")


def normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()
