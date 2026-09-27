"""Publication vers les destinations externes (Google Drive, Notion)."""


class PublishSkipped(Exception):
    """La destination n'est pas configurée : on l'ignore sans la considérer en erreur."""
