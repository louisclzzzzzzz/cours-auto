"""Interface : pages principales, navigation, choix du cours, liste « Cours », avancement et relance d'un
enregistrement, réglages d'une matière, paramètres."""

from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import calendar_ics, db, pipeline as pl, recorder, subjects
from app.publish import notion
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
                                "notion_status": "done", **fields})
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
    db.update_subject(sid, proposed_terms=db.dumps(["Dijkstra", "Kruskal"]))
    page = client.get("/cours").text
    assert 'class="rows card flush subject-rows"' in page and 'class="grid"' not in page
    assert f'data-href="/cours/{sid}"' in page and "Dernière séance : CM 1 – Graphes" in page
    assert "2 termes à valider" in page
    assert 'id="intitules"' in page and "Algorithmique avancée CM" in page
    # Association depuis la liste : retour sur la section, message conservé avant l'ancre.
    r = client.post("/matieres/associer", data={"summary": "Algorithmique avancée CM", "subject_id": str(sid)},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/cours?msg=") and r.headers["location"].endswith("#intitules")
    assert "Algorithmique avancée CM" not in client.get("/cours").text.split('id="intitules"')[-1]


def test_redirect_keeps_anchor_after_message():
    loc = redirect("/parametres#notion", "OK", "err").headers["location"]
    parsed = urlparse(loc)
    assert parsed.path == "/parametres" and parsed.fragment == "notion"
    assert parse_qs(parsed.query) == {"msg": ["OK"], "level": ["err"]}
    assert redirect("/cours").headers["location"] == "/cours"


def test_validating_proposed_terms_discards_unchecked(client):
    sid = subjects.create_subject("Graphes")
    db.update_subject(sid, proposed_terms=db.dumps(["Dijkstra", "Kruskal", "truc"]))
    r = client.post(f"/matieres/{sid}/propositions", data={"accepted": ["Dijkstra", "Kruskal"], "decision": "accept"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("#vocabulaire")
    subject = db.get_subject(sid)
    assert subjects.get_vocabulary(subject) == ["Dijkstra", "Kruskal"]
    assert subjects.get_proposed_terms(subject) == []  # « truc », décoché, est écarté


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
    db.update_recording(rid, status="error", error_step="publication", drive_status="done", notion_status="error")
    assert rec_routes.retry_action(db.get_recording(rid)) == "publish_notion"
    db.update_recording(rid, drive_status="error")
    assert rec_routes.retry_action(db.get_recording(rid)) == "publish"
    db.update_recording(rid, error_step="état de matière")
    assert rec_routes.retry_action(db.get_recording(rid)) == "state"
    db.update_recording(rid, status="interrupted", error_step=None)
    assert rec_routes.retry_action(db.get_recording(rid)) == "finalize"
    assert steps(status="interrupted")[0] == "warn"
    db.update_recording(rid, status="done")
    assert rec_routes.retry_action(db.get_recording(rid)) is None


def test_detail_page_retry_button_and_polling(client):
    sid = subjects.create_subject("Graphes")
    rid = done_recording(sid, status="error", error_step="publication", notion_status="error",
                         error_message="Notion : indisponible")
    page = client.get(f"/enregistrements/{rid}").text
    assert 'name="action" value="publish_notion"' in page and "Réessayer" in page
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
    assert submitted == [("transcribe", True)]  # transcription → mise en forme → publication

    # En file derrière un autre traitement : plus de bouton, badge « En file d'attente », suivi en direct.
    monkeypatch.setattr(pl.pipeline, "is_active", lambda r: r == rid)
    latest = client.get("/fragments/latest").text
    assert "En file d&#39;attente" in latest and "Lancer le traitement" not in latest and "every 4s" in latest


def test_worker_indicator_is_empty_when_idle(client):
    assert client.get("/fragments/worker").text.strip() == ""


def test_notion_settings_save_and_check_in_one_step(client, monkeypatch):
    monkeypatch.setattr(notion, "test_connection", lambda: (False, "Page racine introuvable : partagez-la."))
    r = client.post("/parametres/notion", data={"notion_root": "9f1c2d3e4b5a60718293a4b5c6d7e8f9"}, follow_redirects=False)
    parsed = urlparse(r.headers["location"])
    assert parsed.fragment == "notion" and parse_qs(parsed.query)["level"] == ["err"]
    assert parse_qs(parsed.query)["msg"][0].startswith("Page enregistrée. Page racine introuvable")
    assert db.get_setting("notion_root") == "9f1c2d3e4b5a60718293a4b5c6d7e8f9"
    page = client.get("/parametres").text
    assert "Enregistrer et vérifier" in page and "/parametres/notion/test" not in page


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
