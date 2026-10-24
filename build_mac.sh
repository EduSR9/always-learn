#!/usr/bin/env bash
# Builds "dist/Always Learn.app" and dist/always-learn-mac${MAC_LABEL}.dmg on macOS.
set -euo pipefail
cd "$(dirname "$0")"

python3 -m pip install -r requirements.txt pyinstaller
python3 -c "from PIL import Image; Image.open('assets/icon.png').save('assets/icon.icns')"

python3 -m PyInstaller --noconfirm --windowed \
    --name "Always Learn" \
    --icon assets/icon.icns \
    --add-data "assets:assets" \
    --osx-bundle-identifier com.edusr9.always-learn \
    always-learn.py

# Text macOS shows when Always Learn asks to read the browser's address bar.
plutil -replace NSAppleEventsUsageDescription -string \
    "Always Learn reads the address of your browser's front tab to know when you are in an AI chat." \
    "dist/Always Learn.app/Contents/Info.plist"
# Ad-hoc signature (not notarized): users open it with right-click → Open the first time.
codesign --force --deep --sign - "dist/Always Learn.app"

# Disk image with an Applications shortcut to drag the app onto.
rm -rf dist/dmg && mkdir dist/dmg
cp -R "dist/Always Learn.app" dist/dmg/
ln -s /Applications dist/dmg/Applications
hdiutil create -volname "Always Learn" -srcfolder dist/dmg -ov -format UDZO \
    "dist/always-learn-mac${MAC_LABEL-}.dmg"
echo "Done: dist/always-learn-mac${MAC_LABEL-}.dmg"
