#!/bin/bash
# First hardware test: load snd-pulsar, show card info, boot the DSPs.
# Run as root (e.g. via pkexec).
set -u
REPO=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)

if [ "${1:-}" = "reload" ]; then
	echo "=== rmmod (reload)"
	rmmod snd_pulsar 2>/dev/null && sleep 1
	shift
fi

echo "=== insmod"
if lsmod | grep -q '^snd_pulsar'; then
	echo "snd_pulsar already loaded"
else
	insmod "$REPO/snd-pulsar.ko" || exit 1
fi
sleep 1

echo "=== info"
python3 -I "$REPO/tools/pulsar_loader.py" info || exit 1

if [ "${1:-}" = "diag" ]; then
	echo "=== diag"
	python3 -I "$REPO/tools/pulsar_loader.py" diag
fi

if [ "${1:-}" = "boot" ]; then
	echo "=== boot"
	python3 -I "$REPO/tools/pulsar_loader.py" boot -v --log /tmp/pulsar_boot.log "${@:2}"
	echo "boot exit code: $?"
	echo "=== counter + IRQ rate after boot"
	python3 -I "$REPO/tools/pulsar_loader.py" counter
fi

echo "=== dmesg"
dmesg | grep -iE 'pulsar|14b5|06:01|DMAR|iommu|nobody cared' | tail -20

if [ "${1:-}" = "dump" ]; then
	python3 -I "$REPO/tools/pulsar_loader.py" dump --out "${2:-$REPO/../pulsar_dump.txt}"
	chown "${PKEXEC_UID:-${SUDO_UID:-0}}" "${2:-$REPO/../pulsar_dump.txt}"
fi

if [ "${1:-}" = "peek" ]; then
	python3 -I "$REPO/tools/pulsar_loader.py" peek "${@:2}"
fi

if [ "${1:-}" = "clock" ]; then
	python3 -I "$REPO/tools/pulsar_loader.py" clock
fi

if [ "${1:-}" = "plate" ]; then
	python3 -I "$REPO/tools/pulsar_loader.py" plate "${@:2}"
fi

if [ "${1:-}" = "counter" ]; then
	python3 -I "$REPO/tools/pulsar_loader.py" counter
fi
