#!/bin/bash
# Install the Creamware / Sonic Core Pulsar II driver: kernel module (DKMS), boot service, tools, DSP files.
#
#   sudo ./install.sh [--dsp-from PATH]
#
# PATH is either the "App/Dsp" folder of an extracted SCOPE PCI 5.1 installation, or the installer itself
# (SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe, extracted with innoextract). Default: ../scope_full/app/App/Dsp.
# The DSP files belong to Sonic Core and are not part of this repository.
set -euo pipefail

REPO=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
VERSION=$(cat "$REPO/packaging/VERSION")
DSP_FROM="$REPO/../scope_full/app/App/Dsp"
LIB=/usr/lib/snd-pulsar
DSP_DIR=/var/lib/snd-pulsar/dsp

while [ $# -gt 0 ]; do
	case "$1" in
	--dsp-from) DSP_FROM="$2"; shift 2 ;;
	*) echo "usage: $0 [--dsp-from PATH]"; exit 1 ;;
	esac
done
[ "$(id -u)" = 0 ] || { echo "run as root (sudo or pkexec)"; exit 1; }

step() { echo; echo "=== $*"; }

step "1/5 build dependencies (dkms, kernel headers)"
pkgs="dkms linux-headers-amd64"
dpkg -s "linux-headers-$(uname -r)" >/dev/null 2>&1 || pkgs="$pkgs linux-headers-$(uname -r)"
if [ "${DSP_FROM##*.}" = "exe" ]; then pkgs="$pkgs innoextract"; fi
missing=""
for p in $pkgs; do dpkg -s "$p" >/dev/null 2>&1 || missing="$missing $p"; done
if [ -n "$missing" ]; then
	apt-get update -qq || echo "warning: apt-get update reported errors (third-party repositories?), continuing"
	DEBIAN_FRONTEND=noninteractive apt-get install -y $missing
else
	echo "already installed"
fi

step "2/5 DSP files -> $DSP_DIR"
if [ "${DSP_FROM##*.}" = "exe" ]; then
	tmp=$(mktemp -d)
	innoextract -q -d "$tmp" "$DSP_FROM"
	DSP_FROM="$tmp/app/App/Dsp"
fi
[ -f "$DSP_FROM/puls2os0.21k" ] || { echo "no SCOPE DSP files in $DSP_FROM (see --dsp-from)"; exit 1; }
mkdir -p "$DSP_DIR"
cp -a "$DSP_FROM"/. "$DSP_DIR"/
echo "$(ls "$DSP_DIR" | wc -l) files"
[ -n "${tmp:-}" ] && rm -rf "$tmp"

step "3/5 kernel module snd-pulsar $VERSION (DKMS)"
for old in $(dkms status snd-pulsar 2>/dev/null | sed -n 's|^snd-pulsar/\([^,:]*\).*|\1|p' | sort -u); do
	dkms remove -m snd-pulsar -v "$old" --all || true
	rm -rf "/usr/src/snd-pulsar-$old"
done
SRC=/usr/src/snd-pulsar-$VERSION
mkdir -p "$SRC"
cp "$REPO"/*.c "$REPO"/*.h "$REPO"/Makefile "$SRC"/
sed "s/@VERSION@/$VERSION/" "$REPO/packaging/dkms.conf" > "$SRC/dkms.conf"
dkms add -m snd-pulsar -v "$VERSION"
dkms build -m snd-pulsar -v "$VERSION"
dkms install -m snd-pulsar -v "$VERSION" --force
install -D -m 0644 "$REPO/pulsar_uapi.h" /usr/include/sound/pulsar_uapi.h

step "4/5 tools and boot service"
install -d "$LIB/tools"
install -m 0644 "$REPO"/tools/*.py "$LIB/tools/"
install -m 0755 "$REPO/tools/pulsar_loader.py" "$REPO/tools/pulsard.py" "$REPO/tools/pulsarctl.py" "$LIB/tools/"
ln -sf "$LIB/tools/pulsarctl.py" /usr/bin/pulsarctl
install -m 0755 "$REPO/packaging/pulsar-start" "$LIB/"
install -m 0644 "$REPO/packaging/snd-pulsar@.service" /etc/systemd/system/
install -m 0644 "$REPO/packaging/70-snd-pulsar.rules" /etc/udev/rules.d/
[ -f /etc/default/snd-pulsar ] || install -m 0644 "$REPO/packaging/snd-pulsar.default" /etc/default/snd-pulsar
systemctl daemon-reload
udevadm control --reload

step "5/5 done"
echo "The card will be started automatically at the next boot (service snd-pulsar@hwC<n>D0)."
echo "Settings: /etc/default/snd-pulsar   Log: journalctl -u 'snd-pulsar@*'"
echo "Reboot now to switch to the installed driver."
