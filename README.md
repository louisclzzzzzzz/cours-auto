# cours-auto — prise de notes de cours automatisée

Application web **locale** (FastAPI, ouverte dans le navigateur) pour enregistrer un CM/TD/TP et en obtenir un cours rédigé :

1. choix du cours dans l'emploi du temps ADE (`.ics`) ;
2. enregistrement audio robuste dans le navigateur (morceaux envoyés au serveur toutes les 30 s), **ou import d'un fichier audio** (téléphone, dictaphone…) ;
3. sur votre demande (bouton « Lancer le traitement »), transcription **Mistral Voxtral** (diarisation, horodatage, vocabulaire de la matière) puis **dépôt dans Google Drive** de la transcription et des supports de cours ;
4. une **tâche Claude planifiée** rédige le cours dans Drive ; l'app le **récupère** et l'affiche dans l'onglet Cours, avec le rendu des formules (KaTeX).

Le disque local (`data/`) garde l'audio, les transcriptions et une copie des cours. Usage personnel, aucune authentification : l'app n'écoute que sur `127.0.0.1`.

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

Renseignez ensuite `.env` (voir §3) et connectez Google Drive (§5). Ce fichier, `credentials.json`, `token.json` et `data/` sont exclus de Git.

## 2. Lancement

```bash
uv run cours-auto
```

Ouvrez <http://127.0.0.1:8000>. (Autre port : `PORT=8001 uv run cours-auto`.)

> Utilisez bien `http://127.0.0.1` ou `http://localhost` : les navigateurs n'autorisent le micro que dans un « contexte sécurisé », ce qui inclut ces adresses locales.

### Application Mac : un clic, toujours à jour

Pour lancer l'app depuis le Dock sans ouvrir de terminal, créez une fois l'application « Cours auto » :

```bash
bash packaging/macos/install.sh
```

Elle est créée dans `~/Applications` (le Finder l'affiche) : faites-la glisser dans le Dock. Ensuite, **un clic** :

1. récupère la dernière version de `main` sur GitHub (quelques secondes ; une notification indique les nouveautés) ;
2. démarre l'app en arrière-plan (le premier lancement installe les dépendances : jusqu'à 2 min) ;
3. ouvre la page dans votre navigateur. Un nouveau clic quand l'app tourne déjà ouvre simplement la page.

Pour l'arrêter : **« Quitter l'app »**, en bas de la barre latérale (un traitement en cours reprendra au prochain lancement). La version qui tourne est affichée juste en dessous.

- La mise à jour ne suit que `main` (une fusion de pull request) et n'écrase jamais rien : si vous êtes sur une autre branche, hors ligne, ou si un fichier du projet a été modifié à la main, l'app se lance avec la version actuelle et une notification l'explique. Vos données (`data/`, `.env`, `credentials.json`, `token.json`) ne sont jamais touchées.
- Journal du lanceur et de l'app : `~/Library/Logs/Cours auto/app.log`.
- Le lanceur lui-même (`packaging/macos/launcher.sh`) se met à jour avec le reste du code ; relancez `install.sh` seulement si vous déplacez le dossier du projet.
- Si GitHub demande vos identifiants à chaque `git pull` dans le terminal, la mise à jour automatique échouera (elle ne peut pas les saisir) : enregistrez-les une fois, par exemple avec `gh auth login` ou une clé SSH.

## 3. Mistral (transcription)

1. Créez une clé sur <https://console.mistral.ai/api-keys> et mettez-la dans `MISTRAL_API_KEY`.
2. Dans **Paramètres → Mistral**, « Tester la clé ». Modèle par défaut : `voxtral-mini-latest` (jusqu'à ~3 h d'audio par requête) ; le modèle, la langue et le débit MP3 se changent dans **Réglages avancés**.

Mistral ne sert plus qu'à la transcription : la rédaction du cours est faite par la tâche Claude. Coût indicatif : ~0,003 $/min (≈ 0,36 $ pour 2 h de cours).

## 4. Emploi du temps (ADE)

Dans **Paramètres → Emploi du temps** :

- collez le **lien d'export ICS** d'ADE (dans ADE : icône d'export de l'agenda → « Générer l'URL ») puis « Enregistrer et actualiser » ;
- ou « Importer un fichier .ics » (envoyé dès qu'il est choisi).

L'EDT est rafraîchi à l'ouverture de l'app (et au plus toutes les 30 min quand vous revenez sur l'accueil) ou via le lien **Actualiser** sous la liste des cours de la page Enregistrer. Le dernier EDT connu est gardé en cache : l'app fonctionne hors ligne.

**Correspondance ADE → matière** : les intitulés ADE étant souvent bruités (« Algo. Av. - TD G1 »), chaque intitulé est rattaché à une matière par une règle (intitulé exact, ou expression régulière). Un créneau non reconnu est marqué « Matière à associer » : une fois sélectionné, un petit formulaire permet de créer la matière ou de l'associer à une matière existante. La page **Cours** regroupe aussi, dans une section repliable, tous les intitulés non associés des semaines à venir ; les règles par expression régulière se gèrent dans les **réglages de la matière**.

Le type (CM/TD/TP) est déduit de l'intitulé et reste modifiable au moment de l'enregistrement ; l'enseignant est lu dans la description ADE quand il y figure.

## 5. Google Drive et tâche Claude

L'app dépose dans Drive ce dont la tâche Claude a besoin, puis relit le cours qu'elle a écrit :

- **dépôt** (scope `drive.file`) : à la fin du traitement, la transcription va dans `<Matière>/Transcriptions/` et les supports de cours dans `<Matière>/Supports/` (y compris ceux ajoutés après coup) ;
- **lecture** (scope `drive.readonly`) : les fichiers écrits par Claude ne sont pas visibles avec `drive.file` (qui ne donne accès qu'aux fichiers créés par l'app) ; la lecture seule permet de les récupérer. L'app ne modifie ni ne supprime jamais rien dans Drive en dehors de ses propres dépôts.

### Créer les identifiants (une seule fois)

1. <https://console.cloud.google.com/> → créez un projet (ex. « cours-auto »).
2. *API et services → Bibliothèque* : activez **Google Drive API**.
3. *API et services → Écran de consentement OAuth* (« Google Auth Platform ») : type d'utilisateur **Externe**, renseignez le nom de l'app et votre e-mail ; dans *Accès aux données*, ajoutez les scopes `.../auth/drive.file` et `.../auth/drive.readonly`.
4. **Publiez l'application en « Production »** (*Audience → Publier l'application*). En mode « Test », Google fait expirer les jetons de rafraîchissement **tous les 7 jours** et il faudrait se reconnecter chaque semaine. Pour une app personnelle, aucune validation par Google n'est nécessaire : Google affiche seulement l'avertissement « application non validée » à la connexion.
5. *Identifiants → Créer des identifiants → ID client OAuth* : type **Application de bureau**. Téléchargez le JSON et enregistrez-le à la racine du projet sous le nom **`credentials.json`**.
6. Dans **Paramètres → Google Drive**, cliquez « Se connecter à Google Drive » : la page de connexion Google s'ouvre dans votre **navigateur habituel** (Chrome, Safari… — Google refuse souvent la connexion dans un navigateur intégré). Choisissez votre compte et **laissez cochées les deux autorisations** (créer des fichiers, voir vos fichiers) ; si Google affiche « Google n'a pas validé cette application », cliquez sur « Continuer ». L'app détecte la connexion toute seule (un serveur temporaire sur `127.0.0.1` reçoit l'autorisation) et **relance automatiquement les dépôts restés en échec**. Le jeton est enregistré dans `token.json` avec les autorisations réellement accordées, et rafraîchi automatiquement.

**Connexion faite avant la lecture des cours** (scope `drive.file` seul) : les dépôts continuent, mais Paramètres affiche « Lecture des cours non autorisée ». Ajoutez `.../auth/drive.readonly` dans *Accès aux données* (étape 3), puis « Déconnecter » et reconnectez-vous.

La carte Drive des Paramètres contient une **aide à la connexion** avec des liens directs vers les pages de votre projet Google Cloud (API, Audience, Accès aux données, Clients).

### Arborescence

```
Cours M1/
  <Matière>/
    Transcriptions/                    # déposé par l'app : 2026-09-29_CM03_transcription.txt
    Supports/                          # déposé par l'app : 2026-09-29_CM03_<support>.pdf
    Séances/                           # écrit par la tâche Claude : 2026-09-29_CM03_<titre-court>.md
    <Matière> – Cours complet.md       # écrit par la tâche Claude
    <Matière> – NotebookLM             # Google Doc, écrit par la tâche Claude
    _etat.md                           # écrit par la tâche Claude
```

La transcription déposée commence par les informations de séance connues de l'app : matière, type et numéro, date, horaire, salle, enseignant, intitulé de l'emploi du temps, durée, supports joints. Un nouveau dépôt remplace le même fichier (renommé si le numéro de la séance a changé).

### Ce que l'app attend de la tâche Claude

- Un fichier Markdown par séance dans `<Matière>/Séances/`, nommé **`AAAA-MM-JJ_CM03_<titre>.md`** : même date, type (CM, TD, TP) et numéro que le fichier de transcription. C'est ce préfixe qui relie le cours à la séance de l'app.
- Première ligne : `# CM 3 – <titre court>`. Le titre court devient celui de la séance dans l'app.
- Si Claude réécrit un cours, la nouvelle version est récupérée à la vérification suivante (l'ancienne est gardée dans `versions/`).

L'app vérifie Drive **au démarrage puis toutes les 10 minutes**, et sur demande : bouton « Récupérer depuis Drive » (page Cours, page d'une matière) ou « Vérifier maintenant » (fiche d'une séance en attente). Les liens vers le cours complet et le Google Doc NotebookLM de chaque matière sont mis à jour en même temps.

## 6. Utilisation au quotidien

1. **Enregistrer** (accueil) : ① choisissez le cours — le créneau en cours est présélectionné ; sinon cliquez sur un créneau, changez de jour (‹ ›, ou « Voir le prochain jour de cours » les jours sans cours) ou prenez « Autre cours » (hors emploi du temps) — puis ② **Démarrer**. Seuls les boutons utiles s'affichent ensuite (Pause / Reprendre / Arrêter) et le choix du cours est verrouillé pendant l'enregistrement. Le micro se choisit et se teste (« Tester », vumètre) sous le bouton.
   **Importer un fichier audio…** (même carte) : pour un cours enregistré avec un autre appareil (mp3, m4a, wav, ogg, webm, flac, vidéo mp4…). Choisissez d'abord le cours, cliquez « Importer un fichier audio… » puis sélectionnez le fichier ; une confirmation rappelle le cours choisi. Le fichier d'origine est conservé (`source.<ext>`) et converti en MP3 mono 16 kHz ; il attend ensuite, comme un enregistrement, que vous lanciez le traitement. Voxtral accepte jusqu'à ~3 h par fichier.
   **Support de cours** (diapositives, PDF) : « 📑 Ajouter le support du cours… », sous le cours choisi. Choisi avant de démarrer, il est envoyé dès le début de l'enregistrement (ou avec le fichier audio importé) ; pendant l'enregistrement, il part aussitôt. On peut aussi l'ajouter plus tard depuis l'Historique.
2. À l'arrêt (ou après un import), seul l'audio est préparé (assemblage ou conversion, en local) : l'enregistrement passe en **« Prêt à traiter »**. Rien n'est envoyé à Mistral tant que vous n'avez pas cliqué sur **« Lancer le traitement »** — dans « Derniers traitements » sur l'accueil, dans **Historique** ou sur la fiche de l'enregistrement (où vous pouvez d'abord écouter l'audio ou joindre le support de cours). Le traitement tourne alors en arrière-plan (un à la fois) : `transcription → dépôt Drive`. Le statut se met à jour en direct sur l'accueil, dans la barre du haut et dans **Historique** : « En attente du cours » une fois la transcription déposée, puis « Cours prêt » quand le cours de la tâche Claude a été récupéré.
3. **Historique** : la liste de tous les enregistrements, avec une corbeille pour supprimer un faux départ (les séances suivantes de la matière reculent d'un numéro : CM 3 → CM 2). Un clic ouvre l'avancement en 4 étapes (audio → transcription → dépôt Drive → cours rédigé par Claude), avec un bouton **Réessayer** qui relance l'étape en échec, la section **Support de cours**, l'écoute et la transcription. Sections repliables : corriger les informations (matière, type, numéro…), relancer une étape précise (chaque étape repart des fichiers conservés), supprimer, journal technique.
4. **Cours** : la liste des matières ; un clic ouvre les séances avec l'aperçu du cours (Markdown + formules), « Ouvrir dans Drive » et, dans le menu **⋯**, la transcription sur Drive, le téléchargement du Markdown, la suppression. Au niveau de la matière : « Cours complet », « Récupérer depuis Drive », et dans **⋯** le Google Doc NotebookLM, le cours complet et le dossier Drive.
5. **Cours → Réglages** (une matière) : **vocabulaire** (≤ 100 termes envoyés à Voxtral), nom et enseignants, intitulés ADE associés (exact ou expression régulière), suppression.

### Support de cours (diapositives, PDF)

Joignez à une séance le support de l'enseignant — **PDF, PPTX ou DOCX** (50 Mo max. ; Keynote, Google Slides ou `.ppt` : exportez en PDF). Plusieurs fichiers sont possibles. Le fichier d'origine est déposé tel quel dans `<Matière>/Supports/` avec la transcription : la tâche Claude s'en sert pour écrire exactement les termes, formules et énoncés. Un support ajouté après le dépôt part aussitôt dans Drive ; en retirer un dans l'app ne le retire pas de Drive.

### Robustesse de l'enregistrement

- Chaque morceau de 30 s est écrit immédiatement sur le disque (`data/recordings/<id>/chunk_XXXX.webm`).
- Fermeture d'onglet, rechargement, veille, redémarrage de l'app : les morceaux reçus sont conservés. L'accueil affiche alors l'enregistrement comme **actif/interrompu** avec **« Reprendre »** (nouveau segment, assemblé automatiquement) ou **« Terminer »** (l'audio reçu est assemblé, puis le traitement se lance à la main). Au pire, les ~30 dernières secondes pas encore envoyées sont perdues.
- Micro débranché : l'enregistrement reprend automatiquement sur le micro par défaut.
- L'écran est maintenu allumé (Wake Lock) et le navigateur demande confirmation avant de quitter la page.
- 🤝 Pensez à obtenir l'accord de l'enseignant avant d'enregistrer.

## 7. Données locales

```
data/
  app.sqlite3                  # matières, correspondances, enregistrements, statuts, identifiants Drive
  calendar.ics                 # dernier EDT connu
  recordings/<id>/
    chunk_XXXX.webm            # morceaux bruts reçus du navigateur
    source.<ext>               # fichier audio importé (conservé tel quel)
    audio.mp3                  # audio final (mono 16 kHz)
    transcript.json / .txt     # transcription brute (segments, locuteurs, horodatage) / lisible
    course.md                  # cours rédigé par la tâche Claude, récupéré depuis Drive
    supports/                  # supports de cours (fichiers d'origine)
    versions/                  # anciennes versions du cours
    meta.json
  subjects/<matière>/
    cours_complet.md           # toutes les séances récupérées, à la suite
```

Les séances traitées par les versions précédentes de l'app gardent leurs fichiers (cours rédigés par Mistral, `state_*.md`, `_etat.md`) ; un cours trouvé ensuite dans Drive remplace le `course.md` (l'ancien passe dans `versions/`).

## 8. Dépannage

| Problème | Solution |
|---|---|
| « ffmpeg introuvable » | Installez ffmpeg (§1) puis « Réessayer » sur l'enregistrement (Historique). |
| Micro refusé / non listé | Autorisez le micro pour `127.0.0.1` dans le navigateur, puis « Tester » (sous le bouton Démarrer). |
| Drive : « Autorisation expirée » chaque semaine | Publiez l’app OAuth en **Production** (§5, étape 4), puis reconnectez-vous. |
| Drive : « Accès bloqué » / accès refusé par Google | L’app OAuth est en mode « Test » sans votre adresse : publiez-la en Production ou ajoutez-vous aux utilisateurs test (Audience). |
| Drive : « Ce navigateur ou cette application ne sont peut-être pas sécurisés » | Faites la connexion dans Chrome ou Safari (lien « Ouvrir la connexion Google » dans Paramètres). |
| Drive : « L’API Google Drive n’est pas activée » | Activez-la avec le lien affiché, attendez une minute, puis relancez. |
| « Lecture des cours non autorisée » | Ajoutez le scope `drive.readonly` (§5, étape 3), puis déconnectez et reconnectez Drive en laissant cochée l'autorisation de voir vos fichiers. |
| Un cours écrit par Claude n'apparaît pas | Vérifiez son nom dans `Séances/` : même préfixe `AAAA-MM-JJ_CM03_` que la transcription, extension `.md`. Puis « Récupérer depuis Drive ». |
| Mistral indisponible (erreur 503, 429, réseau) | Rien n'est perdu : la transcription est relancée toute seule (5, 10, 20, 40 min puis toutes les heures, pendant ~17 h ; l'heure du prochain essai est affichée). Une seconde de silence vérifie d'abord que Mistral répond, sans renvoyer tout l'audio. « Réessayer » relance tout de suite. |

## 9. Développement

```bash
uv run pytest
```

Structure : `packaging/macos/` (lanceur Mac), `app/main.py` (FastAPI), `app/routes/` (pages et API), `calendar_ics.py`, `recorder.py`, `pipeline.py` (file de tâches), `transcribe.py`, `subjects.py`, `supports.py`, `publish/drive.py` (dépôt et récupération des cours), `templates/` (Jinja2 + HTMX), `static/` (enregistreur JS, rendu Markdown/KaTeX, bibliothèques et police Plus Jakarta Sans embarquées pour fonctionner hors ligne).

### Points vérifiés dans la documentation (et par des tests réels)

- **Voxtral** (`client.audio.transcriptions.complete`, SDK `mistralai` 2.x) : paramètres `language`, `diarize`, `context_bias` (≤ 100 termes), `timestamp_granularities=["segment"]` ; segments avec `speaker_id`, `start`, `end`. Les termes de `context_bias` **ne doivent contenir ni espace ni virgule** (l'API renvoie une erreur 400) : « graphe pondéré » est envoyé comme `graphe_pondéré`, et l'orthographe d'origine est restaurée dans la transcription lisible. La doc indique que `language` est incompatible avec l'horodatage, mais l'API l'accepte en pratique ; l'app réessaie automatiquement sans `language` si l'API le refuse.
- **Drive API v3** : `files.update` avec média pour remplacer le contenu sur le même identifiant, `webViewLink` mémorisé pour chaque fichier et dossier ; `files.list` (`'<dossier>' in parents`) et `files.get_media` pour lire les cours. Avec le seul scope `drive.file`, `files.list` ne renvoie que les fichiers créés par l'app, sans erreur : l'app vérifie donc les autorisations accordées (Google permet de décocher une autorisation à la connexion) et le signale.
