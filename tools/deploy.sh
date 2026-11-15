#!/bin/zsh
# Packages src/ the way `busy-cli build` lays it out, installs it on a BUSY Bar
# over Wi-Fi through the bar's own API, and starts it. Handy without Node
# tooling; with it, `npm run build && npm run app:install` does the same.
# Usage:  tools/deploy.sh [bar address]      (default busybar.local)
# Needs: Network > HTTP API > Over Wi-Fi turned on in the bar's web page.
set -euo pipefail

BAR="http://${1:-busybar.local}"
ROOT="${0:A:h:h}"
ID=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['id'])" "$ROOT/src/appmeta/manifest.json")
OUT="$ROOT/dist/$ID"
PKG="$ROOT/dist/$ID.tgz"

python3 "$ROOT/tools/make_art.py"
node --check "$ROOT/src/main.js"

rm -rf "$ROOT/dist" && mkdir -p "$OUT/scripts"
cp -R "$ROOT/src/appmeta" "$ROOT/src/images" "$OUT/"
cp "$ROOT/src/main.js" "$OUT/scripts/main.js"
COPYFILE_DISABLE=1 tar --no-mac-metadata --exclude ".DS_Store" -czf "$PKG" -C "$ROOT/dist" "$ID"
echo "package: $PKG ($(du -h "$PKG" | cut -f1))"

STAGE=$(curl -sS -m 30 -X POST "$BAR/api/apps/stage" -H "Content-Type: application/octet-stream" --data-binary "@$PKG")
KEY=$(print -r -- "$STAGE" | python3 -c "import json,sys; print(json.load(sys.stdin)['install_key'])") || { echo "stage failed: $STAGE"; exit 1; }
echo "install: $(curl -sS -m 30 -X POST "$BAR/api/apps/install?install_key=$KEY")"
curl -sS -m 10 -X POST "$BAR/api/apps/quit" >/dev/null || true
echo "launch: $(curl -sS -m 10 -X POST "$BAR/api/apps/launch?app_id=$ID")"
