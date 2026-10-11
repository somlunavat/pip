#!/bin/bash
# Builds the native helpers into overlay/bin (run by `npm install`, or by hand).
#   clickwatch          double-click-outside detector (plain executable)
#   PipListener.app     "Hey Pip" listener (app bundle so it owns its permissions)
# Needs the Xcode command line tools (swiftc). Without them the overlay still works;
# only double-click-outside and "Hey Pip" are unavailable.
set -u
cd "$(dirname "$0")/.."
mkdir -p bin
if ! command -v swiftc >/dev/null; then
  echo "native helpers not built: install the Xcode command line tools (xcode-select --install)"
  exit 0
fi
swiftc -O native/clickwatch.swift -o bin/clickwatch || echo "clickwatch not built"
APP=bin/PipListener.app
rm -rf "$APP" && mkdir -p "$APP/Contents/MacOS"
if swiftc -O native/listener.swift -o "$APP/Contents/MacOS/PipListener"; then
  cp native/PipListener-Info.plist "$APP/Contents/Info.plist"
  # Ad-hoc signature: macOS needs a stable identity to remember the permissions.
  codesign --force --sign - "$APP" >/dev/null 2>&1 || echo "PipListener not signed"
else
  rm -rf "$APP"
  echo "PipListener not built"
fi
