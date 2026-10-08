# Host-RAM delay lines ("PC delay atoms"): every SCOPE delay, chorus/flanger and reverb

How SCOPE 5.1 implements its long delay lines: in host memory, through the card's bus-master slot engine,
with no DSP code for the line itself. This document covers what the atoms are, how Sim2k.dll and
scScope.sys run them, how the DSP modules of a device connect to them, how parameters reach them at run
time, and the Linux implementation (kernel `pulsar_delay.c`, `tools/pulsar_delay.py`, `scope_device.py`,
`pulsard.py`).

Prerequisites: `pcm_streaming.md` (slot table, rings, entry encoding, BAR+0x10 counter), `device_format.md`
(device plans), `module_loading.md` (§4 connections, §4.5 async exports).

Legend: **[C]** read from the code (asm-checked where noted), **[L]** likely, **[?]** guess, **[V]** verified
over the whole device corpus. `FUN_10cxxxxx` = Sim2k.dll (image base 0x10c00000), `0x1800xxxxx` =
scScope.sys (x64). No SCOPE file contents are reproduced; behaviour is described in our own words.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **Four host delay atoms** (plus one non-audio host atom) block every delay and reverb [V]. They are Sim2k
   classes in `.\atoms\pc_delay*.cpp` / `pc_erDelay.cpp`; none has a `.dsp` file:

   | atom (name in the .dev) | Sim2k class | instances / devices | pads (in → out) |
   |---|---|---|---|
   | PC Master 32k Delay | `pc_delay32k` | 99 / 28 | In, Del1..Del8 → Tap1..Tap8 |
   | PC Master 4k Delay | `pc_delay4k` | 75 / 9 | In, Del1..Del8 → Tap1..Tap8 |
   | PC 256k Delay | `pc_delay256k` | 63 / 25 | in, Del → out |
   | PC Early Reflection | `pc_erDelay` | 8 / 4 | In, Del1..16, Gai1..16, Taps → Out |
   | Compensate Delay Linker | `compensate_delay_link_module` | 24 / 7 | D1..D64 → maxD, Link (no audio, §2.6) |

   Float and "1 Tap" variants (`PC Master 4k/32k Delay Float [1 Tap]`) exist in Sim2k but are not used by any
   device. The other host atoms of the corpus (DLL/VxD samplers, recorders, VDAT, SSQ sequencer, MIDI Voice
   Control VxD, ...) are a different mechanism and are not covered here.
2. **Mechanism [C]:** the DSP output that feeds a delay's input is captured into a host ring through the
   slot engine (a normal "DSP → PC" capture slot). Each tap is a PC → DSP playback slot whose table entry
   points **into the same ring with a start offset**:
   `entry = ring | ((0x82 − delay) & (len−1)) << 2 | (len == 0x8000 ? 2 : 0)`. The card reads the ring `delay`
   samples behind the write position (0x82 = 130 samples is the engine round trip DSP → host → DSP). So the
   4k/32k delays are pure slot-table programming: no copying, no DSP code, no CPU load.
3. **256k delay [C]:** input ring (4k) → the kernel copies one block per IRQ into a 0x40000-word (1 MB) host
   line and, for delays ≥ 0x882 (= 2 blocks of 1024 + 0x82), copies the delayed block into the output's own
   4k playback ring. Shorter delays read the input ring like a 4k tap.
4. **Early reflections [C]:** input ring → kernel computes `out = Σ gain_i · line[w − delay_i]` (16 taps,
   1.31 gains) per sample into the output's own 4k ring, from a 0x10000-word host line.
5. **Delay times** come from the host (pad value, samples at 48 kHz scaled by fs/48000, clamped to
   0xC2..ring length) or from a **DSP async output** (DLEXTM0/DLEXTM1/DLINTL5 "DT") that the DSP sends to a
   BAR SRAM dword; the kernel polls that dword once per block. Those modules keep short delays (< 199
   samples) on the DSP and cross-fade around delay changes.
6. **Linux:** `PULSAR_IOCTL_DELAY_ALLOC/FREE/PARAM` (pulsar_uapi.h) in `pulsar_delay.c`;
   `scope_device.py` plans now mark these atoms as `{"kind": "pc_delay"}` modules → **131/211 complete
   plans (was 95), 48 without MIDI/license atoms (was 36)**: Delay M/S, Dual Delay S, MasterVerb,
   MasterVerb Classic/Pro and the SC-* reverbs. pulsard builds them (`load_device`).

---------------------------------------------------------------------------------------------------
## 1. Where the atoms are used [V]

`scope_device.py`-based survey of the 211 `.dev` files (atoms without a `.dsp` file, counted per instance):

| atom | instances | devices |
|---|---|---|
| PC Master 32k Delay | 99 | 28: Delay M/S, Dual Delay S, Delay LCR S, Multitap M/S, all Master/Random/Space/Step Flangers and MasterChorus M/S, Hexa/4Tap Chorus, Pitch Shifter M, MasterVerb (all 3), SC-Ambience/Chorus-Delay/Inverse/Plate/RMX160/Room 5-1 |
| PC Master 4k Delay | 75 | 9: MasterVerb (3), SC-Chorus-Delay/Inverse/Plate/RMX160/Room 5-1, U KNOW 007 |
| PC 256k Delay | 63 | 25: Delay LM/LS, Dual Delay LS, Delay LCR LS, Ducking Delay M/S, SSB Delay M/S, Pattern Delay, 4 SC reverbs, 11 synths (Lightwave, Prodyssey, Profit 5, ...) |
| PC Early Reflection | 8 | 4: E-Reflector, MasterVerb, MasterVerb Pro, SC-Room 5-1 |
| Compensate Delay Linker | 24 | 7: SBC, DynamicMixer, MicroMixer, STM 16 S / 1632 / 48 S, B-2003 |

How their pads are fed [V] (net analysis of every instance):

| pad | from a DSP output | host parameter | saved constant |
|---|---|---|---|
| In (signal) | 245 of 245 (always a **sync** DSP output) | – | – |
| Del (4k/32k/256k) | 133 (DLEXTM1 87+8, DLEXTM0 20, DLINTL5 16, sample-and-hold 2) | 219 | 1103 |
| ER Del / Gai | – | 100 / 100 | 28 / 28 |

Every tap output goes to DSP inputs inside the device (never straight to a device port, never to another
host atom).

---------------------------------------------------------------------------------------------------
## 2. Sim2k side [C]

### 2.1 Construction
All classes derive from `pc_module` (base ctor FUN_10c283e0; `mod+0x28 = 0xFF` = "on the host"). Each owns
0x224-byte slot descriptors (`desc+0 = desc+4 = 0x224`, see pcm_streaming.md §3.3): one for the input (kept at
`this+0x7c`, returned for pad 0 by FUN_10c5ed20), and one per output (array at `this+0x18`).

| class (ctor) | input desc type `+0x34` | output desc type | ring / line | delay clamp (FUN) |
|---|---|---|---|---|
| pc_delay4k (FUN_10c5f2a0) | 0xE | 0x11 per tap (8) | 0x1000 words | 0xC2..0x1000 (FUN_10c5f5a0) |
| pc_delay32k (FUN_10c5eda0) | 0xF | 0x12 per tap (8) | 0x8000 words | 0xC2..0x8000 (FUN_10c5efe0) |
| pc_delay256k (FUN_10c5e7f0) | 0x10 | 0x13 (1) + host line sub-buffer: 1 entry, +0x10 = 0x40000 words | 0x1000 + 0x40000 | 0xC2..0x3FC00 (FUN_10c5eb10) |
| pc_erDelay (FUN_10c5fab0) | 0x16 | 0x17 (1) + sub-buffer 0x10000 words | 0x1000 + 0x10000 | 0x882..0x10881 per tap (FUN_10c5fd70) |

`desc+0x30 = 4` (bytes per sample) everywhere. Output descriptors are initialised with `+0xd8 = -1` (short),
`+0xda/+0xde/+0xe2 = -1`, `+0xe6/+0xea = 0`.

The factories (FUN_10c5f200, FUN_10c5f7c0, FUN_10c5ebe0, FUN_10c5fef0, ...) first call FUN_10c610a0 /
FUN_10c5f9b0, which return an **SDRAM delay** module ("SDRAM Delay", "SDRAM XiteDelay Wrapper 4k..64k",
`xite_sdram_delay*.dsp`) on boards that have SDRAM (Xite); only when that returns NULL is the PC atom built.
Pulsar2 has no SDRAM, so it always gets the host atoms.

### 2.2 Descriptor fields used by the delay types
| offset | meaning |
|---|---|
| `+0x18` | ring length in words (written by the kernel) |
| `+0x1c` | host sub-buffer array (256k / ER line; entry +0x10 = length in words) |
| `+0xd8` (short) | taps: **slot of the input (write) descriptor**; ER/256k use it to find the input ring |
| `+0xda` | taps: board index of that slot |
| `+0xde` | delay pad fed by a DSP: `async word + 0x20000` = BAR dword index (BAR + 4·(0x20000 + w) = BAR + 0x80000 + 4w); −1 = host value |
| `+0xe2` | board index of that BAR |
| `+0xe6` | requested delay (samples), written by the host on every pad change |
| `+0xea` | delay currently programmed in the table (kernel) |
| `+0xee + 4i`, `+0x132 + 4i`, `+0x176` | ER: delay − 0x882, gain (1.31), number of taps (user-side layout, asm 10c5fdf0..10c5fe64) |

The descriptor is **shared memory**: scScope.sys maps the user descriptor into the kernel at IOCTL 0x1d2058
(FUN_180005e90, also the sub-buffer array and the line buffers), so a delay change is just a store to
`desc+0xe6`; no ioctl per parameter change [C].

### 2.3 Connecting (pad callbacks) [C/L]
* **In (pad 0) ← DSP sync output.** The generic DSP → PC path (FUN_10c44c10 → FUN_10c354f0, pcm_streaming.md
  §3.2): the source output gets a comm slot A in its DSP's sync block (allocSyncOutput), `s = (A − 0xC000)/2`,
  IOCTL 0x1d2058 [card, s, input desc]. The connect callback FUN_10c5ea70 (256k) / FUN_10c5f4e0 (4k, and [L]
  32k, identical code folded by the linker) then writes `tap.d8 = board.vt+0x148(A)` (= FUN_10c2cab0,
  `(A − 0xC000)/2`) and `tap.da = board index` into every output descriptor.
* **TapK / out → DSP sync input.** The generic PC → DSP path (allocSyncInput FUN_10c2e250): first free slot in
  0x180..0x1FE, IOCTL 0x1d2058 [card, s, tap desc], the DSP input linked to `0xC000 + 2s`. Only connected taps
  get a slot.
* **DelK ← DSP async output** (pad flag 0x40 of the source pad): FUN_10c44c10 allocates a host dword w in the
  "DSP → PC memory" pool (FUN_10c1a9e0, board+0x8c; error text "No more DSP->PC memory"), the source's async
  export list gets the host header, and the callback stores `tap.de = w + 0x20000`, `tap.e2 = board`.
* Disconnect callbacks FUN_10c5e7a0 / FUN_10c5ed40 / FUN_10c5f980 reset d8/da (input) or de/e2 (delay pad).
* A comm slot that moves (sync block reorganisation) is moved with IOCTL 0x1d2084 (FUN_10c22e20, "moving
  audio buffer %03x to %03x") → scScope.sys FUN_180015f70: descriptor, ring and the 0x80-word SRAM window move
  to the new slot index.

### 2.4 Parameter updates [C]
FUN_10c5efe0 (32k) / FUN_10c5f5a0 (4k) / FUN_10c5eb10 (256k): for every delay pad **not** driven by a DSP
(`vt+0x11c(pad)` false) and without a BAR source (`de < 0`), `desc.e6 = clamp(pad value, 0xC2, max)`.
The pad value is what base.dll wrote with `ROCAtom::SetInPad`: Del pads have `Unit = 2`, so the value is
already `samples@48k · fs/48000` (device_format.md §3.4).

ER (FUN_10c5fd70), per tap i: gain = pad `Gai(i+1)` unless DSP-driven; delay = pad `Del(i+1)` clamped to
0x882..0x10881, stored as `delay − 0x882`; **a delay below 0x882 forces the gain to 0**; `Taps` clamped to
0..16.

### 2.5 Delay-time helper modules on the DSP [C, sharc_dis]
DLEXTM0/DLEXTM1 ("delay external", pads In, RI = return input, Delay → async out DT, sync outs Out, SO = send
output) and DLINTL5 (+ TPos) wrap a PC delay: SO → PC delay In, PC delay Tap → RI, DT → PC delay Del.
* The async code keeps the requested delay (input `Delay`, clamped ≥ 0) and runs a small state machine: on a
  change it fades the output down (steps of 2^25), sends the new value through `os_sendmsgPX2` on DT (to the
  host dword), waits a fixed number of async passes (0x1C2) for the host to apply it, then fades back in.
* The sync code holds a 0xCA-word DSP delay line: delays below 0xC7 (199) are served on the DSP, longer ones
  from the return input (the PC tap).
So click-free modulation and the short-delay range are handled on the DSP; the host only follows DT.

### 2.6 Compensate Delay Linker (not audio)
`compensate_delay_link_module` (FUN_10c592d0, functions FUN_10c590a0/FUN_10c59590): collects per-input latency
values (≤ 16 samples each) of mixer channel strips, computes the maximum (also across other "CompDel:"
modules) and writes `max − delay_i` into the linked DSP module's `delayLen` array (ModuleSetValue-style
writes). It moves no audio. It is left as a blocker in the plans (7 mixer/synth devices); emulating it means
running that max/compensation logic in pulsard.

---------------------------------------------------------------------------------------------------
## 3. Kernel side: scScope.sys [C]

### 3.1 IOCTL 0x1d2058 "Setting buffer" → 0x180015260
* Taps (0x11 / 0x12): **no ring is allocated**; only `desc+0x18 = 4 / 0x20 pages · 0x400` words.
* 0xF (32k input): 0x20 pages = 0x8000 words (128 KB, 128 KB aligned); 0xE / 0x10 / 0x13 / 0x16 / 0x17:
  4 pages = 0x1000 words.
* Maps the descriptor and the sub-buffer array/lines into the kernel (FUN_180005e90), then "start" of the
  slot (0x1800146e0): zeroes the slot's SRAM window (BAR + 0x80000 + 0x200·s) and its ring; for an input type
  (0xE/0xF/0x10/0x16) also zeroes the SRAM windows of every 0x11/0x12 tap whose `d8` is this slot.
* A second descriptor on an already used slot goes to a shared "linked" list (DAT_180027708) — not needed on
  Linux.

### 3.2 Entry encoding 0x180010440 (cases used by delays)
| type | entry |
|---|---|
| 0xE / 0x10 / 0x16 (inputs) | `P | 1` (capture) |
| 0xF (32k input) | `P | 3` (capture, 0x8000-word ring) |
| 0x11 / 0x12 tap | input ring `P_in | ((0x82 − e6) & 0xFFF) << 2` if the input type is 0xE/0x10; `P_in | ((0x82 − e6) & 0x7FFF) << 2 | 2` if it is 0xF; 0x800 (idle) while `d8` is unset; then `ea = e6` |
| 0x13 (256k out) | `e6 > 0x881`: own ring `P_out`; else like 0x11 on the 0x10 input ring |
| 0x17 (ER out) | own ring `P_out` (default case) |

### 3.3 Per block: slot_service 0x180011bd0 (called from the DPC with prev/next block indices)
`prev = (BAR10 − blk) & ~(blk−1)`, `next = (BAR10 + blk) & ~(blk−1)` (block_process 0x180012f00).
* 0x11 / 0x12: if `de > 0`, `e6 = clamp(BAR dword[de], 0xC2, ring length)`; if `e6 ≠ ea` mark the table dirty
  (rebuilt with the new offset at the end of the DPC, 0x180010960).
* 0x13: same with the clamp 0xC2..(line length − 0x400); dirty only when crossing 0x882; then the copy
  (0x180010c60 case 0x13): the **just completed input block** `prev` of the 0x10 ring is appended to the
  host line; if `e6 > 0x881`, `blk` words starting `e6 − 0x882` behind the line's write position are copied
  into the own ring at `next`.
* 0x17: 0x180007a30, per sample: `line[w] = in[prev+i]`, `out[next+i] = Σ_{t < taps} mul31(line[w − d_t], g_t)`
  (32-bit wrap-around sum; mul31 = FUN_180001ac0).
* 0xE / 0xF / 0x10 / 0x16 / 0x11 / 0x12 copy nothing; no watchdog for any delay type.
* Latency bookkeeping: 0x882 = 2·0x400 + 0x82: the copy path takes block k−1 and writes block k+1, which
  assumes the default 1024-frame block.

### 3.4 ER layout mismatch (x64 driver) [C]
The kernel reads the ER reflections at `+0xee + 4i` (delay), **`+0x12e + 4i`** (gain) and **`+0x16e`** (count)
(asm 180007aff..180007b7a), while Sim2k writes gains at `+0x132 + 4i` and the count at `+0x176`. With the
shared descriptor this shifts gain i to reflection i+1 and takes the count from gain 15 — a SCOPE x64 bug
[L]. Linux follows the Sim2k (intended) meaning.

### 3.5 Other IOCTLs
0x1d20e4 returns the ring length 0x1000 (0x8000 on "big ring" boards, ctx+0x3ad0); 0x1d205c releases a slot
(0x180016320); 0x1d2084 moves a slot (§2.3). None of the vxd/ASIO machinery is involved.

---------------------------------------------------------------------------------------------------
## 4. Linux implementation

### 4.1 Kernel: `pulsar_delay.c` + `pulsar_uapi.h`
```c
struct pulsar_delay_alloc {            /* PULSAR_IOCTL_DELAY_ALLOC  _IOWR('P', 0x10) */
	__u32 kind;                        /* PULSAR_DELAY_4K / 32K / 256K / ER */
	__u32 write_slot;                  /* capture slot (A - 0xC000)/2 of the source's comm slot, 0x40..0x17f */
	__u32 ntaps;                       /* 4K/32K: 1..8, 256K/ER: 1 */
	__u16 tap_slot[16];                /* 0 = kernel picks (lowest free >= 0x180); returned */
	__s32 delay[16];                   /* initial delays (ER: the 16 reflections) */
	__u32 handle;                      /* returned */
	__u32 reserved[3];
};
/* PULSAR_IOCTL_DELAY_FREE _IOW('P', 0x11, __u32 handle)
 * PULSAR_IOCTL_DELAY_PARAM _IOW('P', 0x12, {handle, param, index, value}):
 *   P_DELAY  samples (clamped like scScope.sys), P_SOURCE BAR dword 0x800..0x1fff fed by a DSP (0 = host),
 *   P_GAIN / P_NTAPS for ER. */
```
* Rings: `dma_alloc_coherent` 16 KB / 128 KB (naturally aligned, < 4 GB with the 32-bit mask, IOVA under the
  IOMMU); 256k/ER also a 16 KB output ring and a `vzalloc` line (1 MB / 256 KB).
* Table: entries go to both banks (as the PCM fast path). `table_extent()` moves the terminator to
  `max(PCM slots, delay slots) + 1` (new entries first, then the new terminator, then the old terminator
  replaced) and rewrites the PC-slot count in header words 0/1. `table_layout()` (pulsar_pcm.c, at
  SET_ROUTE) includes the delay slots and restores their entries.
* ISR (`pulsar_delay_interrupt`, after the PCM): once per block, poll the DSP-fed delay dwords
  (value ≤ 0 = nothing received yet), rewrite tap entries that changed, run the 256k copy and the ER sum
  with the threshold `2·block + 0x82` (Windows: fixed 0x882). The ER sum saturates instead of wrapping.
* Lifetime: `pulsar_delay_free_all()` on hwdep release (pulsard exit) and on remove; free idles the entries,
  `synchronize_irq()`, waits two blocks, then frees the memory. ALLOC needs a valid PCM route (block size).
* Not ported: the dirty/rebuild bank flip (entries are written in place), the slot move (no relocation on
  Linux), multi-descriptor slots.

### 4.2 Userspace
* `tools/pulsar_delay.py`: atom table (`ATOMS`, `atom_spec`, `pad_role`), ioctl wrappers, `HostWords`
  (BAR dwords 0x1000..0x1FFF for DSP-fed delays; Sim2k's pool is 0x800..0x1FFF with the sysmsg acks at
  0x800+2n and the uC mailbox at 0x80B), `export_to_host()` (adds header `0x63E00000 | w` to the source's
  async export list — the same form as `dspAckDest`), `PcDelay` (one instance).
* `scope_device.py` (separate functions `_pc_delay_module`, `_pc_delay_annotate`, `_pc_delay_converter_steps`):
  * a host delay atom becomes a module `{"kind": "pc_delay", "pc_delay": {kind, type, taps, min_delay,
    max_delay, slot_types, sim2k}, "dsp_file": None, "cycles": 0}` instead of a blocker;
  * `plan["pc_delays"]`: per atom `{key, type, kind, input: [src_key, out], taps_used, pads: {in: "const" |
    "param" | "dsp"}}`; wires from a DSP async output to a delay pad get `"host_async": true`;
  * remaining blockers: delay input not fed by exactly one DSP output, a delay pad fed by something other
    than a DSP async output, delay chained to delay, a device port wired straight to the atom;
  * `DelayTimeCalcEx` is emulated in direct mode: `Tout = min(Tin, Dmax)` (tempo mode `DirT = 0`, where
    Tout = 6000/BPM · 192000 · note/384 ticks, is not followed by the time knob).
* `pulsard.py` `load_device`: a `pc_delay` module becomes an inner node of kind `pc_delay`; after loading the
  DSP modules and **before any wire**, `_setup_pc_delays` moves each delay input's source output to a comm
  slot (`alloc_sync_output`), seeds the delays from the plan constants and calls DELAY_ALLOC; `_link` links
  tap consumers to `0xC000 + 2·tap_slot` and wires DSP async outputs to delay pads through a host dword
  (P_SOURCE + export); `set_value`/`set_param` on a delay pad → P_DELAY / P_GAIN / P_NTAPS; unload frees the
  kernel delay and the host dwords. `devices` hides delay devices if the running module has no delay ioctls
  (cache file bumped to `devices-v2.json`).
* `pulsar_modules.py`: DSP5's sync block may grow up to 0xC300 (the comm area after DSP5's block is unused,
  and capture slots go up to 0x17F = (0xC2FE − 0xC000)/2) [L]. Other DSPs keep 11 free comm slots, so pulsard
  puts devices with more than 8 delay lines on DSP5 when it has the cycles (MasterVerb: 13 lines, 689 cycles;
  MasterVerb Pro: 14 lines, 920 cycles).

### 4.3 Coverage after the change [V]
`scope_device.py survey`: complete plans 95 → **131**; usable (no MIDI, no license atom) 36 → **48**. The 12
new usable devices: Delay M, Delay S, Dual Delay S, MasterVerb, MasterVerb Classic, MasterVerb Pro,
SC-Ambience, SC-Chorus-Delay, SC-Inverse, SC-Plate, SC-RMX160 S, SC-Room 5-1. The other 24 delay devices are
complete but contain an "Effect Package" license atom; 4Tap Chorus and Pattern Delay are polyphonic; the synths
need MIDI/voices. The SC-* reverbs need 1181..2604 DSP cycles, more than one DSP: pulsard must split them over
several DSPs (not implemented) before they can load.

Dry run (`pulsard.Graph` on a simulated board): Delay M → 1 line (write slot 0x62, tap 0x182, 20499 samples),
Dual Delay S → 2 lines, MasterVerb / MasterVerb Pro on DSP5 → 13 / 14 lines (4k/32k/ER), 9 of them fed by
DLEXTM*/DLINTL5 through host dwords 0x1000..; load + set_param + unload run without errors.

---------------------------------------------------------------------------------------------------
## 5. Hardware test plan (in this order; monitors low; `pulsar_test.sh plate --cfg 0x484` = mute)

1. Build + install the module (`make`, deb or insmod), boot via pulsard as usual. `dmesg` clean; PCM still
   plays (regression: table_layout now calls the delay hooks).
2. **Raw 4k line, no device** (python, pulsard stopped, own hwdep fd after `pulsar_loader boot --pcm`): load
   CSineR4 + LINVOL(−30 dB) on DSP2, `alloc_sync_output` of the LINVOL output → A; DELAY_ALLOC(kind 4K,
   write_slot (A−0xC000)/2, ntaps 1, delay 4800); link a second LINVOL(−30 dB) input on DSP1 to
   `0xC000 + 2·tap_slot`, its output → Mix L in1. Expect: the sine on the left output; GetValue of the tap
   DM word non-zero; no DMAR faults. Verify the delay: switch the sine on/off (set LINVOL gain 0/−30 dB) and
   record analog out with `arecord` from a loopback cable: the tap lags the direct path by 4800 ± 2 samples
   (this calibrates 0x82; if not, measure the offset and adjust `DLY_LAT`).
3. P_DELAY sweeps 194 → 4096 (4k) and 32768 (32k); FREE; repeat ALLOC/FREE 100× (no faults, entries back
   to 0x800, terminator correct: dump BAR+0x80000.. with `pulsar_test.sh dump`).
4. pulsard: `pulsarctl load_device "Effects/Mono/Delay/Delay M.dev"`, insert after PC Volume L, play audio:
   echo at 427 ms; `set_param "Delay Time" 200`, Feedback, Bypass. Then Delay S / Dual Delay S.
5. DSP-fed delay: a device with DLEXTM1 (MasterVerb on DSP5): check the host dwords 0x1000.. change
   (`pulsar_test.sh peek 0x84000`), that the kernel follows them (tap entries change), and listen for clicks
   on Reverb/Size changes. This also tests DSP5's grown sync block (13 capture slots, slots up to 0x9E+).
6. ER and 256k: MasterVerb's two ER lines (rooms), then a 256k device without license atom when one exists
   (all current ones carry a license atom: test with a hand-built graph like step 2, delay 0x10000 =
   1.36 s, and around the 0x882 switch point).
7. Block size 256 (route.block 256): delays must stay exact (the copy threshold follows the block).

---------------------------------------------------------------------------------------------------
## 6. Open points
* [L] 0x82 engine round trip measured by Windows for its timing; verify (§5 step 2). It may depend on the
  block size or the bus-master prefetch.
* [L] Capture of comm slots beyond DSP5's normal block (0xC140..0xC2FF) — no DSP or the card is known to use
  that area on a single-board system.
* [?] Whether the engine tolerates a header PC-slot count change while running (Windows does it in the DPC
  rebuild with a bank flip; Linux writes in place).
* Comm slots are never freed by pulsar_modules (each device reload uses new ones); a free list is needed
  before many reloads.
* Multi-DSP placement of large devices (SC-* reverbs) and the Compensate Delay Linker (mixers).
* Tempo-synced delays (`DelayTimeCalcEx` with DirT = 0: BPM / note value) and the `Not`/`If` logic around
  them.
