"""Emploi du temps : import ICS (URL ADE ou fichier), cache local, expansion des récurrences,
déduction du type (CM/TD/TP), enseignant, et correspondance intitulé ADE → matière."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import icalendar
import recurring_ical_events

from . import config, db
from .textutils import normalize_spaces

log = logging.getLogger(__name__)


@dataclass
class Slot:
    uid: str
    start: datetime
    end: datetime
    summary: str
    location: str = ""
    description: str = ""
    teacher: str = ""
    course_type: str = ""
    subject_id: int | None = None
    subject_name: str | None = None
    is_current: bool = False
    suggested_name: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.uid}@{self.start.isoformat()}"


# --- Fuseau horaire ---------------------------------------------------------------------

def local_tz() -> ZoneInfo:
    try:
        return ZoneInfo(db.get_setting("timezone") or "Europe/Paris")
    except Exception:  # fuseau invalide saisi dans les paramètres
        return ZoneInfo("Europe/Paris")


def now_local() -> datetime:
    return datetime.now(local_tz())


# --- Chargement / cache -----------------------------------------------------------------

_cache_lock = threading.Lock()
_parsed: dict = {"stamp": None, "cal": None}


def _parse(data: bytes) -> icalendar.Calendar:
    cal = icalendar.Calendar.from_ical(data)
    try:  # calendriers utilisant X-WR-TIMEZONE (Google, certains ADE)
        import x_wr_timezone

        cal = x_wr_timezone.to_standard(cal)
    except Exception:
        pass
    return cal


def validate_ics(data: bytes) -> int:
    """Vérifie qu'il s'agit d'un ICS lisible ; renvoie le nombre d'événements."""
    if b"BEGIN:VCALENDAR" not in data[:2000].upper():
        raise ValueError("Le contenu reçu n'est pas un fichier iCalendar (.ics).")
    cal = _parse(data)
    return sum(1 for c in cal.walk() if c.name == "VEVENT")


def save_calendar(data: bytes, source: str) -> int:
    n = validate_ics(data)
    config.ensure_dirs()
    tmp = config.CALENDAR_CACHE.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(config.CALENDAR_CACHE)
    db.set_setting("ics_last_refresh", db.now_iso())
    db.set_setting("ics_source", source)
    db.set_setting("ics_last_error", "")
    return n


def refresh_from_url(url: str | None = None, timeout: float = 30.0) -> tuple[bool, str]:
    url = (url if url is not None else db.get_setting("ics_url")).strip()
    if not url:
        return False, "Aucune URL ICS configurée."
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        resp.raise_for_status()
        n = save_calendar(resp.content, "url")
        return True, f"Emploi du temps mis à jour ({n} événements)."
    except Exception as exc:  # hors ligne, URL invalide… on garde le cache
        msg = f"Échec du rafraîchissement : {exc}"
        db.set_setting("ics_last_error", msg)
        log.warning(msg)
        return False, msg + " (dernier emploi du temps connu conservé)"


def refresh_in_background(min_age_minutes: int = 0) -> None:
    """Rafraîchit l'EDT en tâche de fond si une URL est configurée et que le cache est ancien."""
    if not db.get_setting("ics_url").strip():
        return
    last = db.get_setting("ics_last_refresh")
    if last and min_age_minutes:
        try:
            if datetime.now().astimezone() - datetime.fromisoformat(last) < timedelta(minutes=min_age_minutes):
                return
        except ValueError:
            pass
    threading.Thread(target=refresh_from_url, name="ics-refresh", daemon=True).start()


def load_calendar() -> icalendar.Calendar | None:
    path = config.CALENDAR_CACHE
    if not path.exists():
        return None
    st = path.stat()
    stamp = (st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _parsed["stamp"] != stamp:
            try:
                _parsed["cal"] = _parse(path.read_bytes())
            except Exception as exc:
                log.error("ICS en cache illisible : %s", exc)
                _parsed["cal"] = None
            _parsed["stamp"] = stamp
        return _parsed["cal"]


# --- Déductions à partir de l'intitulé ADE ----------------------------------------------

_TYPE_RE = [
    ("TP", re.compile(r"\bTP\s*\d*\b|travaux\s+pratiques", re.I)),
    ("TD", re.compile(r"\bTD\s*\d*\b|travaux\s+dirig", re.I)),
    ("CM", re.compile(r"\bCM\s*\d*\b|cours\s+magistra|\bcours\b|\bamphi\b", re.I)),
]


def detect_type(summary: str) -> str:
    """CM/TD/TP d'après l'intitulé ('' si rien trouvé). En cas de doublon, le premier mot l'emporte."""
    best: tuple[int, str] | None = None
    for kind, rx in _TYPE_RE:
        m = rx.search(summary or "")
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), kind)
    return best[1] if best else ""


_NAME_LINE = re.compile(
    r"^[A-ZÀ-ÖØ-Þ][A-ZÀ-ÖØ-Þ'’\- ]*[A-ZÀ-ÖØ-Þ]\s+[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’\-]+(?:[ \-][A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’\-]+)*$"
)


def extract_teacher(description: str) -> str:
    """ADE place souvent les enseignants dans DESCRIPTION sous la forme « NOM Prénom »."""
    names: list[str] = []
    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line or any(ch.isdigit() for ch in line) or "export" in line.lower() or line.startswith("("):
            continue
        if _NAME_LINE.match(line) and line not in names:
            names.append(line)
    return ", ".join(names)


_NOISE_RE = [
    re.compile(r"\b(CM|TD|TP)\s*\d*\b", re.I),
    re.compile(r"\bG(?:r(?:ou)?p(?:e)?)?\s*\.?\s*\d+[A-Za-z]?\b", re.I),
    re.compile(r"\b(cours\s+magistral|travaux\s+dirig[ée]s|travaux\s+pratiques)\b", re.I),
    re.compile(r"\((?:[^()]*\d[^()]*)\)"),
]


def suggest_subject_name(summary: str) -> str:
    text = summary or ""
    for rx in _NOISE_RE:
        text = rx.sub(" ", text)
    text = re.sub(r"\s*[-–—:/|_]+\s*$", "", normalize_spaces(text))
    text = re.sub(r"^\s*[-–—:/|_]+\s*", "", text)
    return normalize_spaces(text) or normalize_spaces(summary)


def _norm(s: str) -> str:
    return normalize_spaces(s).casefold()


def match_subject(summary: str, mappings: list[dict]) -> int | None:
    """Correspondance exacte (insensible à la casse/espaces) prioritaire, puis regex."""
    target = _norm(summary)
    for m in mappings:
        if not m["is_regex"] and _norm(m["pattern"]) == target:
            return m["subject_id"]
    for m in mappings:
        if m["is_regex"]:
            try:
                if re.search(m["pattern"], summary or "", re.I):
                    return m["subject_id"]
            except re.error:
                continue
    return None


# --- Créneaux ----------------------------------------------------------------------------

def _to_local(value, tz: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return None  # événement « journée entière » : ignoré


def slots_between(start_day: date, end_day: date) -> list[Slot]:
    """Créneaux (heure locale) dont le début tombe dans [start_day, end_day]."""
    cal = load_calendar()
    if cal is None:
        return []
    tz = local_tz()
    # Fenêtre élargie : les fuseaux décalent les bornes ; on filtre ensuite sur la date locale.
    raw = recurring_ical_events.of(cal).between(start_day - timedelta(days=1), end_day + timedelta(days=2))
    mappings = db.list_mappings()
    subjects = {s["id"]: s for s in db.list_subjects()}
    slots: list[Slot] = []
    for ev in raw:
        start = _to_local(ev.get("DTSTART").dt if ev.get("DTSTART") else None, tz)
        if start is None or not (start_day <= start.date() <= end_day):
            continue
        end = None
        if ev.get("DTEND") is not None:
            end = _to_local(ev.get("DTEND").dt, tz)
        elif ev.get("DURATION") is not None:
            end = start + ev.get("DURATION").dt
        end = end or start + timedelta(hours=1)
        summary = normalize_spaces(str(ev.get("SUMMARY", "")))
        description = str(ev.get("DESCRIPTION", "") or "")
        sid = match_subject(summary, mappings)
        slots.append(
            Slot(
                uid=str(ev.get("UID", "")) or f"{summary}-{start.isoformat()}",
                start=start,
                end=end,
                summary=summary,
                location=normalize_spaces(str(ev.get("LOCATION", "") or "")),
                description=description.strip(),
                teacher=extract_teacher(description),
                course_type=detect_type(summary),
                subject_id=sid,
                subject_name=subjects[sid]["name"] if sid in subjects else None,
                suggested_name=suggest_subject_name(summary),
            )
        )
    slots.sort(key=lambda s: (s.start, s.summary))
    return slots


def slots_for_day(day: date) -> list[Slot]:
    slots = slots_between(day, day)
    now = now_local()
    if day == now.date():
        current = pick_current(slots, now)
        if current is not None:
            current.is_current = True
    return slots


def pick_current(slots: list[Slot], now: datetime) -> Slot | None:
    """Créneau en cours (10 min de marge avant le début), sinon le prochain qui commence dans 30 min."""
    for s in slots:
        if s.start - timedelta(minutes=10) <= now < s.end:
            return s
    upcoming = [s for s in slots if now < s.start <= now + timedelta(minutes=30)]
    return upcoming[0] if upcoming else None


def find_slot(key: str, around: date) -> Slot | None:
    for s in slots_between(around - timedelta(days=1), around + timedelta(days=1)):
        if s.key == key:
            return s
    return None
