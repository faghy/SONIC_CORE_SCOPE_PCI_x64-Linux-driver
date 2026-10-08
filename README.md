# Linux driver for the Creamware / Sonic Core Pulsar II (snd-pulsar)

**English** | [Italiano](README.it.md)

A Linux driver for the **Creamware / Sonic Core Pulsar II** DSP sound card (PCI `14b5:0600`, 6× Analog Devices
SHARC ADSP-21065L). It was written by reverse engineering the Windows SCOPE 5.1 software: the kernel driver
`scScope.sys` and the `Sim2k.dll` library.

## Status

| Feature | Status |
|---|---|
| PCI detection, BAR0 mapping, IRQ | working |
| Boot of the 6 DSPs (`puls2os*.21k` OS images) | **working**: every DSP answers |
| Internal audio clock at 44.1 kHz | **working** (measured: 44,095 samples/s) |
| 48 kHz | **working** (default, same as PipeWire) |
| External clock | implemented, not yet tested |
| DSP module loading (linker), analog outputs | **working**: test tone on outputs 1/2 |
| Playback from the PC (ALSA PCM, PipeWire) | **working**: "Pulsar2 Stereo" output |
| Recording from analog inputs 1/2 | **working**: "Pulsar2 Stereo" input |
| Direct input → output monitoring, levels in `alsamixer` | **working**: "DSP Out", "Input Monitor" |
| Installation with automatic start-up (DKMS + systemd service) | **working**: the card starts by itself at boot |
| `pulsard` daemon: DSP modules loaded and wired while the card runs (`pulsarctl`) | **working** |
| "Pulsar Scope" GUI (Qt): module library, drag & drop, cables, input sliders | **working** (first version) |
| Projects: save/open (`.pulsar`), rack restored automatically after a reboot | **working** |
| `.deb` package (DKMS driver, service, tools, GUI) | **working** |
| SCOPE devices (`.dev`): 36 effects (EQ, filters, dynamics, distortion, phaser, flanger, chorus…) as one block with knobs in real units | **working** |
| SCOPE-style knobs (Hz, dB…) for devices and common modules, correct float/integer pad encoding | **working** |
| SCOPE factory mixers (DynamicMixer, MicroMixer, STM 1632, STM 16 S, STM 48 S) as one block: channels appear when their input is connected, channel-strip panel with faders, pan, mute (On/Muted), aux sends and per-channel input meters | **working** (DynamicMixer tested on the card) |
| GUI: drag the rack view with the mouse, zoom to the pointer, on-screen MIDI keyboard (mouse + computer keys), coloured DSP load | **working** |
| Delays and reverbs (PC-side delay lines in host RAM, new kernel ioctls) | **working** (Delay S tested on the card) |
| Licensed "Effect Package" devices: unlocked by the card with **your own** SCOPE licence key file (`sudo pulsar-import-dsp --license YOURSERIAL.v5`) | **working**: 107 devices with a licence |
| Factory presets (`.pre`): preset menu in the device panel, `pulsarctl load_preset` | **working** |
| PC MIDI in (ALSA sequencer client "Pulsar2 MIDI") + polyphonic test synth (4 voices) | **working**: notes and chords in tune |
| Factory synths (voice arrays), ADAT/S/PDIF/MIDI ports | to do |

## Architecture

As on Windows, the kernel driver only exposes the hardware; the DSPs are booted from userspace:

- **`snd-pulsar.ko`**: kernel module. It detects the card and handles the IRQ. The hwdep device
  `/dev/snd/hwC<n>D0` allows `mmap` of BAR0 (4 MB). The ALSA PCM device is created once the DSP modules are
  loaded (`PULSAR_IOCTL_SET_ROUTE` ioctl). ALSA mixer controls are backed by DSP module values.
- **`tools/pulsar_loader.py`**: card reset, DSP OS upload, start-up and clock configuration.
- **`tools/sc_decode.py`**: decoder for SCOPE DSP files (`.21k`/`.dsp`/`.ol`, scrambled Analog Devices COFF).
- **`tools/pulsar_modules.py`**: DSP module linker (relocation, upload, execution chain, connections).
- **`tools/scope_dev.py`**: decoder for SCOPE device files (`.io`/`.dev`/`.mdl`/`.pro`).
- **`tools/pulsard.py`** + **`tools/pulsarctl.py`**: daemon that owns the card after boot and accepts commands
  (`pulsarctl status`, `load`, `connect`, `set`, `unload`) on the `/run/pulsard.sock` socket (group `audio`).
- **`tools/pulsar_scope.py`**: "Pulsar Scope" modular GUI (Qt/PySide6, any desktop): drag modules from the
  library onto the rack, wire pads with the mouse, set input values, save and open projects (`.pulsar`).
  Started from the applications menu. The current rack is also saved automatically and restored at boot.
- **`tools/scope_device.py`**: turns a SCOPE device (`.dev`) into an assembly plan: DSP modules, internal
  wires, external ports, parameters with units and SCOPE curves (`plan`, `survey`, `raw`).
- **`tools/pulsar_values.py`** / **`tools/pulsar_widgets.py`**: pad value encoding (fixed 1.31, integer, float) and
  the SCOPE-style knob used by Pulsar Scope.
- **`tools/sharc_dis.py`**: SHARC disassembler (port of MAME's `sharc_dasm.cpp`, BSD-3).
- **`docs/`**: reverse-engineering notes (register map, command protocol, boot sequence, clock, streaming).

## DSP files (not included)

The DSP files belong to Sonic Core and are **not distributed** in this repository. You need the official
installer `SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe` and a valid licence. Extract it with `innoextract` into a
`scope_full` folder next to the repository:
```bash
innoextract -d ../scope_full SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe
```
Alternatively, point the `PULSAR_DSP_DIR` variable to its `App/Dsp` folder.

## Installation from the .deb package (Debian / Ubuntu)

Download `snd-pulsar_<version>_all.deb` from the [Releases](https://github.com/faghy/SONIC_CORE_SCOPE_PCI_x64-Linux-driver/releases) page, then:
```bash
sudo apt install ./snd-pulsar_0.5.1_all.deb                          # driver (DKMS), service, tools, Pulsar Scope
sudo pulsar-import-dsp SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe      # once: Sonic Core DSP files and devices
```
Reboot (or reload the module): the card starts by itself. Remove with `sudo apt remove snd-pulsar`
(`purge` also deletes the imported DSP files and the saved rack). Build the package yourself with
`packaging/build-deb.sh`.

## Ardour (and other JACK applications)

Tested with Ardour 8.12 on Debian 13: playback, recording from the analog inputs and MIDI tracks playing the DSP synth.
Use the **JACK** backend served by PipeWire (do not use Ardour's ALSA backend: PipeWire owns the card and the
card's buffer has 4 periods of 1024 frames):

```bash
sudo apt install pipewire-jack
sudo cp /usr/share/doc/pipewire/examples/ld.so.conf.d/pipewire-jack-*.conf /etc/ld.so.conf.d/ && sudo ldconfig
```

In Ardour's Audio/MIDI Setup choose **Audio System: JACK** and press Start (no JACK server is needed, PipeWire
answers). Ports: `Pulsar2 Stereo:capture_FL/FR` (analog in 1/2), `Pulsar2 Stereo:playback_FL/FR` (analog out 1/2),
`Pulsar2 MIDI:Synth In` (MIDI to the DSP rack). Rate 48 kHz, buffer 1024 frames (21 ms).

## Installation from source (automatic start-up)

```bash
sudo ./install.sh --dsp-from ../scope_full/app/App/Dsp     # or --dsp-from SONIC_CORE_SCOPE_PCI_v5.1.2709-x64_EN.exe
```
The script installs the dependencies (`dkms`, kernel headers) and the driver through DKMS, which rebuilds it on
every kernel update. It also installs the tools in `/usr/lib/snd-pulsar`, the DSP files in
`/var/lib/snd-pulsar/dsp` and the `snd-pulsar@.service` unit. The service starts by itself when the card is
detected and brings up the DSPs, clock, audio and monitoring. Settings live in `/etc/default/snd-pulsar`; logs
are available with `journalctl -u 'snd-pulsar@*'`. Uninstall with `sudo ./uninstall.sh` (`--purge` also removes
the DSP files).

## Building and testing

```bash
make                                   # uses /lib/modules/$(uname -r)/build
pulsarctl status                       # with the service installed: card and module status
pkexec tools/pulsar_test.sh boot       # load the module, boot the DSPs and the clock
pkexec tools/pulsar_test.sh clock      # measure the DSP word clock
pkexec tools/pulsar_test.sh boot --tone 440 --volume -40   # test tone on analog outputs 1/2
pkexec tools/pulsar_test.sh reload boot --bus-master --irq --pcm --monitor -12   # ALSA/PipeWire card + monitoring
alsamixer -c Pulsar2                   # "DSP Out" and "Input Monitor" levels
tools/pulsar_loader.py boot --dry-run  # simulation without hardware
```
Other `pulsar_test.sh` commands (all need root):
- `info`: reads the card registers and state;
- `diag`: diagnostics of the inter-DSP bus and the shared SRAM;
- `dump`: saves the card registers and SRAM to a file;
- `peek --dsp N --sym NAME`: reads a variable from the OS of a running DSP;
- `clock`: measures the DSP word clock.

## Licence

GPL-2.0-or-later (kernel driver). `tools/sharc_dis.py` is derived from MAME (BSD-3-Clause).
