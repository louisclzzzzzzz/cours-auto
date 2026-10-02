"""Version en cours d'exécution (dernier commit Git), affichée dans l'interface : le lanceur Mac met l'app à
jour à chaque ouverture, on voit ainsi d'un coup d'œil quelle version tourne."""

from __future__ import annotations

import subprocess
from datetime import datetime
from functools import lru_cache

from .config import BASE_DIR


@lru_cache(maxsize=1)
def current() -> dict | None:
    """{"commit": "a1b2c3d", "date": "02/10/2026"} ou None hors d'un dépôt Git."""
    try:
        out = subprocess.run(["git", "-C", str(BASE_DIR), "log", "-1", "--format=%h %cI"],
                             capture_output=True, text=True, timeout=5)
        commit, iso = out.stdout.split()
        return {"commit": commit, "date": datetime.fromisoformat(iso).strftime("%d/%m/%Y")}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
