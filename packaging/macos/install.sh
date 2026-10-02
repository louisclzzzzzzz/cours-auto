#!/bin/bash
# Crée « Cours auto.app » dans ~/Applications : un clic lance l'app (mise à jour comprise).
# À lancer une fois depuis le dossier du projet :   bash packaging/macos/install.sh
# Relancer le script ne fait que recréer l'icône (vos données et réglages ne sont pas touchés).
set -eu

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
APP_DIR="${COURS_APP_DIR:-$HOME/Applications}/Cours auto.app"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

missing=""
command -v git >/dev/null 2>&1 || missing="$missing\n  - git : xcode-select --install"
command -v uv >/dev/null 2>&1 || missing="$missing\n  - uv : curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v ffmpeg >/dev/null 2>&1 || missing="$missing\n  - ffmpeg : brew install ffmpeg"
if [ -n "$missing" ]; then
  printf "À installer d'abord :%b\n" "$missing"
  exit 1
fi
if [ ! -f "$REPO_DIR/.env" ]; then
  echo "Astuce : copiez .env.example en .env et renseignez vos clés (voir le README)."
fi

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

# L'exécutable de l'app appelle le lanceur du dépôt : il se met à jour avec le reste du code.
cat >"$APP_DIR/Contents/MacOS/cours-auto" <<EOF
#!/bin/bash
export COURS_REPO_DIR="$REPO_DIR"
exec /bin/bash "$REPO_DIR/packaging/macos/launcher.sh"
EOF
chmod +x "$APP_DIR/Contents/MacOS/cours-auto"

cat >"$APP_DIR/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Cours auto</string>
  <key>CFBundleDisplayName</key><string>Cours auto</string>
  <key>CFBundleIdentifier</key><string>local.cours-auto.launcher</string>
  <key>CFBundleExecutable</key><string>cours-auto</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
</dict>
</plist>
EOF

# Icône : PNG 1024 px du dépôt converti en .icns (outils fournis avec macOS).
ICON_SRC="$REPO_DIR/packaging/macos/icon.png"
if command -v iconutil >/dev/null 2>&1 && command -v sips >/dev/null 2>&1; then
  ICONSET="$(mktemp -d)/AppIcon.iconset"
  mkdir -p "$ICONSET"
  for size in 16 32 128 256 512; do
    sips -z $size $size "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    sips -z $((size * 2)) $((size * 2)) "$ICON_SRC" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$APP_DIR/Contents/Resources/AppIcon.icns"
  rm -rf "$(dirname "$ICONSET")"
fi
touch "$APP_DIR"  # le Finder rafraîchit l'icône

echo "✅ « Cours auto » est installée dans $(dirname "$APP_DIR")."
echo "   Faites-la glisser dans le Dock pour la lancer en un clic."
if command -v open >/dev/null 2>&1; then
  open -R "$APP_DIR"  # montre l'app dans le Finder
fi
