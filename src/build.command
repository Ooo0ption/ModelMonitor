#!/bin/zsh
set -euo pipefail
cd -- "${0:A:h}"
APP_PATH="${1:-../dist/Model Monitor.app}"
mkdir -p "$APP_PATH/Contents/MacOS" "$APP_PATH/Contents/Resources"
export CLANG_MODULE_CACHE_PATH="${TMPDIR:-/tmp}/modelscope-clang-cache"
export SWIFT_MODULECACHE_PATH="${TMPDIR:-/tmp}/modelscope-swift-cache"
swiftc -O -parse-as-library -swift-version 5 -target arm64-apple-macosx13.0 -module-cache-path "$SWIFT_MODULECACHE_PATH" -framework AppKit -framework SwiftUI ModelScope.swift -o "$APP_PATH/Contents/MacOS/ModelScope"
cp controller.py capture_addon.py capture_diagnostics.py packet_details.py full_capture.py traffic_archive.py store.py model_report.py defaults.json "$APP_PATH/Contents/Resources/"
cp README.txt "$APP_PATH/Contents/Resources/"
cp Info.plist "$APP_PATH/Contents/Info.plist"
if [ -f AppIcon.icns ]; then cp AppIcon.icns "$APP_PATH/Contents/Resources/"; fi
cp "Start Model Monitor.command" "$(dirname "$APP_PATH")/Start Model Monitor.command"
codesign --force --deep --sign - "$APP_PATH"
printf '\nBuilt:%s\n' "$APP_PATH"
