from pathlib import Path

from app.markdown_utils import (
    extract_title,
    fix_lazy_quotes,
    normalize_course_markdown,
    notion_to_standard,
    remove_first_h1,
    replace_title,
    split_markdown,
    to_notion_markdown,
)

FIX = Path(__file__).parent / "fixtures"


def test_single_line_display_math_is_split():
    # Constaté sur l'API Notion : `$$ … $$` sur une seule ligne est mal interprété.
    md = "Texte\n\n$$ \\int_0^1 f(x)\\,dx $$\n\n> $$x^2$$"
    out = normalize_course_markdown(md)
    assert "$$\n\\int_0^1 f(x)\\,dx\n$$" in out
    assert "> $$\n> x^2\n> $$" in out


def test_display_math_with_content_on_delimiter_lines():
    out = normalize_course_markdown("$$ a = b\n+ c $$")
    assert out.strip() == "$$\na = b\n+ c\n$$"


def test_latex_paren_delimiters_converted():
    out = normalize_course_markdown("Soit \\( G = (V, E) \\) et\n\\[\n\\sum x\n\\]\net ⚠️ \\[passage peu clair ~00:42:10\\]")
    assert "Soit $G = (V, E)$ et" in out
    assert "$$\n\\sum x\n$$" in out
    assert "\\[passage peu clair ~00:42:10\\]" in out  # pas une formule


def test_code_is_left_untouched():
    md = "```python\nx = '$$ y $$'\n```\n\n`\\(a\\)`"
    assert normalize_course_markdown(md) == md + "\n"


def test_table_pipes_inside_math():
    out = normalize_course_markdown("| a | b |\n|---|---|\n| abs | $|x|$ et $\\|v\\|$ |")
    assert "$\\vert x\\vert $" in out and "$\\Vert v\\Vert $" in out


def test_lazy_quote_box_is_repaired():
    md = "> **Définition (Graphe)** :\nUn graphe est un couple.\n- $V$ sommets\n\nTexte normal."
    assert fix_lazy_quotes(md) == "> **Définition (Graphe)** :\n> Un graphe est un couple.\n> - $V$ sommets\n\nTexte normal."


def test_labeled_paragraphs_become_boxes():
    md = (
        "**Définition (Graphe)** :\nUn graphe est un couple $G = (V, E)$ :\n- $V$ sommets\n\n"
        "**Propriété (Lemme)** :\nOn a :\n$$\n\\sum d(v) = 2m\n$$\n\n"
        "**Question** : pourquoi ?\n**Réponse** : parce que.\n\n"
        "**⚠️ Dit en cours :** à savoir.\n\n"
        "> **Exemple** : déjà encadré.\n\n"
        "- **Remarque** dans une liste"
    )
    out = normalize_course_markdown(md)
    assert "> **Définition (Graphe)** :\n> Un graphe est un couple $G = (V, E)$ :\n> - $V$ sommets\n\n" in out
    assert "> **Propriété (Lemme)** :\n> On a :\n> $$\n> \\sum d(v) = 2m\n> $$\n\n" in out
    assert "**Question** : pourquoi ?\n**Réponse** : parce que." in out and "> **Question**" not in out
    assert "**⚠️ Dit en cours :** à savoir." in out and "> **⚠️" not in out
    assert "> **Exemple** : déjà encadré." in out and "> > " not in out
    assert "- **Remarque** dans une liste" in out


def test_titles():
    md = "# CM 3 – Graphes\n\nTexte"
    assert extract_title(md) == "CM 3 – Graphes"
    assert replace_title(md, "CM 4 – Autre").startswith("# CM 4 – Autre\n")
    assert remove_first_h1(md) == "Texte"


def test_to_notion_callouts_and_escaping():
    md = normalize_course_markdown(
        "# Titre\n\n"
        "> 💡 **Complément** : ligne 1\n> ligne 2\n\n"
        "> **Définition (Arbre)** : un graphe connexe sans cycle.\n\n"
        "> Citation simple\n\n"
        "> Ligne A\n> Ligne B\n\n"
        "Une balise <callout> et $a < b$ et `x<y`\n"
    )
    out = to_notion_markdown(md)
    assert not out.startswith("# Titre")
    assert '<callout icon="💡" color="yellow_bg">\n\t**Complément** : ligne 1\n\tligne 2\n</callout>' in out
    assert '<callout icon="📘" color="blue_bg">\n\t**Définition (Arbre)** : un graphe connexe sans cycle.\n</callout>' in out
    assert "> Citation simple" in out
    assert "> Ligne A<br>Ligne B" in out
    assert "Une balise \\<callout> et $a < b$ et `x<y`" in out


def test_notion_readback_real_output_1():
    out = notion_to_standard((FIX / "notion_readback_1.md").read_text(encoding="utf-8"), title="Titre")
    assert out.startswith("# Titre\n\n## 1. Introduction\n\nSoit $G = (V, E)$ un graphe")
    assert "$$\n\\sum_{i=1}^{n} i = \\frac{n(n+1)}{2}\n$$" in out
    assert "⚠️ [passage peu clair ~00:42:10]" in out
    assert "| Dijkstra | $O((n+m)\\log n)$ |" in out
    assert "- Point 1\n    - Sous-point a\n- Point 2\n\n1. Étape un\n2. Étape deux" in out
    assert "```python\ndef f(x):\n    return x < 3\n```" in out


def test_notion_readback_real_output_2():
    out = notion_to_standard((FIX / "notion_readback_2.md").read_text(encoding="utf-8"))
    assert "> 💡 **Complément** : ligne 1 avec $x^2$.\n>\n> Ligne 2 du complément.\n>\n> $$\n> \\int_0^1 x\\,dx = \\frac{1}{2}\n> $$\n>\n> - item dans callout" in out
    assert "> Citation ligne 1\n> ligne 2 avec $y$" in out
    assert "### Titre avec $\\mathcal{O}(n)$ math" in out
    assert "| abs | $\\lvert x \\rvert$ |" in out
    assert "- Liste avec formule bloc :\n    $$\n    E = mc^2\n    $$" in out
    assert "Texte coloré et @Alice." in out
    assert "<unknown" not in out and "<empty-block" not in out and "{color" not in out


def test_round_trip_keeps_generated_structure():
    course = normalize_course_markdown(
        "# CM 1 – T\n\n## 1. Partie\n\n> 💡 **Complément** : a\n> b\n\n> **Définition (X)** : y\n\nTexte $x$."
    )
    # Simulation minimale de la lecture Notion : maths $`…`$, un bloc par ligne.
    notion_md = to_notion_markdown(course).replace("$x$", "$`x`$").replace("\n\n", "\n")
    back = notion_to_standard(notion_md, title="CM 1 – T")
    assert back == "# CM 1 – T\n\n## 1. Partie\n\n> 💡 **Complément** : a\n>\n> b\n\n> **Définition (X)** : y\n\nTexte $x$.\n"


def test_split_markdown_respects_blocks():
    md = "\n\n".join([f"## S{i}\n\n" + "x " * 200 for i in range(6)] + ["<callout icon=\"💡\">\n\ta\n\n\tb\n</callout>", "$$\na\n\nb\n$$"])
    parts = split_markdown(md, 1000)
    assert len(parts) >= 3 and all(len(p) <= 1000 for p in parts)
    assert all(p.count("<callout") == p.count("</callout>") for p in parts)
    assert all(p.count("$$") % 2 == 0 for p in parts)
    assert "\n\n".join(parts).replace("\n", "") == md.replace("\n", "")
