"""Interface : pages principales, navigation, choix du cours, liste « Cours », avancement et relance d'un
enregistrement, réglages d'une matière, paramètres."""

from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import calendar_ics, db, pipeline as pl, recorder, subjects
from app.publish import drive
from app.routes import recordings as rec_routes
from app.textutils import fmt_when
from app.web import redirect, static_url

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))  # un dimanche


@pytest.fixture
def client(monkeypatch):
    from app.main import app

    monkeypatch.setattr(calendar_ics, "now_local", lambda: NOW)
    with TestClient(app) as c:
        pl.pipeline.stop()  # aucun traitement réel pendant les tests d'interface
        yield c


def done_recording(sid: int, **fields) -> int:
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-21")
    subjects.course_path(rid).parent.mkdir(parents=True, exist_ok=True)
    subjects.course_path(rid).write_text("# CM 1 – Graphes\n\n## Intro\n\nTexte.", encoding="utf-8")
    db.update_recording(rid, **{"status": "done", "title": "Graphes", "drive_status": "done",
                                "drive_md_id": "f1", "drive_md_url": "https://drive.example/f1", **fields})
    return rid


def test_navigation_and_main_pages(client):
    sid = subjects.create_subject("Graphes")
    rid = done_recording(sid)
    for url in ("/", "/cours", f"/cours/{sid}", f"/cours/{sid}/complet", f"/matieres/{sid}",
                "/enregistrements", f"/enregistrements/{rid}", "/parametres"):
        r = client.get(url)
        assert r.status_code == 200, url
        assert 'href="#main"' in r.text and 'id="main"' in r.text  # lien d'évitement
    # 4 onglets ; les réglages d'une matière sont rattachés à l'onglet « Cours ».
    page = client.get(f"/matieres/{sid}").text
    assert '<a href="/cours" class="active" aria-current="page"><svg' in page and "</svg>Cours</a>" in page
    assert "</svg>Historique</a>" in page and "Matières</a>" not in page
    # L'ancienne liste « Matières » renvoie vers « Cours ».
    r = client.get("/matieres", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cours"
    # Feuilles de style et scripts versionnés (le navigateur recharge les fichiers modifiés).
    assert static_url("app.css") in client.get("/").text and "?v=" in static_url("app.css")
    # Police embarquée (l'app fonctionne hors ligne).
    assert client.get("/static/vendor/fonts/plus-jakarta-sans-latin-wght-normal.woff2").status_code == 200


def test_home_without_course_today_offers_next_day_and_manual_choice(client, ics_bytes):
    calendar_ics.save_calendar(ics_bytes, "file")
    page = client.get("/").text  # dimanche 27/09 : pas de cours
    assert "Aucun cours ce jour-là" in page and "/fragments/slots?day=2026-09-28" in page
    assert 'value="manual" id="slot-manual" checked' in page
    monday = client.get("/fragments/slots?day=2026-09-28").text
    assert 'id="slot-manual" checked' not in monday  # des créneaux existent : rien n'est présélectionné le dimanche
    assert monday.count("data-slot-card") == 5  # 4 créneaux + « Autre cours »
    assert "Matière à associer" in monday  # aucun intitulé n'est encore associé
    assert 'name="match"' not in monday  # l'option regex n'encombre plus le formulaire rapide


def test_courses_page_is_a_list_with_unmapped_summaries(client, ics_bytes):
    calendar_ics.save_calendar(ics_bytes, "file")
    sid = subjects.create_subject("Graphes")
    done_recording(sid)
    page = client.get("/cours").text
    assert 'class="rows card flush subject-rows"' in page and 'class="grid"' not in page
    assert f'data-href="/cours/{sid}"' in page and "Dernière séance : CM 1 – Graphes" in page
    assert "Récupérer depuis Drive" in page
    assert 'id="intitules"' in page and "Algorithmique avancée CM" in page
    # Association depuis la liste : retour sur la section, message conservé avant l'ancre.
    r = client.post("/matieres/associer", data={"summary": "Algorithmique avancée CM", "subject_id": str(sid)},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/cours?msg=") and r.headers["location"].endswith("#intitules")
    assert "Algorithmique avancée CM" not in client.get("/cours").text.split('id="intitules"')[-1]


def test_redirect_keeps_anchor_after_message():
    loc = redirect("/parametres#drive", "OK", "err").headers["location"]
    parsed = urlparse(loc)
    assert parsed.path == "/parametres" and parsed.fragment == "drive"
    assert parse_qs(parsed.query) == {"msg": ["OK"], "level": ["err"]}
    assert redirect("/cours").headers["location"] == "/cours"


def test_progress_steps_and_retry_action():
    sid = subjects.create_subject("Graphes")
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-21")
    files = {"audio": True, "transcript": False, "course": False}

    def steps(**fields):
        db.update_recording(rid, **fields)
        return [s["state"] for s in rec_routes.progress_steps(db.get_recording(rid), files)]

    assert steps(status="transcribing") == ["done", "current", "todo", "todo"]
    assert steps(status="error", error_step="transcription") == ["done", "error", "todo", "todo"]
    assert rec_routes.retry_action(db.get_recording(rid)) == "transcribe"
    files["transcript"] = True
    assert steps(status="publishing") == ["done", "done", "current", "todo"]
    assert steps(status="error", error_step="publication", drive_status="error") == ["done", "done", "error", "todo"]
    assert rec_routes.retry_action(db.get_recording(rid)) == "publish"
    # Transcription déposée : le cours attend la tâche Claude, puis il est récupéré depuis Drive.
    waiting = rec_routes.progress_steps(db.get_recording(rid) | {"status": "done", "drive_status": "done"}, files)
    assert [s["state"] for s in waiting] == ["done", "done", "done", "todo"]
    assert waiting[3]["detail"] == "en attente de la tâche Claude"
    files["course"] = True
    assert steps(status="done", drive_status="done") == ["done"] * 4
    db.update_recording(rid, status="interrupted", error_step=None)
    assert rec_routes.retry_action(db.get_recording(rid)) == "finalize"
    assert steps(status="interrupted")[0] == "warn"
    db.update_recording(rid, status="done")
    assert rec_routes.retry_action(db.get_recording(rid)) is None


def test_detail_page_retry_button_and_polling(client):
    sid = subjects.create_subject("Graphes")
    rid = done_recording(sid, status="error", error_step="publication", drive_status="error",
                         error_message="Drive : indisponible")
    page = client.get(f"/enregistrements/{rid}").text
    assert 'name="action" value="publish"' in page and "Réessayer" in page
    assert 'hx-trigger="every 3s"' not in page  # rien ne tourne : pas de rechargement automatique
    assert "Voir le cours" in page
    db.update_recording(rid, status="transcribing")
    page = client.get(f"/enregistrements/{rid}").text
    assert 'hx-trigger="every 3s"' in page and "Réessayer" not in page
    # Historique : rechargement périodique seulement pendant un traitement.
    assert 'hx-trigger="every 5s"' in client.get("/enregistrements").text
    db.update_recording(rid, status="done")
    assert 'hx-trigger="every 5s"' not in client.get("/enregistrements").text
    assert "every 4s" not in client.get("/fragments/latest").text


def test_audio_ready_waits_for_manual_launch(client, monkeypatch):
    sid = subjects.create_subject("Graphes")
    rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-21")
    recorder.audio_path(rid).write_bytes(b"mp3")
    db.update_recording(rid, status="uploaded", duration_seconds=3600)
    # Rien ne tourne tout seul : un bouton lance le traitement (accueil, Historique, fiche), sans rechargement auto.
    for url in ("/", "/enregistrements", f"/enregistrements/{rid}"):
        assert "Lancer le traitement" in client.get(url).text, url
    assert "Prêt à traiter" in client.get("/fragments/latest").text
    assert "every 4s" not in client.get("/fragments/latest").text
    assert 'hx-trigger="every 3s"' not in client.get(f"/enregistrements/{rid}").text

    submitted = []
    monkeypatch.setattr(pl.pipeline, "submit", lambda kind, target, action, chain=True: submitted.append((action, chain)))
    r = client.post(f"/enregistrements/{rid}/action", data={"action": "transcribe", "back": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/?msg=Traitement+lanc")
    assert submitted == [("transcribe", True)]  # transcription → dépôt dans Drive

    # En file derrière un autre traitement : plus de bouton, badge « En file d'attente », suivi en direct.
    monkeypatch.setattr(pl.pipeline, "is_active", lambda r: r == rid)
    latest = client.get("/fragments/latest").text
    assert "En file d'attente" in latest and "Lancer le traitement" not in latest and "every 4s" in latest


def test_worker_indicator_is_empty_when_idle(client):
    assert client.get("/fragments/worker").text.strip() == ""


def test_waiting_for_claude_then_course_ready(client, monkeypatch):
    sid = subjects.create_subject("Graphes")
    rid = done_recording(sid, drive_md_id=None, drive_md_url=None, drive_transcription_id="t1",
                         drive_transcription_url="https://drive.example/t1")
    subjects.course_path(rid).unlink()
    assert "En attente du cours" in client.get("/enregistrements").text
    page = client.get(f"/enregistrements/{rid}").text
    assert "Transcription déposée dans Drive" in page and "Vérifier maintenant" in page and "Voir le cours" not in page

    def fetch(subject_id=None):
        subjects.course_path(rid).write_text("# CM 1 – Graphes\n\nTexte.", encoding="utf-8")
        db.update_recording(rid, drive_md_id="f2", drive_md_url="https://drive.example/f2")
        return [db.get_recording(rid)]

    monkeypatch.setattr(drive, "fetch_courses", fetch)
    r = client.post("/cours/drive", data={"subject_id": str(sid), "back": f"/enregistrements/{rid}"}, follow_redirects=False)
    location = urlparse(r.headers["location"])
    assert location.path == f"/enregistrements/{rid}"
    assert parse_qs(location.query)["msg"] == ["1 cours récupéré depuis Drive : Graphes CM 1 – Graphes."]
    assert "Cours prêt" in client.get("/enregistrements").text
    page = client.get(f"/enregistrements/{rid}").text
    assert "Voir le cours" in page and "https://drive.example/f2" in page
    preview = client.get(f"/fragments/cours/seance/{rid}").text
    assert "Ouvrir dans Drive" in preview and "Notion" not in preview


def test_fetch_errors_are_shown(client, monkeypatch):
    def fail(subject_id=None):
        drive.sync_state["error"] = drive.READ_MISSING
        raise drive.DriveReadError(drive.READ_MISSING)

    monkeypatch.setattr(drive, "fetch_courses", fail)
    r = client.post("/cours/drive", follow_redirects=False)
    query = parse_qs(urlparse(r.headers["location"]).query)
    assert query["level"] == ["err"] and "drive.readonly" in query["msg"][0]
    assert "Cours non récupérés depuis Drive" in client.get("/cours").text


def test_settings_page_without_notion_and_llm(client, monkeypatch):
    page = client.get("/parametres").text
    assert "Notion" not in page and "mise en forme" not in page.lower() and "Modèle de transcription" in page
    monkeypatch.setattr(drive, "connection_status", lambda force=False: {
        "configured": True, "connected": True, "can_read": False, "message": "Connecté (moi@example.com)"})
    page = client.get("/parametres").text
    assert "Lecture des cours non autorisée" in page and "drive.readonly" in page
    r = client.post("/parametres/modeles", data={"transcription_model": "voxtral-mini-latest",
                                                 "transcription_language": "fr", "audio_bitrate": "64k"},
                    follow_redirects=False)
    assert "level=err" not in r.headers["location"] and db.get_setting("audio_bitrate") == "64k"


def test_fmt_when():
    now = datetime.now().astimezone()
    assert fmt_when(now.isoformat()) == f"aujourd'hui à {now:%H:%M}"
    assert fmt_when("2026-01-05T08:30:00+01:00").endswith("à " + datetime.fromisoformat(
        "2026-01-05T08:30:00+01:00").astimezone().strftime("%H:%M"))
    assert fmt_when("") == "" and fmt_when("pas une date") == "pas une date"


def test_quit_button_and_version(client, monkeypatch):
    from app import version
    from app.routes import settings as settings_routes

    page = client.get("/").text
    assert 'action="/quitter"' in page and "Quitter l'app" in page
    v = version.current()  # tests lancés depuis le dépôt Git
    assert v and f"Version {v['commit']}" in page
    stopped = []
    monkeypatch.setattr(settings_routes, "_stop_server", lambda: stopped.append(True))
    monkeypatch.setattr(settings_routes.threading, "Timer", lambda delay, fn: type("T", (), {"start": lambda self: fn()})())
    r = client.post("/quitter")
    assert r.status_code == 200 and "Cours auto est arrêté" in r.text and stopped == [True]
    # Comme toute action, l'arrêt est refusé depuis un autre site.
    assert client.post("/quitter", headers={"Origin": "https://evil.example"}).status_code == 403


def _course(rid: int, md: str) -> None:
    subjects.course_path(rid).parent.mkdir(parents=True, exist_ok=True)
    subjects.course_path(rid).write_text(md, encoding="utf-8")


def test_deleting_a_false_start_renumbers_following_sessions(client):
    sid = subjects.create_subject("Graphes")
    first = done_recording(sid)  # CM 1
    false_start = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")  # CM 2
    db.update_recording(false_start, status="uploaded", duration_seconds=240)
    real = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")  # CM 3
    td = recorder.create_recording(subject_id=sid, course_type="TD", session_date="2026-09-29")  # TD 1
    md = "# CM 3 – Arbres\n\n*Graphes — CM 3 du 28 septembre 2026 — Enseignant : X*\n\n## Intro\n\nTexte."
    _course(real, md)
    (recorder.recording_dir(real) / "transcript.txt").write_text(
        "# Transcription — Graphes — CM 3 — 2026-09-28\n\n[00:00] Bonjour.\n", encoding="utf-8")
    db.update_recording(real, status="done", title="Arbres", drive_transcription_id="t3")
    assert db.get_recording(real)["session_number"] == 3

    # Corbeille dans l'Historique et sur l'accueil ; entrée du menu ⋯ dans la page « Cours ».
    assert f'action="/enregistrements/{false_start}/delete"' in client.get("/enregistrements").text
    assert f'action="/enregistrements/{false_start}/delete"' in client.get("/fragments/latest").text
    assert "Supprimer la séance…" in client.get(f"/fragments/cours/seance/{real}").text

    r = client.post(f"/enregistrements/{false_start}/delete", data={"back": "/enregistrements"}, follow_redirects=False)
    assert r.status_code == 303
    location = urlparse(r.headers["location"])
    assert location.path == "/enregistrements"
    msg = parse_qs(location.query)["msg"][0]
    assert "Graphes — CM 2 supprimé." in msg and "CM 3 → CM 2" in msg and "Relancez le dépôt Drive de CM 2" in msg

    assert db.get_recording(false_start) is None and not recorder.recording_dir(false_start).exists()
    rec = db.get_recording(real)
    assert rec["session_number"] == 2
    new_md = subjects.read_course(real)
    assert new_md.startswith("# CM 2 – Arbres\n\n*Graphes — CM 2 du 28 septembre 2026")
    transcript = (recorder.recording_dir(real) / "transcript.txt").read_text(encoding="utf-8")
    assert transcript.startswith("# Transcription — Graphes — CM 2 — 2026-09-28\n")
    assert '"session_number": 2' in (recorder.recording_dir(real) / "meta.json").read_text(encoding="utf-8")
    assert db.get_recording(first)["session_number"] == 1 and db.get_recording(td)["session_number"] == 1
    assert db.next_session_number(sid, "CM") == 3


def test_deletion_keeps_numbers_when_duplicated_and_waits_for_processing(client):
    sid = subjects.create_subject("Graphes")
    a = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-21")  # CM 1
    b = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-21")  # CM 2
    c = recorder.create_recording(subject_id=sid, course_type="CM", session_date="2026-09-28")  # CM 3
    db.update_recording(b, session_number=1)  # deux « CM 1 » : en supprimer un ne laisse aucun trou
    client.post(f"/enregistrements/{b}/delete")
    assert db.get_recording(c)["session_number"] == 3 and db.get_recording(a)["session_number"] == 1

    # Séance suivante en plein traitement : son numéro ne peut pas changer maintenant.
    db.update_recording(c, status="transcribing")
    r = client.post(f"/enregistrements/{a}/delete", data={"back": "/enregistrements"}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["level"] == ["err"]
    assert db.get_recording(a) and db.get_recording(c)["session_number"] == 3
    # Elle-même en traitement : pas de corbeille active.
    assert "Traitement en cours : suppression impossible" in client.get("/enregistrements").text


def test_process_all_launches_every_ready_session(client, monkeypatch):
    sid = subjects.create_subject("Graphes")
    ready = []
    for day in ("2026-09-21", "2026-09-28"):
        rid = recorder.create_recording(subject_id=sid, course_type="CM", session_date=day)
        db.update_recording(rid, status="uploaded", auto_retry_count=2)
        ready.append(rid)
    done_recording(sid)
    page = client.get("/enregistrements").text
    assert "Tout traiter (2)" in page and "Lancer le traitement des 2 s\\u00e9ances" in page  # confirmation (JSON)

    submitted = []
    monkeypatch.setattr(pl.pipeline, "submit", lambda kind, target, action, chain=True: submitted.append((target, action, chain)))
    r = client.post("/enregistrements/tout-traiter", follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["msg"][0].startswith("Traitement lancé pour 2 séances")
    assert submitted == [(rid, "transcribe", True) for rid in ready]  # la plus ancienne d'abord
    assert all(db.get_recording(rid)["auto_retry_count"] == 0 for rid in ready)

    # Déjà en file : plus de bouton, et un second clic ne les relance pas.
    monkeypatch.setattr(pl.pipeline, "is_active", lambda rid: rid in ready)
    assert "Tout traiter" not in client.get("/enregistrements").text
    r = client.post("/enregistrements/tout-traiter", follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["msg"] == ["Aucune séance prête à traiter."]
    assert len(submitted) == 2
