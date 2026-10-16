"""Faux service Google Drive (en mémoire) et petit PDF de test."""

from __future__ import annotations

import hashlib
import itertools
import re
from types import SimpleNamespace

from googleapiclient.errors import HttpError

# --- Drive -----------------------------------------------------------------------------------------


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries: int = 0):
        return self._fn()


class FakeDriveFiles:
    def __init__(self, store: dict, log: list):
        self.store = store
        self.log = log
        self.corrupt_uploads = False  # Drive annonce une somme MD5 différente de celle du fichier envoyé
        self._ids = itertools.count(1)
        self._clock = itertools.count(1)

    def _public(self, f: dict) -> dict:
        public = {k: f[k] for k in ("id", "name", "mimeType", "trashed", "webViewLink", "modifiedTime")}
        content = f.get("content")
        if isinstance(content, bytes):  # fichiers « binaires » : Drive calcule taille et MD5
            public["size"] = str(len(content))
            public["md5Checksum"] = f.get("md5") or hashlib.md5(content).hexdigest()
        return public

    def _seal(self, f: dict) -> None:
        """Drive calcule la somme MD5 à l'envoi (ici fausse si `corrupt_uploads`) et ne la recalcule pas ensuite."""
        if isinstance(f.get("content"), bytes):
            f["md5"] = "bad" if self.corrupt_uploads and f.get("source_mime") else hashlib.md5(f["content"]).hexdigest()

    def _touch(self, f: dict) -> None:
        f["modifiedTime"] = f"2026-10-06T00:00:{next(self._clock):02d}.000Z"

    def get(self, fileId, fields=None):
        def run():
            f = self.store.get(fileId)
            if not f:
                raise HttpError(SimpleNamespace(status=404, reason="Not Found"), b"not found")
            return self._public(f)
        return _Req(run)

    def create(self, body, media_body=None, fields=None):
        def run():
            fid = f"f{next(self._ids)}"
            mime = body.get("mimeType") or (media_body.mimetype() if media_body else "application/octet-stream")
            f = {
                "id": fid, "name": body["name"], "mimeType": mime, "parents": body.get("parents", []),
                "trashed": False, "webViewLink": f"https://drive.example/{fid}",
                "content": media_body.getbytes(0, media_body.size()) if media_body else None,
                "source_mime": media_body.mimetype() if media_body else None,
            }
            self._seal(f)
            self._touch(f)
            self.store[fid] = f
            self.log.append(("create", fid, body["name"]))
            return self._public(f)
        return _Req(run)

    def update(self, fileId, body=None, media_body=None, fields=None):
        def run():
            f = self.store[fileId]
            if body and "name" in body:
                f["name"] = body["name"]
            if body and "trashed" in body:
                f["trashed"] = body["trashed"]
            if media_body is not None:
                f["content"] = media_body.getbytes(0, media_body.size())
                f["source_mime"] = media_body.mimetype()
                self._seal(f)
            self._touch(f)
            self.log.append(("update", fileId, f["name"]))
            return self._public(f)
        return _Req(run)

    def list(self, q, pageSize=None, pageToken=None, fields=None, orderBy=None):
        parent = q.split("'")[1]  # « '<id>' in parents and trashed = false [and name = '…'] [and mimeType = '…'] »
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        name = re.sub(r"\\(.)", r"\1", name.group(1)) if name else None
        mime = re.search(r"mimeType (=|!=) '([^']*)'", q)

        def run():
            files = [f for f in self.store.values() if parent in f["parents"] and not f["trashed"]
                     and (name is None or f["name"] == name)
                     and (mime is None or (f["mimeType"] == mime.group(2)) == (mime.group(1) == "="))]
            return {"files": [self._public(f) for f in files]}
        return _Req(run)

    def get_media(self, fileId):
        return _Req(lambda: self.store[fileId]["content"])


class FakeDriveService:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.log: list[tuple] = []
        self._files = FakeDriveFiles(self.store, self.log)

    def files(self):
        return self._files

    def by_name(self, name: str) -> dict:
        matches = [f for f in self.store.values() if f["name"] == name and not f["trashed"]]
        assert len(matches) == 1, f"{name}: {len(matches)} fichier(s)"
        return matches[0]

    def text(self, name: str) -> str:
        return self.by_name(name)["content"].decode("utf-8")

    def add_external(self, name: str, parent: str, content: str) -> dict:
        """Fichier écrit par un autre outil (la tâche Claude) : l'app ne peut que le lire."""
        fid = f"ext{next(self._files._ids)}"
        f = {"id": fid, "name": name, "mimeType": "text/markdown", "parents": [parent], "trashed": False,
             "webViewLink": f"https://drive.example/{fid}", "content": content.encode("utf-8"), "source_mime": None}
        self._files._touch(f)
        self.store[fid] = f
        return f

    def add_folder(self, name: str, parent: str | None = None) -> dict:
        """Dossier créé à la main dans Drive (hors de l'app)."""
        fid = f"man{next(self._files._ids)}"
        f = {"id": fid, "name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent] if parent else [],
             "trashed": False, "webViewLink": f"https://drive.example/{fid}", "content": None, "source_mime": None}
        self._files._touch(f)
        self.store[fid] = f
        return f

    def add_blob(self, name: str, parent: str, content: bytes) -> dict:
        """Fichier envoyé à la main dans Drive (hors de l'app)."""
        f = self.add_folder(name, parent)
        f.update(mimeType="application/octet-stream", content=content, md5=hashlib.md5(content).hexdigest())
        return f

    def children(self, parent: str) -> list[dict]:
        return sorted((f for f in self.store.values() if parent in f["parents"] and not f["trashed"]), key=lambda f: f["name"])

    def rewrite_external(self, fid: str, content: str) -> None:
        self.store[fid]["content"] = content.encode("utf-8")
        self._files._touch(self.store[fid])


class FakeDownloader:
    """Remplace `MediaIoBaseDownload` : le faux service renvoie le contenu d'un coup."""

    def __init__(self, fd, request, chunksize=None):
        self.fd, self.request = fd, request

    def next_chunk(self, num_retries=0):
        self.fd.write(self.request.execute())
        return None, True


# --- Document de test (support de cours) --------------------------------------------------------


def make_pdf(path, pages: list[list[str]]) -> bytes:
    """PDF minimal (Helvetica, une ligne de texte par élément) lisible par pypdf."""

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    body: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
                              3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"}
    page_ids = []
    for i, lines in enumerate(pages):
        pid, cid = 4 + 2 * i, 5 + 2 * i
        page_ids.append(pid)
        ops = ["BT", "/F1 18 Tf", "72 760 Td", "22 TL", *[f"({esc(line)}) Tj T*" for line in lines], "ET"]
        stream = "\n".join(ops).encode("cp1252")
        body[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
                     f"/Contents {cid} 0 R >>").encode()
        body[cid] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
    body[2] = f"<< /Type /Pages /Kids [{' '.join(f'{p} 0 R' for p in page_ids)}] /Count {len(pages)} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for i in sorted(body):
        offsets[i] = len(out)
        out += f"{i} 0 obj\n".encode() + body[i] + b"\nendobj\n"
    xref, size = len(out), max(body) + 1
    out += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offsets[i]:010d} 00000 n \n".encode() for i in range(1, size))
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return bytes(out)
