# Creamware / Sonic Core Pulsar II (rev 02) - Linux ALSA Driver Project

## 1. Project Overview & Mission
This project aims to build a native open-source Linux kernel module (`snd-pulsar`) for the **Creamware / Sonic Core Pulsar II** (and related Scope DSP cards) based on reverse engineering the original Windows x64 driver (`scScope.sys`).

The card is currently installed on the host system, has been probed successfully by our Linux driver, and is recognized by ALSA and PipeWire.

---

## 2. Hardware Architecture & System Topology

### PCI Identifiers
- **Vendor ID:** `0x14B5` (`PCI_VENDOR_ID_CREAMWARE`)
- **Device ID:** `0x0600` (`PCI_DEVICE_ID_PULSAR2`)
- **Subsystem:** Creamware GmBH Pulsar2 (rev 02)
- **Bus Location:** `06:01.0 Multimedia controller [0480]: Creamware GmBH Pulsar2 [14b5:0600] (rev 02)`

### Bus & IOMMU Topology
```text
Intel 82801 PCI Bridge (00:1c.7)
  └── ASMedia PCIe-to-PCI Bridge ASM1083/1085 [1b21:1080] (05:00.0)
        └── Creamware Pulsar2 [14b5:0600] (06:01.0)
```
- **IOMMU Group 9:** Contains **only** the Pulsar2 and its ASMedia PCIe-to-PCI bridge. Fully isolated.

### On-Board DSP Architecture
- **DSP Processors:** Array of 6x Analog Devices SHARC ADSP-21065L (32/40-bit floating point, 66 MHz).
- **Communication:** Host talks to the DSPs via an internal SPI / Host Port bridge through MMIO Mailbox/FIFO buffers.
- **Audio I/O:** Serial ports (SPORT) of the SHARC DSPs drive the physical AKM/Crystal codecs and ADAT/SPDIF optical ports.

---

## 3. Reverse-Engineered Register Map (BAR 0: 4 MB MMIO at 0xf7800000)

Verified against the disassembly of `scScope.sys` and `Sim2k.dll` (Ghidra output in `decompiled/`).
Full evidence with function addresses: `docs/scScope_sys.md`, `docs/boot_sequence.md`.

**Architecture:** the Windows kernel driver does NOT boot the DSPs. It maps the whole BAR into the
SCOPE process (IOCTL 0x1d2048) and userspace (`Sim2k.dll`) drives everything. Linux mirrors this:
the hwdep device `/dev/snd/hwC<n>D0` can be mmap()ed, and `tools/pulsar_loader.py` does the boot.

| Offset | Reg | Description |
|---|---|---|
| `0x00` | reg0 | R: board id, `(v>>8)&0x1f` = 6 for Pulsar2 (`0x4600`). W: control (reset/boot/clock/bus-master bits) |
| `0x04` | reg1 | R: IRQ status. W: IRQ/block-size config (`0x11` = IRQ every 1024 samples) |
| `0x08` | reg2 | R: command-FIFO card read index (`&0x3ff`). W: clock control bits |
| `0x0C` | reg3 | W: command-FIFO host write index |
| `0x10` | reg4 | R: 1:1 frame counter, 15 bit (wraps at 0x8000); ring index = `& 0xfff` |
| `0x14` | reg5 | W: audio cfg serial bit-bang (data 0x400, clock 0x800, latch 0x100) |
| `0x1C` | reg7 | W: write 0 to ack IRQ |
| `0x20` | reg8 | W: active slot-table bank |
| `0x80000` | SRAM | slot tables A (`0x80000`) / B (`0x80800`), word 0/1 = SYNCPC window `0x8400C300/1` |
| `0x81000` | FIFO | 1024-dword host→DSP command FIFO |
| `0x82000+8n` | SRAM | sysmsg ack/reply mailbox of DSP n |

**IRQ:** read `0x10`, write 0 to `0x1C`, read status `0x04`; ours iff `(st&3)!=0 && (st&0xFFFF0000)==0`.

**DSP messages** (Pulsar2): header `len<<25 | dsp<<21 | (0x100000 for SHARC IOP regs) | addr`, wrapped as
`[T|0x120000, T|0x120000, hdr|0x20000000, payload…, T|0x20100000, 0x0FE0C008, 7 pad words, 0]`, T = dsp<<21.
The `(cmd<<26)|(len<<22)|0xA0000|addr` format is the SPI path used only by Xite boards.

**DSP files** (`.21k/.dsp/.ol`): ADI 21k COFF, XOR-scrambled; decode with `tools/sc_decode.py`
(see `docs/sc_format.md`). DSP n runs OS image `puls2os<n>.21k`.

---

## 4. Current Codebase Structure

Repository root = `linux-driver/` (GitHub: faghy/SONIC_CORE_SCOPE_PCI_x64-Linux-driver). Kernel module `snd-pulsar`:
  - `pulsar_core.c`: devm-managed probe, BAR map, board id, shared IRQ handler (Windows-exact ack/check).
  - `pulsar_hwdep.c` + `pulsar_uapi.h`: hwdep device, `mmap` of BAR0 (needs CAP_SYS_RAWIO), `PULSAR_IOCTL_GET_INFO`.
  - `pulsar_pcm.c`: zero-copy PCM over the card's slot engine, created on PULSAR_IOCTL_SET_ROUTE.
  - `pulsar_dsp.c` (kernel SetValue via command FIFO), `pulsar_mixer.c` (DSP-backed ALSA volume controls).
  - `install.sh` / `uninstall.sh` / `packaging/`: DKMS + udev + snd-pulsar@.service auto-start.
- `tools/sharc_dis.py`: SHARC disassembler (port of MAME sharc_dasm, BSD-3).
- `tools/pulsar_test.sh`: root test wrapper, run with `pkexec tools/pulsar_test.sh <info|diag|dump|peek|clock|boot>`.
- `tools/sc_decode.py`: DSP file descrambler + COFF dumper.
- `tools/pulsar_loader.py`: userspace bring-up (`info`, `boot`, `boot --dry-run`).
- `../scope_full/` (outside the repo, NOT to be published: Sonic Core copyright): full extraction of the SCOPE 5.1 installer.
- `../decompiled/` (outside the repo, NOT to be published): Ghidra C output of scScope.sys, Sim2k.dll, base.dll. Ghidra 12.1.4 + JDK21 in `~/tools`.

---

## Publishing rules (public GitHub repo)
- Never commit Sonic Core material: DSP files (`.21k/.dsp/.ol`), DLLs, installer contents, Ghidra output, SRAM/FIFO dumps.
- In docs, disassembly listings show address + mnemonic + comments only, never the raw opcode bytes.

---

## 5. Verified Real Hardware Test Results

The module was loaded with `sudo insmod snd-pulsar.ko` on the live Debian Linux host:

### Kernel Log (`dmesg`):
```text
[ 1973.738308] snd_pulsar: loading out-of-tree module taints kernel.
[ 1973.738318] snd_pulsar: module verification failed: signature and/or required key missing - tainting kernel
[ 1973.740006] snd-pulsar 0000:06:01.0: Creamware/SonicCore card detected at MMIO 0xf7800000 (len: 4096 KB)
[ 1973.740010] snd-pulsar 0000:06:01.0: Hardware ID: 0x00004600, Detected Board Revision: 6
```

### ALSA Subsystem:
```text
$ cat /proc/asound/cards
 2 [Pulsar2        ]: Pulsar2 - SonicCore Pulsar2
                      SonicCore Pulsar2 at 0xf7800000, irq 16 (rev 6)

$ aplay -l
card 2: Pulsar2 [SonicCore Pulsar2], device 0: Pulsar PCM [Pulsar2 DSP Audio]
  Subdevices: 0/1
  Subdevice #0: subdevice #0
```

### PipeWire / WirePlumber / Desktop:
- Automatically created `Pulsar2 Stereo Sink` (ID 76) and `Pulsar2 Stereo Source` (ID 77).
- Automatically selected by GNOME Desktop as the primary default audio output.

---

## 7. How to Build & Run

### Directory:
```bash
cd /home/faghy/puksar2/linux-driver
```

### Build command:
```bash
make            # kernel headers are now installed system-wide (linux-headers-amd64)
sudo ./install.sh --dsp-from ../scope_full/app/App/Dsp   # reinstall via DKMS after changes, then reboot
```

### Load & Unload commands:
```bash
sudo insmod snd-pulsar.ko
sudo rmmod snd-pulsar
```

---

## 8. Roadmap
1. **DSP boot: WORKING (2026-10-07).** `pkexec tools/pulsar_test.sh boot` boots all 6 DSPs; each answers
   GetValue(dspID) with its own index. Key facts: Pulsar2 DSPs are Sim2k class `pluto` (DM 0xC400..0xDFFF);
   boot loader `wait_boot` polls DM 0xDFFF (release flag = Run's stack word); sysmsg block = OS symbol
   `sysMsg` (0xC419), words written to sysMsg+1 as `[a, b, type]`. See docs/dsp_boot_analysis.md.
   Tools: `tools/sharc_dis.py` (SHARC disassembler). Root access for tests: `pkexec tools/pulsar_test.sh <info|diag|dump|boot>`.
2. **Sample rate / clock: WORKING at 44.1 kHz (2026-10-07).** `boot` now runs `finish_run()` (docs/clock_rate.md):
   backplateID=2 (PPlate, 11-bit cfg word, 0x484 = 44.1k internal), uC serial via DSP5, FScale/asRatio, sysmsg 0xB, clock resync.
   Verified: DSP `wclk` advances 44095/s (`pulsar_test.sh clock`). NOTE: BAR+0x10 is NOT a 1:1 sample counter (≈11.6k/s, wraps).
   48 kHz VERIFIED (2026-10-07): `--rate 48000` -> plate cfg 0x402, FScale 0x40000000, asRatio 15; BAR+0x10 47995/s,
   IRQ 46.8/s, ALSA hw_params rate 48000. 48 kHz is now the default in /etc/default/snd-pulsar (matches PipeWire).
3. **Module linker: implemented offline (2026-10-07), not yet run on hardware.** Spec: docs/module_loading.md;
   code: `tools/pulsar_modules.py` (`selftest`, `link --ops --disasm`, `dryrun`). Libraries (.ol) auto-pulled,
   first-fit Sim2k heaps, reloc 2/3/4/6, init via sysmsg 2/7, sync chain via `ret_sync` patch (sysmsg 6 + codeBuf),
   inputs = SetValue of seg_mod input word + patch of `inputN` sites, cross-DSP sync slots via sysmsg 0xB.
   `Board.upload_code` now has the state-2 path. Next: run CSineR4 -> P2_ANO on hardware (`pm.execute(board, ops)`).
3b. **FIRST AUDIO OUT (2026-10-07):** `pkexec tools/pulsar_test.sh boot --tone 440 [--volume -30]` plays a sine on
   analog out 1/2: P2_AINIT(+PINIT.ol)@DSP0, P2_ANO(+P2_IO.ol)+CSineR4+LINVOL@DSP1 (P-Plate analog modules are fixed to DSP1).
   KEY: PPlate cfg bit 0x80 MUTES the analog outputs. Run writes 0x484 (muted), so 0x404 (44.1k) must be written after the
   modules are loaded. `pulsar_test.sh plate --cfg 0x484` mutes again, `--cfg 0x404` un-mutes.
   SAFETY: CSineR4 is full scale (0 dBFS); always go through LINVOL (Out = In*Vol, 1.31 fraction).
4. **PCM PLAYBACK: WORKING (2026-10-07).** `pkexec tools/pulsar_test.sh reload boot --bus-master --irq --pcm`:
   loader builds PC slots 0x180/0x181 (DM 0xC300/0xC302) -> 2x LINVOL (-30 dB safety gain) -> P2_ANO@DSP1, un-mutes
   (0x404) and calls PULSAR_IOCTL_SET_ROUTE; the kernel then creates the ALSA PCM (zero-copy, non-interleaved S32_LE,
   4096-frame ring = buffer, period = 1024 = IRQ block, slots enabled at ring wrap, pointer = BAR+0x10 & 0xfff).
   Verified: speaker-test via plughw:2,0 and pw-play via PipeWire sink "Pulsar2 Stereo", both channels clean.
   BAR+0x10 is a 1:1 frame counter (15 bit, wraps at 0x8000); IRQ 43/s at block 1024.
   NOTE: WirePlumber only picks up the PCM if it starts after SET_ROUTE (in tests: `systemctl --user restart
   wireplumber`); in the final package the loader runs at boot before the user session.
4b. **CAPTURE + DIRECT MONITOR + MIXER: WORKING (2026-10-07).** `--pcm --monitor -12`: P2_ANI@DSP1 sync outs ->
   comm slots 0xC0A4/0xC0A6 -> capture slots 0x52/0x53 (entry |1); verified with arecord on hw:2,0 (24-bit samples, L != R).
   Monitor per channel: PC->LINVOL and ANI->LINVOL -> ADD2N ((a+b)/2) -> P2_ANO. Mixer controls (PULSAR_IOCTL_SET_CONTROLS):
   "DSP Out Playback Volume" and "Input Monitor Playback Volume" = LINVOL Vol slots, written by the kernel via the command
   FIFO (pulsar_dsp.c); deliberately NOT named "PCM" so PipeWire keeps software volume and cannot lift the safety gain.
   Test workflow: `systemctl --user stop wireplumber` before reload/boot (it holds the card), start it again afterwards.
4c. **AUTO-START PACKAGE: WORKING after reboot (2026-10-07).** Service snd-pulsar@hwC0D0 (card index can change) boots the card in ~1.1 s; GNOME shows Pulsar2 output+input; alsactl restored the saved mixer levels. `sudo ./install.sh --dsp-from
   ../scope_full/app/App/Dsp` installed: DKMS snd-pulsar 0.1.0 (/usr/src/snd-pulsar-0.1.0, signed with /var/lib/dkms/mok.key),
   tools in /usr/lib/snd-pulsar, DSP files in /var/lib/snd-pulsar/dsp, /etc/default/snd-pulsar (PULSAR_ARGS),
   udev rule /etc/udev/rules.d/70-snd-pulsar.rules -> snd-pulsar@hwC<n>D0.service (pulsar-start: loader boot + alsactl restore).
   After reboot check: `systemctl status 'snd-pulsar@*'`, `journalctl -b -u 'snd-pulsar@*'`, `wpctl status` (Pulsar2 sink/source),
   `amixer -c Pulsar2 contents`. Repo test scripts (pulsar_test.sh reload/insmod) would now fight the installed module.
4d. **pulsard DAEMON: WORKING (2026-10-07), driver 0.1.1.** snd-pulsar@.service now runs `tools/pulsard.py` (Type=notify,
   stays alive, owns the hwdep): boots the card + default graph, then serves JSON-lines on /run/pulsard.sock (group audio):
   status / catalog / load / unload / connect / disconnect / set (`pulsarctl` CLI in /usr/bin). Verified on hardware: CSineR4 +
   LINVOL loaded at runtime on DSP2 (auto-picked), wired cross-DSP into Mix L on DSP1, audible; unload restores.
   After set_controls every DSP frame goes through PULSAR_IOCTL_SEND_MSG (kernel fifo_mutex): the kernel also writes the FIFO
   (mixer), and a userspace-cached write index went stale ("command FIFO stalled").
   Default graph node ids: pc_play, pc_rec, n1 Analog Init, n2 Analog Out, n3 Analog In, n4/n7 PC Volume L/R,
   n5/n8 Monitor Volume L/R, n6/n9 Mix L/R.
4e. **GUI "Pulsar Scope" v1: WORKING (2026-10-08).** `tools/pulsar_scope.py` (PySide6, Fusion style + own dark palette, so it
   is desktop independent), installed to /usr/lib/snd-pulsar/tools + /usr/share/applications/pulsar-scope.desktop.
   Library tree by category (other boards' and hardware I/O modules hidden), drag & drop modules, drag cables pad->pad
   (handled in RackView.mousePressEvent: NO grabMouse, it froze the app), Delete/right-click removes, double-click opens
   input sliders (0..max of the pad; Apply sends only moved sliders). Layout in ~/.config/pulsar-scope/layout.json.
   Verified by the user: sine -> LINVOL -> Mix L/R audible on both channels.
4f. **Projects (2026-10-08).** pulsard: save_project / load_project / reset / set_gui; project = JSON (.pulsar) with added
   modules, wires that differ from the default graph, removed default wires, values, gui.layout; ids are remapped on load.
   Autosave of the rack to /var/lib/snd-pulsar/current-project.json after every change, restored when pulsard starts.
   `set` on base (fixed) nodes is refused: their gains belong to the ALSA mixer. GUI: File menu New/Open/Save/Save as.
4g. **.deb package 0.2.0 (2026-10-08), INSTALLED on this PC via apt (replaces install.sh).** `packaging/build-deb.sh` ->
   dist/snd-pulsar_<VERSION>_all.deb: DKMS source in /usr/src, unit in /usr/lib/systemd/system, udev rule in
   /usr/lib/udev/rules.d, conffile /etc/default/snd-pulsar, /usr/bin/pulsar-scope + pulsarctl, /usr/sbin/pulsar-import-dsp
   (DSP files from the SCOPE installer .exe via innoextract or a folder). Bump packaging/VERSION for every release.
   To update this PC: build the deb, `sudo apt install ./dist/...deb`, then stop wireplumber, rmmod/modprobe snd_pulsar.
4h. **SCOPE devices + knobs (2026-10-08), deb 0.3.0.** `tools/scope_device.py` (docs/device_format.md): .dev -> plan
   (modules, internal wires, ports incl. passthrough, consts, params with SCOPE knob/display curves, switches). 95/211
   complete, 36 usable (no MIDI, no licensed "Effect Package" atoms). pulsard: `devices` (survey, cached
   /var/cache/pulsard/devices-v1.json, ~60 s first time, served outside the board lock), `load_device`, `set_param`
   (display units; switch params re-plan + rewire, e.g. Bypass), devices are ONE node (inner modules have parent set,
   hidden from status), wiring goes through `_src_ep`/`_dst_eps`/`_link`; projects store devices + params.
   Pad encoding: type&0xF 1 = fix 1.31 (int if max < 0x10000), 2 = IEEE float (min/max float bits), 0xE = MIDI
   (tools/pulsar_values.py). Frequency pads (unit 1) are scaled by 48000/fs, time pads (unit 2) by fs/48000.
   GUI: knobs (tools/pulsar_widgets.py, live, Shift = fine, double-click = default), DevicePanel, "SCOPE devices"
   library branch. Devices folder: /var/lib/snd-pulsar/devices (pulsar-import-dsp copies <installer>/app/Devices).
4i. **deb 0.5.0 (2026-10-08): PC delays, MIDI, licence, presets.** Kernel pulsar_delay.c (DELAY_ALLOC/FREE/PARAM, host-RAM
   rings, docs/pc_delay.md). MIDI: tools/pulsar_midi.py, ALSA seq client "Pulsar2 MIDI" -> SNC2MIDI on DSP2 (node pc_midi),
   builtin:test_synth (docs/midi_synths.md). Licence (docs/presets_license.md, tools/pulsar_license.py): key file in
   /var/lib/snd-pulsar/license/*.v5 (0600, `pulsar-import-dsp --license`), board sno from uC 0x229; modules with magicProt get
   uC unlock ops (DSP5, cmd 0x220) before fnInit; devices() hides devices whose seg_id the licence does not cover (107 shown).
   Presets: `presets`/`load_preset` commands, .pre in /var/lib/snd-pulsar/presets, GUI combo in DevicePanel.
   Device survey uses multiprocessing "spawn" (fork deadlocked inside the threaded daemon). NEVER commit the user's .v5/serial.
5. **Next:** factory synths (voice arrays from pc_midi), hardware test of delays/reverbs and licensed audio, ADAT/S/PDIF/MIDI, 88.2/96 kHz (PPlate cannot; check other plates), JACK/Ardour check, .deb package, then the SCOPE-like config app.
