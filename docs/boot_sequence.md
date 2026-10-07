> **CORRECTION (verified on hardware 2026-10-07):** the Pulsar2 DSP class is `pluto` (FUN_10c3c100), not `sharc`.
> DM layout is 0xC400/0x1C00, so Run's stack word goes to **0xDFFF** (the boot-release flag), and sysmsg goes to
> OS symbol `sysMsg`+1 (= 0xC41A) as **[a, b, type]**. Sections below that say 0x27FFF / 0x24201 / {type,a,b} are wrong.
> See dsp_boot_analysis.md.

# Pulsar II bring-up as done by SCOPE userspace (Sim2k.dll) — reverse-engineering notes

Source: Ghidra decompilation `/home/faghy/puksar2/decompiled/Sim2k.dll.c` (32-bit, image base
0x10c00000 as Ghidra loaded it), cross-checked with `objdump -d` of
`scope_full/app/App/Bin/Sim2k.dll` where Ghidra dropped varargs, and with
`decompiled/scScope.sys.c` for the kernel side of each IOCTL.
Kernel-side details: see `re_notes/scScope_sys.md` (it agrees with everything here; where it is more precise about
the kernel, it takes precedence). DSP file format and decoder: `re_notes/sc_format.md`,
`tools/sc_decode.py`.

Legend: **[C]** = read directly from the code (decompile and/or disassembly), **[L]** = likely, an inference
supported by the code, **[?]** = uncertain or a guess.
`FUN_x` = Ghidra function at address x. "vt+0xNN" = virtual slot of the C++ object.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **Sim2k performs almost every hardware access itself, from user mode.** IOCTL `0x1d2048` maps the entire
   4 MB BAR0 into the process (`MmMapLockedPages(UserMode)`), and the result is stored at `board+0x9c`. All
   register writes and all SRAM reads/writes are plain loads and stores through that pointer:
   `ScopeBoard::readReg(i)` = vt+0x120 `FUN_10c20410` → `*(u32*)(bar + i*4)`, and
   `ScopeBoard::writeReg(i,v)` = vt+0x1c4 `FUN_10c20420`. [C]
   Only DSP *messages* go through the kernel (IOCTL `0x1d20a4`). The kernel copies them into the 1024-dword
   command FIFO at `BAR+0x81000` and publishes the write index to `BAR+0x0C`.
2. Register index `i` is a **dword index**. Index 0..9 is `BAR+0x00..0x24` (control/status). Index
   `0x20000 + k` is `BAR + 0x80000 + 4k`, the card SRAM: slot/master table at k=0..0x3ff, command FIFO at
   k=0x400..0x7ff, DSP→host reply mailboxes at k=0x800+2·dsp. [C]
3. Card type: `ScopeRev = ((BAR[0] >> 8) & 0x1f) - 1`. Our card reads `0x4600`, so ScopeRev = 5 = **Pulsar2**,
   and the board's own check `id==6` passes. The board has **6 SHARCs**, ids 0..5. **DSP *n* boots
   `puls2os<n>.21k`.** [C]
4. **Pulsar2 does not use the SPI path.** `enableSPI` defaults to 1 only for ScopeRev 0xd/0x15 (Scope XTC/Xite).
   The `(t<<26)|(len<<22)|0xA0000|addr` header in CLAUDE.md belongs to the Xite SPI ring. On Pulsar2, every
   DSP access is a *FIFO frame* whose header is
   `0x20000000 | len<<25 | dsp<<21 | flags | addr`. [C]
5. Boot path, in order (§3):
   - open/init the registers;
   - reset, with an SRAM test and two reset pulses;
   - broadcast the 256-instruction SHARC boot stream, byte by byte, through the FIFO (header `0x8a000000|b5`);
   - load the rest of each `puls2os<n>.21k` by programming each DSP's external-port DMA over the bus (IOP
     writes) and streaming frames;
   - Run: set reg bits, start the word clock, write `dspID` and `dspAckDest` into each OS;
   - load modules (`P2_IO.ol`, `P2_AINIT.dsp`, `PINIT.ol`, …) as ordinary relocatable modules chained into the
     OS sync list;
   - set the sample rate (`FScale` and `asRatio` per DSP, plus the 14-bit audio-config word bit-banged
     through reg5).
6. `PINIT.ol`, `P2_AINIT.dsp` and the other init modules are **not referenced by name in Sim2k**. They are
   instantiated through the exported `ModuleInit`/`ModuleInitEx` (@10c06fc0/@10c071d0), driven by the
   project/device files that Scope.exe/base.dll load. They are ordinary relocatable SC/COFF modules.

---------------------------------------------------------------------------------------------------
## 1. Driver handle, IOCTL wrappers, every IOCTL used

### 1.1 Opening the device
* `FUN_10c2be50` @10c2be50 [C] tries, according to the bitmask `DAT_10cab83c` (cset.ini `[hw] selectDriver`;
  the shipped `cset.ini` has `SelectDriver=1`):
  bit0 `\\.\scScope`, bit1 `\\.\scScopeXite`, bit2 `\\.\cwscope`.
  Each attempt is `CreateFileA(name, GENERIC_READ|GENERIC_WRITE (0xC0000000), share 3, OPEN_EXISTING, FILE_FLAG_OVERLAPPED)`.
  The handle is cached in the global `DAT_10ca0f28` (with "already tried" flag `DAT_10ca0f2c`). On failure the
  message is "scope driver not open and cannot open it!".
* `FUN_10c2bee0`/`FUN_10c2c0c0` install and start a kernel service (SCM). They are only used for the optional
  VxD/driver files, not for scScope. [C]
* Plug-in DLLs: `FUN_10c14930` resolves `EFFVXD_W32_DeviceIOControl` in a DLL. Codes `0x1d2000..0x1d2024` go
  there via an object's vt+4 or vt+8 (call sites 10c0bxxx/10c14xxx, lines 16701, 17020, 65215, 66087–66265
  of the decompile). scScope.sys **does not handle codes below 0x1d2028**; they fall into its default branch.
  These are irrelevant for hardware. [C]
* `CreateFileA` at line 63632 together with codes `0x222xxx` (`FUN_10c4ff20`…) belongs to a USB device class
  and is not used for Pulsar2. [L]

### 1.2 Wrappers
| wrapper | what it does |
|---|---|
| `FUN_10c2c270(h, code, in, inLen, out, outLen, &ret)` @10c2c270 | Raw `DeviceIoControl` with a zeroed `OVERLAPPED` (hEvent=NULL). The kernel completes synchronously. [C] |
| `FUN_10c2c2c0` @10c2c2c0 | Same, but takes `*h`. [C] |
| **`ScopeDriverIoctl(board, code, nIn, nOut, args...)` = `FUN_10c21780`** @10c21780 | The main wrapper (Ghidra lost the varargs; checked in asm). `in` buffer at `board+0xa0` = `{ board->hKernel (=board+0x98), arg0..arg[nIn-1] }`, `inLen = 4*(nIn+1)`. `out` buffer at `board+0xc8` = `{ status (pre-set to -99), out0..out[nOut-1] }`, `outLen = 4*(nOut+1)`. bytesReturned goes to `board+0xf0`. Returns `status==0`. Error print "ScopeDriverIoctl (%d) returned error code %d" unless `board+0x118`. [C] |
| `FUN_10c36160(&hDev, code, nIn, nOut, args...)` → `FUN_10c360a0` @10c360a0 | Same layout, but `in[0]` = handle of the *current master board* (`host->boards[DAT_10cab954]->+0x98`, or -1). Staging at `obj+8` (in) and `obj+0x30` (out). Used by the module/stream code. [C] |

Status codes written by the kernel into `out[0]`: 0 ok, -1 no such card, -2 busy/in use, -3 card not opened,
-5 (0xfffffffb) bad buffer size or bad parameter, -10 unsupported. [C] (`FUN_180009120` in scScope.sys)

`hKernel` is the value returned in `out[1]` by `0x1d2028`, i.e. `ctx->+0` of the kernel card. `FUN_1800090a0`
looks it up; -1 means "global pseudo-card".

### 1.3 Every IOCTL used by Sim2k (device type 0x1D, METHOD_BUFFERED, `code = 0x1d2000 + 4*n`)
`in[]` and `out[]` are dword arrays. `in[0]` is always hKernel unless noted.

| code | Sim2k call site(s) | in | out | kernel meaning (scScope.sys `FUN_1800099d0` unless noted) |
|---|---|---|---|---|
| 0x1d2028 OPEN | `FUN_10c21880` @10c21880 (probe), `FUN_10c21a50` @10c21a50 (open) | `[cardIndex]` (cset `boardID`, default = board#) | `[st, hKernel, -, -]` (0x10 B) | Opens the card and refcounts it; st=-2 if already open. [C] |
| 0x1d202c CLOSE | `FUN_10c21880`, `FUN_10c22950` area (line 27472) | `[h]` | `[st, refcnt]` | Decrements refcount; on the last close the kernel resets the card (see scScope_sys.md §4). [C] |
| 0x1d2030 VERSION | `FUN_10c21a50`, SimCall | `[h]` | `[st, 0x50000, 0]` | Sim2k requires `0x50000`, otherwise "Program and driver do not match". [C] |
| 0x1d2034 PEEK | `FUN_10c21880` | `[h, byteOfs]` | `[st, value]` | `value = *(u32*)(BAR+byteOfs)`. [C] |
| 0x1d2038 POKE | `FUN_10c21880` (only for ScopeRev 4) | `[h, byteOfs, value]` | `[st]` | `*(u32*)(BAR+ofs) = value`. [C] |
| **0x1d2048 MAPBAR** | `FUN_10c21a50` | `[h]` | `[st, userVA]` | MDL over the 4 MB BAR, `MmMapLockedPages(UserMode)`. Sim2k stores the VA at `board+0x9c`. [C] |
| 0x1d2050 CLR_CALLBACK | IRQ thread exit `FUN_10c23330`, line 27415/27467 | `[h]` | `[st]` | Clears ctx+0x30/+0x38. [C] |
| 0x1d2058 SET_AUDIO_BUF | `FUN_10c21ec0` @10c21ec0 | `[h, bufIdx, bufDescPtr]` | `[st]` | "scScope: Setting buffer %d" (`FUN_180015260`): locks user pages and fills the slot tables. [C] |
| 0x1d205c REL_AUDIO_BUF | `FUN_10c21fd0` @10c21fd0 | `[h, bufIdx, bufDescPtr]` | `[st]` | `FUN_180016320`. [C] |
| 0x1d2068 ALLOC_SHARED | `FUN_10c53d90` (pc2vxd_module ctor) | 6 args | `[st, va, phys?]` | Non-paged allocation shared with the module. [L] |
| 0x1d2074 SET_RATE | `FUN_10c22f30`/`FUN_10c22f50` (board vt+0x15c/+0x158 path) | `[h, rate]` (0 = mark only) | `[st]` | Stores the rate for timing (`FUN_1800191d0`); 12000..96000 accepted. **No HW write.** [C] |
| 0x1d207c | `FUN_10c22f80` | `[h, 0]` | `[st]` | Sets ctx+0x64 bit 4 and signals. [C] |
| 0x1d2080 SET_MASTER | `FUN_10c23670` @10c23670 | `[h, hMasterBoard]` | `[st]` | Clock-master linking (ctx+0x88). [C] |
| 0x1d2084 MOVE_AUDIO_BUF | `FUN_10c22e20` | `[h, from, hOther, to]` | `[st]` | `FUN_180015f70` (copies 0x80 dwords of PIO buffer). [C] |
| 0x1d2088 / 0x1d208c | `FUN_10c532b0`…, `FUN_10c53ad0` (vxd_module) | `[h, ptr]` | `[st]` | Create/destroy a kernel stream client ("vxd" endpoint = WDM/ASIO). [C] |
| 0x1d2090 | `FUN_10c53240` | `[h, client, ch, mode, src, idx]` (5 args) | `[st]` | Connect a stream channel (`FUN_18001d260`). [C] |
| 0x1d2094 | `FUN_10c21df0` @10c21df0 | `[h]` | `[st, 0x80000002, char[0x104]]` | Registry path string. [C] |
| 0x1d2098 | `FUN_10c4a480` | `[h]` | `[st]` | Stream reset (`FUN_18001e4b0`/`FUN_180013500`). [C] |
| 0x1d20a0 | `FUN_10c21a50` | `[h]` | `[st, lo, hi]` | One 32 KB 'DRVM' kernel buffer (64-bit pointer). Sim2k uses it as a host-memory pool, once globally. [L] |
| **0x1d20a4 SENDMSG** | `FUN_10c33140` @10c33140 (vt+0x264), `FUN_10c22f90` (line 49637), `FUN_10c34900` @10c34900 | `[h, userPtrWords, nWords]` | `[st]` (Sim2k passes outLen 4 or 0x14) | `SendMsgBuf` @18000f570: copies the words into the FIFO `BAR+0x81000 + (wr&0x3ff)*4`, then `writel(wr, BAR+0x0C)`. Free space is computed from `readl(BAR+0x08)`. [C] |
| **0x1d20ac FIFO_INIT** | `FUN_10c21dc0` @10c21dc0 (vt+0x1a0) | `[h, wrIndex]` | `[st]` | Resets the ring state (ctx+0xcc): `wr = in[1]`. Sim2k passes 0 at open and `readReg(2)` (= current card read index) after each reset. [C] |
| 0x1d20c4 SET_BLOCK | `FUN_10c220b0` @10c220b0 (vt+0x1dc) | `[-1, intBlkSize]` (in[0]=-1 means all cards) | `[st]` | Power of 2, ≤0x400. Stored at ctx+0x9c/0xa4. [C] |
| 0x1d20c8 | `FUN_10c53c40` | `[h, client, ch, hBoard, sramWordIdx]` | `[st]` | Points a stream channel at `BAR+0x80000+idx*4` (`FUN_18001d600`). [C] |
| 0x1d20cc RESET_WCLK | `FUN_10c22230` @10c22230 (resetClk) | `[h]` | `[st]` | ctx+0x78 = blk, ctx+0x3a88 = -blk & 0xfff. [C] |
| 0x1d20d0 / 0x1d20d4 | `FUN_10c08xxx` (SimCall) | `[h, -, strPtr]` | `[st]` | Store/fetch a ≤0x4f-char string in the driver. [C] |
| 0x1d20d8 | `FUN_10c53xxx` (lines 66251/66261) | `[h]` | `[st, 0x800091e0, 1]` | Kernel callback pointer (stream engine). [C] |
| 0x1d20e0 | `FUN_10c1a260` (only when board0 ScopeRev≠0x80) | `[h, intBlkSize, 0]` | `[st]` | Advances the word clock by n and runs stream callbacks (`FUN_18001e280`). [L] |
| 0x1d2100 GET_POS | `FUN_10c2fca0` @10c2fca0 | `[cardIndex]` | `[st, ctx+0x74, ctx+0x70, wclk, readl(BAR+0x10)]` (0x14 B) | Sample position (BAR+0x10 = HW sample counter). [C] |
| 0x1d2118 CONFIG_SPI | `FUN_10c31940` area (line 71878; ScopeXite only) | `[h, targetMask, ptr→24 B]` | `[st]` | "DIOC_SCOPE_CONFIG_SPI". **Not used for Pulsar2.** [C] |
| 0x1d2190 / 0x1d2198 | IRQ thread `FUN_10c23330` @10c23330 (x64 path, `FUN_10c63190()==0xb`) | 0x2190: `[h, hEvent "x64BlkWait"]`; 0x2198: `[h]` | 0x2198: `[st, wclk]` | Register the event, then wait for each block interrupt. Every wake calls `FUN_10c20270(wclk)`, which calls `FUN_10c445c0` once per elapsed block. [C] |
| 0x1d21a8 | `FUN_10c532b0` | `[h, 0]` | `[st]` | `FUN_18001c040`. [L] |

---------------------------------------------------------------------------------------------------
## 2. Card detection and identification

### 2.1 Configuration (`FUN_10c1e2b0` @10c1e2b0, cset.ini)
Per board section `board%i` (default `numBoards=0x10`; the loop stops at the first board that does not answer):
`ScopeRev` (default -1 = auto), `boardID` (default = index), `hwRev`, `numDSP` (default -1),
`clkSrc` (default 1), `useSDRAM`, `audioCfg`(1), `audioConfig`(-1), `maxComSlots`, `directExtClk`(0),
`enablePCIMaster`(1), `enableInterrupt`(1), **`syncPcChannels`(0x80, clamped 4..0xff)**,
`maxPCchannels`(0x100), `asyncFifoSize`(0x43), `cyclesPerSyncmsg`(6), **`enableSPI` (1 only for ScopeRev 0xd/0x15)**,
`backPlate`. Per DSP, from sections `dsp`/`dsp%d`: `kernel`, `coreClock`, `pmstart`, `pmsize`, `dmstart`, … (§3.4).
Globals: `intBlkSize` (0x400, power of 2, 0x40..0x400), `rescanHW` (1), `selectDriver`,
`simfreq` (44100), `noInitTest`, `noIrqTest`. The board config struct is 0x1ab4 bytes at `DAT_10cac9b0`;
the per-DSP sub-struct is 0xd4 bytes at +0x108. [C]

### 2.2 Probe (`FUN_10c21880(boardID, &hwRev)` @10c21880), run when `ScopeRev == -1` [C]
```
IOCTL 0x1d2028 [boardID]           -> st, h            (st 0 or -2 is OK)
IOCTL 0x1d2034 [h, 0]              -> v = BAR[0x00]
ScopeRev = ((v >> 8) & 0x1f) - 1 ;  hwRev = (v >> 13) & 7
if ScopeRev == 4 (WersiScope): IOCTL 0x1d2038 [h,0,0xffffffff]; IOCTL 0x1d2034 [h,4] -> hwRev = (v>>7)&4
IOCTL 0x1d202c [h]
```
For our card, `BAR[0] = 0x00004600` gives ScopeRev **5** and hwRev **2**. ("ScopeRev" is the board-type number
minus 1. The kernel's `rev` is the same field without the -1, so rev 6.)

### 2.3 Class factory (`FUN_10c4f0d0` @10c4f0d0, switch on ScopeRev) [C]
0 DevBoard, 1 Scope, 2 Pulsar, 3 PulsarSRB, 4 WersiScope, **5 Pulsar2 (`FUN_10c30980`, vtable 0x10c87d74)**,
7..10 Apollo, 0xd/0x15 ScopeXite (SPI, SDRAM), 0x80 Host-only (no HW), 0xfa/0xfb Placebox, 0xfd/0xfe serial/ezkit.
After construction: vt+0x2c `FUN_10c1a3b0` creates the DSP objects and vt+0x12c `FUN_10c327b0` → `FUN_10c21a50`
(open, §3.1).

### 2.4 Pulsar2 class constants (vtable 0x10c87d74; slots that differ from ScopeBoard) [C]
| slot | fn | value / meaning |
|---|---|---|
| +0x028 | `FUN_10c309c0` | name "Pulsar2" |
| +0x0f4 | `FUN_10c21110` | numDSP = cset `numDSP`, **default 6** |
| +0x100 | `FUN_10c309a0` | default kernel file: `sprintf("puls2os%d.21k", dspIndex)` |
| +0x1b0 | `FUN_10c309d0` | isMyBoard(id): `id == 6` (id = `(BAR[0]>>8)&0x1f`) |
| +0x0ec | `FUN_10c32db0` | reset (§3.2) |
| +0x124 | `FUN_10c32f80` | Run (§3.5) |
| +0x164 | `FUN_10c31100` | audio-cfg capabilities (detects the backplate via vt+0x280) |
| +0x17c | `FUN_10c30a80` | module placement filter (`flags & 0x1e800000` → not allowed) |
| +0x21c | `FUN_10c30a20` | DSP0 external-port DMA arm (§3.5) |
| +0x224 | `FUN_10c314c0` | **DSP 5** talks to the board micro-controller (`uc*` symbols) |
| +0x228 | `FUN_10c28600` | **DSP 4** is the sync-plate DSP |
| +0x274 | `FUN_10c30f70` | 1 |
| +0x278 | `FUN_10c311e0` | 2 |
| +0x27c | `FUN_10c309e0` | pairs (2,3) and (4,5) for n<2 when numDSP>3 [?] (likely ADAT/SPDIF DSP pairs) |
| +0x280 | `FUN_10c303f0` | backplate detection: reads DSP0 OS symbol `backplateID` (&0x3f). 0x3f → EmptyBackPlate, otherwise factory `FUN_10c0a050` (0x40 PulsarPlate, 0x41 PulsarPlusPlate, 1 CPlate, 2/0x19 PPlate, 3 PPlatePlus, 4 CPlate2, 5 BeckerPlate, 0x42 ApolloPlate, …) |
| +0x284 | `FUN_10c30100` | writeAudioCfg (reg5 bit-bang) |
| +0x288 | `FUN_10c301d0` | readAudioCfg |
| +0x290/+0x294 | `FUN_10c30310`/`FUN_10c30380` | set/get audio config through the backplate |
| +0x28c | `FUN_10c30520` | "resetHavarie" (clock-loss recovery) |

DSP objects are class `sharc` (vtable 0x10c8a4ac, base `ad2106x`). `dsp+8` = index 0..5, used as the message
DSP id. `dsp+0x48` = loaded kernel image. Per-board DSP state `board+0x11c+4*dsp`: 0 = reset, 1 = boot stream
done, 2 = OS running. [C]

---------------------------------------------------------------------------------------------------
## 3. Boot sequence

Top level, `Host::init FUN_10c4f0d0` → for each try (`DAT_10cab84c`):
`FUN_10c4e0a0` (hardware boot) → `FUN_10c4eae0(host, rate)` (sample rate) → `FUN_10c0c360` (modules). [C]

`FUN_10c4e0a0` @10c4e0a0 [C]:
```
for b in boards: b->vt+0x8c (FUN_10c330d0 prepareRestart)
FUN_10c444f0:  host->+4 ; for b: b->vt+0xec (RESET, §3.2) ; for b: b->vt+0xf0 (FUN_10c20600, §3.2)
for b: b->vt+0x128 (FUN_10c2cec0 -> FUN_10c20910: for each DSP: dsp->vt+0xd0 = loadKernel, §3.3/3.4)
       b->vt+0x124 (RUN, §3.5)
master->vt+0x1ec (FUN_10c2cf50: interrupt block config, §3.6)
FUN_10c446b0(host, 0xffff): for b: b->vt+4 (start every DSP)
```

Register shadow: `board+0xf4+2*i` is a u16 shadow of control reg i (0..9).
Helpers: `setReg FUN_10c20440(i,v)`, `bitSet FUN_10c204b0(i,m)`, `bitClr FUN_10c204e0(i,m)`,
`maskSet FUN_10c20470(i,v,mask)`. Each writes the whole shadow value to `BAR + 4*i`.
`Sleep(ms)` = `FUN_10c2c3f0`, which actually sleeps `((ms+9)/10)*10+1` ms. [C]

### 3.1 Open / register init — `FUN_10c21a50` @10c21a50 [C]
```
vt+0x130 (FUN_10c2c670: if PCI-master: bitClr(0,0x80)) ; close leftovers
IOCTL 0x1d2030 -> must be 0x50000
IOCTL 0x1d2028 [boardID] -> hKernel (board+0x98)
IOCTL 0x1d2048 -> bar (board+0x9c)                     // user mapping of BAR0
id = (bar[0] >> 8) & 0x1f ; require id == 6            // Pulsar2
once: IOCTL 0x1d20a0 -> 32 KB kernel pool
vt+0x204 (FUN_10c2c9f0: init SRAM slot usage table)
for k in 0..0x3ff: bar[0x20000+k] = 0                  // BAR+0x80000..0x80FFF cleared (slot tables A+B)
reg0 = 0x2e ; reg1 = 0 ; reg2 = 0 ; reg3 = 0 ; reg4 = 0x14 ; reg5 = 4 ; reg6 = 0x14 ; reg8 = 0
if (board+0xab0 != -1) reg7 = board+0xab0               // normally -1 → not written
IOCTL 0x1d20ac [h, 0]                                   // FIFO wr index := 0
```

### 3.2 Reset — Pulsar2 vt+0xec = `FUN_10c32db0` @10c32db0 [C]
```
// (+0x4ba satellite object is NULL on Pulsar2: no satellites)
SRAMTest (vt+0x1a8 FUN_10c22460): walking-1 on bar[0x20000];
          bar[0x20000+2^k]=0xffffffff/0 for 2^k = 0x8000..1   // SRAM ≥ 0x10000 dwords @BAR+0x80000
SYNCPC_MEMBASE = cset [hw] "SYNCPC_MEMBASE", default (0x6200 - syncPcChannels)*2  // 0xC300 for 128 ch
// board+0xac4 = cfg+0xcc, which the parser always sets to 1 → this branch is taken:
bar[0x20000] = SYNCPC_MEMBASE | 0x84000000   // = 0x8400C300  (BAR+0x80000)
bar[0x20001] = SYNCPC_MEMBASE | 0x84000001   // = 0x8400C301  (BAR+0x80004)
//  (other branch: (0x6210-n)*2 | 0x84200000 / |0x84200001)
bitClr(5, 0x1000)
repeat 2x:
    reg0 = 0x2f
    bitClr(2, 0x2)              ; Sleep(10)
    bitClr(0, 0x4)  (→0x2b)     ; Sleep(20)
    bitClr(0, 0x13) (→0x28)
    IOCTL 0x1d20ac [h, readReg(2)]          // FIFO wr := card read index (ring empty)
    (reg7 restore if set)
    first pass only: vt+0x11c FUN_10c33110(dsp0, addr 0, val 0) → raw FIFO frame
                     [0x02000000, 0x00000000]  (state 0 → not wrapped) [C]
FUN_10c205e0: board+0x11c..+0x197 = 0  (all DSP states := 0); dsp->vt+4 (no-op for sharc)
// then Host calls vt+0xf0 = FUN_10c20600:
bitSet(0,4); bitClr(0,4)     // pulse reg0 bit2  → reg0 = 0x28
```
Interpretation [?]: reg0 bits 0x01/0x02/0x04 are reset or hold lines. 0x20 is "boot mode / byte-boot path
enabled": it is cleared after DSP0's boot stream (§3.3). 0x10 is "word-clock running". 0x40 and 0x80 are set in
Run (0x80 = PCI bus-master enable).
reg0 **read** is a status register: bits 0..5 are the "stalled at DSP n" round-robin pointer, 0x10/0x20 are
Cebulon sync/async (see `FUN_10c22650`, `FUN_10c2cb90`), bits 8..12 are the board id and bits 13..15 are hwRev.
reg2 read is the FIFO read index (&0x3ff). Reads and writes of reg2 have different meanings. [L]

### 3.3 DSP boot stream (first code section of each kernel) — `UploadCode` = Pulsar2 vt+0x10c = `FUN_10c2d310` @10c2d310, state 0 [C, asm]
```
if dsp == 0:
    for each 48-bit instruction b[0..5] (b0 = MSB):
        FIFO frame (raw, header bit31 → never wrapped, flushed per instruction):
           [0x8a000000 | b5, b4, b3, b2, b1, b0]        // 6 dwords; len field = 5
    pad with [0x8a000000, 0,0,0,0,0] up to 0x100 instructions
// dsp != 0: nothing is sent for this section [L: bit31 = broadcast; all SHARCs receive DSP0's stream]
state[dsp] = 1 ; Sleep(10) ; bitClr(0, 0x20)   // reg0 0x28 → 0x08
```
This is the standard SHARC 256-word (0x100 × 48-bit) host/EPROM boot loader, fed one byte per dword with the
LSB in the header low byte. [L]

### 3.4 Rest of each OS kernel — `loadKernel` = sharc vt+0xd0 = `FUN_10c3a7c0` @10c3a7c0 [C]
1. `FUN_10c3a640(dsp, rate)`: cycle budgets from `coreClock`, `maxSyncCycles`, …
2. Memory-allocator lists from the per-DSP layout (defaults: sharc vt+0xd8..0x104):
   `pmstart 0x20000 / pmsize 0x2000`, `dmstart 0x25000 / dmsize 0x3000`, `inputstart 0x24000 / inputsize 0x1000`,
   `0x24200` (sysmsg block), `cyclesPerSyncClk 0x24`, `coreClock 40000000`. [C]
3. `FUN_10c1aec0` → kernel file name `FUN_10c1a4c0`: cset `kernel` (printf format with the DSP index) else
   board vt+0x100 → **`puls2os<dspIndex>.21k`**. The file is found on the DSP path (`FUN_10c51690`) and loaded
   by `FUN_10c1ad10` → `FUN_10c0c120` (fopen) → `FUN_10c0bd60` (COFF + descramble, see sc_format.md). The
   fallback is `BASE!CallBackLoadAtom` (`FUN_10c031b0`; a stub in base.dll). Accepted magics 0x521c/0x4353('SC')/0x4758('XG');
   flags & 2 (executable) is required. Error: "kernel file %s not found or invalid".
4. Board vt+0x84 = `FUN_10c20c60` @10c20c60 patches the image, then uploads every section:
   * symbol `loadPX1` (code): instruction byte[3] = `0x0A` if this is the last DSP (index numDSP-1), else
     `0x08` (`FUN_10c2d260`, vt+0x1b8).
   * symbol `call_serCommSetDMA` (code): bytes[0..1] = `06 BE` only for satellite DSPs, otherwise `00 00`
     (`FUN_10c2d2e0`, vt+0x1c0). On Pulsar2 the instruction becomes a NOP-ish word. [L]
   * symbol `cmdMask` (40-bit DM word): byte0 = `0x80 | 0x40` (board+0xac4=1), bytes 1..3 = 0 → `0xC0000000`
     (`FUN_10c2d2a0`, vt+0x1bc).
   * symbol `dspID` (DM): byte[3] = dsp index.
   * a symbol at a "negative" address: a byte is set to numDSP (6). [?]
   * Each section with size>0 is reserved in the allocator (`FUN_10c37a00` code / `FUN_10c37930`,
     `FUN_10c37a50` seg_dmd2, `FUN_10c37aa0` seg_dmd3). The returned address must equal the section's vaddr,
     otherwise "The kernel file %s does not fit to the DSP memory layout".
     **Code** (flag &1): `UploadCode(dsp, data, vaddr, size/6, objflags)` (vt+0x10c).
     **Data**: `UploadData(dsp, data, vaddr, size/5, 0)` (vt+0x108).
5. `UploadCode` in **state 1** (boot stream already done) [C, asm @10c2d4a3..10c2d5ff]:
   ```
   IOP(dsp, 0x1c, 0x20e0)   // see IOP() below
   IOP(dsp, 0x42, nInstr)
   IOP(dsp, 0x41, 1)
   IOP(dsp, 0x1c, 0xe1)
   for i in pairs of instructions A=b[0..5], B=b[6..11]:
       frame( ((dsp|0x30)<<21) | addr,  [A47..16, (B15..0<<16)|A15..0, B47..16] )   // len 3
       addr += 2
   last odd instruction: frame( ((dsp|0x20)<<21) | addr, [A47..16, A15..0] )        // len 2
   ```
   In **state 2** (OS running; used later for modules), chunks of ≤10 instructions:
   `send(dsp<<21|0x120000, ∅, batch)`, `IOP(0x1c,0x20e0,b)`, `IOP(0x41,1,b)`, `IOP(0x1c,0xe1,b)`,
   then the packed frame with `len = #dwords`, then `IOP(0x1c,0x40,b)`, `IOP(0x41,2,b)`,
   `IOP(dsp, 0x12001c, 0x41)` (flush). [C]
6. `UploadData` = vt+0x108 `FUN_10c338b0` @10c338b0 [C]:
   * all-zero blocks (>0x20 words) with the OS symbol `sys_clearMem` present → `sysmsg(dsp, 0x11, addr, n)`
     instead.
   * Word packing: a 40-bit DM word `d[0..4]` is sent as `d0<<24|d1<<16|d2<<8|d3`. **byte d4 (the 8 LSBs of the
     40-bit float extension) is dropped.** With is32=1, raw u32.
   * n < 3: one `SetValue`-type frame per word (vt+0x11c): header `((dsp|0x10)<<21)|addr`.
   * state 1 (or n ≤ `DAT_10ca0f24`): first `IOP(0x1c,0x2040)`, `IOP(0x42,n)`, `IOP(0x1c,0x41)`. Then chunks
     of 14: `send(dsp<<21|0x120000,∅,batch)`, `IOP(0x41,1,batch)`, wrapped frame
     `((cnt<<4|dsp)<<21) | addr+off` with cnt words, then `IOP(dsp, 0x120041, 2)`.
   * otherwise: two interleaved frames per ≤28-word chunk (even words to `addr`, odd words to `addr+1`, stride 2).
     This matches the kernel's `dsp_write_block` @18000fb70.
7. After every DSP is loaded, **Run** (§3.5) first calls `FUN_10c20990`: for each DSP `vt+0xd4` (`FUN_10c3aa50`:
   if the kernel has no `seg_stak` contents, `UploadData` 1 word at the stack top), then state[dsp] = 2. [C]

`IOP(dsp, reg, val, batch)` = vt+0x1d4 `FUN_10c2d180` @10c2d180 [C]:
frame header `((dsp|0x10)<<21) | 0x100000 | (batch?0x20000:0) | reg`, 1 data word `val`.
Interpretation [L]: the target is the SHARC's IOP register space. `0x1c` is the DMA control of the external-port
DMA channel, and `0x40/0x41/0x42` are its II/IM/C registers. The values 0x2040/0x20e0/0xe1/0x41/0x40 are the
DMAC words (disable+flush / 48-bit packed enable / 32-bit enable). The exact 21065L bit meanings are not
verified [?].

### 3.5 Run — Pulsar2 vt+0x124 = `FUN_10c32f80` @10c32f80 [C]
```
FUN_10c20990                         // each DSP: vt+0xd4, state = 2
vt+0x21c FUN_10c30a20:  Sleep(10)
        IOP(0, 0x1c, 0x2040) ; IOP(0, 0x41, 2) ; IOP(0, 0x42, 0xffffffff) ; IOP(0, 0x1c, 0x41)
        // DSP0 external-port DMA: endless receive, modifier 2
if (board+0xac4 && cfg.enablePCIMaster(1)):
        fill bar[0x20002 .. 0x203ff] = 0  (vt+0x1d0 FUN_10c205a0) ; bitSet(0, 0x80)
maskSet(1, 0, 0x2b)                  // reg1 &= ~0x2b
bitSet(0, 0x40) ; bitClr(0, 0x08) ; Sleep(5)        // reg0 = 0xC0
vt+0x218 FUN_10c2cda0:  clkSrc==0 → bitSet(5,1);  clkSrc==3 → bitSet(5,0x2000);  (default 1: nothing)
                        startClk(0) (vt+0x38 FUN_10c22380):
                            bitSet(2,2); Sleep(2); bitSet(2,4); bitSet(0,0x10); Sleep(2); bitClr(2,4); Sleep(2)
vt+0x258 FUN_10c2d030: for each DSP n:
        SetValue(n, sym "dspID",      n)
        SetValue(n, sym "dspAckDest", 0x63e00800 | n*2)      // = host SRAM word 0x800+2n (encoded addr)
if (!DAT_10cab840):
        for each DSP: GetValue(n, sym "dspID")               // round-trip check via sysmsg 8
        vt+0xf8  FUN_10c32b00  (DSP5/uc: ucMagicDest := host(0x801+2*5), cmd 0x229 → +0xac8)
        vt+0x220 FUN_10c32c50  (DSP5/uc: cmd 0x21f; result>>16 → board+0xacc, used by vt+0xfc)
        vt+0x164 FUN_10c31100  (backplate detection via DSP0 symbol "backplateID")
```
Then `FUN_10c2cf50` (master vt+0x1ec) [C]:
`IOCTL 0x1d20c4 [-1, intBlkSize]`, then
`maskSet(1, code | (PCImaster ? 0x10 : 0x18), 0x3f)` with code = {0x400:1, 0x200:3, 0x100:0x21, 0x80:0x22, 0x40:0x23}.
With the defaults this gives **reg1 = 0x11** (interrupt every 1024 samples, IRQ enabled [L]).

Approximate final register state with the defaults: reg0=0xD0, reg1=0x11, reg2=0x02, reg3=0, reg4=0x14,
reg5=0x04, reg6=0x14, reg8=0. [L: derived by following the shadow writes]

### 3.6 Host↔DSP message primitives
**sendMsg** = vt+0x264 `FUN_10c33140(header, words, n, batch)` @10c33140 [C]. Words collect in `board+0x19c`
(≤0x3e dwords, count at `board+0x198`) and are flushed with `IOCTL 0x1d20a4 [h, &buf, count]` when `batch==0`.
If the target DSP is past state 0 and the message is not raw (bit31 clear), or if `board+0xac0` is set, the
frame is **wrapped**:
```
[T|0x120000, T|0x120000]           (only if batch==0)       T = dsp<<21
header | 0x20000000
payload[n]
[T|0x20100000]                      (only if batch==0)
0x0FE0C008, 0x1212, 0x2424, 0x3636, 0x4848, 0x5a5a, 0x6c6c, 0x7e7e
[0]                                 (only if batch==0)
```
This is the same frame the kernel builds in `dsp_write_word` @18000fa10. The kernel pads with
0x9999..0x3333 instead of 0x1212..0x7e7e, so the padding values are don't-care [L]. `FUN_10c34900` @10c34900 builds
such a frame by hand (14 dwords). [C]

**Header layout** (Sim2k side) [C]:
```
bit 31      raw/broadcast (boot stream); suppresses wrapping
bit 29      0x20000000 set on wrapped frames
bits 28..25 payload length in dwords (field written as (len<<4|dsp)<<21)
bits 24..21 DSP id (0..15; host = 0xf in DSP-side "dest" encodings; id≥16 adds bit 18)
bit 20      IOP-register space (0x100000)
bit 17      0x20000 (in IOP and "null" headers 0x120000) [?]
bits 20..0  target address
```
DSP-side destination encoding, used when the host tells a DSP where to write (vt+0xa8 `FUN_10c2e420`):
`dest(dsp,a,isPCI,len) = (isPCI?0x80000000:0) | len<<25 | (dsp&0xf)<<21 | (dsp&0x10)<<14 | (a&0x1ffff)`, then
`|0x20000000` if bit31 is clear. Host SRAM target: `a | 0x41e00000 (|0x20000000)`. Hence
`dspAckDest = 0x63e00800|2n`. This matches the kernel's `target(a) = ((a>>21)&0xF)|(((a>>18)&1)<<4)`. [C]

**SetValue(dsp, addr, val)** = vt+0x114 `FUN_10c2d200` ("DSP%x: SetValue (0x%x, 0x%x)"):
`sendMsg(((dsp|0x10)<<21)|addr, &val, 1, 0)`. The address comes from the kernel symbol table
(sharc vt+0x138 `FUN_10c37c10`). [C]

**sysmsg(dsp, type, a, b)** = vt+0x104 `FUN_10c33410` @10c33410 [C, asm]:
`bar[0x20000 + 0x800 + 2*dsp] = 0` (BAR `0x82000 + 8*dsp`) → `UploadData(dsp, {type,a,b}, 0x24201, 3, is32=1)`
→ wait for ack (vt+0x1e4 `FUN_10c22650`: poll `bar[0x20800+2*dsp] != 0`, 1 s, twice; "timeout waiting for
acknowledge from dsp %d"). The reply value is `bar[0x20801+2*dsp]` (BAR `0x82004+8*dsp`).
Types seen: 2 (connect module into chain), 3, 6 (patch via `codeBuf`/`registerPatch`), **8 = GetValue**
(vt+0x110 `FUN_10c22890`), 0xf (clock symbols `alastClk`, `FUN_10c30640`), 0x11 (`sys_clearMem`).

DSP-side symbols that the host resolves in the OS kernels (all through the .21k symbol table):
`dspID, dspAckDest, loadPX1, cmdMask, call_serCommSetDMA, codeBuf, registerPatch, backplateID, FScale, asRatio,
SPDIFprof, syncPlateMaster/SlaveSel/setSyncPlate/Found, ucBytePosOut/ucDataOut/ucCmdOut/ucMagicDest, magicProt,
alastClk, fadeInOut/fadeAdd/fadePos/fadeAdr/fadeRead/fadeJmp/fadeBuf, _firstmod, _firstsync, _firstasync,
ret_sync, ___lib_FPINV, sys_clearMem, bootXDSPinfo, seg_stak`.

### 3.7 Sample rate / clock — `FUN_10c4eae0(host, rate)` @10c4eae0 [C]
* Per board: vt+0x158 `FUN_10c35970(rate)` → vt+0x28c `FUN_10c30520` (reg9 = 0; if backplate status bit 0x100
  is set: stopClk, backplate resetHavarie, startClk), then `IOCTL 0x1d2074 [h, rate]`.
* Per DSP: sharc vt+0x6c `FUN_10c3bf10(rate)`:
  `SetValue(dsp, "FScale", {48k:0x40000000, 44.1k:0x3acccccc, 32k:0x2aaaaaaa, 88.2k:0x75999999, 96k:0x80000000,
  else round(rate*2^31/96000)})`, then `SetValue(dsp, "asRatio", clamp(rate/3125, minSyncAsyncRatio, maxSyncAsyncRatio))`.
* Hardware rate/clock-source word (backplate, written by `pulsar_cfg_module` `FUN_10c60720` → board vt+0x290
  `FUN_10c30310` → backplate vt+8 `FUN_10c08e10` → board vt+0x284 `FUN_10c30100`):
  ```
  cfg bit0 = external clock (1) / internal (0)
  cfg & 0x4006: 0x0000=96k, 0x4000=88.2k, 0x0002=48k, 0x0004=44.1k, 0x0006=32k   (+0x2000 always ORed in)
  default after reset: 0x84 (|0x2000) = 44.1 kHz internal  (backplate vt+0x10 FUN_10c08de0)
  directExtClk: bitSet(5,0x1000) and cfg = cfg & ~0xb | 0x84
  writeAudioCfg(cfg):            // reg5 = BAR+0x14; data bit 0x400, clock 0x800, latch 0x100, mask 0xd00
     maskSet(5, 0, 0xd00)
     repeat nbits (backplate vt+0 = 14 for Pulsar/PulsarPlus plates):
        d = (cfg&1)<<10 ; maskSet(5,d,0xd00) ; maskSet(5,d|0x800,0xd00) ; maskSet(5,d,0xd00) ; cfg >>= 1
     maskSet(5,0x100,0xd00) ; maskSet(5,0,0xd00)      // latch
  readAudioCfg(): pulses 0x800/0/0x100/0x900/0x100/0, then shifts in bits from reg5 bit 6
     status & 0x1003: 0=96k, 0x1000=88.2k, 1=48k, 2=44.1k, 3=32k (external rate); 0x100 = lock lost
  ```
  Which backplate class the Pulsar2 actually reports (`backplateID`, read from the running OS) is unknown
  [?]. The 14-bit PulsarPlate format is the best guess.

### 3.8 Init / IO modules (PINIT.ol, P2_AINIT.dsp, P2_IO.ol, AINIT.ol, c2_minit.dsp …) [C/L]
Sim2k has no hard-coded references to these files. They are loaded like every other DSP module:
`ModuleInit/ModuleInitEx` (exports @10c06fc0/@10c071d0) → parse (`FUN_10c0bd60`) → relocate
(`FUN_10c0aba0`, externals `FUN_10c0a9c0`) → section placement (`seg_pmco`, `seg_sync`, `seg_asyn`, `seg_init`,
`seg_exit`, `seg_dmda`, `seg_dmd2`, `seg_dmd3`, `seg_inda`; `FUN_10c0a760`) → `UploadCode`/`UploadData` (state-2
path) → hook into the OS execution chains: `_firstsync`/`ret_sync` patching (`FUN_10c1a760`), connect sysmsg type
2 (`FUN_10c1b170`), fade-in/out via `fadeInOut` (`FUN_10c1b430`).
The device/project files that select which IO modules run on which DSP are opaque in this install (no
plain-text references to `P2_IO`/`PINIT` were found under `scope_full`). [?]

### 3.9 Audio I/O routing (what is known)
* Inter-DSP TDM ("comm") slots: `allocSyncOutput` `FUN_10c2e070` allocates slot numbers ≥0xC000 per DSP window
  (`dsp+0xec`/`+0xe8`), with usage table `board+0x12dc`.
* PC↔DSP sync channels: `syncPcChannels` (128) at DSP-side addresses from `SYNCPC_MEMBASE` (0xC300).
  Channel address = `0xC000 + 2*ch` (`FUN_10c2ca70`, board+0xac4=1). The SRAM master-table words
  `[0x80000]=0x8400C300` / `[0x80004]=0x8400C301` tell the card's bus-master engine where that window is [L].
* WDM/ASIO: kernel "vxd" stream clients (0x1d2088/0x1d2090/0x1d20c8) are bound to SRAM slots or
  bus-master pages (0x1d2058). The kernel moves data per block IRQ (see scScope_sys.md §2/§3 for the slot-table
  encoding at BAR+0x80000/0x80800). The DSP side is the `asioIn/asioOut/dsp2pc/pc2dsp/waveIn/waveOut`
  modules. [L]

---------------------------------------------------------------------------------------------------
## 4. UploadCode / UploadData → IOCTL payload (worked example)

`SetValue(dsp=3, addr=0x24210, 0x12345678)` while the OS is running (state 2) produces
`IOCTL 0x1d20a4`, in = `[h, ptr, 14]`, where `ptr` points to:
```
0x00720000, 0x00720000,                 // T|0x120000, T = 3<<21
0x22624210,                             // ((3|0x10)<<21 | 0x24210) | 0x20000000  (len 1, dsp 3)
0x12345678,
0x20700000,                             // T|0x20100000
0x0FE0C008, 0x1212, 0x2424, 0x3636, 0x4848, 0x5a5a, 0x6c6c, 0x7e7e,
0x00000000
```
Boot-stream instruction `0x0123456789AB` → `[0x8a0000AB, 0x89, 0x67, 0x45, 0x23, 0x01]`.
Code pair A=`0x0123456789AB`, B=`0xCDEF01234567` at 0x20000 on DSP 2, state 1 →
payload `[0x01234567, 0x456789AB, 0xCDEF0123]` with header `(2|0x30)<<21 | 0x20000 = 0x06420000`
(0x26420000 after wrapping, inside the T|0x120000 … trailer frame). Inside the kernel each dword becomes one `writel` into `BAR+0x81000+4*(wr&0x3ff)`,
followed by `writel(wr, BAR+0x0C)`.

---------------------------------------------------------------------------------------------------
## 5. Minimal Linux sequence (proposal)

Prerequisites: the kernel module exposes BAR0 to a userspace tool (mmap, or a "write words to FIFO" ioctl)
and implements the FIFO writer exactly like `SendMsgBuf`:
`rd = readl(bar+8)&0x3ff`, free = `0x400 - ((wr-rd)&0x3ff)`, keep ≥0x100 dwords of headroom, write the
words at `bar+0x81000+4*wr`, wrap at 0x400, then `writel(wr, bar+0x0C)`.

1. **Probe**: `readl(bar)` = 0x4600 → `(v>>8)&0x1f == 6`.
2. **Open init** (§3.1): zero `bar+0x80000..0x80FFF`; write regs
   `0:0x2e 1:0 2:0 3:0 4:0x14 5:4 6:0x14 8:0`; FIFO wr = 0.
3. **Reset** (§3.2): optional SRAM test; `[0x80000]=0x8400C300`, `[0x80004]=0x8400C301`; reg5 &= ~0x1000;
   2× { reg0=0x2f; reg2&=~2; sleep 10–20 ms; reg0&=~4; sleep 20–30 ms; reg0&=~0x13; wr = readl(bar+8)&0x3ff;
   1st pass: FIFO `[0x02000000, 0]` }; reg0|=4; reg0&=~4.
4. **Decode** `puls2os0..5.21k` with `tools/sc_decode.py` and parse the COFF sections and symbols. Apply the
   patches: `dspID` = n, `loadPX1` byte3 = 0x08 (0x0A for DSP5), `cmdMask` = 0xC0000000,
   `call_serCommSetDMA` bytes0..1 = 0.
5. **Boot stream** (DSP0 kernel, first code section): 256 × `[0x8a000000|b5, b4, b3, b2, b1, b0]`; sleep 10 ms;
   reg0 &= ~0x20. **[?] Check on hardware** whether the first section of DSP0 is really the 256-word loader. Dump
   the section order with `sc_decode.py --dump`.
6. **For each DSP n = 0..5**, every other section: code → `IOP(n,0x1c,0x20e0)`, `IOP(n,0x42,count)`,
   `IOP(n,0x41,1)`, `IOP(n,0x1c,0xe1)`, then the packed frames (§3.4.5). Data → `IOP(n,0x1c,0x2040)`,
   `IOP(n,0x42,count)`, `IOP(n,0x1c,0x41)`, then chunked frames (§3.4.6). Use wrapped frames (state ≥ 1).
7. **Run** (§3.5): state=2 for all; DSP0 `IOP 0x1c=0x2040, 0x41=2, 0x42=0xffffffff, 0x1c=0x41`;
   zero `bar+0x80008..0x80FFF`; reg0|=0x80; reg1&=~0x2b; reg0|=0x40; reg0&=~8; startClk(0);
   per DSP: SetValue `dspID`, `dspAckDest`. Verify with GetValue (sysmsg 8 → mailbox at `bar+0x82000+8n`).
   If the round-trip works, the DSPs are alive.
8. **Rate**: per DSP SetValue `FScale` / `asRatio`; write audio cfg `0x2084` (44.1k internal) or `0x2082` (48k)
   via the reg5 bit-bang (14 bits, LSB first, then latch).
9. **Interrupts**: reg1 = 0x11 (block 1024) or 0x21|0x10=0x31 for 256. The ISR side is in scScope_sys.md §3.
10. **Audio out**: requires loading at least the Pulsar2 IO module (`P2_IO.ol`; analog-init `P2_AINIT.dsp`,
    plus `PINIT.ol` [?]) as relocatable modules linked into the DSP OS chain (§3.8), and an ASIO/WDM-type module
    (`asioOut`/`pc2dsp`) that reads the PC sync channels. This module-linking step is the largest piece that is
    not yet reconstructed. The next RE targets are `FUN_10c1a760` (chain patch), `FUN_10c1b170`/`FUN_10c1b070`
    (connect), `FUN_10c2e250` (allocSyncInput), and the stream-client IOCTLs above.
    A cheaper first milestone: after step 8, use SetValue/GetValue on DSP memory to confirm the OS responds,
    then try one hand-placed IO module.

### Open questions / risks
* The exact semantics of reg0/reg2 bits and of the IOP DMA values are inferred, not proven.
* Which section of puls2os0 forms the 256-word boot stream, and whether DSP1..5 really boot from the broadcast.
* The Pulsar2 backplate type and bit count (14 assumed).
* `DAT_10ca0f24` (UploadData threshold) is a runtime value that has not been looked up.
* The 0x1d20a4 frames are reproduced byte-exactly from code; the padding words differ between Sim2k and the
  kernel, so they are presumably ignored by the card [L].
