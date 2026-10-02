#!/bin/bash
# Lanceur de « Cours auto.app » (créée par packaging/macos/install.sh).
#
# À chaque clic sur l'icône :
#   1. si l'app tourne déjà : ouvre simplement la page dans le navigateur ;
#   2. sinon, récupère la dernière version de `main` sur GitHub (avance rapide uniquement : jamais de
#      modification locale écrasée), démarre le serveur en arrière-plan et ouvre la page.
# L'app s'arrête avec « Quitter l'app » (en bas de la barre latérale).
#
# Le script est lu par l'app depuis le dépôt : une mise à jour du lanceur arrive avec `git pull`.
# Compatible avec le bash 3.2 de macOS. Tout le code est dans main() : bash lit la fonction entière avant de
# l'exécuter, le fichier peut donc être remplacé par la mise à jour pendant qu'il tourne.

main() {
  set -u
  REPO_DIR="${COURS_REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
  LOG_DIR="${COURS_LOG_DIR:-$HOME/Library/Logs/Cours auto}"
  LOG="$LOG_DIR/app.log"
  BRANCH="main"
  OPEN_CMD="${COURS_OPEN_CMD:-open}"
  # Une app lancée depuis le Finder n'hérite pas du PATH du terminal (Homebrew, uv…).
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
  mkdir -p "$LOG_DIR"
  cd "$REPO_DIR" || { alert "Dossier du projet introuvable : $REPO_DIR"; exit 1; }

  PORT="$(read_port)"
  URL="http://127.0.0.1:$PORT"

  if is_up; then
    "$OPEN_CMD" "$URL"
    exit 0
  fi

  if ! command -v uv >/dev/null 2>&1; then
    alert "uv est introuvable. Installez-le (voir le README, section « Application Mac »), puis relancez."
    exit 1
  fi

  update
  notify "Démarrage de Cours auto…"
  log "Démarrage ($(git log -1 --format='%h, %cd' --date=short 2>/dev/null))"
  nohup uv run --quiet cours-auto >>"$LOG" 2>&1 &

  # Attente du serveur (le premier lancement installe les dépendances : jusqu'à 2 min).
  i=0
  while [ $i -lt 240 ]; do
    if is_up; then
      "$OPEN_CMD" "$URL"
      exit 0
    fi
    sleep 0.5
    i=$((i + 1))
  done
  alert "Cours auto n'a pas démarré. Le journal va s'ouvrir : $LOG"
  "$OPEN_CMD" "$LOG"
  exit 1
}

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG"; }

notify() {
  log "$1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"$1\" with title \"Cours auto\"" >/dev/null 2>&1 || true
  fi
}

alert() {
  log "ERREUR : $1"
  if command -v osascript >/dev/null 2>&1; then
    osascript -e "display alert \"Cours auto\" message \"$1\" as critical" >/dev/null 2>&1 || true
  fi
}

read_port() {
  local port=""
  if [ -f .env ]; then
    port="$(sed -n 's/^[[:space:]]*PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' .env | tail -n 1)"
  fi
  echo "${port:-8000}"
}

is_up() { curl -s -o /dev/null --max-time 1 "$URL/static/app.css"; }

# Commande limitée dans le temps (macOS n'a pas `timeout`) : réseau absent ou identifiants demandés.
with_timeout() {
  local seconds="$1"
  shift
  perl -e 'alarm shift; exec @ARGV' "$seconds" "$@"
}

update() {
  local branch before after count
  if [ ! -d .git ]; then
    return
  fi
  branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ "$branch" != "$BRANCH" ]; then
    notify "Branche « $branch » : pas de mise à jour automatique (seule « $BRANCH » est suivie)."
    return
  fi
  if ! GIT_TERMINAL_PROMPT=0 with_timeout 20 git fetch --quiet origin "$BRANCH" >>"$LOG" 2>&1; then
    notify "Mise à jour impossible (hors ligne ?) : lancement de la version actuelle."
    return
  fi
  before="$(git rev-parse HEAD)"
  after="$(git rev-parse "origin/$BRANCH")"
  if [ "$before" = "$after" ]; then
    log "Déjà à jour ($(git rev-parse --short HEAD))."
    return
  fi
  if ! git merge --ff-only --quiet "origin/$BRANCH" >>"$LOG" 2>&1; then
    notify "Mise à jour impossible (fichiers du projet modifiés à la main) : lancement de la version actuelle."
    return
  fi
  count="$(git rev-list --count "$before..$after")"
  notify "Mise à jour installée ($count nouveauté(s)) : $(git log -1 --format=%s | cut -c1-80)"
}

main "$@"
exit
