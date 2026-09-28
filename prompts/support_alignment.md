Tu reçois la transcription horodatée d'une séance de cours (L1 est en général l'enseignant) et le support de l'enseignant : un ou plusieurs documents `<support n="…" fichier="…">`, découpés en pages `[Page N]` ou `[Diapositive N]` (texte extrait automatiquement).

Pour chaque document, classe ses pages en comparant leur contenu à ce qui est dit dans la transcription :
- `abordees` : pages dont le contenu propre (définition, formule, algorithme, résultat, exemple de la page) est présenté ou expliqué à l'oral — lu, commenté, développé, ou montré (« comme on le voit ici », « sur ce transparent »).
  Ne suffisent pas : une simple allusion au thème, une question d'étudiant sur un sujet voisin, l'annonce d'une notion pour plus tard (« on verra un autre algorithme la semaine prochaine »).
- `non_abordees` : pages au contenu substantiel dont rien n'est présenté ou expliqué à l'oral. En cas de doute, choisis `non_abordees`.
- Ne mets dans aucune des deux listes les pages sans contenu utile : titre, plan, objectifs, logistique, bibliographie, remerciements, pages vides.

Réponds uniquement avec un objet JSON dont les clés sont les numéros de document (`n`), par exemple :
{"1": {"abordees": [2, 3], "non_abordees": [5]}}
