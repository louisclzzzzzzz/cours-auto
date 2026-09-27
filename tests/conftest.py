"""Configuration des tests : données dans un dossier temporaire, aucun secret, aucun appel réseau."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="cours-auto-tests-"))
os.environ["COURS_DATA_DIR"] = str(_TMP / "data")
os.environ["GOOGLE_CREDENTIALS_FILE"] = str(_TMP / "credentials.json")
os.environ["GOOGLE_TOKEN_FILE"] = str(_TMP / "token.json")
# Valeurs vides : load_dotenv() ne remplace pas une variable déjà définie → .env ignoré pendant les tests.
os.environ["MISTRAL_API_KEY"] = ""
os.environ["NOTION_TOKEN"] = ""
os.environ["NOTION_URL"] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from app import config, db  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_data():
    """Base et dossiers de données vierges pour chaque test."""
    shutil.rmtree(config.DATA_DIR, ignore_errors=True)
    for f in (config.CREDENTIALS_FILE, config.TOKEN_FILE):
        if f.exists():
            f.unlink()
    db.init_db()
    yield


@pytest.fixture
def ics_bytes() -> bytes:
    return (FIXTURES / "ade_sample.ics").read_bytes()
