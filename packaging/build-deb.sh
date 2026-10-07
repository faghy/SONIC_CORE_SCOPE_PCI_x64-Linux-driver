#!/bin/bash
# Build snd-pulsar_<version>_all.deb from this repository (no root needed).
#   packaging/build-deb.sh [OUTDIR]
set -euo pipefail
REPO=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
VERSION=$(cat "$REPO/packaging/VERSION")
OUT=${1:-$REPO/dist}
MAINT="$(git -C "$REPO" config user.name 2>/dev/null || echo snd-pulsar) <$(git -C "$REPO" config user.email 2>/dev/null || echo root@localhost)>"
ROOT=$(mktemp -d)
trap 'rm -rf "$ROOT"' EXIT

# kernel module sources for DKMS
SRC=$ROOT/usr/src/snd-pulsar-$VERSION
install -d "$SRC"
install -m 0644 "$REPO"/pulsar*.c "$REPO"/pulsar*.h "$REPO/Makefile" "$SRC/"
sed "s/@VERSION@/$VERSION/" "$REPO/packaging/dkms.conf" > "$SRC/dkms.conf"
install -D -m 0644 "$REPO/pulsar_uapi.h" "$ROOT/usr/include/sound/pulsar_uapi.h"

# tools, service, udev, desktop
LIB=$ROOT/usr/lib/snd-pulsar
install -d "$LIB/tools" "$ROOT/usr/bin" "$ROOT/usr/sbin"
install -m 0644 "$REPO"/tools/*.py "$LIB/tools/"
chmod 0755 "$LIB/tools/pulsar_loader.py" "$LIB/tools/pulsard.py" "$LIB/tools/pulsarctl.py" "$LIB/tools/pulsar_scope.py"
install -m 0755 "$REPO/packaging/pulsar-start" "$LIB/"
ln -s ../lib/snd-pulsar/tools/pulsarctl.py "$ROOT/usr/bin/pulsarctl"
install -m 0755 "$REPO/packaging/pulsar-scope" "$ROOT/usr/bin/pulsar-scope"
install -m 0755 "$REPO/packaging/pulsar-import-dsp" "$ROOT/usr/sbin/pulsar-import-dsp"
install -D -m 0644 "$REPO/packaging/snd-pulsar@.service" "$ROOT/usr/lib/systemd/system/snd-pulsar@.service"
install -D -m 0644 "$REPO/packaging/70-snd-pulsar.rules" "$ROOT/usr/lib/udev/rules.d/70-snd-pulsar.rules"
install -D -m 0644 "$REPO/packaging/pulsar-scope.desktop" "$ROOT/usr/share/applications/pulsar-scope.desktop"
install -D -m 0644 "$REPO/packaging/snd-pulsar.default" "$ROOT/etc/default/snd-pulsar"
install -D -m 0644 "$REPO/README.md" "$ROOT/usr/share/doc/snd-pulsar/README.md"
install -m 0644 "$REPO/README.it.md" "$ROOT/usr/share/doc/snd-pulsar/README.it.md"
cp -r "$REPO/docs" "$ROOT/usr/share/doc/snd-pulsar/docs"
find "$ROOT/usr/lib/snd-pulsar" -name __pycache__ -prune -exec rm -rf {} +

# control files
install -d "$ROOT/DEBIAN"
for f in control postinst prerm postrm; do
	src="$REPO/packaging/deb/$f"; [ "$f" = control ] && src="$REPO/packaging/deb/control.in"
	sed -e "s/@VERSION@/$VERSION/" -e "s|@MAINTAINER@|$MAINT|" "$src" > "$ROOT/DEBIAN/$f"
done
chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/prerm" "$ROOT/DEBIAN/postrm"
echo "/etc/default/snd-pulsar" > "$ROOT/DEBIAN/conffiles"
echo "Installed-Size: $(du -sk --exclude=DEBIAN "$ROOT" | cut -f1)" >> "$ROOT/DEBIAN/control"

mkdir -p "$OUT"
DEB=$OUT/snd-pulsar_${VERSION}_all.deb
dpkg-deb --root-owner-group --build "$ROOT" "$DEB" >/dev/null
echo "$DEB"
