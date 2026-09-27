"""Connexion Google Drive : flux « application de bureau » (navigateur par défaut + serveur temporaire)."""

import json
import time
from types import SimpleNamespace

import httpx
import pytest
from googleapiclient.errors import HttpError

from app import config
from app.publish import drive


class FakeFlow:
    """Imite google_auth_oauthlib.flow.Flow (pas d'appel à Google)."""

    instances: list = []

    def __init__(self):
        self.redirect_uri = None
        self.code = None
        FakeFlow.instances.append(self)

    def authorization_url(self, **kwargs):
        return f"https://accounts.example/auth?redirect_uri={self.redirect_uri}&state=etat-123", "etat-123"

    def fetch_token(self, code):
        self.code = code

    @property
    def credentials(self):
        return SimpleNamespace(to_json=lambda: json.dumps({"token": "t", "refresh_token": "r"}))


@pytest.fixture
def oauth(monkeypatch):
    config.CREDENTIALS_FILE.write_text(json.dumps({"installed": {"project_id": "projet-exemple", "client_id": "x"}}))
    FakeFlow.instances.clear()
    opened = []
    monkeypatch.setattr(drive.Flow, "from_client_secrets_file", staticmethod(lambda *a, **k: FakeFlow()))
    monkeypatch.setattr(drive.webbrowser, "open", lambda url, **k: opened.append(url) or True)
    drive._auth.clear()
    yield opened
    drive._auth.clear()


def wait_status(timeout=5.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        session = drive.auth_session()
        if session.get("status") != "waiting":
            return session
        time.sleep(0.05)
    raise AssertionError("la connexion est restée en attente")


def test_browser_auth_success(oauth):
    done = []
    session = drive.start_browser_auth(on_success=lambda: done.append(True))
    assert session["status"] == "waiting" and session["browser_opened"] is True
    flow = FakeFlow.instances[0]
    assert flow.redirect_uri.startswith("http://127.0.0.1:") and oauth == [session["url"]]
    # Le navigateur revient sur le serveur temporaire avec le code d'autorisation.
    page = httpx.get(flow.redirect_uri, params={"state": "etat-123", "code": "code-abc"})
    assert "réussie" in page.text
    session = wait_status()
    assert session["status"] == "done"
    assert flow.code == "code-abc"
    assert json.loads(config.TOKEN_FILE.read_text())["refresh_token"] == "r"
    assert done == [True]


def test_browser_auth_second_click_reuses_pending_session(oauth):
    first = drive.start_browser_auth()
    second = drive.start_browser_auth()
    assert first["url"] == second["url"] and len(FakeFlow.instances) == 1
    assert len(oauth) == 2  # la page Google est simplement rouverte
    httpx.get(FakeFlow.instances[0].redirect_uri, params={"state": "etat-123", "error": "access_denied"})
    wait_status()


def test_browser_auth_access_denied_explains_test_mode(oauth):
    drive.start_browser_auth()
    page = httpx.get(FakeFlow.instances[0].redirect_uri, params={"state": "etat-123", "error": "access_denied"})
    assert "refusée" in page.text
    session = wait_status()
    assert session["status"] == "error" and "Test" in session["message"]
    assert not config.TOKEN_FILE.exists()


def test_browser_auth_rejects_state_mismatch(oauth):
    drive.start_browser_auth()
    httpx.get(FakeFlow.instances[0].redirect_uri, params={"state": "autre", "code": "c"})
    session = wait_status()
    assert session["status"] == "error" and "inattendue" in session["message"]
    assert not config.TOKEN_FILE.exists()


def test_browser_auth_cancel(oauth):
    first = drive.start_browser_auth()
    drive.cancel_browser_auth()
    session = wait_status()
    assert session["status"] == "error" and session["message"] == "Connexion annulée."
    # Une nouvelle tentative crée une nouvelle session (nouvelle URL, nouveau serveur temporaire).
    second = drive.start_browser_auth()
    assert second["status"] == "waiting" and len(FakeFlow.instances) == 2
    httpx.get(FakeFlow.instances[1].redirect_uri, params={"state": "etat-123", "code": "c"})
    assert wait_status()["status"] == "done"
    assert first["url"]  # (même format d'URL, serveur différent)


def test_console_links_and_client_type(oauth):
    assert drive.project_id() == "projet-exemple" and drive.client_type() == "installed"
    assert drive.console_links()["api"].endswith("drive.googleapis.com?project=projet-exemple")


def test_api_disabled_error_is_explained(oauth):
    content = b'{"error": {"code": 403, "message": "Google Drive API has not been used in project 123 before or it is disabled.", "status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}'

    class Req:
        def execute(self, num_retries=0):
            raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), content)

    with pytest.raises(drive.DriveAuthError) as err:
        drive.DriveClient._exec(Req())
    assert "API Google Drive n'est pas activée" in str(err.value) and "projet-exemple" in str(err.value)


def test_settings_page_and_old_callback_link(oauth):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        page = client.get("/parametres").text
        assert "Aide à la connexion" in page and "projet-exemple" in page
        r = client.post("/drive/connect")
        assert "Connexion en cours dans votre navigateur" in r.text
        httpx.get(FakeFlow.instances[0].redirect_uri, params={"state": "etat-123", "code": "c"})
        wait_status()
        r = client.get("/fragments/drive-auth")
        assert r.headers.get("HX-Refresh") == "true" and "connecté" in r.text
        # Un ancien lien de retour sur la racine renvoie vers Paramètres avec une explication.
        r = client.get("/?state=x&code=y", follow_redirects=False)
        assert r.status_code == 303 and "/parametres" in r.headers["location"]
