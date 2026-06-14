#!/usr/bin/env bash
# Bouwt "Lazytype.app" voor macOS. MOET op een Mac draaien.
#
#   chmod +x build_app.sh && ./build_app.sh
#
# Vereist: python3, pip. Het script installeert de dependencies + PyInstaller.
set -euo pipefail
cd "$(dirname "$0")"

echo "→ Dependencies installeren…"
python3 -m pip install -r requirements.txt pyinstaller

# .icns aanmaken als die er nog niet is (Pillow), anders met iconutil.
if [ ! -f icon.icns ]; then
  echo "→ icon.icns genereren…"
  python3 dictate_tray.py --make-icons || true
fi
ICON_ARG=""
[ -f icon.icns ] && ICON_ARG="--icon icon.icns"

echo "→ Bouwen met PyInstaller…"
python3 -m PyInstaller --noconfirm --clean --windowed \
  --name "Lazytype" $ICON_ARG \
  --osx-bundle-identifier com.lazytype \
  --collect-all sounddevice --collect-all pystray \
  dictate_tray.py

echo
echo "Klaar: dist/Lazytype.app"

echo "→ DMG aanmaken…"
hdiutil create \
  -volname "Lazytype" \
  -srcfolder "dist/Lazytype.app" \
  -ov -format UDZO \
  "dist/Lazytype.dmg"
mkdir -p site/downloads
cp "dist/Lazytype.dmg" "site/downloads/Lazytype.dmg"
echo "Klaar: dist/Lazytype.dmg  (ook gekopieerd naar site/downloads/)"

echo
echo "BELANGRIJK — permissies (eenmalig, vraag de gebruiker dit te doen):"
echo "  Systeeminstellingen → Privacy & Beveiliging →"
echo "   • Toegankelijkheid      : 'Lazytype' aanvinken (voor de sneltoets + plakken)"
echo "   • Invoermonitoring      : 'Lazytype' aanvinken (voor de globale toets)"
echo "   • Microfoon             : toestaan (wordt bij eerste opname gevraagd)"
