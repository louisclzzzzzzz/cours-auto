from app.markdown_utils import extract_title, replace_title


def test_titles():
    md = "# CM 3 – Graphes\n\nTexte"
    assert extract_title(md) == "CM 3 – Graphes"
    assert replace_title(md, "CM 4 – Autre").startswith("# CM 4 – Autre\n")
    assert replace_title("Texte", "CM 1 – Titre") == "# CM 1 – Titre\n\nTexte"
    assert extract_title("```\n# pas un titre\n```\n# Vrai titre") == "Vrai titre"
