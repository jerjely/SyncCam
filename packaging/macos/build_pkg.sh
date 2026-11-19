#!/bin/bash
# Build an installer .pkg that puts SyncCam.app into /Applications.
#   packaging/macos/build_pkg.sh <version> <AppleSilicon|Intel>
# Run from the repository root after `pyinstaller SyncCam.spec`.
set -euo pipefail

VERSION="${1:?usage: build_pkg.sh <version> <AppleSilicon|Intel>}"
ARCH_LABEL="${2:?usage: build_pkg.sh <version> <AppleSilicon|Intel>}"
APP="dist/SyncCam.app"
OUT_DIR="installer_out"
PKG="${OUT_DIR}/SyncCam-${VERSION}-macOS-${ARCH_LABEL}.pkg"

# the package version must be numeric; "1.0.0-rc1" -> "1.0.0"
PKG_VERSION="$(echo "$VERSION" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+')"

[ -d "$APP" ] || { echo "error: $APP not found - run pyinstaller first" >&2; exit 1; }
mkdir -p "$OUT_DIR"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/root"
ditto "$APP" "$WORK/root/SyncCam.app"

# do not let macOS "relocate" the app to some other copy it finds on the disk
pkgbuild --analyze --root "$WORK/root" "$WORK/components.plist" >/dev/null
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$WORK/components.plist"

pkgbuild \
  --root "$WORK/root" \
  --component-plist "$WORK/components.plist" \
  --install-location /Applications \
  --identifier io.github.jerjely.synccam.pkg \
  --version "$PKG_VERSION" \
  "$PKG"

echo "built $PKG"
ls -l "$PKG"
