#!/bin/bash
# Builds dist/OctoDave-mac.zip from mac/OctoDave.app.
set -euo pipefail
cd "$(dirname "$0")/.."
chmod +x mac/OctoDave.app/Contents/MacOS/OctoDave
mkdir -p dist
rm -f dist/OctoDave-mac.zip
(cd mac && zip -qr -X ../dist/OctoDave-mac.zip OctoDave.app -x '*.DS_Store' -x '*__pycache__*')
echo "Built dist/OctoDave-mac.zip"
