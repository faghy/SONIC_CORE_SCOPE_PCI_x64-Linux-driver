# PCM streaming between the host and the Pulsar2 DSPs (spec for a real ALSA PCM)

How SCOPE 5.1 moves PCM audio between host memory and the SHARCs, reconstructed from `scScope.sys`
(kernel), `Sim2k.dll` (userspace), the software I/O device files (`App/Application/IOs/Software/*.mdl`)
and the DSP modules they instantiate. It ends with a concrete Linux design (kernel + loader) and a safe
step-by-step test plan.

Sources: `decompiled/scScope.sys.c` (+ asm, image base 0x180000000), `decompiled/Sim2k.dll.c` (+ asm,
image base 0x10c00000), `tools/scope_dev.py --summary`, `tools/sc_decode.py --dump`, `tools/sharc_dis.py`.
Related: `scScope_sys.md` (§2 BAR map, §3 ISR/DPC, §7 audio), `boot_sequence.md` §3.5/§3.9,
`module_loading.md` (§2 module ABI, §4 connections), `dsp_boot_analysis.md` §4.4 (os_sync / EPB1 ring),
`io_format.md` (P2_ANO/P2_ANI, P2_IO.ol frame map).

Legend: **[C]** read from code (asm-checked where Ghidra drops arguments), **[L]** likely / strong
inference, **[?]** guess. `FUN_x` = Sim2k function, `0x1800xxxxx` = scScope.sys function. Listings show
address + mnemonic only (no opcode words), per the publishing rules.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **There is no "audio DMA engine" for the driver to program, and no special DSP I/O module.** The card's
   PCI logic (the bus-master "slot engine") moves one 32-bit word per *slot* per word clock between a
   host ring and the DSP cluster bus. **Slot s ⇔ DSP DM double-buffered pair `0xC000 + 2·s` (+ I7)**. [C/L]
   * Playback (PC → DSP): slots **0x180..0x1FF** = DM **0xC300..0xC3FE**, the "PC sync window"
     (`SYNCPC_MEMBASE` = 0xC300, `syncPcChannels` = 128). First stream channel = slot 0x180 = DM 0xC300,
     next 0x181 = 0xC302, … [C]
   * Capture (DSP → PC): the slot of a DSP **sync-output comm slot** in that DSP's EPB1 broadcast block,
     `s = (A − 0xC000)/2`, A = `0xC080 + 0x20·d + 4 + 2k` (e.g. DSP1 first slot 0xC0A4 → s = 0x52). [C]
   * Any DSP module simply reads `DM(0xC000+2s, I7)` as a normal sync input (or its sync output is
     redirected to A). The DSP OS's EPB0 slave DMA (IMEP0 = 2) receives the words. [C]
2. **Per slot: one physically contiguous ring of 0x1000 32-bit words (16 KB), below 4 GB, 16 KB aligned.**
   Its bus address (| flags) goes into the **slot table at BAR+0x80000+4·s (bank A) / BAR+0x80800+4·s
   (bank B)**, BAR+0x20 selects the bank. Flags: bit0 = capture (card writes host), bit1 = 0x8000-word
   ring, 0x800 = idle, 0xC00+k = skip to slot k, 0 = end of table. [C values, L meaning]
3. **Ring index = `BAR+0x10 & 0xFFF`.** BAR+0x10 is used everywhere in scScope.sys as a sample-frame
   counter [C]. The "≈11.6k/s" measurement is explained by a counter that wraps at 2^15 or 2^16 sampled
   over ~2.02 s: `(44100·2.02 mod 65536)/2.02 ≈ 11.6k` [L — verify, §6 step 1].
4. **IRQ every block**: reg1 = 0x11 → one IRQ per 1024 frames (codes for 512/256/128/64 in §2.5). ISR =
   read 0x10, write 0 to 0x1C, read 0x04, ours iff `(st&3) && !(st&0xFFFF0000)` (already implemented).
5. **Sample format on the bus: 32-bit signed, MSB-justified (1.31 fixed point)**, one channel per slot.
   SCOPE's 16-bit "Wave" devices pack two 16-bit channels in one slot (L in the high half) and unpack on
   the DSP with `Wav2ster.dsp`; 24-bit Wave and ASIO use one channel per slot (`2NIX.dsp` = plain copy).
   For Linux: **one slot per channel, S32_LE, the DSP input linked straight to 0xC300/0xC302**.
6. **The Windows kernel copies (WavePci) into the rings at `hw + 0x5C0`; the card prefetches 64-sample
   chunks into a per-slot SRAM window `BAR+0x80000 + s·0x200`.** Linux can instead expose the rings
   directly as an ALSA non-interleaved mmap buffer (zero copy), §5.

---------------------------------------------------------------------------------------------------
## 1. Which DSP modules implement the host<->DSP channels (question 1)

### 1.1 The software I/O devices are host atoms + trivial DSP converters [C]

`scope_dev.py --summary` of `App/Application/IOs/Software/*.mdl` plus the Sim2k atom classes
(`pc_module` subclasses, vtable 0x10c868d4, base ctor FUN_10c283e0, `mod+0x28 = 0xFF` = "on the host"):

| SCOPE device (Windows view) | host atom (Sim2k class, ctor) | slot type (desc+0x34) | DSP module(s) in the .mdl | slots |
|---|---|---|---|---|
| Wave Source (Windows playback) | "Wave Output Device" `waveOut_module` FUN_10c64b00, Stereo=1 | 4 | `Wav2ster.dsp` (In → L, R) | 1 per stereo pair (16-bit packed) |
| Wave 24 Bit Source | 2 × Wave Output Device (Ch 1/2, Stereo=0) | 4 | 2 × `2NIX.dsp` | 1 per channel |
| Wave Interleaved Source | 16 × Wave Output Device | 4 | 16 × 2NIX | 1 per channel |
| Wave Dest (Windows record) | "Wave In Device" `waveIn_module` FUN_10c64470 | 3 | `Ster2wav.dsp` (L, R → Out) | 1 per stereo pair |
| Wave 24 Bit Dest | 2 × Wave In Device | 3 | 2 × 2NIX | 1 per channel |
| ASIO Source / Dest | asioOut.pc / asioIn.pc (FUN_10c58040 / FUN_10c57ad0) | 10 / 9 | `2nix64.dsp` | 1 per channel |
| ASIO2 Source / Dest | 64 × asioOut.pc + 64 × asioIn.pc | 10 / 9 | `asioOutI32.dsp`, `asioInputMonitor32.dsp` | 1 per channel |
| ASIO2 Float | asioOutFlt.pc / asioInFlt.pc | 10 / 9 | `asioOutFlt.dsp`, `asioInputMonitorFlt.dsp` | 1 per channel |
| Sound Card Source / Dest | pc2dsp.pc / dsp2pc.pc (+ExtWave) | 1 / 0xB | Wav2ster / Ster2wav | |

* The atoms **do not load any DSP module** and contain no DSP code; they only own a 0x224-byte slot
  descriptor and get a slot when one of their pads is connected to a DSP pad (§3). [C]
* `waveOut16/64.dsp`, `waveIn16/64.dsp`, `asioOut16/32.dsp`, `asioIn16/32.dsp` exist in `App/Dsp` but are
  not referenced by any `Software/*.mdl` [C for those files; other .dev/.pro not scanned, ?]. Their code
  confirms the same data model (below).
* Pad type of a host-fed input: `0x20008001` ("pc input", sync scalar, bit 0x20000000 = "wave/PC" type),
  range 0x80000000..0x7FFFFFFF [C]. Module flags 0x410000 = no fixed DSP (any DSP) [C].

### 1.2 What the DSP code does with the samples [C, sharc_dis]

Every one of these modules is plain `seg_sync` code (no init, no IOP access, no DMA). It reads its input
pair with `DM(input, I7)` like any module input; the **samples are already in DSP DM** when the sync chain
runs. So the answer to "host ring via bus master, BAR SRAM, or EPB DMA?" is: **host ring → card slot
engine (PCI bus master + SRAM prefetch) → cluster bus → each DSP's EPB0 slave DMA (IMEP0 = 2) → DM
0xC000+2s+half**. No module-side bookkeeping, no per-block counters.

`2NIX.dsp` ("2 nix", 1 sync in, 1 sync out, 3 cycles) — identity:
```
sync:   JUMP ret_sync (DB)
        R0 = DM(input0, I7)
        DM(syncOut0, I7) = R0
```
`Wav2ster.dsp` ("Wave 2 Stereo", in `Wave In` type 0x20008001, outs L/R, 7 cycles) — 16-bit packed pair:
```
        R0 = DM(input0, I7)
        R1 = 0xFFFF0000
        R1 = R1 AND R0            ; left  = high half, already MSB-justified
        DM(L, I7) = R1
        JUMP ret_sync (DB)
        R0 = FDEP R0 BY 16:16     ; right = low half moved to bits 16..31
        DM(R, I7) = R0
```
`Ster2wav.dsp` is the inverse (`(L >> 16) << 16 | (R >> 16)`), `wave2flt.dsp` the float version
(`FLOAT R BY −31`). `waveOut16.dsp` = 4 × Wav2ster in one module (4 "pc input" → 8 outs, voice jump table),
`waveIn16.dsp` = 4 × Ster2wav, `asioIn32.dsp` = 32 × 2NIX. `asioOut32.dsp` ("Asio In/Out Mixer 32") mixes
each PC input with an async "dsp input" (input monitoring): `out = 4·clip(pc·0x1FFFFFFF(SSF) + dsp, ±0x1FFFFFFF)`.

**Sample format** [C]: 32-bit two's complement, full scale ±2^31 (1.31). P2_ANO forwards the top 24 bits
to the codec (`FEXT R BY 8:24`), so S32_LE with the low byte ignored = 24-bit DAC resolution.
**Channels per module**: 1 (2NIX), 2 packed (Wav2ster), 4/32 (waveOut16/64), 32/64 (ASIO). For Linux no
converter module is needed at all: the consuming module's input is linked directly to the slot address.

### 1.3 Who receives the words on the DSP side [C, dsp_boot_analysis.md §3/§4.4]

* `os_initdma` arms EPB0 as an endless 32-bit slave DMA (`DMAC0=0x41, IMEP0=2, CEP0=-1`, re-armed every
  word clock in `os_sync`). A bus frame `header(addr, len) + len words` lands at addr, addr+2, addr+4 …
* Each DSP broadcasts its own sync outputs every word clock (EPB1 master DMA of the block at
  `0xC080+0x20·d`, words at stride 2 from `base + I7`), headers `0xC0000000|len<<25|addr` [C].
* The card itself is the extra bus master for the PC window: the two SRAM words
  `BAR+0x80000 = 0x8400C300` and `BAR+0x80004 = 0x8400C301` (written by Sim2k Reset FUN_10c32db0) have the
  same header layout — bit31 (PCI), len field = slot count, address 0xC300 / 0xC301 = the two halves of the
  double buffer [L]. The kernel rewrites only the count field (§2.3).
* So for playback channel k the DSPs see `DM[0xC300 + 2k + half]`; a module reading `DM(0xC300+2k, I7)`
  gets the current sample. For capture the card snoops the DSP broadcast at `0xC000+2s` and writes the word
  into the host ring [L: inferred from the per-slot table covering s = 0x10..0x1FF and the capture bit].

---------------------------------------------------------------------------------------------------
## 2. The card side: slot table, rings, counter, IRQ (question 2)

### 2.1 Slot buffers — IOCTL 0x1d2058 "Setting buffer" → 0x180015260 [C]

* Input `[card, slot s (0 ≤ s < 0x200), user VA of a 0x224-byte descriptor]`.
* Ring size: **4 pages = 0x1000 samples (16 KB)** for all audio types (3/4/9/10/1/0xB); 0x20 pages
  (0x8000 samples) only for types 8/0xF (and 0x12). `desc+0x18 = pages·0x400`.
* Allocation `0x180005fb0`: `MmAllocateContiguousMemorySpecifyCache(pages·4K, low=1, high=0xFFFFFFFF,
  boundary = pages·4K, MmCached)` → contiguous, **below 4 GB, naturally aligned to its size**, zeroed.
  Low 32 bits of the physical address → ctx+0x1A78[s]. Wave/ASIO rings are cached per (dir, channel)
  and reused across reopen.
* No BAR write in the IOCTL: it sets `maxslot = max(maxslot, s)` and the dirty flag; the table is rebuilt
  in the next DPC (§2.3). Start of a slot (`0x1800146e0`) zeroes the ring and the **per-slot SRAM window
  BAR+0x80000 + s·0x200 (0x200 bytes)**.
* Release 0x1d205c → 0x180016320 (ring freed or parked in the cache).

### 2.2 Entry encoding `0x180010440` [C] (P = ring address)

| slot type (desc+0x34) | entry |
|---|---|
| none registered | 0x800 (idle) |
| 4 wave out | P while the watchdog desc+0xCC ≠ 0, else 0x800 |
| 3 wave in | P\|1 while desc+0xCC ≠ 0, else 0x800 |
| 10 ASIO out | P if desc+0x38 ≥ 0, else 0x800 |
| 9 ASIO in | P\|1 if desc+0x38 ≥ 0, else 0x800 |
| 0xB dsp2pc, 0xE, 0x10, 0x14, 0x16, 0x18 | P\|1 |
| 8 / 0xF (large rings) | P\|2 / P\|3 |
| 0x1B GSIF, 0x11..0x13 (linked/tap) | P \| (start offset & 0xFFF) << 2 |
| 0 | 0xC00 → rewritten as `0xC00 + next_used_slot` (skip) |
| default (1, 2, …) | P |

Meaning [L]: **bit0 = card→host (capture)**, **bit1 = 0x8000-word ring**, bits 2..13 = start offset in
samples (ring tap), 0x800 = idle, 0xC00+k = skip, 0 = terminator. The low 14 bits are free because the
ring is 16 KB aligned (17 bits for 128 KB rings). The Windows wave-out "watchdog" (desc+0xCC set to 0x18,
re-armed to 0x14 on each host access, decremented per block; at 0 the slot goes idle and the ring is
zeroed) is driver policy, not hardware: Linux just writes P at start and 0x800 at stop.

### 2.3 Table layout and bank switch — rebuild `0x180010960` (rev ≥ 2), asm-checked [C]

```c
/* runs at the end of slot_service() in the DPC, only when dirty */
end  = max(maxslot + 1, 0x10);
base = max((readl(BAR+0x80000) & 0x3FF) >> 1, 0x10);   /* 0xC300 -> 0x180 = first PC slot */
n    = max(maxslot + 1 - base, 2);                       /* number of PC (playback) slots   */
w0   = (readl(BAR+0x80000) & 0xC03FFFFF) | (n & 0x1F) << 25 | (n & 0xE0) << 17;
writel(w0,     BAR+0x80000);                             /* header for half 0 (0xC300)      */
writel(w0 + 1, BAR+0x80004);                             /* header for half 1 (0xC301)      */
off = bank ? 0x80000 : 0x80800;                          /* write the INACTIVE bank          */
writel(0, BAR + off + 4*end);                            /* terminator                      */
for (s = 0x10; s < end; s++) writel(entry(s), BAR + off + 4*s);   /* 0xC00 -> skip pointers  */
bank ^= 1; writel(bank, BAR+0x20);                       /* 0 = 0x80000 (A), 1 = 0x80800 (B) */
```
* Table words 0 and 1 live only in the A area (the headers); entries start at **s = 0x10**; s < 0x40 are
  reserved by Sim2k (FUN_10c2c9f0), s 0x40..0x17F are DSP comm slots (capture), s ≥ 0x180 PC slots.
* Fast path [C]: `updateWaveBuffer`/`updateAsioBuffer` (0x180013c80 / 0x180014420) and the stream lookup
  0x180016d90 write `entry(s)` **directly into both banks** without a rebuild. Linux can do the same for
  start/stop of an already-laid-out table.
* Sim2k never writes entries or BAR+0x20 during streaming (it only reads `bar[0x20000+s]`/`bar[0x20200+s]`
  for a debug print in FUN_10c21ec0) [C]. At open it zeroes BAR+0x80000..0x80FFF and writes reg8 = 0; in
  Run it zero-fills 0x80008..0x80FFF before setting reg0 |= 0x80 (bus master) [C].

### 2.4 Position counter BAR+0x10

* Every scScope.sys use treats it as a frame counter [C]: ring index `& (len-1)` (0xFFF; 0x180017290,
  0x18000df90, 0x180010c60), block boundaries `& ~(blk-1)`, wclk += `(prev − last) & 0xFFF`, DPC
  latency stats `& 0x7FFF`, events `& ~0x3F`. IOCTL 0x1d20e4 reports ring length 0x1000.
* At least 15 bits are significant (`& 0x7FFF`). The loader's `clock` command measured `(c1−c0)/dt` over
  ~2 s; with a 15- or 16-bit counter at 44.1 kHz this gives `(89082 − 65536)/2.02 ≈ 11.6k/s` — i.e. the
  "11.6k/s" is a wrap artefact, the counter is 1:1 with samples [L, verify with fast sampling, §6 step 1].
* The card reads ahead: per-slot SRAM prefetch window of 2 × 64 words at `BAR+0x80000 + s·0x200`
  (0x180012a60 refreshes the half not being read; mask `(len−1) & ~0x3F`) [L]. Slot 0x180 → BAR+0xB0000,
  0x1FF → BAR+0xBFE00 (the 256 KB SRAM ends there). Expect 64–128 frames of engine latency.
* Clock-slave boards step the counter with reg2 bit1 (`while (reg4 < 0x200) { reg2|=2; reg2&=~2; }`,
  FUN_10c2cce0) [C] — further evidence that it is the sample/word-clock counter.

### 2.5 Interrupts and the DPC wave service

* Block size: Sim2k master vt+0x1ec FUN_10c2cf50: `IOCTL 0x1d20c4 [-1, intBlkSize]`, then
  `reg1 = code | 0x10` (PCI master) with code {0x400:1, 0x200:3, 0x100:0x21, 0x80:0x22, 0x40:0x23} →
  **0x11 = 1024 frames** (default), 0x13 = 512, 0x31 = 256, 0x32 = 128, 0x33 = 64 [C]. The kernel never
  writes reg1 except `= 0` on last close [C].
* ISR 0x180007090 (read 0x10, ack 0x1C=0, read 0x04, test) → DPC 0x180007720 → `block_process`
  0x180012f00: `cur = BAR[0x10]; prev = (cur − blk) & ~(blk−1); next = (cur + blk) & ~(blk−1)`; if
  `prev != last`: `slot_service(prev, next)` (0x180011bd0; rebuilds the table if dirty), client callbacks,
  `last = prev`. Status bit0 → `wave_service_all` (0x18000f270 → 0x18000eb70 per stream). [C]
* slot_service per slot: read side `a = prev & (len−1)`, write side `b = next & (len−1)`. Wave-out (4):
  only the watchdog — **the DPC copies nothing for wave-out**. ASIO-out (10) without host sub-buffers:
  memset of `ring[next .. next+blk]` (clears the block after the one playing, ahead of the client). [C]
* WavePci stream (0x18000eb70 / copy 0x18000e870): `hw = BAR[0x10] & (len−1)`; playback writes up to
  `(hw + 0x5C0) & (len−1)` (**preload 0x5C0 = 1472 frames**), starts at index 0x40 if hw == 0; zero-fills
  on underrun/pause; capture reads behind hw. Formats: 16-bit 1ch/slot `s << 16`; 16-bit packed
  `(ch[2i] << 16) | ch[2i+1]` (left high); 24-in-32 `x & 0xFFFFFF00`. GetPosition = frames copied
  (software). [C]

---------------------------------------------------------------------------------------------------
## 3. Userspace side: binding a module to a slot (question 3)

### 3.1 Playback (atom output → DSP input), FUN_10c44c10 → pluto FUN_10c385d0 → allocSyncInput FUN_10c2e250 [C]
1. Bandwidth check (board vt+0x78 FUN_10c329c0 vs FUN_10c2df20).
2. Board vt+0x13c FUN_10c20af0: first free s in `0x180..0x1FE` (`s ≥ 0x200 − syncPcChannels`, FUN_10c2e3d0;
   free = `board+0x29c[s] == 0`, limit cfg `maxPCchannels` = 0x100).
3. SetAudioBuffer FUN_10c22dd0 → FUN_10c21ec0 → **IOCTL 0x1d2058 [card handle, s, &desc]** (asm 10c21ecc..).
4. `addr = FUN_10c2ca70(s) = 0xC000 + 2·s` (board+0xac4 = 1 on Pulsar2); usage `board+0xadc[(addr−0xC000)/2]++`.
5. The DSP module's input is linked to `addr` by the ordinary link_input path (SetValue of the seg_mod input
   word + sysmsg 6 patch of the `inputN` sites, module_loading.md §4.2). No PC-slot-specific sysmsg.
6. With defaults: first channel s = 0x180 / DM 0xC300, then 0x181 / 0xC302, …

### 3.2 Capture (DSP sync output → atom input), FUN_10c44c10 → FUN_10c354f0 [C, asm 10c44e52..]
1. `desc = dst->vt+0xe8(pad)`.
2. allocSyncOutput on the source DSP (FUN_10c346a0 → FUN_10c2e070): comm slot A = `0xC080+0x20·d+4+2k`
   (block grown with sysmsg 0xB), the module's syncOut writes re-pointed to A (FUN_10c34100).
3. FUN_10c20a10: **`s = (A − 0xC000)/2`** (< 0x180), IOCTL 0x1d2058 [card, s, desc]. Failure text
   "No more DSP->PC audio channels".
4. The kernel sets bit0 in the entry (types 3/9/0xB).

### 3.3 Descriptor fields that matter (0x224 bytes) [C]
`+0x00/+0x04` = 0x224 (sizes), `+0x10` ← ring kernel VA, `+0x18` ← ring length, `+0x1c/+0x28/+0x2c` host
sub-buffer array (1 entry, 0x134 B each), `+0x30` = 4 bytes/sample [L], **`+0x34` type** (waveOut 4,
waveIn 3, asioOut 10, asioIn 9, pc2dsp 1, dsp2pc 0xB), `+0x38` (wave 0, asio −1), `+0xd8` Dev−1,
`+0xda` Ch−1, `+0xdc` Stereo (bit0 → 2 packed channels per slot). Only ring size/type/direction are
relevant to Linux.

### 3.4 The other IOCTLs
* 0x1d2088 / 0x1d208c / 0x1d2090 / 0x1d20c8 / 0x1d21a8 belong to `vxd_module` (kernel-hosted PC plug-in
  effects: client objects, input connections, parameter pointers `&BAR[0x80000+4·word]`). **Not used by the
  Wave/ASIO path, not needed for ALSA.** [C]
* 0x1d20c4 = block size (all cards), 0x1d20e4 = ring length (0x1000), 0x1d2084 = move slot, 0x1d205c =
  release. [C]
* Async (non-sync) DSP→PC values use BAR-SRAM dwords 0x800..0x1FFF (allocator board+0x8c, FUN_10c1a9e0),
  not slots. [C]

---------------------------------------------------------------------------------------------------
## 4. What Linux must reproduce, in one picture

```
   ALSA buffer (dma_alloc_coherent, <4 GB IOVA)          card SRAM (BAR)                    DSP DM (every DSP)
   ch0 ring 0x1000 x u32 @ IOVA0 (16 KB aligned)  --->  [0x80000+4*0x180] = IOVA0        0xC300/0xC301 (pair)
   ch1 ring 0x1000 x u32 @ IOVA0+16K              --->  [0x80000+4*0x181] = IOVA0+16K    0xC302/0xC303
                                                         [0x80000] = 0x8400C300 | n=2      ^ read by LINVOL/P2_ANO
   index played now = BAR[0x10] & 0xFFF                  [0x80004] = 0x8400C301 | n=2        input = 0xC300 / 0xC302
   IRQ every 1024 frames (reg1 = 0x11)                   BAR+0x20 = active bank

   capture: DSP1 P2_ANI LOut/ROut -> comm slots 0xC0A4/0xC0A6 -> s = 0x52/0x53 -> entry = IOVA|1
```

---------------------------------------------------------------------------------------------------
## 5. Linux design (question 4)

### 5.1 Division of work
* **Userspace loader** (already owns boot, clock, module linking): loads the DSP graph, links module
  inputs to the PC window, allocates capture comm slots, sets reg1 (IRQ) and reg0 bus master, and tells
  the kernel which slots carry which ALSA channel (new hwdep ioctl).
* **Kernel** owns: DMA rings, slot-table entries ≥ 0x10, the count field of words 0/1, BAR+0x20, the
  per-slot SRAM windows of its slots, IRQ → `snd_pcm_period_elapsed`, pointer from BAR+0x10.
* Ordering rule: the loader's `boot` zero-fills BAR+0x80000..0x80FFF; it must run **before** any stream
  is configured, and must not touch the table afterwards (add a guard: `boot` refuses while a PCM is open).

### 5.2 New hwdep ioctl (sketch, `pulsar_uapi.h`)
```c
struct pulsar_pcm_route {
	__u32 rate;            /* current word clock, e.g. 44100 (loader knows it; kernel cannot) */
	__u32 block;           /* frames per IRQ: 1024 (reg1 0x11), 512, 256, 128, 64 */
	__u16 play_slot[8];    /* 0x180.. ; 0 = unused   (DSP input linked to 0xC000 + 2*slot) */
	__u16 cap_slot[8];     /* (A-0xC000)/2 of the DSP sync-output comm slot; 0 = unused   */
	__u32 play_channels, cap_channels;
	__u32 flags;           /* bit0: route valid (DSP graph loaded) */
};
#define PULSAR_IOCTL_SET_ROUTE _IOW(PULSAR_IOCTL_MAGIC, 0x02, struct pulsar_pcm_route)
```
Validate: play slots in 0x180..0x1FF, capture slots in 0x40..0x17F, no duplicates, block ∈ {64..1024}
power of two. Refuse while a substream is open. The PCM is only registered (or `open` returns -EBUSY /
-ENODEV) until a valid route exists, so PipeWire never sees a silent device.

### 5.3 Buffers
* One coherent allocation per direction: `channels × 16 KB`, e.g. 32 KB for stereo, via
  `snd_pcm_set_managed_buffer(ss, SNDRV_DMA_TYPE_DEV, &pci->dev, 32K, 32K)` (or `dma_alloc_coherent`).
  The DMA API guarantees the CPU and DMA addresses are aligned to the allocation's page order
  (`get_order(32K)` → 32 KB), so channel c's ring at `+c·16K` is 16 KB aligned. With the IOMMU in
  DMA-FQ mode the address written to the table is the **IOVA** (`runtime->dma_addr`), never the
  physical address; `dma_set_mask_and_coherent(32)` (already done) keeps it below 4 GB.
  Assert at runtime: `!(dma_addr & 0x3FFF) && upper_32_bits(dma_addr) == 0`.
* Ring = 0x1000 frames exactly; the buffer size is fixed: `buffer_size = 4096 frames`,
  `period_size = block` (1024 → 4 periods; 256 → 16 periods).

### 5.4 ALSA hardware description (zero-copy, non-interleaved)
```c
static const struct snd_pcm_hardware pulsar_pcm_hw = {
	.info = SNDRV_PCM_INFO_MMAP | SNDRV_PCM_INFO_MMAP_VALID |
		SNDRV_PCM_INFO_NONINTERLEAVED | SNDRV_PCM_INFO_BLOCK_TRANSFER,
	.formats = SNDRV_PCM_FMTBIT_S32_LE,      /* MSB-justified, DAC uses bits 8..31 */
	.rates = SNDRV_PCM_RATE_KNOT,            /* constrained to route.rate in open() */
	.channels_min = 1, .channels_max = 8,    /* constrained to route.*_channels */
	.buffer_bytes_max = 8 * 0x4000,
	.period_bytes_min = 64 * 4, .period_bytes_max = 1024 * 4 * 8,
	.periods_min = 4, .periods_max = 64,
};
/* open(): rate := route.rate (snd_pcm_hw_constraint_single), channels := route.*_channels,
 *         buffer_size := 4096 frames, period_size := route.block (both single). */
```
ALSA non-interleaved layout = channel c at `c · dma_bytes/channels` (`snd_pcm_lib_ioctl` channel_info),
which is exactly ring c. PipeWire and JACK handle planar S32 natively; `plughw:` converts for others.
(Alternative if interleaved access is required: keep an ordinary interleaved buffer and copy/deinterleave
one block ahead in the IRQ, like Windows with its 0x5C0 preload — more latency, same card programming.)

### 5.5 Stream lifecycle (sketch)
```c
#define SLOT_A(s)   (0x80000 + 4 * (s))
#define SLOT_B(s)   (0x80800 + 4 * (s))
#define SLOT_WIN(s) (0x80000 + 0x200 * (s))       /* per-slot 2x64-word prefetch window */
#define SLOT_IDLE   0x800

static void slot_write(struct pulsar_card *c, int s, u32 e)    /* fast path, like 0x180013c80 */
{
	writel(e, c->iobase + SLOT_A(s));
	writel(e, c->iobase + SLOT_B(s));
}

/* once per route change (no stream running): lay out the table in both banks */
static void table_layout(struct pulsar_card *c)
{
	int s, end = max(c->maxslot + 1, 0x10), n = max(c->maxslot + 1 - 0x180, 2);
	u32 w0 = (readl(c->iobase + 0x80000) & 0xC03FFFFF) | (n & 0x1F) << 25 | (n & 0xE0) << 17;
	for (s = 0x10; s < end; s++) slot_write(c, s, SLOT_IDLE);
	writel(0, c->iobase + SLOT_A(end)); writel(0, c->iobase + SLOT_B(end));   /* terminators */
	writel(w0, c->iobase + 0x80000); writel(w0 + 1, c->iobase + 0x80004);
	/* bank: keep whatever is selected; both banks are identical */
}

prepare():  ring memory already silenced by ALSA; memset_io(SLOT_WIN(s), 0, 0x200) for our slots;
            c->armed = 0; c->running = 0.
trigger(START): c->armed = 1;                 /* enable at the next ring wrap, see below */
trigger(STOP):  for each slot: slot_write(c, s, SLOT_IDLE); c->armed = c->running = 0;
ISR (after the existing ack/status test):
	u32 pos = readl(bar + 0x10) & 0xFFF;
	if (c->armed && pos < c->block) {         /* ring index just wrapped to 0 */
		for each playback channel ch: slot_write(c, play_slot[ch], dma_addr + ch * 0x4000);
		for each capture  channel ch: slot_write(c, cap_slot[ch], (dma_addr + ch * 0x4000) | 1);
		c->armed = 0; c->running = 1;
	} else if (c->running)
		snd_pcm_period_elapsed(substream);
pointer():  return c->running ? (readl(bar + 0x10) & 0xFFF) : 0;  /* frames; ring == ALSA buffer */
            runtime->delay = 128 (engine prefetch, playback) [L, measure]
```
Why "enable at wrap": the card plays ring index `BAR[0x10] & 0xFFF`, an absolute position, while ALSA
starts at buffer offset 0. Enabling the slot when the index is in the first block makes ALSA position 0 ==
ring index 0 (start latency ≤ 4096 frames ≈ 93 ms). The first ≤128 frames may be lost to the prefetch.
If the counter turns out to wrap at a multiple of 0x1000 (2^15/2^16) this stays consistent forever.

Close / remove / suspend: write `SLOT_IDLE` to our slots in both banks, then `synchronize_irq()` and wait
≥ 2 blocks (e.g. `msleep(60)`) **before freeing the DMA memory** (the card may still prefetch); on remove
also `writel(0, bar + 0x04)` (reg1, as Windows last-close). Never free a ring whose entry is live.

### 5.6 Loader side for stereo 44.1 kHz playback (`tools/pulsar_loader.py` + `pulsar_modules.Rack`)
```
boot --rate 44100 --bus-master           # reg0 |= 0x80 after zero-filling 0x80008..0x80FFF (Windows order)
rack.load P2_AINIT.dsp  dsp 0            # + PINIT.ol: codec init
rack.load P2_ANO.dsp    dsp 1            # + P2_IO.ol: SPORT0, analog out slots 9/10
volL = rack.load LINVOL.dsp dsp 1 ; volR = rack.load LINVOL.dsp dsp 1
rack.dsp[1].link_input(volL, 0, 0xC300)  # signal input <- PC slot 0x180
rack.dsp[1].link_input(volR, 0, 0xC302)  # signal input <- PC slot 0x181
rack.set_in_pad(volL, 1, gain(-30 dB)) ; rack.set_in_pad(volR, 1, gain(-30 dB))
rack.connect(volL, 0, ano, 0) ; rack.connect(volR, 0, ano, 1)
plate cfg 0x404                          # un-mute (0x484 = muted)
reg1 = 0x11                              # --irq: block IRQ every 1024 frames (kernel ISR loaded first!)
ioctl(PULSAR_IOCTL_SET_ROUTE, {rate 44100, block 1024, play_slot {0x180, 0x181}, play_channels 2})
```
LINVOL keeps a software-controlled safety gain in front of the DAC (the PC stream is full scale by
definition); later it can be exported as an ALSA mixer control (SetValue of the volume pair).
The modules can live on any DSP (the window is broadcast), but DSP1 avoids an extra comm slot to P2_ANO.

Capture (later): `ani = rack.load P2_ANI.dsp dsp 1`; `sL, ops = rack.dsp[1].alloc_sync_output(ani, 0)`,
`sR, … (ani, 1)` **before** linking (no patch needed); route `cap_slot = {(sL−0xC000)/2, (sR−0xC000)/2}`
(0x52/0x53 if DSP1's block is untouched). Samples arrive as `FDEP(24-bit) << 8` = S32_LE MSB-justified.

---------------------------------------------------------------------------------------------------
## 6. Safe step-by-step test plan

General safety: monitors/headphones at minimum volume; every audio path goes through LINVOL at −30 dB or
lower; keep the plate muted (cfg 0x484) until a step says otherwise. The IOMMU (DMA-FQ, group 9) blocks
any card DMA outside mapped buffers; a stray access shows up as a DMAR fault in `dmesg`, not as memory
corruption — check `dmesg` after every step. `pulsar_test.sh plate --cfg 0x484` is the panic button
(mute), `rmmod snd-pulsar` after idling the slots is the second.

| # | step | expected / pass criterion |
|---|---|---|
| 0 | Offline: build the module, `pulsar_modules.py link … --ops` for the §5.6 graph, review ops (inputs 0xC300/0xC302 patched). | no unresolved symbols |
| 1 | Counter: after `boot`, sample BAR+0x10 every 5 ms for 1 s (hwdep mmap read only). | Δ ≈ 220 per 5 ms; wrap value (0x8000 or 0x10000) observed; wrap is a multiple of 0x1000 |
| 2 | IRQ: load snd-pulsar, `boot --irq` (reg1 = 0x11). Watch `PULSAR_IOCTL_GET_INFO.irq_count`. | ≈ 43.07 IRQ/s, status bit0 set, no "nobody cared"; BAR+0x10 & 0x3FF small at each IRQ (log a few in ISR) |
| 3 | Bus master with an empty table: `boot --bus-master` (zero-fill first, as Windows). | DSPs still answer GetValue, wclk 44.1k, **no DMAR faults** |
| 4 | Kernel-only DMA test, plate **muted**: test ioctl (debug build) allocates the 32 KB ring, fills slot 0x180 with a ramp/marker pattern (e.g. `i << 16`) and slot 0x181 with a constant, lays out the table and enables both entries. From userspace GetValue(DSP1, 0xC300/0xC301/0xC302). | DM 0xC302 = the constant; DM 0xC300 values change and match ring words near `BAR[0x10]&0xFFF` (+ prefetch offset ≈ 64..128 → record it as `delay`) |
| 5 | Load the §5.6 graph with the inputs on 0xC300/0xC302, LINVOL −40 dB, still muted; fill ring with a −20 dBFS sine of 93 cycles/4096 frames (1001.3 Hz, seamless loop). Then un-mute (0x404) at minimum monitor volume. | clean 1 kHz tone on outs 1/2; GetValue of LINVOL outputs shows the sine |
| 6 | ALSA: `enable_pcm=1`, route ioctl, `speaker-test -D plughw:Pulsar2 -c 2 -r 44100 -t sine -f 440` then `aplay -D plughw:Pulsar2` a test file. | tone/file plays, `/proc/asound/card*/pcm0p/sub0/status` hw_ptr advances 44100/s, no xruns at period 1024 |
| 7 | Stop/start/close loops (100 × `speaker-test -l 1`), rmmod while idle. | no DMAR faults, entries back to 0x800, no IRQ storm |
| 8 | Capture: P2_ANI → comm slots, route cap slots, `arecord -D plughw:Pulsar2 -c 2 -f S32_LE -r 44100` with a known analog input (or loop out 1/2 → in 1/2 at low level). | recorded signal matches; bit0 entries only for our slots |
| 9 | PipeWire/JACK: period 256 (reg1 0x31, route.block 256), JACK `-d alsa -d hw:Pulsar2 -p 256 -n 16`. | stable, no xruns |

Step 4 is the key experiment: it validates the slot→DM mapping, the entry format, the IOVA path and the
counter/ring alignment without any audible output.

---------------------------------------------------------------------------------------------------
## 7. Open points / risks

* [L] Header words 0x8400C300/…C301 = engine headers for the two double-buffer halves; count field = number
  of PC slots from 0x180. If the engine uses the count differently, step 4 shows which DM words change.
* [L] Capture via snooping of the DSP broadcast at `0xC000+2s` (bit0). Not yet observed.
* [?] Whether an all-zero table word at s = 0x180/0x181 (state after Run, before any stream) makes the
  engine read IOVA 0 when bus master is on (count = 2 by default). Windows has the same state, and the
  terminator at s = 0x10 probably stops the walk; step 3 checks for DMAR faults.
* [?] Exact prefetch latency (64 or 128 frames) and whether the IRQ fires exactly at `BAR[0x10] % blk == 0`.
* [?] Slot table words 2..0xF and the meaning of 0x80000 w0 bits 30/31 (0x84 = PCI master header).
* [L] Rate changes: the ring/period scheme is rate-independent; the kernel only needs `route.rate` for the
  ALSA constraint. Re-run SET_ROUTE after `--rate 48000`.
* Windows-only machinery that is **not** needed: wave/ASIO buffer caches, the wave-out watchdog,
  vxd host clients (0x1d2088…), GSIF/linked/tap slots, the 32 KB pool, heartbeat SetValue.
