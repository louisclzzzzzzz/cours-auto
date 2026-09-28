# cours-auto — prise de notes de cours automatisée

Application web **locale** (FastAPI, ouverte dans le navigateur) pour enregistrer un CM/TD/TP et en produire automatiquement un cours rédigé :

1. choix du cours dans l'emploi du temps ADE (`.ics`) ;
2. enregistrement audio robuste dans le navigateur (morceaux envoyés au serveur toutes les 30 s), **ou import d'un fichier audio** (téléphone, dictaphone…) ;
3. transcription **Mistral Voxtral** (diarisation, horodatage, vocabulaire de la matière) → mise en forme du cours par **Mistral Small 4** → mise à jour de l'« état » de la matière ;
4. publication vers **Google Drive** (archive Markdown + Google Doc pour NotebookLM) et **Notion** (lecture et annotation), consultation des cours avec rendu des formules (KaTeX).

Le disque local (`data/`) est la source de vérité. Usage personnel, aucune authentification : l'app n'écoute que sur `127.0.0.1`.

---

## 1. Installation

Prérequis :

- [uv](https://docs.astral.sh/uv/) (installe Python 3.12 automatiquement) ;
- **ffmpeg** (assemblage et conversion de l'audio) : `brew install ffmpeg` (macOS), `sudo apt install ffmpeg` (Debian/Ubuntu), ou <https://ffmpeg.org> (Windows) ;
- un navigateur récent : Chrome/Edge (recommandé), Firefox ou Safari.

```bash
uv sync
```

```bash
cp .env.example .env
```

Renseignez ensuite `.env` (voir §3 à §5). Ce fichier, `credentials.json`, `token.json` et `data/` sont exclus de Git.

## 2. Lancement

```bash
uv run cours-auto
```

Ouvrez <http://127.0.0.1:8000>. (Autre port : `PORT=8001 uv run cours-auto`.)

> Utilisez bien `http://127.0.0.1` ou `http://localhost` : les navigateurs n'autorisent le micro que dans un « contexte sécurisé », ce qui inclut ces adresses locales.

## 3. Mistral

1. Créez une clé sur <https://console.mistral.ai/api-keys> et mettez-la dans `MISTRAL_API_KEY`.
2. Dans **Paramètres → Mistral**, « Tester la clé ». Modèles par défaut : **Mistral Small 4** `mistral-small-2603` (mise en forme, contexte 256 k tokens ; alias `mistral-small-latest`) et `voxtral-mini-latest` (transcription, jusqu'à ~3 h par requête). Le modèle se change dans Paramètres (ex. `mistral-large-latest`). Small 4 « raisonne » avant de rédiger : l'app envoie `reasoning_effort=high` par défaut (le seul autre choix accepté par Small 4 est `none`, réponse directe). La réflexion rend chaque appel plus lent et consomme des tokens en plus (une marge de 16 000 tokens lui est réservée) ; réglable dans **Paramètres → Mistral → Modèles et réglages avancés**.

Coût indicatif : ~0,003 $/min de transcription (≈ 0,36 $ pour 2 h), LLM négligeable ; lecture d'un support de cours par OCR : quelques centimes (≈ 4 $ les 1 000 pages avec OCR 4, soit ≈ 0,16 $ pour 40 diapositives).

## 4. Emploi du temps (ADE)

Dans **Paramètres → Emploi du temps** :

- collez le **lien d'export ICS** d'ADE (dans ADE : icône d'export de l'agenda → « Générer l'URL ») puis « Enregistrer et actualiser » ;
- ou « Importer un fichier .ics » (envoyé dès qu'il est choisi).

L'EDT est rafraîchi à l'ouverture de l'app (et au plus toutes les 30 min quand vous revenez sur l'accueil) ou via le lien **Actualiser** sous la liste des cours de la page Enregistrer. Le dernier EDT connu est gardé en cache : l'app fonctionne hors ligne.

**Correspondance ADE → matière** : les intitulés ADE étant souvent bruités (« Algo. Av. - TD G1 »), chaque intitulé est rattaché à une matière par une règle (intitulé exact, ou expression régulière). Un créneau non reconnu est marqué « Matière à associer » : une fois sélectionné, un petit formulaire permet de créer la matière ou de l'associer à une matière existante. La page **Cours** regroupe aussi, dans une section repliable, tous les intitulés non associés des semaines à venir ; les règles par expression régulière se gèrent dans les **réglages de la matière**.

Le type (CM/TD/TP) est déduit de l'intitulé et reste modifiable au moment de l'enregistrement ; l'enseignant est lu dans la description ADE quand il y figure.

## 5. Notion

1. Créez une **intégration interne** : <https://www.notion.so/profile/integrations> → « Nouvelle intégration » (type *Interne*), capacités **Lire**, **Mettre à jour** et **Insérer du contenu**. Copiez le jeton dans `NOTION_TOKEN`.
2. Créez une page **« Cours M1 »** et **partagez-la avec l'intégration** : menu `•••` de la page → *Connexions* → ajoutez votre intégration.
3. Collez l'URL de la page dans **Paramètres → Notion** (ou dans `NOTION_URL`), puis « Enregistrer et vérifier » (l'accès à la page est testé aussitôt).

À la première publication, l'app crée sous cette page :

- une base **Matières** : Nom, Enseignant(s), Lien Drive, Lien Google Doc NotebookLM ;
- une base **Séances** : Titre, Matière (relation), Type, Numéro, Date, Durée, Statut, Lien Drive, Enseignant — une page par séance dont le contenu est le cours ;
- trois vues dans « Séances » : **Par matière** (groupée par matière, triée par date), **Calendrier**, **Dernières séances**.

**Règle de non-écrasement.** Vous pouvez annoter librement les pages Séance : après sa création, l'app **ne réécrit jamais** le contenu d'une page (seules les propriétés sont mises à jour). Si vous relancez la mise en forme d'une séance déjà publiée, l'app crée une **nouvelle page « (v2) »** et garde l'ancienne (statut « Remplacée ») ; l'écrasement n'a lieu que si vous le choisissez explicitement, après un avertissement.

**Importer mes annotations** (page **Cours**, par séance ou pour toute la matière) : l'app lit la page Notion, enregistre `…_annote.md` en local et sur Drive, et utilise cette version dans le cours complet et le Google Doc NotebookLM. Le fichier généré d'origine reste intact.

## 6. Google Drive et NotebookLM

L'app utilise uniquement le scope **`drive.file`** : elle ne voit que les fichiers qu'elle a créés (elle mémorise leurs identifiants).

### Créer les identifiants (une seule fois)

1. <https://console.cloud.google.com/> → créez un projet (ex. « cours-auto »).
2. *API et services → Bibliothèque* : activez **Google Drive API**.
3. *API et services → Écran de consentement OAuth* (« Google Auth Platform ») : type d'utilisateur **Externe**, renseignez le nom de l'app et votre e-mail ; dans *Accès aux données*, ajoutez le scope `.../auth/drive.file`.
4. **Publiez l'application en « Production »** (*Audience → Publier l'application*). En mode « Test », Google fait expirer les jetons de rafraîchissement **tous les 7 jours** et il faudrait se reconnecter chaque semaine. `drive.file` étant un scope non sensible, la publication ne demande pas de validation par Google.
5. *Identifiants → Créer des identifiants → ID client OAuth* : type **Application de bureau**. Téléchargez le JSON et enregistrez-le à la racine du projet sous le nom **`credentials.json`**.
6. Dans **Paramètres → Google Drive**, cliquez « Se connecter à Google Drive » : la page de connexion Google s'ouvre dans votre **navigateur habituel** (Chrome, Safari… — Google refuse souvent la connexion dans un navigateur intégré). Choisissez votre compte et autorisez l'accès ; si Google affiche « Google n'a pas validé cette application », cliquez sur « Continuer ». L'app détecte la connexion toute seule (un serveur temporaire sur `127.0.0.1` reçoit l'autorisation) et **relance automatiquement les publications Drive restées en échec**. Le jeton est enregistré dans `token.json` et rafraîchi automatiquement. En cas de jeton révoqué ou expiré, la publication Drive passe en erreur (sans rien perdre) : reconnectez-vous.

La carte Drive des Paramètres contient une **aide à la connexion** avec des liens directs vers les pages de votre projet Google Cloud (API, Audience, Accès aux données, Clients).

### Arborescence créée

```
Cours M1/
  <Matière>/
    <Matière> – Cours complet.md      # toutes les séances, dans l'ordre, avec sommaire
    <Matière> – NotebookLM            # Google Doc, même contenu
    _etat.md
    Séances/
      2026-09-29_CM03_<titre-court>.md
      2026-09-29_CM03_<titre-court>_annote.md   # après import des annotations Notion
    Sources/                           # option : audio + transcription
```

### NotebookLM

Ajoutez **une seule fois** le Google Doc « <Matière> – NotebookLM » comme source de votre carnet NotebookLM. L'app met ce document à jour **sur le même fichier** à chaque séance (il n'est jamais recréé), donc la source n'a jamais besoin d'être ré-importée. Selon la version de NotebookLM, la mise à jour est prise en compte automatiquement ou via « Synchroniser avec Google Drive » sur la source. Les formules y restent en LaTeX brut, ce qui ne gêne pas NotebookLM. L'option se désactive dans Paramètres.

## 7. Utilisation au quotidien

1. **Enregistrer** (accueil) : ① choisissez le cours — le créneau en cours est présélectionné ; sinon cliquez sur un créneau, changez de jour (‹ ›, ou « Voir le prochain jour de cours » les jours sans cours) ou prenez « Autre cours » (hors emploi du temps) — puis ② **Démarrer**. Seuls les boutons utiles s'affichent ensuite (Pause / Reprendre / Arrêter) et le choix du cours est verrouillé pendant l'enregistrement. Le micro se choisit et se teste (« Tester », vumètre) sous le bouton.
   **Importer un fichier audio…** (même carte) : pour un cours enregistré avec un autre appareil (mp3, m4a, wav, ogg, webm, flac, vidéo mp4…). Choisissez d'abord le cours, cliquez « Importer un fichier audio… » puis sélectionnez le fichier ; une confirmation rappelle le cours choisi. Le fichier d'origine est conservé (`source.<ext>`), converti en MP3 mono 16 kHz puis traité comme un enregistrement. Voxtral accepte jusqu'à ~3 h par fichier.
   **Support de cours** (diapositives, PDF) : « 📑 Ajouter le support du cours… », sous le cours choisi. Choisi avant de démarrer, il est envoyé dès le début de l'enregistrement (ou avec le fichier audio importé) ; pendant l'enregistrement, il part aussitôt. On peut aussi l'ajouter plus tard depuis l'Historique (voir « Support de cours » ci-dessous).
2. À l'arrêt, le traitement démarre en arrière-plan (un à la fois) : `finalisation audio → transcription → mise en forme → publication`. Le statut se met à jour en direct sur l'accueil (« Derniers traitements »), dans la barre du haut et dans **Historique**.
3. **Historique** : la liste de tous les enregistrements ; un clic ouvre l'avancement en 4 étapes (audio → transcription → mise en forme → publication), avec un bouton **Réessayer** qui relance l'étape en échec, la section **Support de cours**, l'écoute et la transcription. Sections repliables : corriger les informations (matière, type, numéro…), relancer une étape précise (chaque étape repart des fichiers conservés), supprimer, journal technique.
4. **Cours** : la liste des matières ; un clic ouvre les séances avec l'aperçu du cours (Markdown + formules), « Ouvrir dans Notion » et, dans le menu **⋯**, l'import des annotations Notion, les fichiers Drive, le téléchargement du Markdown. Au niveau de la matière : « Cours complet », et dans **⋯** le Google Doc NotebookLM, le dossier Drive, l'import des annotations de toutes les séances.
5. **Cours → Réglages** (une matière) : **termes proposés** après chaque cours (cochez ceux à garder puis « Valider » ; les autres sont écartés), **vocabulaire** (≤ 100 termes envoyés à Voxtral), nom et enseignants, intitulés ADE associés (exact ou expression régulière), **mémoire de la matière** (état, éditable), suppression.

### Support de cours (diapositives, PDF)

Joignez à une séance le support de l'enseignant — **PDF, PPTX ou DOCX** (50 Mo max. ; Keynote, Google Slides ou `.ppt` : exportez en PDF). Plusieurs fichiers sont possibles.

- **Lecture** : dès l'ajout, le texte est lu par l'**OCR Mistral** (`mistral-ocr-latest`, modifiable dans Paramètres → Mistral) : Markdown, tableaux, formules en LaTeX, en-têtes et pieds de page retirés. Pour un PowerPoint, les **notes de l'intervenant** sont ajoutées à chaque diapositive. Si l'OCR échoue, le texte est extrait localement (PDF et PPTX, sans les formules). Le texte lu est consultable (« Texte lu »).
- **Mise en forme** : le cours reste **celui qui a été dit**. Un premier appel repère les pages du support **abordées à l'oral** ; seules celles-ci sont transmises à la rédaction, qui s'en sert pour écrire exactement les termes (y compris ceux mal transcrits), les formules, notations, définitions et énoncés que l'enseignant présente ou commente. Rien de ce qui n'est que sur le support n'est ajouté au déroulé ; si l'oral contredit une diapositive, le cours suit l'oral et le signale (`> ⚠️ **Écart avec le support** : …`). Les pages utiles **jamais abordées** sont résumées à la fin, dans « Sur le support, non abordé en cours ».
- **Ajout après coup** : si le cours est déjà rédigé, « Refaire le cours avec le support » relance la mise en forme (dans Notion, une nouvelle page « (v2) » est créée ; l'ancienne et vos annotations sont conservées). La ligne d'en-tête du cours indique le support utilisé.

### Robustesse de l'enregistrement

- Chaque morceau de 30 s est écrit immédiatement sur le disque (`data/recordings/<id>/chunk_XXXX.webm`).
- Fermeture d'onglet, rechargement, veille, redémarrage de l'app : les morceaux reçus sont conservés. L'accueil affiche alors l'enregistrement comme **actif/interrompu** avec **« Reprendre »** (nouveau segment, assemblé automatiquement) ou **« Terminer et traiter »**. Au pire, les ~30 dernières secondes pas encore envoyées sont perdues.
- Micro débranché : l'enregistrement reprend automatiquement sur le micro par défaut.
- L'écran est maintenu allumé (Wake Lock) et le navigateur demande confirmation avant de quitter la page.
- 🤝 Pensez à obtenir l'accord de l'enseignant avant d'enregistrer.

## 8. Mise en forme du cours

Les prompts sont des fichiers texte modifiables :

- `prompts/format_course.md` — règles de rédaction (fidélité, compléments balisés `> 💡 **Complément** : …`, passages douteux `⚠️ [passage peu clair ~00:42:10]`, structure et numérotation continues, LaTeX, TD/TP par exercice, questions d'étudiants, « Points clés » / « À retenir pour la suite ») ;
- `prompts/support_alignment.md` et `prompts/support_gaps.md` — pages du support abordées à l'oral, et résumé de celles qui ne l'ont pas été (seulement si un support est joint) ;
- `prompts/update_state.md` — mise à jour de l'état de matière (appel n°2, < ~3 000 tokens) ;
- `prompts/suggest_vocabulary.md` et `prompts/key_points.md`.

Chaque séance reçoit l'**état de la matière** (plan cumulé, notions, notations, où en est le cours) plutôt que tout l'historique. L'état « avant séance » est figé dans le dossier de l'enregistrement, ce qui rend l'étape rejouable ; une séance ancienne relancée n'écrase pas l'état produit par une séance plus récente. Les transcriptions très longues sont découpées sur les pauses (Paramètres → Mistral → Modèles et réglages avancés).

## 9. Données locales

```
data/
  app.sqlite3                  # matières, correspondances, enregistrements, statuts, identifiants Drive/Notion
  calendar.ics                 # dernier EDT connu
  recordings/<id>/
    chunk_XXXX.webm            # morceaux bruts reçus du navigateur
    source.<ext>               # fichier audio importé (conservé tel quel)
    audio.mp3                  # audio final (mono 16 kHz)
    transcript.json / .txt     # transcription brute (segments, locuteurs, horodatage) / lisible
    state_before.md / state_after.md
    course.md                  # cours généré
    course_annote.md           # version annotée importée de Notion
    supports/                  # supports de cours : fichier d'origine + texte lu (.md)
    versions/                  # anciennes versions en cas de nouvelle mise en forme
    meta.json
  subjects/<matière>/
    _etat.md, etats/, cours_complet.md
```

## 10. Dépannage

| Problème | Solution |
|---|---|
| « ffmpeg introuvable » | Installez ffmpeg (§1) puis « Réessayer » sur l'enregistrement (Historique). |
| Micro refusé / non listé | Autorisez le micro pour `127.0.0.1` dans le navigateur, puis « Tester » (sous le bouton Démarrer). |
| Drive : « Autorisation expirée » chaque semaine | Publiez l’app OAuth en **Production** (§6, étape 4), puis reconnectez-vous. |
| Drive : « Accès bloqué » / accès refusé par Google | L’app OAuth est en mode « Test » sans votre adresse : publiez-la en Production ou ajoutez-vous aux utilisateurs test (Audience). |
| Drive : « Ce navigateur ou cette application ne sont peut-être pas sécurisés » | Faites la connexion dans Chrome ou Safari (lien « Ouvrir la connexion Google » dans Paramètres). |
| Drive : « L’API Google Drive n’est pas activée » | Activez-la avec le lien affiché, attendez une minute, puis relancez. |
| Notion : « Page racine introuvable » | Partagez la page avec l’intégration (§5, étape 2). |
| Base Notion supprimée par erreur | Elle est recréée à la publication suivante ; les anciennes pages ne sont pas modifiées. |
| Erreur 429 / 5xx | Relances automatiques avec attente ; sinon « Réessayer » plus tard depuis l'Historique. |
| Support « illisible » | Document scanné vide ou protégé : « Relire », ou exportez-le à nouveau en PDF. |

## 11. Développement

```bash
uv run pytest
```

Structure : `app/main.py` (FastAPI), `app/routes/` (pages et API), `calendar_ics.py`, `recorder.py`, `pipeline.py` (file de tâches), `transcribe.py`, `llm.py`, `markdown_utils.py`, `subjects.py`, `publish/drive.py`, `publish/notion.py`, `templates/` (Jinja2 + HTMX), `static/` (enregistreur JS, rendu Markdown/KaTeX, bibliothèques embarquées pour fonctionner hors ligne).

### Points vérifiés dans la documentation (et par des tests réels)

- **Voxtral** (`client.audio.transcriptions.complete`, SDK `mistralai` 2.x) : paramètres `language`, `diarize`, `context_bias` (≤ 100 termes), `timestamp_granularities=["segment"]` ; segments avec `speaker_id`, `start`, `end`. Les termes de `context_bias` **ne doivent contenir ni espace ni virgule** (l'API renvoie une erreur 400) : « graphe pondéré » est envoyé comme `graphe_pondéré`, et l'orthographe d'origine est restaurée dans la transcription lisible. La doc indique que `language` est incompatible avec l'horodatage, mais l'API l'accepte en pratique ; l'app réessaie automatiquement sans `language` si l'API le refuse.
- **Mistral Small 4** (`mistral-small-2603`) : contexte de 256 k tokens, raisonnement réglable par `reasoning_effort` (Small 4 n'accepte que `high` et `none` ; sans paramètre, il ne réfléchit pas ; les modèles qui ne le proposent pas, comme Large, le refusent : l'app renvoie alors la requête sans ce paramètre ; si la réflexion épuise le budget avant toute réponse, l'appel est relancé avec un budget doublé) ; si une réponse est tronquée (`finish_reason = length`), elle est prolongée avec un message assistant `prefix`.
- **Notion API `2026-03-11`** : pages créées en Markdown (`POST /v1/pages` avec `markdown`, `allow_async` pour les gros contenus), lecture `GET /v1/pages/:id/markdown`, ajout `PATCH …/markdown` (`insert_content.position`), bases créées via `initial_data_source`, pages créées sous un `data_source_id`, vues via `POST /v1/views`, limite ~3 req/s avec respect de `Retry-After` (429/529). Tests de rendu réels : `$…$` → équations en ligne et `$$` sur des lignes séparées → bloc équation, mais **`$$ … $$` sur une seule ligne est cassé** (normalisé par l'app) et plusieurs lignes `>` deviennent des blocs citation séparés (les encadrés deviennent des *callouts*). En lecture, Notion renvoie les maths sous la forme `` $`…`$ `` et les tableaux en HTML : l'import des annotations les reconvertit en Markdown standard.
- **OCR Mistral** (`client.ocr.process`, modèle `mistral-ocr-latest`) : fichier envoyé par `client.files.upload(purpose="ocr")` puis lu via `{"type": "file", "file_id": …}` (PDF, PPTX, DOCX ; 50 Mo et 1 000 pages max.), puis supprimé ; `extract_header` / `extract_footer` retirent en-têtes et pieds de page. Essais réels : pour un **PDF**, les formules sortent en LaTeX `\( … \)` (converties en `$…$`) ; pour un **PPTX/DOCX**, `\(` n'est qu'une parenthèse échappée façon Markdown (l'échappement est retiré). Images et tableaux séparés sont signalés par des repères (`![img-0.jpeg](img-0.jpeg)`, `[tbl-0.md](tbl-0.md)`).
- **Drive API v3** : conversion à l'import Markdown → Google Doc (`text/markdown` vers `application/vnd.google-apps.document`), `files.update` avec média pour remplacer le contenu sur le même identifiant, `webViewLink` mémorisé pour chaque fichier et dossier.
