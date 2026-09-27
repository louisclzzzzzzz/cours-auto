"""Notion : bases « Matières » et « Séances », pages de séance créées depuis le Markdown,
règle de non-écrasement des pages annotées, import des annotations.

API REST appelée directement avec httpx (version 2026-03-11 : endpoints Markdown, data sources, vues).
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from urllib.parse import urlparse

import httpx

from .. import config, db, subjects
from ..markdown_utils import extract_title, notion_to_standard, split_markdown, to_notion_markdown
from ..textutils import fmt_duration
from . import PublishSkipped

log = logging.getLogger(__name__)

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MIN_INTERVAL = 0.35  # ≈ 3 requêtes/s (limite des offres gratuites)
PART_CHARS = 20000  # taille max d'une requête Markdown (on complète la page par ajouts successifs)
ASYNC_THRESHOLD = 15000  # au-delà : allow_async + suivi de la tâche

STATUS_NEW = "Nouveau"
STATUS_ANNOTATED = "Annotations importées"
STATUS_REPLACED = "Remplacée"

MATIERES_PROPS = {
    "Nom": {"type": "title", "title": {}},
    "Enseignant(s)": {"type": "rich_text", "rich_text": {}},
    "Lien Drive": {"type": "url", "url": {}},
    "Lien Google Doc NotebookLM": {"type": "url", "url": {}},
}


def seances_props(matieres_ds: str) -> dict:
    return {
        "Titre": {"type": "title", "title": {}},
        "Matière": {
            "type": "relation",
            "relation": {
                "data_source_id": matieres_ds,
                "type": "dual_property",
                "dual_property": {"synced_property_name": "Séances"},
            },
        },
        "Type": {"type": "select", "select": {"options": [
            {"name": "CM", "color": "blue"}, {"name": "TD", "color": "green"}, {"name": "TP", "color": "orange"}]}},
        "Numéro": {"type": "number", "number": {"format": "number"}},
        "Date": {"type": "date", "date": {}},
        "Durée": {"type": "rich_text", "rich_text": {}},
        "Statut": {"type": "select", "select": {"options": [
            {"name": STATUS_NEW, "color": "blue"}, {"name": STATUS_ANNOTATED, "color": "green"},
            {"name": STATUS_REPLACED, "color": "gray"}]}},
        "Lien Drive": {"type": "url", "url": {}},
        "Enseignant": {"type": "rich_text", "rich_text": {}},
    }


class NotionError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Notion {status} {code} : {message}")
        self.status = status
        self.code = code


# --- Utilitaires ------------------------------------------------------------------------------------

def extract_id(value: str | None) -> str | None:
    """ID (32 hex) depuis une URL Notion ou un identifiant, avec ou sans tirets."""
    value = (value or "").strip()
    if not value:
        return None
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value)
    if m:
        return m.group(0).replace("-", "").lower()
    path = urlparse(value).path if "://" in value else value
    last = path.rstrip("/").split("/")[-1]
    m = re.search(r"([0-9a-fA-F]{32})$", last)
    return m.group(1).lower() if m else None


def _text(value: str) -> list[dict]:
    value = value or ""
    return [{"type": "text", "text": {"content": value[i:i + 2000]}} for i in range(0, len(value), 2000)] if value else []


def _url(value: str | None) -> dict:
    return {"url": value or None}


def page_title(page: dict) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))
    return ""


def is_configured() -> bool:
    return bool(config.notion_token()) and bool(extract_id(db.get_setting("notion_root")))


# --- Client HTTP --------------------------------------------------------------------------------------

class NotionClient:
    _lock = threading.Lock()
    _last_request = 0.0

    def __init__(self, token: str | None = None, http: httpx.Client | None = None):
        token = token if token is not None else config.notion_token()
        if not token:
            raise PublishSkipped("Notion non configuré (NOTION_TOKEN absent du .env).")
        self.http = http or httpx.Client(
            base_url=API_URL,
            headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION},
            timeout=httpx.Timeout(120.0, connect=15.0),
        )

    def _throttle(self) -> None:
        with NotionClient._lock:
            wait = NotionClient._last_request + MIN_INTERVAL - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            NotionClient._last_request = time.monotonic()

    def request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                max_attempts: int = 6) -> dict:
        idempotent = method in ("GET", "DELETE")
        for attempt in range(max_attempts):
            self._throttle()
            last = attempt == max_attempts - 1
            try:
                resp = self.http.request(method, path, json=json, params=params)
            except httpx.TransportError as exc:
                if idempotent and not last:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                raise NotionError(0, "network_error", str(exc)) from exc
            status = resp.status_code
            try:
                body = resp.json()
            except ValueError:
                body = {"message": resp.text[:500]}
            if status in (429, 529) and not last:
                reason = (body.get("additional_data") or {}).get("rate_limit_reason")
                if reason != "public_api_request_blocked":
                    retry_after = resp.headers.get("retry-after")
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 30)
                    time.sleep(delay + random.uniform(0, 0.25))
                    continue
            if status in (500, 502, 503, 504) and idempotent and not last:
                time.sleep(min(2 ** attempt, 30) + random.uniform(0, 0.25))
                continue
            if status == 503 and not idempotent:
                # L'écriture a pu être enregistrée malgré l'erreur : ne pas la répéter.
                committed = (body.get("additional_data") or {}).get("committed_resource_id")
                if committed:
                    return {"object": "committed", "id": committed}
            if status >= 400:
                raise NotionError(status, body.get("code", "error"), body.get("message", resp.text[:300]))
            return body
        raise NotionError(0, "retries_exhausted", f"{method} {path}")

    # Raccourcis
    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params=params or None)

    def wait_async(self, task: dict, timeout: float = 600.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = task.get("status")
            if status == "succeeded":
                return task.get("result") or {}
            if status == "failed":
                err = task.get("error") or {}
                raise NotionError(err.get("status", 400), err.get("code", "async_failed"), err.get("message", ""))
            if time.monotonic() > deadline:
                raise NotionError(0, "async_timeout", f"Tâche Notion {task.get('id')} non terminée.")
            time.sleep(max(float(task.get("poll_after_seconds") or 2), 1.0))
            task = self.get(f"/async_tasks/{task['id']}")

    def create_page(self, parent: dict, properties: dict, markdown: str | None = None) -> dict:
        body: dict = {"parent": parent, "properties": properties}
        if markdown is not None:
            body["markdown"] = markdown
            if len(markdown) > ASYNC_THRESHOLD:
                body["allow_async"] = True
        res = self.request("POST", "/pages", json=body)
        if res.get("object") == "async_task":
            res = self.wait_async(res)
        if res.get("object") == "committed" or "url" not in res:
            res = self.get(f"/pages/{res['id']}")
        return res

    def update_properties(self, page_id: str, properties: dict) -> dict:
        return self.request("PATCH", f"/pages/{page_id}", json={"properties": properties})

    def append_markdown(self, page_id: str, markdown: str) -> None:
        body: dict = {"type": "insert_content", "insert_content": {"content": markdown, "position": {"type": "end"}}}
        if len(markdown) > ASYNC_THRESHOLD:
            body["allow_async"] = True
        res = self.request("PATCH", f"/pages/{page_id}/markdown", json=body)
        if res.get("object") == "async_task":
            self.wait_async(res)

    def replace_markdown(self, page_id: str, markdown: str) -> None:
        body: dict = {"type": "replace_content", "replace_content": {"new_str": markdown}}
        if len(markdown) > ASYNC_THRESHOLD:
            body["allow_async"] = True
        res = self.request("PATCH", f"/pages/{page_id}/markdown", json=body)
        if res.get("object") == "async_task":
            self.wait_async(res)

    def page_markdown(self, page_id: str) -> str:
        """Contenu complet de la page ; les blocs non chargés (`<unknown>`) sont récupérés à part."""
        res = self.get(f"/pages/{page_id}/markdown")
        md = res.get("markdown", "")
        for block_id in res.get("unknown_block_ids") or []:
            try:
                sub = self.get(f"/pages/{block_id}/markdown").get("markdown", "")
            except NotionError as exc:
                if exc.code == "object_not_found":
                    continue  # bloc non partagé avec l'intégration
                raise
            compact = block_id.replace("-", "")
            pattern = re.compile(r'<unknown url="[^"]*' + re.escape(compact) + r'"[^>]*/>')
            md, n = pattern.subn(lambda _m: sub, md, count=1)
            if not n:
                md += "\n" + sub
        return md

    def is_alive(self, kind: str, obj_id: str | None) -> bool:
        if not obj_id:
            return False
        try:
            obj = self.get(f"/{kind}/{obj_id}")
        except NotionError as exc:
            if exc.status in (404, 400) or exc.code in ("object_not_found", "validation_error"):
                return False
            raise
        return not obj.get("in_trash") and not obj.get("archived")


# --- Connexion / structure -------------------------------------------------------------------------

def test_connection() -> tuple[bool, str]:
    try:
        client = NotionClient()
        me = client.get("/users/me")
        name = me.get("name") or "intégration"
        root = extract_id(db.get_setting("notion_root"))
        if not root:
            return False, f"Jeton valide (« {name} »), mais l'ID ou l'URL de la page racine est manquant."
        page = client.get(f"/pages/{root}")
        return True, f"Connexion OK (« {name} »). Page racine : « {page_title(page) or root} »."
    except PublishSkipped as exc:
        return False, str(exc)
    except NotionError as exc:
        if exc.code == "object_not_found":
            return False, "Page racine introuvable : partagez-la avec l'intégration (menu ••• → Connexions)."
        return False, str(exc)


def _create_database(client: NotionClient, root_id: str, title: str, props: dict, icon: str) -> tuple[str, str, str]:
    res = client.request("POST", "/databases", json={
        "parent": {"type": "page_id", "page_id": root_id},
        "title": _text(title),
        "is_inline": False,
        "icon": {"type": "emoji", "emoji": icon},
        "initial_data_source": {"properties": props},
    })
    sources = res.get("data_sources") or []
    if not sources:
        res = client.get(f"/databases/{res['id']}")
        sources = res.get("data_sources") or []
    return res["id"], sources[0]["id"], res.get("url", "")


def _ensure_properties(client: NotionClient, ds_id: str, expected: dict) -> None:
    """Rajoute les propriétés manquantes (si elles ont été supprimées ou renommées dans Notion)."""
    ds = client.get(f"/data_sources/{ds_id}")
    existing = set((ds.get("properties") or {}).keys())
    missing = {k: v for k, v in expected.items() if k not in existing and v.get("type") != "title"}
    if missing:
        client.request("PATCH", f"/data_sources/{ds_id}", json={"properties": missing})


def _create_views(client: NotionClient, db_id: str, ds_id: str) -> None:
    """Vues suggérées : par matière (triée par date), calendrier, dernières séances."""
    try:
        props = client.get(f"/data_sources/{ds_id}").get("properties") or {}
        date_id, subject_id = props["Date"]["id"], props["Matière"]["id"]
    except (NotionError, KeyError) as exc:
        log.warning("Vues Notion non créées : %s", exc)
        return
    views = [
        {"name": "Par matière", "type": "table", "sorts": [{"property": "Date", "direction": "ascending"}],
         "configuration": {"type": "table", "group_by": {
             "type": "relation", "property_id": subject_id, "sort": {"type": "ascending"}}}},
        {"name": "Calendrier", "type": "calendar",
         "configuration": {"type": "calendar", "date_property_id": date_id, "view_range": "week"}},
        {"name": "Dernières séances", "type": "table", "sorts": [{"property": "Date", "direction": "descending"}]},
    ]
    for view in views:
        try:
            client.request("POST", "/views", json={"database_id": db_id, "data_source_id": ds_id, **view})
        except NotionError as exc:
            log.warning("Vue Notion « %s » non créée : %s", view["name"], exc)


def ensure_structure(client: NotionClient) -> dict:
    root = extract_id(db.get_setting("notion_root"))
    if not root:
        raise PublishSkipped("Notion : page racine non renseignée (Paramètres).")
    if db.get_setting("notion_root_used") != root:  # nouvelle page racine : nouvelles bases
        for key in ("notion_matieres_db", "notion_matieres_ds", "notion_seances_db", "notion_seances_ds"):
            db.set_setting(key, "")
        db.set_setting("notion_root_used", root)
        db.run("UPDATE subjects SET notion_page_id = NULL, notion_page_url = NULL")

    mat_db, mat_ds = db.get_setting("notion_matieres_db"), db.get_setting("notion_matieres_ds")
    if not client.is_alive("databases", mat_db):
        mat_db, mat_ds, url = _create_database(client, root, "Matières", MATIERES_PROPS, "📚")
        db.set_setting("notion_matieres_db", mat_db)
        db.set_setting("notion_matieres_ds", mat_ds)
        db.set_setting("notion_matieres_url", url)
        db.run("UPDATE subjects SET notion_page_id = NULL, notion_page_url = NULL")
        db.set_setting("notion_seances_db", "")  # la relation doit viser la nouvelle base
    else:
        _ensure_properties(client, mat_ds, MATIERES_PROPS)

    se_db, se_ds = db.get_setting("notion_seances_db"), db.get_setting("notion_seances_ds")
    expected = seances_props(mat_ds)
    if not client.is_alive("databases", se_db):
        se_db, se_ds, url = _create_database(client, root, "Séances", expected, "🎓")
        db.set_setting("notion_seances_db", se_db)
        db.set_setting("notion_seances_ds", se_ds)
        db.set_setting("notion_seances_url", url)
        _create_views(client, se_db, se_ds)
        _detach_session_pages()
    else:
        _ensure_properties(client, se_ds, expected)
    return {"matieres_ds": mat_ds, "seances_ds": se_ds}


def _detach_session_pages() -> None:
    """Nouvelle base « Séances » : les séances y seront recréées à leur prochaine publication.
    Les anciennes pages ne sont jamais modifiées ; elles restent listées comme anciennes versions."""
    for rec in db.q("SELECT id, notion_page_id, notion_page_url, notion_version, notion_old_pages FROM recordings "
                    "WHERE notion_page_id IS NOT NULL"):
        old = db.loads(rec["notion_old_pages"], [])
        old.append({"id": rec["notion_page_id"], "url": rec["notion_page_url"], "version": rec["notion_version"] or 1})
        db.update_recording(rec["id"], notion_page_id=None, notion_page_url=None, notion_old_pages=db.dumps(old),
                            notion_parts_done=0, notion_content_complete=0, notion_content_hash=None,
                            notion_status="pending")


def ensure_subject_page(client: NotionClient, subject: dict, ids: dict) -> str:
    props = {
        "Nom": {"title": _text(subject["name"])},
        "Enseignant(s)": {"rich_text": _text(subject.get("teachers") or "")},
        "Lien Drive": _url(subject.get("drive_folder_url")),
        "Lien Google Doc NotebookLM": _url(subject.get("drive_gdoc_url")),
    }
    page_id = subject.get("notion_page_id")
    if page_id and client.is_alive("pages", page_id):
        client.update_properties(page_id, props)
        return page_id
    page = client.create_page({"type": "data_source_id", "data_source_id": ids["matieres_ds"]}, props)
    db.update_subject(subject["id"], notion_page_id=page["id"], notion_page_url=page.get("url"))
    return page["id"]


# --- Séances ---------------------------------------------------------------------------------------

def _session_title(rec: dict, version: int) -> str:
    title = subjects.session_label(rec)
    return f"{title} (v{version})" if version > 1 else title


def _session_props(rec: dict, subject_page_id: str, version: int) -> dict:
    date: dict = {"start": rec["session_date"]}
    if rec.get("event_start"):
        date = {"start": rec["event_start"]}
        if rec.get("event_end"):
            date["end"] = rec["event_end"]
    return {
        "Titre": {"title": _text(_session_title(rec, version))},
        "Matière": {"relation": [{"id": subject_page_id}]},
        "Type": {"select": {"name": rec["course_type"]}},
        "Numéro": {"number": rec["session_number"]},
        "Date": {"date": date},
        "Durée": {"rich_text": _text(fmt_duration(rec.get("duration_seconds")))},
        "Lien Drive": _url(rec.get("drive_md_url")),
        "Enseignant": {"rich_text": _text(rec.get("teacher") or "")},
    }


def _write_new_page(client: NotionClient, rec: dict, ids: dict, subject_page: str, course: str, version: int) -> None:
    parts = split_markdown(to_notion_markdown(course), PART_CHARS)
    props = _session_props(rec, subject_page, version)
    props["Statut"] = {"select": {"name": STATUS_NEW}}
    page = client.create_page({"type": "data_source_id", "data_source_id": ids["seances_ds"]}, props, parts[0])
    db.update_recording(
        rec["id"], notion_page_id=page["id"], notion_page_url=page.get("url"), notion_version=version,
        notion_parts_done=1, notion_content_complete=1 if len(parts) == 1 else 0,
        notion_content_hash=subjects.content_hash(course), notion_pending_action=None,
    )
    _append_remaining(client, rec["id"], page["id"], parts, 1)


def _append_remaining(client: NotionClient, rec_id: int, page_id: str, parts: list[str], done: int) -> None:
    for i in range(done, len(parts)):
        client.append_markdown(page_id, parts[i])
        db.update_recording(rec_id, notion_parts_done=i + 1)
    db.update_recording(rec_id, notion_content_complete=1)


def publish_recording(recording_id: int, client: NotionClient | None = None) -> None:
    client = client or NotionClient()
    ids = ensure_structure(client)
    rec = db.get_recording(recording_id)
    subject = db.get_subject(rec["subject_id"])
    course = subjects.read_course(recording_id)
    if course is None:
        raise RuntimeError("Aucun cours mis en forme à publier.")
    subject_page = ensure_subject_page(client, subject, ids)
    digest = subjects.content_hash(course)
    page_id = rec.get("notion_page_id")
    version = max(int(rec.get("notion_version") or 0), 1)

    if not page_id or not client.is_alive("pages", page_id):
        if page_id:
            db.log(recording_id, "La page Notion précédente est introuvable ou à la corbeille : nouvelle page créée.", "warning")
        _write_new_page(client, rec, ids, subject_page, course, version)
        return

    same_content = rec.get("notion_content_hash") == digest
    if same_content and not rec.get("notion_content_complete"):
        # Création interrompue (ex. coupure réseau) : on termine d'ajouter les parties manquantes.
        parts = split_markdown(to_notion_markdown(course), PART_CHARS)
        _append_remaining(client, recording_id, page_id, parts, int(rec.get("notion_parts_done") or 1))
    elif not same_content:
        # Le cours a été régénéré : on ne réécrit JAMAIS la page annotée, sauf confirmation explicite.
        if rec.get("notion_pending_action") == "overwrite":
            parts = split_markdown(to_notion_markdown(course), PART_CHARS)
            client.replace_markdown(page_id, parts[0])
            db.update_recording(recording_id, notion_content_hash=digest, notion_pending_action=None,
                                notion_parts_done=1, notion_content_complete=0)
            _append_remaining(client, recording_id, page_id, parts, 1)
            db.log(recording_id, "Page Notion écrasée avec le nouveau cours (confirmé par l'utilisateur).", "warning")
        else:
            old_pages = db.loads(rec.get("notion_old_pages"), [])
            old_pages.append({"id": page_id, "url": rec.get("notion_page_url"), "version": version})
            try:
                client.update_properties(page_id, {"Statut": {"select": {"name": STATUS_REPLACED}}})
            except NotionError as exc:
                log.warning("Statut de l'ancienne page non mis à jour : %s", exc)
            db.update_recording(recording_id, notion_old_pages=db.dumps(old_pages))
            _write_new_page(client, db.get_recording(recording_id), ids, subject_page, course, version + 1)
            db.log(recording_id, f"Nouvelle version v{version + 1} créée dans Notion (l'ancienne page est conservée).")
            return
    # Propriétés : mises à jour sans risque pour les annotations.
    rec = db.get_recording(recording_id)
    client.update_properties(page_id, _session_props(rec, subject_page, int(rec.get("notion_version") or 1)))


def update_links(recording_id: int) -> None:
    """Après une publication Drive : met à jour les liens Drive côté Notion (propriétés seulement)."""
    if not is_configured():
        return
    rec = db.get_recording(recording_id)
    client = NotionClient()
    subject = db.get_subject(rec["subject_id"])
    if subject and subject.get("notion_page_id"):
        client.update_properties(subject["notion_page_id"], {
            "Lien Drive": _url(subject.get("drive_folder_url")),
            "Lien Google Doc NotebookLM": _url(subject.get("drive_gdoc_url")),
        })
    if rec.get("notion_page_id"):
        client.update_properties(rec["notion_page_id"], {"Lien Drive": _url(rec.get("drive_md_url"))})


def import_annotations(recording_id: int, client: NotionClient | None = None) -> str:
    """Lit la page Notion et enregistre `course_annote.md` (le cours d'origine reste intact)."""
    rec = db.get_recording(recording_id)
    if not rec.get("notion_page_id"):
        raise RuntimeError("Cette séance n'a pas (encore) de page Notion.")
    client = client or NotionClient()
    md = client.page_markdown(rec["notion_page_id"])
    course = subjects.read_course(recording_id) or ""
    title = extract_title(course) or subjects.session_label(rec)
    text = notion_to_standard(md, title=title)
    subjects.annotated_path(recording_id).write_text(text, encoding="utf-8")
    db.update_recording(recording_id, annotations_imported_at=db.now_iso())
    try:
        client.update_properties(rec["notion_page_id"], {"Statut": {"select": {"name": STATUS_ANNOTATED}}})
    except NotionError as exc:
        log.warning("Statut Notion non mis à jour : %s", exc)
    return text
