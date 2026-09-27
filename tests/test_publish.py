"""Publication Drive et Notion avec des services factices (aucun appel réseau)."""

import pytest

from app import db, recorder, subjects
from app.publish import PublishSkipped
from app.publish import drive as drive_pub
from app.publish import notion as notion_pub
from app.publish.notion import NotionClient

from fakes import FakeDriveService, FakeNotion, notion_like, title_of

COURSE_1 = """# CM 1 – Introduction aux graphes

*Graphes — CM 1 du lundi 21 septembre 2026 — Enseignant : DURAND Sophie*

## 1. Définitions

> **Définition (Graphe)** : un graphe est un couple $G = (V, E)$.

> 💡 **Complément** : ligne 1
> ligne 2 avec $x^2$

$$
\\sum_{v} d(v) = 2m
$$

## Points clés
- Un graphe est un couple.
"""

COURSE_2 = """# CM 2 – Dijkstra

*Graphes — CM 2 du lundi 28 septembre 2026 — Enseignant : DURAND Sophie*

## 2.3 Dijkstra (cf. CM 1 – Introduction aux graphes)

Complexité $O((n+m)\\log n)$.
"""


def make_session(sid: int, number: int, day: str, title: str, course: str) -> int:
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date=day)
    db.update_recording(rid, status="formatted", title=title, duration_seconds=5400, session_number=number,
                        event_start=f"{day}T08:00:00+02:00", event_end=f"{day}T10:00:00+02:00")
    subjects.course_path(rid).write_text(course, encoding="utf-8")
    return rid


@pytest.fixture
def subject():
    sid = subjects.create_subject("Graphes", "DURAND Sophie")
    subjects.write_state(db.get_subject(sid), "# État de la matière : Graphes\n")
    return sid


# --- Drive -----------------------------------------------------------------------------------------

def test_drive_tree_files_and_notebooklm_same_id(subject):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    drive_pub.publish_recording(r1, client)

    root = svc.by_name("Cours M1")
    folder = svc.by_name("Graphes")
    seances = svc.by_name("Séances")
    assert folder["parents"] == [root["id"]] and seances["parents"] == [folder["id"]]
    md = svc.by_name("2026-09-21_CM01_introduction-aux-graphes.md")
    assert md["parents"] == [seances["id"]] and md["content"].decode() == COURSE_1
    assert "État de la matière" in svc.text("_etat.md")
    doc = svc.by_name("Graphes – NotebookLM")
    assert doc["mimeType"] == drive_pub.GDOC_MIME and doc["source_mime"] == "text/markdown"
    rec = db.get_recording(r1)
    assert rec["drive_md_url"] == md["webViewLink"]
    subj = db.get_subject(subject)
    assert subj["drive_gdoc_id"] == doc["id"] and subj["drive_folder_url"] == folder["webViewLink"]

    # Deuxième séance : le Google Doc est mis à jour sur le MÊME identifiant (NotebookLM garde sa source).
    r2 = make_session(subject, 2, "2026-09-28", "Dijkstra", COURSE_2)
    drive_pub.publish_recording(r2, client)
    doc2 = svc.by_name("Graphes – NotebookLM")
    assert doc2["id"] == doc["id"]
    assert ("update", doc["id"], "Graphes – NotebookLM") in svc.log
    full = svc.text("Graphes – Cours complet.md")
    assert full.index("CM 1 – Introduction aux graphes") < full.index("CM 2 – Dijkstra")
    assert "## Table des matières" in full and "1. CM 1 – Introduction aux graphes" in full
    assert doc2["content"].decode() == full
    assert len([f for f in svc.store.values() if f["name"] == "Cours M1"]) == 1  # pas de doublon de dossier


def test_drive_recreates_deleted_folder_and_uses_annotated_version(subject):
    svc = FakeDriveService()
    client = drive_pub.DriveClient(service=svc)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    drive_pub.publish_recording(r1, client)
    svc.by_name("Séances")["trashed"] = True  # supprimé à la main dans Drive
    subjects.annotated_path(r1).write_text(COURSE_1 + "\nMa note perso.\n", encoding="utf-8")
    drive_pub.publish_recording(r1, client)
    assert svc.by_name("2026-09-21_CM01_introduction-aux-graphes_annote.md")["content"].decode().endswith("Ma note perso.\n")
    assert "Ma note perso." in svc.text("Graphes – Cours complet.md")
    assert svc.by_name("2026-09-21_CM01_introduction-aux-graphes.md")["content"].decode() == COURSE_1  # original intact


def test_drive_not_configured_or_not_connected(subject, tmp_path):
    from app import config

    with pytest.raises(PublishSkipped):
        drive_pub.load_credentials()
    config.CREDENTIALS_FILE.write_text("{}", encoding="utf-8")
    with pytest.raises(drive_pub.DriveAuthError):
        drive_pub.load_credentials()


def test_drive_sources_option(subject):
    db.set_setting("drive_upload_sources", "1")
    svc = FakeDriveService()
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    (recorder.recording_dir(r1) / "transcript.txt").write_text("[00:00:00] L1 : bonjour", encoding="utf-8")
    recorder.audio_path(r1).write_bytes(b"ID3fake")
    drive_pub.publish_recording(r1, drive_pub.DriveClient(service=svc))
    assert svc.by_name("Sources")
    assert svc.by_name("2026-09-21_CM01_introduction-aux-graphes.mp3")["source_mime"] == "audio/mpeg"
    assert svc.text("2026-09-21_CM01_introduction-aux-graphes_transcription.txt").startswith("[00:00:00]")


# --- Notion ----------------------------------------------------------------------------------------

def notion_client(fake: FakeNotion) -> NotionClient:
    client = NotionClient.__new__(NotionClient)
    client.request = fake.request
    return client


def test_notion_structure_page_and_properties(subject):
    fake = FakeNotion()
    db.set_setting("notion_root", "https://app.notion.com/p/Cours-M1-" + fake.root)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    db.update_recording(r1, drive_md_url="https://drive.example/f1")
    notion_pub.publish_recording(r1, notion_client(fake))

    assert len(fake.databases) == 2
    seances_ds = db.get_setting("notion_seances_ds")
    relation = fake.data_sources[seances_ds]["properties"]["Matière"]["relation"]
    assert relation["data_source_id"] == db.get_setting("notion_matieres_ds")
    assert {v["name"] for v in fake.views} == {"Par matière", "Calendrier", "Dernières séances"}
    [page] = fake.session_pages()
    assert title_of(page) == "CM 1 – Introduction aux graphes"
    props = page["properties"]
    assert props["Type"] == {"select": {"name": "CM"}} and props["Numéro"] == {"number": 1}
    assert props["Date"]["date"]["start"] == "2026-09-21T08:00:00+02:00"
    assert props["Durée"]["rich_text"][0]["text"]["content"] == "1 h 30 min"
    assert props["Lien Drive"] == {"url": "https://drive.example/f1"}
    assert props["Statut"] == {"select": {"name": "Nouveau"}}
    subject_page = db.get_subject(subject)["notion_page_id"]
    assert props["Matière"] == {"relation": [{"id": subject_page}]}
    # Contenu : titre retiré (il est dans la propriété), compléments en callouts, bloc équation sur 3 lignes.
    md = page["markdown"]
    assert not md.startswith("# ")
    assert '<callout icon="💡" color="yellow_bg">' in md and "\tligne 2 avec $x^2$" in md
    assert '<callout icon="📘" color="blue_bg">' in md
    assert "$$\n\\sum_{v} d(v) = 2m\n$$" in md


def test_notion_never_overwrites_annotated_page(subject):
    fake = FakeNotion()
    client = notion_client(fake)
    db.set_setting("notion_root", fake.root)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    notion_pub.publish_recording(r1, client)
    [page] = fake.session_pages()
    page["markdown"] += "\nMon annotation."  # annotation faite dans Notion

    # Republication sans changement de contenu : propriétés seulement.
    db.update_recording(r1, drive_md_url="https://drive.example/new")
    notion_pub.publish_recording(r1, client)
    assert len(fake.session_pages()) == 1
    assert page["markdown"].endswith("Mon annotation.")
    assert page["properties"]["Lien Drive"] == {"url": "https://drive.example/new"}

    # Mise en forme relancée (contenu différent) : nouvelle page v2, ancienne conservée intacte.
    subjects.course_path(r1).write_text(COURSE_1.replace("Définitions", "Définitions (v2)"), encoding="utf-8")
    notion_pub.publish_recording(r1, client)
    pages = fake.session_pages()
    assert len(pages) == 2
    assert page["markdown"].endswith("Mon annotation.")
    assert page["properties"]["Statut"] == {"select": {"name": "Remplacée"}}
    new = next(p for p in pages if p is not page)
    assert title_of(new) == "CM 1 – Introduction aux graphes (v2)"
    rec = db.get_recording(r1)
    assert rec["notion_page_id"] == new["id"] and rec["notion_version"] == 2
    assert db.loads(rec["notion_old_pages"])[0]["id"] == page["id"]

    # Écrasement explicitement confirmé : même page, contenu remplacé.
    subjects.course_path(r1).write_text(COURSE_1.replace("Définitions", "Définitions (v3)"), encoding="utf-8")
    db.update_recording(r1, notion_pending_action="overwrite")
    notion_pub.publish_recording(r1, client)
    assert len(fake.session_pages()) == 2
    assert "Définitions (v3)" in new["markdown"]
    assert db.get_recording(r1)["notion_pending_action"] is None


def test_notion_recreates_deleted_database_without_touching_old_pages(subject):
    fake = FakeNotion()
    client = notion_client(fake)
    db.set_setting("notion_root", fake.root)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    notion_pub.publish_recording(r1, client)
    [old_page] = fake.session_pages()
    old_db = db.get_setting("notion_seances_db")
    fake.databases[old_db]["in_trash"] = True  # base « Séances » supprimée à la main
    notion_pub.publish_recording(r1, client)
    assert db.get_setting("notion_seances_db") != old_db
    rec = db.get_recording(r1)
    assert rec["notion_page_id"] != old_page["id"]
    assert db.loads(rec["notion_old_pages"])[0]["id"] == old_page["id"]
    assert old_page["properties"]["Statut"] == {"select": {"name": "Nouveau"}}  # ancienne page intacte


def test_notion_long_page_split_and_resumed_after_failure(subject, monkeypatch):
    monkeypatch.setattr(notion_pub, "PART_CHARS", 400)
    fake = FakeNotion(fail_append_at=2)
    client = notion_client(fake)
    db.set_setting("notion_root", fake.root)
    long_course = "# CM 1 – Long\n\n" + "\n\n".join(f"## Partie {i}\n\n" + "Texte du cours. " * 20 for i in range(1, 8))
    r1 = make_session(subject, 1, "2026-09-21", "Long", long_course)
    with pytest.raises(notion_pub.NotionError):
        notion_pub.publish_recording(r1, client)  # coupure pendant l'ajout de la 3e partie
    rec = db.get_recording(r1)
    assert rec["notion_content_complete"] == 0 and rec["notion_parts_done"] == 2
    notion_pub.publish_recording(r1, client)  # reprise : on complète la même page
    [page] = fake.session_pages()
    assert db.get_recording(r1)["notion_content_complete"] == 1
    for i in range(1, 8):
        assert page["markdown"].count(f"## Partie {i}") == 1


def test_notion_import_annotations(subject):
    fake = FakeNotion()
    client = notion_client(fake)
    db.set_setting("notion_root", fake.root)
    r1 = make_session(subject, 1, "2026-09-21", "Introduction aux graphes", COURSE_1)
    notion_pub.publish_recording(r1, client)
    [page] = fake.session_pages()
    # Ce que renvoie Notion en lecture : maths $`…`$, lignes sans séparation, annotation ajoutée.
    page["markdown"] = notion_like(page["markdown"]).replace("\n\n", "\n") + "\n**Ma remarque** : penser à revoir $`d(v)`$."
    text = notion_pub.import_annotations(r1, client)
    assert text.startswith("# CM 1 – Introduction aux graphes\n")
    assert "> 💡 **Complément** : ligne 1" in text
    assert "**Définition (Graphe)** : un graphe est un couple $G = (V, E)$." in text
    assert "**Ma remarque** : penser à revoir $d(v)$." in text
    assert "$`" not in text and "<callout" not in text
    assert subjects.course_path(r1).read_text(encoding="utf-8") == COURSE_1  # original intact
    assert page["properties"]["Statut"] == {"select": {"name": "Annotations importées"}}


def test_notion_skipped_without_token(subject):
    with pytest.raises(PublishSkipped):
        NotionClient()


def test_extract_notion_id():
    assert notion_pub.extract_id("https://app.notion.com/p/Cours-M1-9f1c2d3e4b5a60718293a4b5c6d7e8f9") == "9f1c2d3e4b5a60718293a4b5c6d7e8f9"
    assert notion_pub.extract_id("https://www.notion.so/workspace/Cours-M1-0123456789abcdef0123456789abcdef?pvs=4") == "0123456789abcdef0123456789abcdef"
    assert notion_pub.extract_id("01234567-89ab-cdef-0123-456789abcdef") == "0123456789abcdef0123456789abcdef"
    assert notion_pub.extract_id("pas une url") is None
