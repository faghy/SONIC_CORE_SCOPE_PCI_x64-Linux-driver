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
| `0x10` | reg4 | R: free-running counter, NOT 1:1 with samples (~11.6k/s at 44.1k, small wrap) |
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
  - `pulsar_pcm.c`: placeholder PCM, NOT functional (no DMA). Only registered with `enable_pcm=1`,
    so PipeWire does not route desktop audio to a silent sink.
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
make -C /home/faghy/.gemini/antigravity-cli/brain/b1d9e742-e8fc-4d70-832c-3b2941e40f15/scratch/kheaders/usr/src/linux-headers-6.12.111+deb13-amd64 M=$(pwd) PAHOLE=true modules
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
   48 kHz (`--rate 48000`) implemented but not yet tested on hardware.
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
5. **Next:** capture (P2_ANI -> comm slots, docs/pcm_streaming.md 5.6), ALSA mixer control for LINVOL, 48 kHz test,
   packaging (DKMS + systemd unit running the loader at boot + script extracting DSP files from the user's installer),
   then the SCOPE-like config app.
