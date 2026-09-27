Tu aides un système de reconnaissance vocale (Mistral Voxtral, « context biasing ») à mieux transcrire les prochaines séances d'une matière universitaire enseignée en français.

Relève dans le cours fourni les mots ou expressions, **tels qu'ils sont prononcés à l'oral**, qu'un système de reconnaissance vocale risque de mal orthographier :
- noms propres : chercheurs, théorèmes ou algorithmes éponymes, logiciels, langages, bibliothèques ;
- termes techniques rares ou anglicismes ;
- sigles.

Règles :
- **Uniquement des termes qui apparaissent dans le cours fourni.** Ne propose jamais un terme « du domaine » qui n'y figure pas.
- Exclus les mots courants du français, même techniques (arc, sommet, chemin, orienté, fonction, ensemble…).
- Exclus toute notation mathématique, formule ou symbole (pas de $, =, |, indices, exposants, lettres isolées).
- Exclus le nom de l'enseignant et les termes déjà présents dans le vocabulaire actuel (même à la casse près).
- 15 termes maximum, les plus spécifiques d'abord, avec leur orthographe correcte. Une liste vide est une bonne réponse s'il n'y a rien d'utile.

Réponds uniquement en JSON, au format : {"termes": ["terme 1", "terme 2"]}
