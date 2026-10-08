# Contributing

Thank you for your interest in the Linux driver for the Creamware / Sonic Core Pulsar II.
This project is a clean-room reimplementation built by reverse engineering, so a few rules are stricter
than usual. Please read them before opening a pull request.

(Italian speakers: issues and pull requests in Italian are welcome too.)

## 1. What must never be committed

The repository is public. Sonic Core's material is copyrighted and is **not** redistributable.

- **No Sonic Core files**: DSP files (`.21k`, `.dsp`, `.ol`), devices (`.dev`), presets (`.pre`), DLLs,
  drivers, skins, or anything else extracted from the SCOPE installer.
- **No decompiler or disassembler dumps** of Sonic Core binaries (Ghidra/IDA output, decoded sections).
- **No raw opcode bytes** in the docs. Disassembly listings show address + mnemonic + comments only.
- **No memory dumps** of the card (SRAM, command FIFO, DSP memory).
- **No licence data**: no key files (`*.v5`), board serials, licence keys or unlock words, and no tools
  that generate them. The driver only uses the user's own key file, from `/var/lib/snd-pulsar/license`.

Users import the Sonic Core files from their own SCOPE installer with `sudo pulsar-import-dsp`.
Code and tests must work with that and must not ship any of those files.

## 2. Reporting a problem

Please include:

- distribution, kernel version (`uname -r`) and the package version (`dpkg -l snd-pulsar`);
- the board (Pulsar II, Pulsar, Luna, Scope…) and the back plate, if you know it;
- `systemctl status 'snd-pulsar@*'` and `journalctl -b -u 'snd-pulsar@*'`;
- `dmesg | grep -i pulsar`;
- for GUI or device problems: the device name and the steps that reproduce the problem.

Do not attach your licence key file or your board serial.

## 3. Building and testing

```bash
make                                   # kernel module (needs the kernel headers)
make W=1                               # please keep the module warning-free
packaging/build-deb.sh                 # .deb package in dist/
```

Most of the userspace code can be tested **without the card**: `pulsard` has a simulated board.

```bash
cd tools
PULSAR_DSP_DIR=/path/to/your/SCOPE/app/App/Dsp \
    python3 -I pulsard.py --dry-run --no-midi --socket /tmp/pulsard-test.sock
PULSARD_SOCKET=/tmp/pulsard-test.sock python3 -I pulsarctl.py status
```

`python3 tools/scope_device.py plan <file.dev>` shows how a SCOPE device is converted.

Tests on real hardware are very welcome, especially on boards and back plates the maintainer does not own
(ADAT, S/PDIF, other plates, other Scope cards). Please say what you tested and how.

**Safety:** some DSP modules (oscillators, for example) output full scale. Keep the volume low and route test
signals through a volume module when you try new graphs.

## 4. Pull requests

- Keep each pull request small and focused, and explain *why* as well as *what*.
- Match the style of the surrounding code (Python: plain standard library + PySide6; C: kernel style).
- If you change the register map, the DSP message format or a file format, update the matching document in
  `docs/` and say where the evidence comes from (function or address in the original driver, a hardware test…).
- New user-visible features: update `README.md` (English) and, if you can, `README.it.md` (Italian).
- Bump `packaging/VERSION` only for releases (the maintainer does that).

## 5. Licence

The driver is released under the GPL-2.0-or-later licence (see the SPDX headers in the sources);
`tools/sharc_dis.py` is derived from MAME and stays BSD-3-Clause.
By contributing you agree that your contribution is released under the licence of the file you change.
