"""Échanges avec Google Drive : dépôt des transcriptions, récupération des cours."""


class PublishSkipped(Exception):
    """La destination n'est pas configurée : on l'ignore sans la considérer en erreur."""
