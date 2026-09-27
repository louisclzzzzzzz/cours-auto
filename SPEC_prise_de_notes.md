# Spec : App locale de prise de notes de cours automatisée

> Document de cadrage destiné à Claude Code. Lis-le en entier avant de coder, puis implémente phase par phase (section 11) en validant chaque phase avant de passer à la suivante.

## 1. Objectif

Application web **locale** (tourne sur mon PC portable, ouverte dans le navigateur) qui me permet, en CM/TD/TP :

1. de choisir le cours que j'enregistre à partir de mon emploi du temps importé (.ics) ;
2. de lancer / mettre en pause / arrêter un enregistrement audio ;
3. de faire traiter automatiquement l'enregistrement : transcription (Mistral Voxtral) → mise en forme du cours par LLM (Mistral) → publication ;
4. de consulter les derniers enregistrements, leur statut, les cours générés, et d'accéder directement aux pages Notion et aux fichiers/dossiers Drive.

**Rôle de chaque destination :**
- **Notion** : lecture et annotation des cours (une page par séance, organisée en bases de données).
- **Google Drive** : archive complète en **Markdown** + source pour **NotebookLM**.
- **Disque local** : source de vérité de tout ce que l'app génère.

Le cours généré doit rester **fidèle** à ce qu'a dit l'enseignant. Des compléments (précisions, rappels, liens avec les cours précédents) sont autorisés mais **toujours balisés visiblement**. Chaque nouveau cours s'insère à la suite des précédents de la même matière avec une continuité logique (numérotation, renvois, notations cohérentes).

Utilisateur unique (moi), usage personnel, pas d'authentification applicative.

## 2. Stack imposée

- **Python 3.12**, gestion des dépendances avec `uv` (`pyproject.toml`).
- **Backend** : FastAPI + Uvicorn, écoute uniquement sur `127.0.0.1`.
- **Frontend** : templates Jinja2 servis par FastAPI + HTMX pour les mises à jour partielles + JS vanilla pour l'enregistreur (MediaRecorder). Pas de framework JS, pas de build step.
- **Rendu Markdown dans l'UI** : marked.js + KaTeX (CDN), formules `$...$` et `$$...$$`.
- **Persistance** : SQLite (via `sqlite3` ou SQLModel) + fichiers sur disque dans `data/`.
- **Mistral** : SDK officiel `mistralai`.
- **Google Drive** : `google-api-python-client`, `google-auth-oauthlib`.
- **Notion** : `httpx` directement sur l'API REST (le SDK Python `notion-client` peut ne pas couvrir les endpoints Markdown récents ; l'utiliser seulement s'il les supporte).
- **Emploi du temps** : `icalendar` + `recurring-ical-events`.
- **Audio** : `ffmpeg` (appel système) pour la concaténation/conversion.
- Secrets dans `.env` (`MISTRAL_API_KEY`, `NOTION_TOKEN`), jamais commités. `.gitignore` doit couvrir `.env`, `data/`, `credentials.json`, `token.json`.

## 3. Points à vérifier dans la documentation avant d'implémenter

Ne pas supposer, vérifier dans la doc officielle actuelle :

- **Mistral transcription** (`client.audio.transcriptions.complete`) : noms exacts des paramètres pour la langue, la diarisation, le context biasing (liste de termes, max 100), les timestamps ; formats audio acceptés et taille max de fichier. Modèle : `voxtral-mini-latest` (Voxtral Mini Transcribe V2, jusqu'à ~3 h d'audio par requête).
- **Mistral LLM** : `mistral-large-latest` par défaut, configurable. Vérifier taille de contexte et `max_tokens` de sortie max.
- **Notion API** (version `2026-03-11` ou plus récente) :
  - création de page avec le paramètre `markdown` (`POST /v1/pages`), lecture (`GET /v1/pages/:id/markdown`), modification (`PATCH /v1/pages/:id/markdown`, dont `insert_content.position`) ; option `allow_async` pour les contenus longs ;
  - modèle bases de données / *data sources* (depuis l'API `2025-09-03`, les pages d'une base se créent sous un `data_source_id`) ;
  - **conversion du LaTeX** : vérifier comment le Markdown Notion traite `$...$` et `$$...$$` (doit donner des équations inline / blocs équation). Tester dès la phase Notion ; si la conversion est incorrecte, pré-traiter le Markdown ou créer les blocs équation via l'API blocs ;
  - limites de débit (prévoir un throttling ~3 req/s et retry sur 429).
- **Drive API v3** : upload/update de fichiers `.md`, **conversion à l'upload** de Markdown vers Google Doc (`mimeType` cible `application/vnd.google-apps.document` avec contenu `text/markdown`), création de dossiers, champ `webViewLink`.

## 4. Fonctionnalités

### 4.1 Emploi du temps

- Paramètre : URL d'export ICS (ADE de l'ISIMA) **ou** fichier `.ics` importé.
- Rafraîchissement à l'ouverture de l'app et via un bouton, avec cache local (l'app doit fonctionner hors ligne avec le dernier EDT connu).
- Expansion des événements récurrents.
- Chaque créneau affiché : horaire, intitulé brut ADE, salle, enseignant si présent, type déduit (CM/TD/TP, depuis l'intitulé, modifiable).
- **Mapping ADE → Matière** : les intitulés ADE sont souvent bruités. Une table de correspondance (regex ou intitulé exact → matière) est éditable dans l'UI. Si un intitulé n'est pas mappé, proposer de créer la matière ou de l'associer à une existante au moment de la sélection.

### 4.2 Enregistrement

- Écran d'accueil : liste des créneaux du jour, le **créneau en cours pré-sélectionné**. Je sélectionne manuellement le cours que j'enregistre (possibilité de choisir un créneau d'un autre jour ou « hors EDT » avec matière + type à la main).
- Boutons Démarrer / Pause / Reprendre / Arrêter, chronomètre, vumètre du micro, choix du périphérique d'entrée.
- **Robustesse (critique : un cours dure jusqu'à 2 h)** :
  - MediaRecorder avec `timeslice` (~30 s) ; chaque chunk est envoyé immédiatement au backend et écrit sur disque (`data/recordings/<id>/chunk_XXXX.webm`).
  - Si l'onglet se ferme ou le PC se met en veille, les chunks déjà reçus sont conservés ; l'enregistrement apparaît comme « interrompu » avec possibilité de le finaliser tel quel.
  - Empêcher la mise en veille de l'écran pendant l'enregistrement (Wake Lock API si dispo) et avertir avant de quitter la page.
- À l'arrêt : concaténation des chunks avec ffmpeg en un seul fichier dans un format accepté par Voxtral (ex. mp3 mono 16 kHz, à confirmer), puis mise en file du traitement.

### 4.3 Pipeline de traitement

Traitement en arrière-plan (file de tâches simple en thread/asyncio, un traitement à la fois). Chaque enregistrement a un statut persistant :

`recording → uploaded → transcribing → transcribed → formatting → formatted → publishing → done` (+ `error` avec message et étape en échec).

La publication a un **sous-statut par destination** (`drive`, `notion`) : l'échec de l'une ne bloque pas l'autre et chacune se relance séparément.

- Chaque étape est **idempotente et relançable individuellement** depuis l'UI. Les résultats intermédiaires sont conservés sur disque : audio, transcription brute (JSON avec segments/locuteurs), cours Markdown, état de matière.
- Retry avec backoff sur erreurs 429/5xx.

**Étape 1 : Transcription**
- Langue : français.
- Diarisation activée ; conserver les segments avec locuteur et timestamps.
- Context biasing : vocabulaire de la matière (section 5.2), max 100 termes.
- Sauvegarder `transcript.json` (brut) et `transcript.txt` (lisible, avec locuteurs et timestamps).

**Étape 2 : Mise en forme du cours (appel LLM n°1)**
- Entrées : transcription lisible, fichier d'état de la matière, métadonnées (matière, type CM/TD/TP, date, numéro de séance, enseignant).
- Sortie : le cours en Markdown (voir règles section 6).
- Si la transcription est trop longue pour le contexte, découper par blocs cohérents (sur les pauses/segments) et assembler, en passant l'état + le plan en cours à chaque bloc.

**Étape 3 : Mise à jour de l'état de matière (appel LLM n°2)**
- Entrées : ancien état + cours Markdown qui vient d'être généré.
- Sortie : nouvel état (section 5.1) en Markdown.
- Deux appels séparés plutôt qu'un seul JSON géant, pour éviter les sorties tronquées.
- Proposer aussi de nouveaux termes de vocabulaire détectés (ajout soumis à ma validation dans l'UI, pas automatique).

**Étape 4 : Publication** vers Drive (section 7) et Notion (section 8).

## 5. Données par matière

### 5.1 Fichier d'état (`_etat.md`)

Mémoire compacte de la matière, envoyée à chaque appel LLM au lieu de l'historique complet (doit rester sous ~3 000 tokens) :

- plan cumulé des séances (numéro, date, type, titre, grandes parties) ;
- notions, définitions et théorèmes déjà introduits (nom + séance où ils sont apparus) ;
- notations en vigueur ;
- résumé de la fin de la dernière séance (où en est le cours) ;
- éventuels points annoncés « pour la prochaine fois ».

Éditable à la main dans l'UI.

### 5.2 Vocabulaire

Liste de termes (≤ 100) par matière pour le context biasing : noms propres, termes techniques, noms d'algorithmes. Éditable dans l'UI.

### 5.3 Autres

Nom, code/intitulés ADE associés, enseignants, compteur de séances par type, et identifiants externes : dossier Drive, Google Doc NotebookLM, page Notion de la matière.

## 6. Règles de mise en forme (prompt du LLM n°1)

Le prompt système doit être dans un fichier séparé (`prompts/format_course.md`) facilement modifiable. Il doit imposer :

1. **Fidélité** : ne rien inventer qui soit présenté comme dit par l'enseignant. Reformuler l'oral en écrit propre, supprimer hésitations, répétitions et digressions sans intérêt, mais garder tous les contenus (définitions, exemples, remarques, avertissements du type « ça tombe à l'examen »).
2. **Compléments balisés** : toute précision, correction d'une erreur de transcription probable ou rappel ajouté par le modèle va dans un bloc distinct :
   `> 💡 **Complément** : ...`
   Les renvois aux séances précédentes : `(cf. CM 3 – Titre)`.
3. **Passages douteux** : si la transcription est incompréhensible ou ambiguë, le signaler `⚠️ [passage peu clair ~00:42:10]` au lieu de deviner.
4. **Structure** : titre de séance, puis sections hiérarchisées ; numérotation continue avec les séances précédentes si le cours la suit (chapitres/parties) ; encadrés pour définitions, théorèmes, propriétés, exemples.
5. **Maths** : LaTeX (`$...$` en ligne, `$$...$$` en bloc), notations cohérentes avec l'état de matière.
6. **TD/TP** : organiser par exercice (énoncé résumé si donné oralement, méthode, correction, pièges signalés).
7. **Interventions des étudiants** : ne garder que les questions/réponses utiles, reformulées (« Question : ... / Réponse : ... »).
8. En fin de document : une section « Points clés » (5 à 10 puces) et « À retenir pour la suite » si l'enseignant a annoncé quelque chose.

Rester sur un Markdown « simple » compatible avec Notion et Drive (titres, listes, citations, gras/italique, tableaux, code, LaTeX) ; pas de HTML.

## 7. Google Drive : archive Markdown + NotebookLM

### 7.1 Auth

- OAuth 2.0, client de type « Desktop app », fichier `credentials.json` fourni par moi. Scope **`drive.file` uniquement** (accès aux seuls fichiers créés par l'app). Token stocké dans `token.json`, rafraîchi automatiquement.
- README : procédure pour créer le projet Google Cloud, activer Drive API, écran de consentement « External », puis **publier l'app en Production** (sinon les refresh tokens expirent tous les 7 jours en mode Testing).
- L'UI affiche l'état de connexion et un bouton « Se connecter / Reconnecter ». En cas de token invalide, la publication Drive passe en erreur proprement sans perdre le travail.

### 7.2 Arborescence

Créée par l'app (IDs stockés en base, puisque `drive.file` ne voit pas les dossiers créés à la main) :

```
Cours M1/
  <Matière>/
    <Matière> – Cours complet.md          # concaténation ordonnée de toutes les séances
    <Matière> – NotebookLM                # Google Doc, même contenu (voir 7.3)
    _etat.md
    Séances/
      2026-09-29_CM03_<titre-court>.md
```

- Tous les cours sont stockés en **`.md`** : un fichier par séance + le cours complet.
- Le « Cours complet » est régénéré à partir des séances locales (source de vérité = disque local), dans l'ordre chronologique, avec une table des matières en tête.
- Option (désactivée par défaut) : uploader aussi l'audio et la transcription dans `Sources/`.
- Stocker le `webViewLink` de chaque fichier et dossier.

### 7.3 Google Doc miroir pour NotebookLM

NotebookLM ne synchronise automatiquement que les **fichiers Google natifs** (Docs, Sheets, Slides) ; un `.md` ajouté comme source est figé au moment de l'import et devrait être ré-importé à chaque nouvelle séance. Donc, en plus des `.md` :

- un **Google Doc par matière** (« <Matière> – NotebookLM »), contenu identique au cours complet, créé par conversion Markdown → Google Doc à l'upload, puis **mis à jour sur le même ID de fichier** à chaque séance (ne jamais recréer : NotebookLM perdrait la source) ;
- je l'ajoute une fois comme source dans NotebookLM, il se met ensuite à jour tout seul ;
- le LaTeX y reste en texte brut (non rendu), ce qui ne gêne pas NotebookLM ;
- paramètre pour désactiver cette fonctionnalité.

## 8. Notion : lecture et annotation

### 8.1 Auth

- Intégration interne Notion, token dans `NOTION_TOKEN`. README : créer l'intégration, puis partager avec elle la page racine « Cours M1 ».
- Dans Paramètres : saisie de l'ID/URL de la page racine, bouton de test de connexion.

### 8.2 Structure

Créée par l'app sous la page racine (IDs stockés en base) :

- **Base « Matières »** : Nom (titre), Enseignant(s), lien Drive (URL), lien Google Doc NotebookLM (URL).
- **Base « Séances »** : Titre, Matière (relation vers Matières), Type (select CM/TD/TP), Numéro (nombre), Date, Durée, Statut (select), Lien Drive (URL du `.md`), Enseignant.
- Contenu de chaque page Séance = le cours Markdown.
- Vues suggérées (à créer si l'API le permet, sinon documenter dans le README) : par matière triée par date, calendrier, « dernières séances ».

### 8.3 Annotations : règle de non-écrasement

Je vais annoter les pages Notion. Donc :

- **Après sa création, l'app ne réécrit jamais le contenu d'une page Séance.**
- Si je relance la mise en forme d'une séance déjà publiée : l'app crée une **nouvelle version** de la page (titre suffixé « v2 », ancienne page conservée) ou me demande confirmation avant d'écraser, avec avertissement clair.
- Les propriétés (statut, liens) peuvent être mises à jour sans risque.

### 8.4 Rapatrier les annotations vers Drive (bouton, pas automatique)

- Action « Importer mes annotations Notion » (par séance et par matière) : lit la page via `GET /v1/pages/:id/markdown`, enregistre une copie `<fichier>_annote.md` en local et sur Drive, et utilise cette version annotée dans le cours complet et le Google Doc NotebookLM.
- Le fichier généré d'origine reste conservé intact.

## 9. Interface

Pages (navigation simple en haut) :

1. **Enregistrer** (accueil) : créneaux du jour, sélection du cours, contrôles d'enregistrement, statut en direct du dernier traitement.
2. **Enregistrements** : liste chronologique (date, matière, type, durée, statut avec sous-statuts Drive/Notion), actions : écouter, voir la transcription, relancer une étape ou une publication, supprimer (avec confirmation), finaliser un enregistrement interrompu.
3. **Cours** : par matière, liste des séances ; aperçu rendu (Markdown + KaTeX) ; liens **« Ouvrir dans Notion »**, « Fichier Drive », « Cours complet », « Google Doc NotebookLM », « Dossier Drive » ; bouton « Importer mes annotations Notion ».
4. **Matières** : liste, mapping ADE, vocabulaire, état de matière (voir/éditer), validation des termes proposés.
5. **Paramètres** : URL/fichier ICS, connexion Drive, connexion Notion (page racine), test de la clé Mistral, choix des modèles, options (upload audio, Google Doc NotebookLM).

Statuts mis à jour en direct (polling HTMX ou SSE). Interface sobre, lisible, thème clair/sombre selon le système.

## 10. Structure de projet suggérée

```
app/
  main.py            # FastAPI, routes pages + API
  config.py
  db.py              # modèles et accès SQLite
  calendar_ics.py    # import/parse EDT, mapping ADE → matière
  recorder.py        # réception des chunks, finalisation ffmpeg
  pipeline.py        # file de tâches, orchestration des étapes
  transcribe.py      # Mistral Voxtral
  llm.py             # appels Mistral (format + état)
  publish/
    drive.py         # auth + fichiers .md + Google Doc NotebookLM
    notion.py        # bases, pages, lecture des annotations
  templates/
  static/
prompts/
  format_course.md
  update_state.md
data/                # (gitignored) audio, transcriptions, cours, états, sqlite
tests/
README.md
pyproject.toml
.env.example
```

## 11. Plan d'implémentation (phases)

Valider chaque phase (lancer l'app, tester à la main, tests unitaires quand pertinent) avant la suivante.

1. **Squelette** : projet uv, FastAPI, pages vides, SQLite, config `.env`. Critère : `uv run` lance l'app sur `127.0.0.1:8000`.
2. **Emploi du temps** : import ICS (URL + fichier), expansion des récurrences, créneaux du jour, mapping ADE → matière, CRUD matières. Critère : mes créneaux du jour s'affichent correctement.
3. **Enregistrement** : MediaRecorder, chunks, pause/reprise, vumètre, finalisation ffmpeg, gestion des interruptions. Critère : un enregistrement de 5 min survit à un rechargement de page en plein milieu.
4. **Transcription** : intégration Voxtral avec vocabulaire et diarisation, statuts, relance. Critère : transcription lisible d'un enregistrement test.
5. **Mise en forme + état** : prompts, deux appels LLM, stockage local, vue Cours avec rendu KaTeX. Critère : deux séances successives d'une même matière, la seconde renvoie correctement à la première.
6. **Drive** : OAuth, arborescence, `.md` par séance, cours complet, Google Doc NotebookLM mis à jour sur le même ID, liens dans l'UI. Critère : fichiers visibles sur Drive ; le Google Doc ajouté dans NotebookLM reflète une nouvelle séance sans ré-import.
7. **Notion** : bases Matières/Séances, création des pages depuis le Markdown, **test du rendu LaTeX en premier**, règle de non-écrasement, import des annotations. Critère : une séance s'affiche correctement dans Notion avec ses formules ; une annotation faite dans Notion se retrouve dans le cours complet Drive après import.
8. **Finitions** : page Paramètres, gestion d'erreurs, README complet (installation, ffmpeg, config Google, config Notion, lancement).

## 12. Hors périmètre (pour l'instant)

- Transcription en temps réel pendant le cours.
- Usage sur téléphone / hébergement en ligne.
- Multi-utilisateur, authentification.
- Synchronisation bidirectionnelle automatique Notion ↔ Drive (seulement l'import manuel des annotations, section 8.4).

## 13. Divers

- Coût indicatif : transcription ~0,003 $/min (≈ 0,36 $ par séance de 2 h), LLM négligeable ou couvert par le tier gratuit Mistral selon les limites du compte. Notion et Drive : plans gratuits suffisants (pas besoin de Notion AI).
- Rappel dans l'UI (discret, au premier lancement) : obtenir l'accord de l'enseignant avant d'enregistrer.
