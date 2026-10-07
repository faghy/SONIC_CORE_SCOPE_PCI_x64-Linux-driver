# Pulsar II: the rest of Run, the post-Run start, and sample rate / clock (Sim2k.dll)

Scope: the steps Windows SCOPE (Sim2k.dll) runs **after** the `GetValue(dspID)` loop in Run. The goal is to finish
bring-up with the DSP OS in the same state as under Windows and the word clock running at 44.1 or 48 kHz.
Every address is Sim2k (image base 0x10c00000). Vtable slots were resolved from the DLL (Pulsar2 board vt
0x10c87d74, DSP class **pluto** vt 0x10c8a634, PPlate vt 0x10c8209c). Call sites where Ghidra drops arguments
were checked in `sim2k.asm`.

Legend: **[C]** confirmed in code/asm, **[L]** likely, **[?]** guess.
`SetValue(d, sym, v)` = board vt+0x114 (FIFO frame `((d|0x10)<<21)|addr`, wrapped). `sysmsg(d, type, a, b)` =
board vt+0x104 `FUN_10c33410` (block {a, b, type} → `sysMsg+1`, then wait for the ack). `GetValue` = `sysmsg(8, addr, 0)`
plus a read of host SRAM 0x801+2d. `Sleep(ms)` = `FUN_10c2c3f0`, which actually sleeps `((ms+9)/10)*10+1` ms.

Live data used here (supplied by the coordinator; the board has run the current loader): DSP0 `backplateID` (DM 0xC508) = **2**,
`dspAckDest` = 0x63E00800+2n (computed by the OS), `cmdMask` = 0xC0000000, `FScale` = 0x40000000, `asRatio` = 0x10 (the
image defaults).

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **The rate path never writes a rate into the hardware.** `FUN_10c4eae0(host, rate)` only sets the DSP variables
   `FScale`/`asRatio` and re-syncs the clock. The hardware clock and rate live in the **backplate config word**, which is
   bit-banged through reg5. Windows writes that word in two places only:
   (a) in Run, through the backplate's default config (vt+0x10); (b) whenever the SCOPE "pulsar_cfg" device module or
   SimCall 0x1f calls board vt+0x290.
   **Our loader never writes it, which is the most likely reason the clock runs at a non-standard 11634 Hz.** [L]
2. `backplateID = 2` selects **PPlate** (`FUN_10c09d80(board, 2)`, vt 0x10c8209c) [C]. That plate has:
   **11 bits written** (vt+0 `FUN_10c094b0` = 0xB), **16 bits read** (vt+4 `FUN_10c09780` = 0x10), and its "set" function is
   `FUN_10c094c0`, which **always ORs in 0x400**. This is not the 0x2000 that `PulsarPlate`/`PulsarPlusPlate` use, so the
   older notes' "0x2084, 14 bits" is wrong for this card. [C]
3. Words to write (11 bits, LSB first) [C for the encoding, L for which word SCOPE's panel sends]:
   * boot default (Run, PPlate vt+0x10 `FUN_10c08de0`): **0x484** = 44.1 kHz internal, with one-shot bit 0x80;
   * 44.1 kHz internal: **0x404**; 48 kHz internal: **0x402**; 32 kHz: **0x406**; external clock: bit0 = 1 (**0x401**).
   Each write is wrapped in stopClk + "switch to DSP 1" before it and startClk after it, unless the clock is stopped or the
   rate is unchanged.
4. On pluto (Pulsar2), the DSP-side rate step is `FUN_10c3c290` [C]:
   `SetValue(FScale)`, then `SetValue(asRatio = clamp(rate/3125, 10, 30))`, then `sysmsg(0xB, 3, 0xC080+0x20·n)`
   (sys_syncmsghead, the same TCB setup that `os_initdma` already does).
   There is **no alastClk sysmsg 0xF on pluto**: `FUN_10c30640`/`FUN_10c222f0` run only for DSPs whose vt+0x170 is non-zero,
   and pluto's vt+0x170 = `FUN_10c37210`, which returns 0. `resetClk` instead does **SetValue `wclk`=1 / `alastClk`=0**.
5. These Run-tail steps are informational and **not needed for audio** [L]: the micro-controller queries 0x229/0x21f
   return the serial number ("Sn %08lx") and a uC info word. `vt+0x258` (`SetValue dspID/dspAckDest`) is skipped on
   pluto [C], which is why the OS computes `dspAckDest` itself.
6. Interrupts: master vt+0x1ec sets **reg1 = 0x11** (block 1024, IRQ enable). Only enable this once the kernel ISR acks
   (BAR+0x1C) [L].

---------------------------------------------------------------------------------------------------
## 1. Rest of Run (Pulsar2 vt+0x124 `FUN_10c32f80`) after the dspID loop

All three calls below run only when `DAT_10cab840 == 0` (normal operation) [C].

### 1.1 vt+0xf8 `FUN_10c32b00`: uC serial number (cmd 0x229) [C, asm 10c32b99..10c32c2b]
`vt+0x224` = `FUN_10c314c0` returns **5**, so DSP5 is the DSP that talks to the board micro-controller. The query runs
once (`board+0xac8 == -1`) and only if DSP5 has the symbols `ucCmdOut` (0xC57A) and `ucMagicDest` (0xC57B).
```
mbox = 0x800 + 1 + 2*5 = 0x80B                     // vt+0x208 FUN_10c2c9d0 returns 0x800
writeReg(0x20000 + 0x80B, 0xFFFFFFFF)              // BAR+0x8202C := -1  (FUN_10c215c0 adds 0x20000;
                                                   //  the -1 is the argument left on the stack from the vt+0x208 call)
dest = vt+0xa8(0xff, 0x80B, isPCI=0, len=1)        // = 0x41E00000|0x80B|1<<25|0x20000000 = 0x63E0080B
SetValue(5, ucMagicDest, 0x63E0080B)
SetValue(5, ucCmdOut,    0x229)
vt+0x270 FUN_10c355e0(11)                          // = Sleep(11) when no XTC master (FUN_10c2fd80()==0)
board+0xac8 = readReg(0x2080B)                     // serial; printed as ", Sn %08lx"
```
DSP side (`puls2os5`): the IRQ0 handler `ucSpr1Asserted` (0x82E3) shifts `ucCmdOut` out to the uC: bits 0..7 go on the
IOSTAT flag pins and bits 8..9 go on FLAG0/1 in ASTAT. 4 reply bytes collect in `ucDataIn`. `ucAsync` (main loop) then
sends the 32-bit `ucDataIn` to `ucMagicDest` (host SRAM 0x80B) and clears `ucMagicDest`. [C]

### 1.2 vt+0x220 `FUN_10c32c50`: uC info (cmd 0x21f) [C]
This is the same exchange with `0x21F`: `writeReg(0x2080B, -1)`, `SetValue(5, ucMagicDest, 0x63E0080B)`,
`SetValue(5, ucCmdOut, 0x21F)`, `Sleep(11)`, `board+0xacc = (readReg(0x2080B) >> 16) & 0xFFFF`.
`board+0xacc` is returned by vt+0xfc `FUN_10c2c9a0` and copied into the board-info struct (`FUN_10c...` @line 2770, next to
the numDSP and backplate fields).
**Required? No** [L]. Both values are only reported to the UI and possibly used for licensing (`magicProt`). They are
useful as a uC sanity check: a reply other than 0xFFFFFFFF means DSP5↔uC works.

### 1.3 vt+0x164 `FUN_10c31100` → vt+0x280 `FUN_10c303f0`: backplate detection [C]
```
if board+0x12e4 (backplate obj) == NULL:
    bp = GetValue(DSP0, sym "backplateID") & 0x3F       // DM 0xC508; sysmsg 8, b=0
    if cfg.audioConfig(-1) != -1 and != bp: warn "audioConfig %d of %s not as expected"
    bp == 0x3F ? EmptyBackPlate : FUN_10c0a050(board, bp)
    plate->vt+0x10()                                     // DEFAULT CONFIG -> HW write (section 3.3)
return plate+8 (=bp) | (cfg.hasSyncPlate(-1) != 0 ? 0x100 : 0)
```
The OS fills `backplateID` itself in `os_init` (puls2os0 PM 0x8102..0x810F): it reads IOP `IOSTAT`, takes bits 0..4, and
sets bit 5 from IOSTAT bit 6. The value comes from the plate's ID pins. [C]

**Backplate classes** (`FUN_10c0a050`, vtables at 0x10c82xxx) [C]. Every plate class has 6 slots:
+0 nbitsW, +4 nbitsR, +8 set, +0xc read, +0x10 default, +0x14 resetHavarie.

| id | class | vtable | nbits W / R | set (+8) | default (+0x10) |
|---|---|---|---|---|---|
| 0x3f | EmptyBackPlate (`FUN_10c08b80` base) | 0x10c81fd4 base | 14 / 16 | `FUN_10c08be0` (raw write) | `FUN_10c08bb0`: write 4 |
| other | NoBackPlate | 0x10c82010 | – | – | – |
| 1 | CPlate | 0x10c82080 | 14 / 16 | `FUN_10c09130` | `FUN_10c08bb0` (4) |
| **2**, 0x19 | **PPlate** | **0x10c8209c** | **11 / 16** | **`FUN_10c094c0`** (\|0x400) | **`FUN_10c08de0` (0x84)** |
| 3 | PPlatePlus | 0x10c820b8 | 11 / 16 | `FUN_10c094c0` | `FUN_10c08de0` |
| 4 | CPlate2 | 0x10c820d4 | | | |
| 5 | BeckerPlate | 0x10c820f0 | | | |
| 10, 0x3b | ZPlate | 0x10c8210c | | | |
| 0xb | ZPlatePlus | 0x10c82128 | | | |
| 0xc | GPlate | 0x10c82144 | | | |
| 0xe / 0xf | MadiPlate / MadiPlateOpt | 0x10c82160 / 0x10c8217c | | | |
| 0x10 / 0x11 / 0x13 | BraehlerEDAT / Combi / DAM | 0x10c82198 / b4 / d0 | | | |
| 0x16 / 0x17 | NameAudio / NameAudio2 | 0x10c821ec / 0x10c82208 | | | |
| 0x40 | PulsarPlate | 0x10c8202c | 14 / 16 | `FUN_10c08e10` (\|0x2000) | `FUN_10c08de0` (0x84) |
| 0x41 | PulsarPlusPlate | 0x10c82048 | 14 / 16 | `FUN_10c08e10` | `FUN_10c08de0` |
| 0x42 | ApolloPlate | 0x10c82064 | | | |
| 0x51 | NoahAudio | 0x10c82224 | | | |

**Our Pulsar II reports 2, so it uses PPlate (11-bit write word, always-on bit 0x400)** [C, from the live value].

---------------------------------------------------------------------------------------------------
## 2. After Run: master vt+0x1ec and FUN_10c446b0 (`FUN_10c4e0a0` tail)

### 2.1 master vt+0x1ec = `FUN_10c2cf50` [C]
```
only on the master board (index DAT_10cab954 = 0)
IOCTL 0x1d20c4 [-1, intBlkSize]      // vt+0x1dc FUN_10c220b0: kernel bookkeeping only
code = {0x400:1, 0x200:3, 0x100:0x21, 0x80:0x22, 0x40:0x23}[intBlkSize]     // default 0x400 -> 1
if cfg.enableInterrupt (default 1):
    maskSet(1, code | (board+0xac4 ? 0x10 : 0x18), 0x3F)   // board+0xac4 == 1 always -> reg1 = 0x11
```
For 0x40..0x100, vt+0x248 (`FUN_10c30a60` returns 1) confirms that the size is supported. Reg1 bit 0x10 is IRQ enable [L].
The low bits select the block size [C, from the table].

### 2.2 `FUN_10c446b0(host, 0xffff)` [C]
```
host+0x98 |= 1|2
for board: vt+4 = FUN_10c1a440 -> for each DSP: pluto vt+8 = FUN_10c3be00(0xffff):
      if !(dsp+0x14 & 1): SetValue(n, "_firstsync",  first sync module ? its entry : sym "ret_sync")
      if !(dsp+0x14 & 2): SetValue(n, "_firstasync", first async module ? its +0x84 : 0)
host+0x50 (class vdsp, vt 0x10c8d90c) vt+8 = FUN_10c156f0: only sets software flags, no HW.
```
With no modules loaded this writes `_firstsync = ret_sync` (0x8192 on DSP0..4, **0x8193 on DSP5**) and `_firstasync = 0`.
Those values equal the image defaults, so the step is a no-op on a freshly booted OS. Do it anyway for parity.
`dsp+0x14` is never set by `FUN_10c3be00` [L: it is 0 on pluto, so both SetValues are sent].
pluto's vt+4 is the no-op `FUN_10c40550`. **There is no sysmsg 0xB here.**

---------------------------------------------------------------------------------------------------
## 3. Sample rate / clock

### 3.1 Host init order (`FUN_10c4f0d0`, lines 510-541 of hostinit) [C]
```
FUN_10c4e0a0(host)                       // prepare, reset, load OS, Run (section 1), vt+0x1ec (2.1), FUN_10c446b0 (2.2)
if !cset.noInitChangeSampleRate (0):
    FUN_10c4eae0(host, DAT_10ca036c)     // DAT_10ca036c is statically 0xAC44 = 44100 (data section)
FUN_10c0c360(host)                       // modules / project
```
`simfreq` (cset, default 44100) applies only to the simulator. Defaults that matter here: `clkSrc=1` (no reg5 bit), `directExtClk=0`,
`audioConfig=-1`, `synchronizeBoards=1` (DAT_10cab8d8), `intBlkSize=0x400`, `enableInterrupt=1`, per DSP
`reservedSyncMsgs=0`, `minSyncAsyncRatio=10`, `maxSyncAsyncRatio=30` (per-DSP ctor `FUN_10c1dad0`). The shipped
`cset.ini` overrides none of these [C].

### 3.2 `FUN_10c4eae0(host, rate)` on its first call (host+0xf0 == 0, host+0xf4 == 0) [C]
```
(a) FUN_10c44770(host, 3)
      per board vt+8 FUN_10c1a480 -> pluto vt+0xc FUN_10c37e10: only if dsp+0x14 bits set -> [L] nothing
      host vdsp vt+0xc
    per board vt+0xc FUN_10c1ab30:
      per DSP pluto vt+0x10 FUN_10c3c230:
          sysmsg(n, 0xB, a = reservedSyncMsgs+3 = 3, b = dsp+0xec = 0xC080 + 0x20*n)    // sys_syncmsghead
          FUN_10c3bed0 (referenced libs: none)
      board+8 &= 4                    // sysmsg sending disabled until (b)
    FUN_10c4b710 / FUN_10c424c0 / FUN_10c36950   (host module graph) [?: assumed no HW]
(b) host+0xf0 = 1 ; per board vt+0x10 FUN_10c1ab60: board+8 |= ~4 (sysmsg on again)
    FUN_10c446b0(host, 3)             // _firstsync / _firstasync again (2.2)
(c) rate block (rate != 0):
    per board  vt+0x158 FUN_10c35970(rate)                      -> 3.4
    per DSP    pluto vt+0x6c FUN_10c3c290(rate)                 -> 3.5
    host vdsp vt+0x6c (FUN_10c15fe0: modules only); FUN_10c15470/FUN_10c54a90 (plug-in DLL IOCTL 0x1d2010, no HW)
    DAT_10ca036c = rate
    FUN_10c0c3e0(host): 1 board -> true -> FUN_10c44870(host)   -> 3.6 (clock resync)
    per board: GetValue(DSP0, pluto vt+0xe0 = dmstart = 0xC400 = "wclk")   // liveness read, value unused
    FUN_10c4ea60(host,1) (module graph) ; per board vt+0x160 FUN_10c22f80: IOCTL 0x1d207c (kernel only)
```
Later rate changes (`SimCall`/project) run only (c). The board+8 bits are 0x1f from the board ctor (line 28241) [L], so the
sysmsgs/GetValues in Run are really sent and acked.

### 3.3 The hardware word: PPlate set `FUN_10c094c0(cfg)` → board vt+0x284 `writeAudioCfg FUN_10c30100` [C]
```
PPlate.set(cfg):
    cfg = (cfg & 0x10000) ? cfg | 0x100 : cfg & ~0x100
    if !(cfg & 1) and FUN_10c2fd80()==0:                       // internal clock, no XTC master
        rate = {0x4000: 88200, 6: 32000, 2: 48000}.get(cfg & 0x4006, 44100)
        if rate == plate+0xc or !(shadow reg0 & 0x10):           // same rate or clock not running
            writeAudioCfg(cfg | 0x400)
        else:
            run = vt+0x40 FUN_10c20400 (= shadow reg0 bit 4)
            if run: vt+0x30 FUN_10c2cb90 (stopClk + realign, 3.6) ; Sleep(2)
            writeAudioCfg(cfg | 0x400)
            if run: Sleep(2) ; vt+0x38 startClk(0)
        plate+0xc = rate
    else:  writeAudioCfg(cfg | 0x400)                            // external: plain write
    plate+0x10 = (cfg | 0x400) & ~0x500 ; return it

PPlate.default (vt+0x10 FUN_10c08de0):  plate+0x10 = 4 ; set(0x84) ; plate+0x10 &= ~0x80
PPlate.resetHavarie (vt+0x14 FUN_10c08c10):  set(plate+0x10 | 0x40) ; set(plate+0x10 & ~0x40)

writeAudioCfg(v)   (reg5 = BAR+0x14; mask 0xD00: data 0x400, clock 0x800, latch 0x100)
    maskSet(5, 0, 0xD00)
    repeat nbitsW (= PPlate vt+0 = 11):
        d = (v & 1) << 10
        maskSet(5, d, 0xD00) ; maskSet(5, d|0x800, 0xD00) ; maskSet(5, d, 0xD00) ; v >>= 1
    maskSet(5, 0x100, 0xD00) ; maskSet(5, 0, 0xD00)           // latch
```
Bit meaning of the config word, as far as the code shows:

| bit | meaning | tag |
|---|---|---|
| 0x001 | 1 = external clock, 0 = internal | [C] |
| 0x006 | internal rate: 2 = 48k, 4 = 44.1k, 6 = 32k (0 is treated as 44.1k by PPlate, 96k by pulsar_cfg) | [C] |
| 0x008 | cleared together with bits 0/1 by directExtClk (`& ~0xb \| 0x84`) | [?] |
| 0x040 | havarie (clock-loss) reset, pulsed by resetHavarie | [C use, L meaning] |
| 0x080 | set only by the boot default, then dropped from the cache: a one-shot init bit | [L] |
| 0x100 | from caller flag 0x10000 | [C] |
| 0x400 | always set by PPlate (11-bit plate's "valid/strobe" bit) | [C] |
| 0x4000 | 88.2k: cannot be sent with 11 bits, so PPlate supports only 32 / 44.1 / 48 kHz | [C] |

With the reg5 base value 0x0004 (open: reg5=4; reset clears 0x1000; clkSrc=1 adds nothing), the register writes for
**0x484** are (LSB first, bits 0,0,1,0,0,0,0,1,0,0,1):
```
0x004 | 0:004 804 004 | 0:004 804 004 | 1:404 C04 404 | 0 | 0 | 0 | 0 | 1:404 C04 404 | 0 | 0 | 1:404 C04 404 | 104 004
```
Sequences for the other words: 44.1k = 0x404 (bits 0,0,1,0,0,0,0,0,0,0,1); 48k = 0x402 (0,1,0,0,0,0,0,0,0,0,1).

`directExtClk` (cset, default 0) or an XTC master go through board vt+0x290 `FUN_10c30310` instead: `bitSet(5, 0x1000)`, then
`cfg = cfg & ~0xb | 0x84` [C]. `clkSrc=0` → `bitSet(5,1)` and `clkSrc=3` → `bitSet(5,0x2000)`, both in Run's vt+0x218 [C].

**When it is written** [C]:
1. Run → vt+0x164 → `FUN_10c303f0` → PPlate default → **set(0x84)**. The clock is already running (startClk was done in
   vt+0x218), so the sequence is: stopClk+realign, Sleep(2), write **0x484**, Sleep(2), startClk(0).
   Cached cfg ends up = 0x04, plate rate = 44100.
2. pulsar_cfg module `FUN_10c60720` (`FUN_10c604e0`) and SimCall case 0x1f (`FUN_10c0750e`) → board vt+0x290
   `FUN_10c30310(cfg)` → PPlate.set(cfg). The cfg word comes from the SCOPE device's sample-rate/clock panel (its pad 0).
   The rate the module reports back is derived from cfg (internal) or from the status (external), see 3.7.
   **`FUN_10c4eae0` itself never writes cfg.**
3. `FUN_10c35d00` (XTC sync) temporarily writes `cfg & ~0xc | 2` (not used on a single Pulsar2).

### 3.4 Per board vt+0x158 `FUN_10c35970(rate)` [C]
```
vt+0x28c FUN_10c30520 (resetHavarie check):
    setReg(9, 0)
    st = plate vt+0xc FUN_10c09c70 -> readAudioCfg()       (| 0x100 if host+0xf4 "restart pending")
    if st & 0x100:                                          // clock lost
        Sleep(5) ; vt+0x30 FUN_10c2cb90 (stopClk+realign)
        plate.resetHavarie()                                // set(c|0x40); set(c&~0x40)  -> writes (c|0x440), (c|0x400)
        Sleep(5) ; vt+0x38 startClk(0)
FUN_10c22f50: IOCTL 0x1d2074 [h, rate]                     // kernel bookkeeping only, no HW (scScope_sys.md)
              FUN_10c5a350 = no-op
if FUN_10c2fd80() (XTC master)…: FUN_10c35690 (XTC, n/a)
vt+0x25c FUN_10c2f190(rate): asserts that the clock is running (vt+0x40); per DSP vt+0x16c -> pluto FUN_10c37210 = 0 -> nothing
```

### 3.5 Per DSP pluto vt+0x6c `FUN_10c3c290(rate)` [C]
```
FUN_10c3bf10(dsp, rate):
    FUN_10c3a640: host-side cycle budgets; dsp+0x34 = asRatio = max(min(rate/3125, maxSAR=30), minSAR=10)
    SetValue(n, "FScale", {48000:0x40000000, 44100:0x3ACCCCCC, 32000:0x2AAAAAAA,
                           88200:0x75999999, 96000:0x80000000}.get(rate, round(rate*2^32/192000)))
    SetValue(n, "asRatio", dsp+0x34)            // 44.1k -> 14 (0xE), 48k -> 15 (0xF), 32k -> 10
    FUN_10c15fe0: per loaded module vt+0x104(rate)   (none at boot)
pluto vt+0x170 == 0 ->
    dsp+0xe8 = reservedSyncMsgs + 3 = 3 ; dsp+0xec = vt+0xf8 (0xC080) + 0x20*n
    sysmsg(n, 0xB, 3, 0xC080 + 0x20*n)          // sys_syncmsghead -> updateTCBCounter(R5=3, R0=0xC080+0x20n), ack
    for i < reservedSyncMsgs (0): SetValue(ec+4+2i, 0), SetValue(ec+5+2i, 0)   -> none
```
`FScale` is a fraction of 96 kHz (1.31) [L]. `asRatio` = word-clock ticks per async-loop slice (`waitloop` in `sloop`) [C].

### 3.6 Clock resync: vt+0x30 `FUN_10c2cb90`, `FUN_10c44870`, `resetClk` [C]
```
stopClk  FUN_10c22140:  bitSet(2,4); Sleep(2); bitClr(2,2); bitSet(2,2); bitClr(0,0x10); Sleep(2); bitClr(2,4)
startClk FUN_10c22380(fast):  if !fast {bitSet(2,2); Sleep(2)}; bitSet(2,4); bitSet(0,0x10);
                              if !fast Sleep(2); bitClr(2,4); if !fast Sleep(2)
vt+0x30 FUN_10c2cb90:   stopClk
                        if board+8 & ~4:  Sleep(2); wait ≤1 s for reg0 & 0x20
                            repeat ≤ numDSP+1: if (reg0 & 0xF) == 1 break; bitClr(2,2); bitSet(2,2); Sleep(2)
                            else error "cannot switch to DSP 1, word clock or communication stalled"
resetClk FUN_10c22230 (vt+0x34 FUN_10c2ccc0):
                        bitSet(0,2); bitClr(0,2); bitSet(2,2); IOCTL 0x1d20cc (kernel wclk reset)
                        per DSP vt+0x1e0 FUN_10c2ce90 -> (pluto) FUN_10c222f0:
                            SetValue(n, "wclk", 1) ; SetValue(n, "alastClk", 0)
                        board+0xad4 = 0x7FFFFFFF
FUN_10c44870 (synchronizeBoards=1):
                        per board: vt+0x30 ; vt+0x34      Sleep(2)
                        per board: vt+0x38 startClk(fast=1)  Sleep(2)
                        (multi-board compare skipped for 1 board); per board vt+0x44 FUN_10c30640 -> pluto: nothing
```

### 3.7 readAudioCfg `FUN_10c301d0` and status bits [C]
```
for v in 0x800,0,0x100,0x900,0x100,0: maskSet(5, v, 0xD00)         // parallel load
st = 0
9x:  st = (st<<1) | ((readReg(5) >> 6) & 1) ; maskSet(5,0x800,0xD00) ; maskSet(5,0,0xD00)   // bits 8..0, MSB first
for i in 9 .. nbitsR-1 (PPlate: 16):  st |= ((readReg(5)>>6)&1) << i ; clock pulse
if host+0xf4: st |= 0x100
```
Status bits (from `FUN_10c60720` pulsar_cfg and `FUN_10c30520`):
* **0x100 = clock lost / "havarie"** → recovery in 3.4 [C].
* **0x004 = rate valid / locked**: pulsar_cfg only derives a rate when it is set [C use, L meaning].
* **st & 0x1003 = detected external rate**: 0 = 96k, 0x1000 = 88.2k, 1 = 48k, 2 = 44.1k, 3 = 32k [C]. PulsarPlate's read
  masks 0x1003 away when internal, so these bits describe the external input [L].

---------------------------------------------------------------------------------------------------
## 4. The complete Windows sequence after `GetValue(dspID)` (single Pulsar2, defaults, no modules)

```
# --- Run tail (FUN_10c32f80) ---
BAR[0x8202C] = 0xFFFFFFFF ; SetValue(5,0xC57B,0x63E0080B) ; SetValue(5,0xC57A,0x229) ; Sleep(11) ; sn  = BAR[0x8202C]
BAR[0x8202C] = 0xFFFFFFFF ; SetValue(5,0xC57B,0x63E0080B) ; SetValue(5,0xC57A,0x21F) ; Sleep(11) ; inf = BAR[0x8202C]>>16
bp = GetValue(0, 0xC508) & 0x3F                         # = 2 -> PPlate
stopClk ; Sleep(2) ; wait reg0&0x20 ; realign until (reg0&0xF)==1    # FUN_10c2cb90
Sleep(2) ; writeAudioCfg(0x484, 11 bits) ; Sleep(2) ; startClk(0)
# --- FUN_10c4e0a0 tail ---
maskSet(1, 0x11, 0x3F)                                  # IRQ every 1024 samples (only with a working ISR)
for n: SetValue(n, 0xC406 _firstsync, ret_sync[n]) ; SetValue(n, 0xC405 _firstasync, 0)
# --- FUN_10c4eae0(host, 44100), first call ---
for n: sysmsg(n, 0xB, 3, 0xC080+0x20n)                  # FUN_10c3c230
for n: SetValue(n, _firstsync, ret_sync[n]) ; SetValue(n, _firstasync, 0)
setReg(9, 0) ; st = readAudioCfg() ; if st & 0x100: resetHavarie dance (3.4)
for n: SetValue(n, 0xC401 FScale, 0x3ACCCCCC) ; SetValue(n, 0xC410 asRatio, 14) ; sysmsg(n, 0xB, 3, 0xC080+0x20n)
stopClk+realign ; reg0|=2 ; reg0&=~2 ; reg2|=2 ; for n: SetValue(n,0xC400 wclk,1), SetValue(n,0xC40F alastClk,0)
Sleep(2) ; startClk(fast) ; Sleep(2)
GetValue(0, 0xC400)                                     # wclk is advancing
```
For 48 kHz (later, e.g. from the panel): PPlate.set(0x02) → stop/realign, write **0x402**, startClk; then the rate block
with FScale 0x40000000 and asRatio 15.

### Why 11634 Hz now [L/?]
Our loader starts the clock but never writes the plate config (Windows writes 0x484 in Run). The plate logic therefore runs
with its power-up/undefined divider. The first thing to try is the Run-tail write of 0x484. After it, `diag` should show
~44100 Hz at BAR+0x10. If it still does not, check that `readAudioCfg()` bit 0x100 (havarie) is clear and do the
resetHavarie sequence. As a second variable, try 0x404/0x402 without the 0x80 bit.
Alternative hypothesis [?]: BAR+0x10 advances per completed DSP ring cycle and the ring (MSGR5 token, `os_sync`
`wait_msg`) is slower than the clock. The realign step (`(reg0&0xF)==1`) and resetClk (wclk=1/alastClk=0) address that.

---------------------------------------------------------------------------------------------------
## 5. Proposed loader patch (not applied)

Assumptions: `Board` gets `self.syms` (the per-DSP dict `cmd_boot` already builds), and `run()` stays as it is. All
helpers below use only `set_reg/bit_set/bit_clr/mask_set/set_value/get_value/sysmsg/reg_rd/bar`.

```python
# ---- constants (add near the top)
UC_DSP = 5                                 # Pulsar2 vt+0x224 FUN_10c314c0
UC_MBOX = 0x800 + 1 + 2 * UC_DSP           # host SRAM dword 0x80B (BAR+0x8202C)
UC_DEST = 0x63E00000 | UC_MBOX             # vt+0xa8(0xff, 0x80B, 0, len=1)
PPLATE_ID = 2                              # backplateID -> PPlate (FUN_10c09d80)
PPLATE_NBITS_W, PPLATE_NBITS_R = 11, 16    # PPlate vt+0 / vt+4
COMM_BASE = 0xC080                         # pluto vt+0xf8; TCB block = COMM_BASE + 0x20*dsp
RATE_BITS = {48000: 0x2, 44100: 0x4, 32000: 0x6}
FSCALE = {48000: 0x40000000, 44100: 0x3ACCCCCC, 32000: 0x2AAAAAAA,
          88200: 0x75999999, 96000: 0x80000000}


class ClockMixin:
    """Methods to add to Board. Requires self.syms[dsp] (symbol dicts from load_kernel)."""

    # ---- clock primitives (FUN_10c22380 / FUN_10c22140 / FUN_10c2cb90 / FUN_10c22230)
    def start_clk(self, fast=False):
        if not fast:
            self.bit_set(2, 2); self.sleep(2)
        self.bit_set(2, 4)
        self.bit_set(0, 0x10)
        if not fast:
            self.sleep(2)
        self.bit_clr(2, 4)
        if not fast:
            self.sleep(2)

    def stop_clk(self):
        self.bit_set(2, 4); self.sleep(2)
        self.bit_clr(2, 2)
        self.bit_set(2, 2)
        self.bit_clr(0, 0x10); self.sleep(2)
        self.bit_clr(2, 4)

    def stop_clk_sync(self):
        """vt+0x30: stopClk, then re-align the DSP ring pointer to DSP 1."""
        self.stop_clk()
        if self.dry_run:
            return True
        self.sleep(2)
        t = time.monotonic() + 1.0
        while not (self.reg_rd(0) & 0x20) and time.monotonic() < t:
            self.sleep(2)
        for _ in range(NUM_DSP + 1):
            if (self.reg_rd(0) & 0xF) == 1:
                return True
            self.bit_clr(2, 2); self.bit_set(2, 2); self.sleep(2)
        print("warning: cannot switch to DSP 1, word clock or communication stalled "
              "(reg0=0x%08x)" % self.reg_rd(0))
        return False

    def reset_clk(self):
        self.bit_set(0, 2); self.bit_clr(0, 2)
        self.bit_set(2, 2)
        for d in range(NUM_DSP):                       # FUN_10c222f0 (pluto)
            self.set_value(d, self.syms[d]["wclk"], 1)
            self.set_value(d, self.syms[d]["alastClk"], 0)

    def resync_clock(self):
        """FUN_10c44870 (synchronizeBoards=1), single board."""
        self.stop_clk_sync()
        self.reset_clk()
        self.sleep(2)
        self.start_clk(fast=True)
        self.sleep(2)

    # ---- backplate serial config (FUN_10c30100 / FUN_10c301d0)
    def write_audio_cfg(self, cfg, nbits=PPLATE_NBITS_W):
        if self.verbose:
            print("HW: writeAudioCfg (0x%x)" % cfg)
        self.mask_set(5, 0, 0xD00)
        for _ in range(nbits):
            d = (cfg & 1) << 10
            self.mask_set(5, d, 0xD00)
            self.mask_set(5, d | 0x800, 0xD00)
            self.mask_set(5, d, 0xD00)
            cfg >>= 1
        self.mask_set(5, 0x100, 0xD00)
        self.mask_set(5, 0, 0xD00)

    def read_audio_cfg(self, nbits=PPLATE_NBITS_R):
        for v in (0x800, 0, 0x100, 0x900, 0x100, 0):
            self.mask_set(5, v, 0xD00)
        st = 0
        for i in range(nbits):
            bit = (self.reg_rd(5) >> 6) & 1
            st = (st << 1) | bit if i < 9 else st | (bit << i)
            self.mask_set(5, 0x800, 0xD00)
            self.mask_set(5, 0, 0xD00)
        return st

    # ---- PPlate vt+8 FUN_10c094c0
    def plate_set(self, cfg):
        cfg = (cfg | 0x100) if cfg & 0x10000 else (cfg & ~0x100)
        w = (cfg | 0x400) & 0xFFFF
        if cfg & 1:
            self.write_audio_cfg(w)
        else:
            rate = {0x4000: 88200, 6: 32000, 2: 48000}.get(cfg & 0x4006, 44100)
            running = bool(self.shadow[0] & 0x10)
            if rate == getattr(self, "plate_rate", -1) or not running:
                self.write_audio_cfg(w)
            else:
                self.stop_clk_sync(); self.sleep(2)
                self.write_audio_cfg(w)
                self.sleep(2); self.start_clk()
            self.plate_rate = rate
        self.plate_cfg = w & ~0x500
        return self.plate_cfg

    def plate_reset_havarie(self):                     # PPlate vt+0x14 FUN_10c08c10
        self.plate_set(self.plate_cfg | 0x40)
        self.plate_set(self.plate_cfg & ~0x40)

    # ---- uC query through DSP5 (FUN_10c32b00 / FUN_10c32c50)
    def uc_query(self, cmd):
        s = self.syms[UC_DSP]
        if "ucCmdOut" not in s or "ucMagicDest" not in s:
            return None
        self.bar.wr(SRAM + UC_MBOX, 0xFFFFFFFF)
        self.set_value(UC_DSP, s["ucMagicDest"], UC_DEST)
        self.set_value(UC_DSP, s["ucCmdOut"], cmd)
        self.sleep(11)
        return self.bar.rd(SRAM + UC_MBOX)

    def tcb_init(self):                                # pluto vt+0x10 / tail of vt+0x6c
        for d in range(NUM_DSP):
            if not self.sysmsg(d, 0xB, 3, COMM_BASE + 0x20 * d):
                raise LoaderError("timeout waiting for acknowledge from dsp %d (sysmsg 0xB)" % d)

    def chains_init(self):                             # FUN_10c446b0 -> pluto vt+8 FUN_10c3be00
        for d in range(NUM_DSP):
            self.set_value(d, self.syms[d]["_firstsync"], self.syms[d]["ret_sync"])
            self.set_value(d, self.syms[d]["_firstasync"], 0)

    # ---- Run tail + start (FUN_10c32f80 tail, FUN_10c2cf50, FUN_10c446b0, first FUN_10c4eae0)
    def finish_run(self, irq=False, rate=44100):
        sn = self.uc_query(0x229)                      # vt+0xf8
        inf = self.uc_query(0x21F)                     # vt+0x220
        print("  uC: serial=%s info=%s" % (
            "n/a" if sn is None else "0x%08x" % sn,
            "n/a" if inf is None else "0x%04x" % ((inf >> 16) & 0xFFFF)))
        bp = self.get_value(0, self.syms[0]["backplateID"]) & 0x3F   # vt+0x164 -> vt+0x280
        print("  backplateID = 0x%02x%s" % (bp, "" if bp == PPLATE_ID else "  (NOT PPlate, cfg may be wrong)"))
        self.plate_rate, self.plate_cfg = -1, 4        # PPlate vt+0x10 FUN_10c08de0
        self.plate_set(0x84)                           # -> stop/realign, write 0x484, startClk
        self.plate_cfg &= ~0x80
        if irq:                                        # master vt+0x1ec FUN_10c2cf50 (intBlkSize 0x400)
            self.mask_set(1, 0x11, 0x3F)
        self.chains_init()                             # FUN_10c446b0
        self.tcb_init()                                # FUN_10c4eae0 first call, FUN_10c3c230
        self.chains_init()
        self.set_rate(rate, write_plate=False)         # FUN_10c4eae0 rate block with DAT_10ca036c = 44100

    # ---- sample rate / clock source
    def set_rate(self, rate, clock="internal", write_plate=True):
        if write_plate:                                # pulsar_cfg -> vt+0x290 -> PPlate.set
            if clock == "internal":
                if rate not in RATE_BITS:
                    raise LoaderError("PPlate supports only %s Hz internal" % sorted(RATE_BITS))
                self.plate_set(RATE_BITS[rate])
            elif clock == "external":
                self.plate_set(0x1)                    # [?] ext source select beyond bit0 unknown
            else:
                raise LoaderError("clock must be 'internal' or 'external'")
        # FUN_10c35970 -> FUN_10c30520
        self.set_reg(9, 0)
        st = self.read_audio_cfg()
        if self.verbose:
            print("HW: readAudioCfg () = 0x%x" % st)
        if st & 0x100:
            self.sleep(5); self.stop_clk_sync()
            self.plate_reset_havarie()
            self.sleep(5); self.start_clk()
        # pluto vt+0x6c FUN_10c3c290 per DSP
        fs = FSCALE.get(rate, int(round(rate * 2 ** 32 / 192000.0)) & 0xFFFFFFFF)
        ratio = max(10, min(30, rate // 3125))
        for d in range(NUM_DSP):
            self.set_value(d, self.syms[d]["FScale"], fs)
            self.set_value(d, self.syms[d]["asRatio"], ratio)
            if not self.sysmsg(d, 0xB, 3, COMM_BASE + 0x20 * d):
                raise LoaderError("timeout waiting for acknowledge from dsp %d (sysmsg 0xB)" % d)
        self.resync_clock()                            # FUN_10c44870
        w = self.get_value(0, self.syms[0]["wclk"])
        st = self.read_audio_cfg()
        print("  rate %d (%s): wclk=0x%x status=0x%04x%s%s" % (
            rate, clock, w, st, "" if st & 4 else " (no lock bit 0x4)",
            " HAVARIE(0x100)" if st & 0x100 else ""))
        return st
```

Wiring in `cmd_boot` (after the existing `[5/5]` loop, only if every DSP answered):
```python
    b.syms = syms
    if ok and not args.no_finish:
        print("[6/6] finish run + sample rate %d (%s)" % (args.rate, args.clock))
        b.finish_run(irq=args.irq, rate=44100)
        if args.rate != 44100 or args.clock != "internal":
            b.set_rate(args.rate, args.clock)
# argparse:
    ap.add_argument("--rate", type=int, default=44100, choices=(32000, 44100, 48000))
    ap.add_argument("--clock", default="internal", choices=("internal", "external"))
    ap.add_argument("--irq", action="store_true", help="reg1 = 0x11 (block IRQ); needs the snd-pulsar ISR")
    ap.add_argument("--no-finish", action="store_true")
```
Also add `self.syms = {}` in `Board.__init__`, mix `ClockMixin` into `Board`, and teach `SimBar` nothing: `stop_clk_sync`
skips the reg0 waits under `--dry-run`. `read_audio_cfg` reads the reg5 shadow there, so its status is meaningless in a dry
run.

Verification on hardware, step by step: `boot -v` → expect `backplateID = 0x02` and `writeAudioCfg (0x484)`. Then
`diag` → sample counter ≈ 44100 Hz. Then `boot --rate 48000` → ≈ 48000 Hz, FScale 0x40000000, asRatio 0xF
(GetValue 0xC410).

---------------------------------------------------------------------------------------------------
## 6. Corrections to boot_sequence.md
* §2.4 / §3.5: `vt+0x258 FUN_10c2d030` (SetValue dspID/dspAckDest) is **skipped for pluto** (it runs only when vt+0x170≠0) [C].
* §3.7: the per-DSP rate step is pluto `FUN_10c3c290` (FScale, asRatio, then **sysmsg 0xB**), not sharc `FUN_10c3bf10`
  alone. The audio word is **11 bits with 0x400** (PPlate), not 14 bits with 0x2000 (PulsarPlate). The sysmsg 0xF
  `alastClk` path does not apply to pluto; `resetClk` uses SetValue `wclk`=1 / `alastClk`=0.
* §3.5 Run: reg1 is written with `board+0xac4 ? 0x10 : 0x18`, which is not the PCI-master flag. `board+0xac4` is always 1.
