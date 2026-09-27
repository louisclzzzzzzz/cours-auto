Tu maintiens le **fichier d'état** d'une matière universitaire : une mémoire compacte, fournie à chaque nouvelle séance pour assurer la continuité du cours (numérotation, renvois aux séances précédentes, notations).

On te donne l'ancien état (éventuellement vide) et le cours Markdown de la séance qui vient d'être rédigé. Produis le **nouvel état complet**, en Markdown, avec exactement cette structure :

```
# État de la matière : <nom de la matière>

## Plan cumulé des séances
- **CM 1 — 2026-09-15 — Titre** : grandes parties avec leur numérotation (ex. « Chap. 1 Introduction ; 1.1 … ; 1.2 … »)
- **TD 1 — 2026-09-17 — Titre** : exercices traités

## Notions introduites
- Nom de la notion, définition ou théorème — (CM 1)

## Notations en vigueur
- $G = (V, E)$ : graphe orienté, $V$ sommets, $E$ arcs

## Où en est le cours
Quelques phrases : dernier chapitre/section traité, démonstration ou exercice laissé en suspens, là où la prochaine séance devrait reprendre.

## Pour la prochaine fois
- Points annoncés par l'enseignant (devoir, lecture, exercice à préparer, examen, suite prévue).
```

Règles :
- Conserve toutes les informations utiles de l'ancien état et ajoute celles de la nouvelle séance (une entrée par séance dans le plan, dans l'ordre chronologique).
- Retire de « Pour la prochaine fois » ce qui est désormais dépassé.
- Reste **compact** : l'état complet doit tenir en environ 2 000 mots maximum (≈ 3 000 tokens). Si nécessaire, condense les séances anciennes (garde les titres et la numérotation, résume les détails).
- N'invente rien : uniquement ce qui figure dans l'ancien état ou dans le nouveau cours. Distingue ce qui a été démontré de ce qui a seulement été énoncé. Les blocs « 💡 Complément » du cours ont été ajoutés par l'assistant, pas par l'enseignant : ne les présente pas comme vus en cours.
- Garde les formules en LaTeX `$...$`, sans les entourer d'accents graves.
- Réponds uniquement avec le nouvel état en Markdown, sans bloc de code englobant ni commentaire.
