#!/bin/bash
# Remove everything install.sh put in place (DSP files and /etc/default/snd-pulsar are kept unless --purge).
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root (sudo or pkexec)"; exit 1; }

systemctl stop 'snd-pulsar@*' 2>/dev/null
rm -f /etc/systemd/system/snd-pulsar@.service /etc/udev/rules.d/70-snd-pulsar.rules
systemctl daemon-reload; udevadm control --reload
for v in $(dkms status snd-pulsar 2>/dev/null | sed -n 's|^snd-pulsar/\([^,:]*\).*|\1|p' | sort -u); do
	dkms remove -m snd-pulsar -v "$v" --all
	rm -rf "/usr/src/snd-pulsar-$v"
done
rm -rf /usr/lib/snd-pulsar /usr/include/sound/pulsar_uapi.h
if [ "${1:-}" = "--purge" ]; then
	rm -rf /var/lib/snd-pulsar /etc/default/snd-pulsar
fi
rmmod snd_pulsar 2>/dev/null || echo "snd_pulsar still loaded (in use): it will be gone after a reboot"
echo "snd-pulsar removed."
