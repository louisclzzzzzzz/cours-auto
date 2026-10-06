"""Accès SQLite (module sqlite3 standard). Une connexion par opération : simple et sûr entre threads."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    teachers TEXT NOT NULL DEFAULT '',
    vocabulary TEXT NOT NULL DEFAULT '[]',
    drive_folder_id TEXT, drive_folder_url TEXT,
    drive_sessions_folder_id TEXT, drive_sessions_folder_url TEXT,
    drive_full_id TEXT, drive_full_url TEXT,
    drive_gdoc_id TEXT, drive_gdoc_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS ade_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    is_regex INTEGER NOT NULL DEFAULT 0,
    subject_id INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
    course_type TEXT NOT NULL DEFAULT 'CM',
    session_number INTEGER,
    session_date TEXT NOT NULL,
    title TEXT,
    teacher TEXT NOT NULL DEFAULT '',
    event_uid TEXT, event_summary TEXT, event_start TEXT, event_end TEXT, location TEXT,
    mime_type TEXT,
    origin TEXT NOT NULL DEFAULT 'browser',
    source_filename TEXT,
    status TEXT NOT NULL DEFAULT 'recording',
    client_state TEXT,
    error_step TEXT, error_message TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    last_seen_at TEXT,
    elapsed_seconds REAL NOT NULL DEFAULT 0,
    duration_seconds REAL,
    segment_count INTEGER NOT NULL DEFAULT 0,
    drive_status TEXT NOT NULL DEFAULT 'pending', drive_error TEXT,
    drive_md_id TEXT, drive_md_url TEXT, drive_md_name TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    segment INTEGER NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (recording_id, seq)
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Supports de cours (diapositives, PDF…) joints à une séance, déposés dans Drive avec la transcription.
CREATE TABLE IF NOT EXISTS supports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_recordings_subject ON recordings(subject_id);
CREATE INDEX IF NOT EXISTS idx_logs_recording ON logs(recording_id);
CREATE INDEX IF NOT EXISTS idx_supports_recording ON supports(recording_id);
"""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# Colonnes ajoutées après la première version : ajoutées aux bases existantes au démarrage.
MIGRATIONS: dict[str, dict[str, str]] = {
    "recordings": {
        "origin": "TEXT NOT NULL DEFAULT 'browser'",  # 'browser' (enregistré dans l'app) | 'import' (fichier)
        "source_filename": "TEXT",
        # Transcription déposée dans <Matière>/Transcriptions/ de Drive
        "drive_transcription_id": "TEXT",
        "drive_transcription_url": "TEXT",
        # Mistral momentanément indisponible : date du prochain essai automatique et nombre d'essais faits
        "auto_retry_at": "TEXT",
        "auto_retry_count": "INTEGER NOT NULL DEFAULT 0",
        # Cours rédigé par la tâche Claude (drive_md_id) : date de modification de la version récupérée
        "drive_md_modified": "TEXT",
    },
    "subjects": {
        "drive_transcriptions_folder_id": "TEXT",
        "drive_transcriptions_folder_url": "TEXT",
        "drive_supports_folder_id": "TEXT",
        "drive_supports_folder_url": "TEXT",
    },
    "supports": {  # copie déposée dans <Matière>/Supports/
        "drive_id": "TEXT",
        "drive_url": "TEXT",
    },
}


def init_db() -> None:
    config.ensure_dirs()
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    _columns_cache.clear()


def q(sql: str, params: tuple | list | dict = ()) -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def q1(sql: str, params: tuple | list | dict = ()) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def run(sql: str, params: tuple | list | dict = ()) -> int:
    """Exécute une requête d'écriture ; renvoie lastrowid (INSERT) ou rowcount."""
    with get_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else cur.rowcount


_columns_cache: dict[str, set[str]] = {}


def _columns(table: str) -> set[str]:
    if table not in _columns_cache:
        with get_conn() as conn:
            _columns_cache[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    return _columns_cache[table]


def _update(table: str, row_id: int, fields: dict[str, Any]) -> None:
    if not fields:
        return
    cols = _columns(table)
    unknown = set(fields) - cols
    if unknown:
        raise ValueError(f"Colonnes inconnues pour {table}: {unknown}")
    if "updated_at" in cols:
        fields = {**fields, "updated_at": now_iso()}
    assignments = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE {table} SET {assignments} WHERE id = ?", [*fields.values(), row_id])


def _insert(table: str, fields: dict[str, Any]) -> int:
    cols = _columns(table)
    unknown = set(fields) - cols
    if unknown:
        raise ValueError(f"Colonnes inconnues pour {table}: {unknown}")
    if "created_at" in cols and "created_at" not in fields:
        fields = {**fields, "created_at": now_iso()}
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    return run(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(fields.values()))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


# --- Paramètres -------------------------------------------------------------------------

def get_setting(key: str) -> str:
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    if row is not None:
        return row["value"]
    return config.DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: str) -> None:
    run(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_bool_setting(key: str) -> bool:
    return get_setting(key).strip().lower() in ("1", "true", "on", "yes", "oui")


def get_int_setting(key: str) -> int:
    try:
        return int(get_setting(key))
    except ValueError:
        return int(config.DEFAULT_SETTINGS.get(key, "0") or 0)


# --- Matières ---------------------------------------------------------------------------

def list_subjects() -> list[dict]:
    return q("SELECT * FROM subjects ORDER BY name COLLATE NOCASE")


def get_subject(subject_id: int | None) -> dict | None:
    if subject_id is None:
        return None
    return q1("SELECT * FROM subjects WHERE id = ?", (subject_id,))


def get_subject_by_name(name: str) -> dict | None:
    return q1("SELECT * FROM subjects WHERE name = ? COLLATE NOCASE", (name.strip(),))


def create_subject(name: str, slug: str, teachers: str = "") -> int:
    return _insert("subjects", {"name": name.strip(), "slug": slug, "teachers": teachers.strip()})


def update_subject(subject_id: int, **fields: Any) -> None:
    _update("subjects", subject_id, fields)


def delete_subject(subject_id: int) -> None:
    run("DELETE FROM subjects WHERE id = ?", (subject_id,))


# --- Correspondances ADE → matière ------------------------------------------------------

def list_mappings(subject_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT m.*, s.name AS subject_name FROM ade_mappings m "
        "JOIN subjects s ON s.id = m.subject_id"
    )
    if subject_id is not None:
        return q(sql + " WHERE m.subject_id = ? ORDER BY m.id", (subject_id,))
    return q(sql + " ORDER BY m.id")


def add_mapping(pattern: str, is_regex: bool, subject_id: int) -> int:
    return _insert(
        "ade_mappings", {"pattern": pattern.strip(), "is_regex": 1 if is_regex else 0, "subject_id": subject_id}
    )


def delete_mapping(mapping_id: int) -> None:
    run("DELETE FROM ade_mappings WHERE id = ?", (mapping_id,))


# --- Enregistrements (= séances) --------------------------------------------------------

RECORDING_SELECT = (
    "SELECT r.*, s.name AS subject_name, s.slug AS subject_slug FROM recordings r "
    "LEFT JOIN subjects s ON s.id = r.subject_id"
)


def create_recording(**fields: Any) -> int:
    return _insert("recordings", fields)


def get_recording(recording_id: int) -> dict | None:
    return q1(RECORDING_SELECT + " WHERE r.id = ?", (recording_id,))


def update_recording(recording_id: int, **fields: Any) -> None:
    _update("recordings", recording_id, fields)


def list_recordings(subject_id: int | None = None, limit: int | None = None) -> list[dict]:
    sql = RECORDING_SELECT
    params: list[Any] = []
    if subject_id is not None:
        sql += " WHERE r.subject_id = ?"
        params.append(subject_id)
    sql += " ORDER BY r.started_at DESC, r.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return q(sql, params)


def list_recordings_by_status(*statuses: str) -> list[dict]:
    marks = ", ".join("?" for _ in statuses)
    return q(RECORDING_SELECT + f" WHERE r.status IN ({marks}) ORDER BY r.id", statuses)


def delete_recording(recording_id: int) -> None:
    run("DELETE FROM supports WHERE recording_id = ?", (recording_id,))
    run("DELETE FROM recordings WHERE id = ?", (recording_id,))
    run("DELETE FROM logs WHERE recording_id = ?", (recording_id,))


# --- Supports de cours --------------------------------------------------------------------

def insert_support(**fields: Any) -> int:
    return _insert("supports", fields)


def get_support(support_id: int) -> dict | None:
    return q1("SELECT * FROM supports WHERE id = ?", (support_id,))


def list_supports(recording_id: int) -> list[dict]:
    return q("SELECT * FROM supports WHERE recording_id = ? ORDER BY id", (recording_id,))


def update_support(support_id: int, **fields: Any) -> None:
    _update("supports", support_id, fields)


def delete_support(support_id: int) -> None:
    run("DELETE FROM supports WHERE id = ?", (support_id,))


def next_session_number(subject_id: int, course_type: str, exclude_id: int | None = None) -> int:
    row = q1(
        "SELECT MAX(session_number) AS n FROM recordings WHERE subject_id = ? AND course_type = ? AND id != ?",
        (subject_id, course_type, exclude_id or -1),
    )
    return int(row["n"] or 0) + 1 if row else 1


def sessions_to_renumber(rec: dict) -> list[dict]:
    """Séances qui reculent d'un cran si `rec` est supprimée (CM 3 → CM 2…) : les suivantes du même type dans la
    matière, sauf si son numéro reste porté par une autre séance (doublon)."""
    if not rec.get("subject_id") or not rec.get("session_number"):
        return []
    same = " WHERE r.subject_id = ? AND r.course_type = ? AND r.id != ?"
    args = (rec["subject_id"], rec["course_type"], rec["id"])
    if q1(RECORDING_SELECT + same + " AND r.session_number = ?", (*args, rec["session_number"])):
        return []
    return q(RECORDING_SELECT + same + " AND r.session_number > ? ORDER BY r.session_number, r.id",
             (*args, rec["session_number"]))


def session_counts(subject_id: int) -> dict[str, int]:
    rows = q(
        "SELECT course_type, COUNT(*) AS n FROM recordings WHERE subject_id = ? GROUP BY course_type",
        (subject_id,),
    )
    return {r["course_type"]: r["n"] for r in rows}


# --- Morceaux audio ---------------------------------------------------------------------

def upsert_chunk(recording_id: int, seq: int, segment: int, ext: str, size: int) -> None:
    run(
        "INSERT INTO chunks (recording_id, seq, segment, ext, size, received_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(recording_id, seq) DO UPDATE SET segment = excluded.segment, ext = excluded.ext, "
        "size = excluded.size, received_at = excluded.received_at",
        (recording_id, seq, segment, ext, size, now_iso()),
    )


def list_chunks(recording_id: int) -> list[dict]:
    return q("SELECT * FROM chunks WHERE recording_id = ? ORDER BY seq", (recording_id,))


def chunk_stats(recording_id: int) -> dict:
    row = q1(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size), 0) AS total, COALESCE(MAX(seq), 0) AS max_seq "
        "FROM chunks WHERE recording_id = ?",
        (recording_id,),
    )
    return row or {"n": 0, "total": 0, "max_seq": 0}


# --- Journal ----------------------------------------------------------------------------

def log(recording_id: int | None, message: str, level: str = "info") -> None:
    _insert("logs", {"recording_id": recording_id, "level": level, "message": message[:4000]})


def list_logs(recording_id: int, limit: int = 100) -> list[dict]:
    return q(
        "SELECT * FROM logs WHERE recording_id = ? ORDER BY id DESC LIMIT ?",
        (recording_id, limit),
    )
