"""Configuration : chemins, secrets (.env) et paramètres modifiables (stockés en base)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

HOST = "127.0.0.1"  # jamais exposé sur le réseau
PORT = int(os.environ.get("PORT", "8000"))

DATA_DIR = Path(os.environ.get("COURS_DATA_DIR", BASE_DIR / "data"))
RECORDINGS_DIR = DATA_DIR / "recordings"
SUBJECTS_DIR = DATA_DIR / "subjects"
DB_PATH = DATA_DIR / "app.sqlite3"
CALENDAR_CACHE = DATA_DIR / "calendar.ics"

CREDENTIALS_FILE = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", BASE_DIR / "credentials.json"))
TOKEN_FILE = Path(os.environ.get("GOOGLE_TOKEN_FILE", BASE_DIR / "token.json"))

COURSE_TYPES = ("CM", "TD", "TP")


def mistral_api_key() -> str:
    return os.environ.get("MISTRAL_API_KEY", "").strip()


# Valeurs par défaut des paramètres éditables dans l'UI (table `settings`).
DEFAULT_SETTINGS: dict[str, str] = {
    "ics_url": "",
    "timezone": "Europe/Paris",
    "transcription_model": "voxtral-mini-latest",
    "transcription_language": "fr",
    "audio_bitrate": "48k",
    "drive_root_name": "Cours M1",
    "consent_reminder_dismissed": "0",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, RECORDINGS_DIR, SUBJECTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
