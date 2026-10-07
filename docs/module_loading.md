# Loading, linking, activating and wiring DSP modules on a running Pulsar2 DSP

How SCOPE's `Sim2k.dll` puts a relocatable DSP module (`.dsp`) or library (`.ol`) onto a running
`pluto` (ADSP-21065L) DSP, makes it run, connects its pads to other modules, and sets its parameters.
Implementation: `tools/pulsar_modules.py` (offline linker + op generator; the hardware frames are sent by
`tools/pulsar_loader.py` `Board`). Prerequisite: the DSPs are booted and in state 2 (OS running), see
`dsp_boot_analysis.md` and `clock_rate.md`.

Legend: **[C]** = read from the code (Sim2k asm checked where Ghidra drops virtual-call arguments, or the
DSP OS disassembly), **[L]** = likely, **[?]** = guess. `FUN_x` = Sim2k function, "vt+0xNN" = virtual slot.
Objects: `brd` = Pulsar2 board (vtable 0x10c87d74), `dsp` = pluto (vtable 0x10c8a634, ctor FUN_10c3c100),
`mod` = dsp_module instance (vtable 0x10c83c84, ctor FUN_10c18c40), `cls` = class info (`mod+0x60`, FUN_10c017b0).
DSP addresses are for `puls2os0` unless noted. Always take OS addresses from each image's own symbol table:
os4/os5 shift some PM code, os1/2/3 shift the DM variables after 0xC513 by -1 (e.g. `_null` = 0xC514 on os0/os4,
0xC513 on os1/2/3/5).

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **Link** [C]: parse the COFF (`sc_decode`). Allocate the sections first-fit in the pluto heaps
   (PM 0x8000..0x97FF and DM 0xC400..0xDFFF, minus the OS image's own sections). Then apply relocation types 2/3/4/6.
   Undefined symbols resolve against, in order: the module's `inputN` pins (unconnected = OS `_null`),
   the OS image's EXT symbols, and the `seg_pmco`/`seg_dmda` EXT symbols of `.ol` libraries already on this DSP.
   Libraries are pulled in automatically: the first `*.ol` that exports a missing symbol.
2. **Upload** [C]: DM sections with UploadData and PM sections with the **state-2 UploadCode** path (10-instruction
   batched messages, §3.1). The metadata sections `seg_desc/name/info/id/attr/junc` never go to the DSP.
3. **Run init** [C]:
   - Library `seg_init` runs with **sysmsg 7** (`sys_callfunction`, a = address, b = 0).
   - Module `seg_init` runs inside **sysmsg 2** (`sys_addmodule`, a = seg_mod address), which CALLs `fnInit` (seg_mod word 1)
     with I0 = module struct. In the same message it links the module into a list: the async chain (`b` = predecessor's
     seg_mod or `&_firstasync`), or the dummy list `_firstmod` when the module has init code but no async code.
   - Init and inda memory is a temporary overlay, freed right after.
4. **Hook into the sync chain** [C]: every `seg_sync` ends with `JUMP ret_sync (DB)`. The host retargets that jump
   (low 16 bits of the instruction) with **sysmsg 6** (`sys_patchinstruction`; the instruction addresses go into OS
   `codeBuf` 0xC41D first). The new module's exit is pointed at the next module (or `ret_sync`), and the
   predecessor's exit (or `_firstsync`, via SetValue) at the new module's entry.
5. **Connect** [C]:
   - An input is "the DM address of a 2-word pair (sync) or 1 word (async)". It is stored in the module's seg_mod
     input word (SetValue), **and** patched (sysmsg 6) into every `seg_sync` instruction that was relocated against
     `inputN`.
   - Same-DSP source = the source module's output word.
   - Cross-DSP sync source = a slot in the source DSP's EPB1 sync block (0xC080+0x20·d+4+2k, grown with sysmsg 0xB).
   - Cross-DSP async source = an export header list consumed by `os_sendmsgPX2`.
6. **Parameters** [C]: gain/frequency etc. are async **input pads**. The host allocates a DM word (2 for a sync pad),
   SetValues the raw 32-bit value into it and links the input to it once; later changes are just SetValue.
   `ModuleSetValue(sym, idx)` writes EXT DM symbols directly.

---------------------------------------------------------------------------------------------------
## 1. Load path

```
ModuleInit @10c06fc0 / ModuleInitEx @10c071d0            (exports; Ex also takes hints: board, dsp, voices, slot)
  FUN_10c03fe0 -> newModule @10c03ef0 -> FUN_10c02ba0     class factory; COFF parse FUN_10c0bd60; desc FUN_10c017b0
  mod vt+0x90                                            (voices)
  FUN_10c4cf00(boardset, mod, dsp hint, board hint)      choose board/DSP
    FUN_10c16350(dsp, mod, after)
      dsp vt+0x44 -> vt+0xa0  FUN_10c3dff0               allocate all segments (§1.4)
      dsp vt+0x48 -> vt+0x9c  FUN_10c3b890               link + upload + activate (§1.5)
      host lists: dsp+0x1c (all), dsp+0x20 (async)       inserted AFTER the download
```

### 1.1 COFF objects [C]
FUN_10c0bd60 creates a 0x6c-byte object:
- +0x14 sections (0x38 bytes each, FUN_10c0bc70): **+0xc paddr = "new base"**, overwritten at link time by
  FUN_10c0a760 / FUN_10c0a910; **+0x10 vaddr = original base**; +0x24 nreloc; +0x30 relocations as 12-byte
  `{vaddr, symndx, type}`.
- +0x18 symbols (0x14-byte entries: +8 value, +0xc scnum, +0x10 sclass, +0x11 numaux).
- +0x30 "satellite" flag (COFF flag 0x4000; never set on Pulsar2 files).

Symbol name match (FUN_10c0b8c0/FUN_10c0b940): case-sensitive. Names of 8 characters or fewer are compared with
`strncmp(8)`, longer ones exactly. `ext_only` requires sclass 2 (C_EXT).

### 1.2 Relocation: FUN_10c0b1d0 → FUN_10c0b090 → FUN_10c0aba0 → FUN_10c0a9c0 [C]
Each section is copied, all of its relocations are applied, and then it is uploaded. Jump table @10c0ad10.
```
off   = r.vaddr - sect.vaddr                       # word offset in the section
field = t2/t6: PM word bytes 3..5 (low 24 bits)    # JUMP/CALL addresses
        t3   : PM word bytes 2..5 (low 32 bits)    # ureg = imm32, DM(imm32, Ik) ...
        t4   : DM word bytes 0..3 (top 32 of 40)   # data pointers
        else : "unknown relocation type %d"
val = resolve(sym)                                 # FUN_10c0a9c0; val < 1 -> link error
if t6: val -= sect.newbase + off                   # PC-relative to the NEW instruction address
if sym.scnum != 0: field -= sym.value              # defined symbol: field held sym.value + addend
val += field                                       # undefined symbol: field = addend (e.g. spr0_svc+1)
if module: val = module.vt+0x80(val, &ireg)        # FUN_10c17c60: own sync outputs -> allocated slot (§4.3)
store low 24 / 32 bits big-endian
```
- **resolve(sym)**:
  - scnum > 0: `sect[scnum-1].newbase - sect.vaddr + sym.value`
  - scnum < 0 (ABS/DEBUG): failure
  - scnum == 0: `dsp vt+0x13c(name, module, 0, ext=1)` (§1.3); "Unknown external symbol" if < 0
  - Polyphonic modules (flag 0x400000) replace a `…VoiceDef` suffix by the voice number (not implemented in our tool).
- Corpus check (1117 `.dsp` + 53 `.ol`): type 2 6285, type 3 15670, type 4 16798, type 6 702. Every type-6 relocation
  targets a defined symbol in the same section, and 671 of them have field == sym.value, so the result is
  `target - pc (+addend)`.
- Type 3 on `0xAE/0xAF` instructions (SHARC type 15 `ureg = DM(imm32, Ii)` / `DM(imm32, Ii) = ureg`: 0xAE = read
  via I7, 0xAF = write via I7). Sim2k would rewrite the I-register field (`byte0 ^= ((ireg*2) ^ byte0) & 0xE`) if
  ireg != I7, and warns "syncout is not written via i7" for other forms. On pluto vt+0x170 = 0, so ireg is always I7:
  **no instruction rewriting on Pulsar2**. vt+0x150 FUN_10c37870 and vt+0x14c FUN_10c375b0 (satellite address and
  register translation) are no-ops here [C].

### 1.3 External symbol lookup: pluto vt+0x13c FUN_10c3b1c0 [C]
1. `input<digits>` (FUN_10c0b010) when a module is being linked:
   - N ≥ numIn is an error.
   - Otherwise use the pin record (`mod+4[N]` = {src module, src pad, address}):
     - source on another DSP: the pin address;
     - source on this DSP: the source's async word `src.mod+5+2k`, or its sync output mapped to its slot;
     - unconnected: OS **`_null`**.
   - The OS also exports `input0..9` (= `_null`), but the pin path wins, and modules import up to input90+.
2. An OS image EXT symbol: its absolute value.
3. A library already placed on this DSP (`dsp+0x4c[bank]`): only EXT symbols in its **seg_pmco** (value − vaddr + pmco
   base) or **seg_dmda** (+ dmda base). Symbols in other library sections are not exported.

There is no module-to-module symbol linking; modules only see the OS, libraries and their pins.

**Libraries** [C]:
- Every `*.ol` in the DSP directory is registered (FUN_10c17210 `_findfirst("*.ol")`, FUN_10c17080 / FUN_10c16e90).
- Per class, once (FUN_10c17510 / FUN_10c173c0): for each undefined symbol that is not an OS EXT symbol, not
  `inputN` and not already covered, the first library that exports it is appended. The libraries' own imports are
  processed the same way.

Results for the test modules:

| module | library pulled in | symbol it needs |
|---|---|---|
| P2_AINIT | PINIT.ol | `InitPPlateAnalog` (PINIT seg_dmda) |
| P2_ANO | P2_IO.ol | `_tx_pp1ptr` (P2_IO seg_dmda) |
| CSineR4 | none | – |

### 1.4 Placement: allocators and pluto ranges [C]

**Heaps** (created in loadKernel FUN_10c3a7c0):
- PM heap `[vt+0xd8, +vt+0xdc]` = **0x8000..0x97FF**.
- DM heap `[vt+0xe0, +vt+0xe4]` = **0xC400..0xDFFF**.
- The mod2/dmd2 and mod3/dmd3 heaps exist only with per-DSP cfg +0x140..+0x14c (not on Pulsar2 [L]).
- `sram_*` gives −1 on pluto (vt+0x154/0x158 = FUN_10c371f0/37200).
- The comm/TCB area 0xC080..0xC3FF and the IOP space are outside the heaps.

**OS reservation** (thunk FUN_10c20c60 @10c28960): each OS section is fixed-allocated at its own address:
- PM sections: FUN_10c37a00
- DM sections: FUN_10c37930
- dmd2 / dmd3: FUN_10c37a50 / FUN_10c37aa0

Nothing else is reserved: there is no stack, and the boot flag at 0xDFFF lies inside the heap.

**Heap algorithm** (FUN_10c25c30 alloc / FUN_10c25a80 alloc_fixed / FUN_10c25e70 free):
- Blocks form a circular, address-ordered list.
- `alloc(n)`: first fit starting at the **head**, taking the low end of the block. No alignment.
- `alloc_fixed(a,n)`: if a block is exactly `[a,a+n)`, bump its use count (sharing). Otherwise carve the range out of a free block.
- `free`: decrement the use count, merge with free neighbours, and **return the merged block, which every caller
  stores as the new head**. The next search therefore starts at the last freed block. `Heap` in pulsar_modules.py
  replicates this, so the addresses match Sim2k's.

Free memory after each OS image:

| image | PM free | DM free |
|---|---|---|
| os0 | 0x8111-0x813F, 0x82E0-0x97FF | 0xC577-0xDFFF |
| os1, os3 | 0x80FF-0x813F, 0x82E0-0x97FF | 0xC576-0xDFFF |
| os2 | 0x8102-0x813F, 0x82E0-0x97FF | 0xC576-0xDFFF |
| os4 | 0x811A-0x813F, 0x831A-0x97FF | 0xC57A-0xDFFF |
| os5 | 0x8110-0x813F, 0x8346-0x97FF | 0xC580-0xDFFF |

The small hole is between the OS `seg_init` end and `seg_pmco` @0x8140; first fit uses it.

**Allocation order**, pluto vt+0xa0 FUN_10c3dff0:
1. FUN_10c3aad0: the init block size is the max of the module's `seg_init` and the `seg_init` of every library of the class not
   yet on this DSP. The inda block is sized the same way.
2. FUN_10c3dc80(mod, 1): for each library in **reverse** list order that is new on this DSP, allocate PM pmco, DM dmda,
   PM exit, DM exda.
3. First instance of the class on this DSP (FUN_10c158f0): PM `seg_pmco`, `seg_exit`, `seg_asyn`; DM `seg_dmda`
   (plus dmd2/dmd3). Later instances `alloc_fixed` the same addresses (shared code, refcounted).
4. DM `seg_mod` (plus mod2/mod3/sram_mod).
5. PM `seg_sync` (always per instance: its JUMP is patched per instance).
6. PM init block (`mod+0xc0`), DM inda block (`mod+0xc8`). `seg_init`/`seg_inda` are placed there.
   FUN_10c3dc80(mod, 2): new libraries `alloc_fixed` the same blocks for their own init/inda.

**Which segments go where:**

| segment | space | to DSP | lifetime |
|---|---|---|---|
| seg_mod (+mod2/mod3) | DM | yes | per instance |
| seg_sync | PM | yes | per instance |
| seg_pmco, seg_asyn, seg_exit | PM | yes | per class (shared) |
| seg_dmda (+dmd2/dmd3) | DM | yes | per class (shared) |
| seg_init | PM | yes | **temporary**: freed after activation |
| seg_inda | DM | yes | **temporary**: freed after activation |
| library seg_pmco/seg_dmda/seg_exit/seg_exda | PM/DM | yes | per DSP, use-counted |
| library seg_init/seg_inda | PM/DM | yes | temporary |
| seg_desc, seg_name, seg_info, seg_id (licence check), seg_attr, seg_junc, seg_cfg, seg_md | – | **host only** | – |

### 1.5 Link and upload order: pluto vt+0x9c FUN_10c3b890 [C]
1. FUN_10c3ab80: each library new on this DSP, in reverse order. FUN_10c1cc30:
   - relocate with module = NULL;
   - upload pmco, dmda, exda, exit, inda, init;
   - **`sysmsg 7 (a = lib init base, b = 0)`**, gated on board flag +8 & 0x40 (set on Pulsar2 [L]);
   - drop the library's init/inda references (flags |= 2 | 4).
2. FUN_10c1bea0(brd, dsp, mod, shared): FUN_10c0a760 writes the bases into the sections, then relocates and uploads:
   - `seg_mod`: before upload, mod vt+0x88 FUN_10c179c0 (voice count) and FUN_10c1ab90 patch preset values at words
     `6+2nA+nS+numIn+i`; our tool uploads the file values;
   - `seg_mod2`, `seg_mod3`, `sram_mod`, then the `seg_id` licence check (host);
   - only if not shared: `seg_pmco`, `seg_exit`, `seg_asyn` (PM) and `seg_dmda`, `seg_dmd2`, `seg_dmd3`, `sram_com` (DM);
   - always: `seg_init` (PM), `seg_inda` (DM), `seg_sync` (PM).
   - PM goes through brd vt+0x10c UploadCode FUN_10c2d310. DM goes through brd vt+0x108 UploadData FUN_10c338b0,
     40-bit words cut to their top 32 bits.
3. `mod+0x50 |= 0x200`. Then **activation**: brd vt+0xa4 FUN_10c1b170 (§3.2).
4. mod vt+0x100 FUN_10c18610: replay remembered ModuleSetValue values.
5. Free the init and inda blocks; `seg_init`/`seg_inda` bases := −1.

**Offline result** (`pulsar_modules.py link P2_AINIT.dsp P2_ANO.dsp CSineR4.dsp --ops`, DSP0; it matches fork-C's model of Sim2k):
```
DSP0 P2_AINIT.dsp   mod=0xC578 sync=- libs=['PINIT.ol'] unresolved=none
DSP0 UploadData 0xC577    1 words  PINIT.ol:seg_dmda
DSP0 UploadCode 0x8111   34 instr  PINIT.ol:seg_exit
DSP0 UploadCode 0x82E0   92 instr  PINIT.ol:seg_init
DSP0 sysmsg 0x7 a=0x82E0 b=0x0  PINIT.ol: run seg_init (sys_callfunction)
DSP0 UploadData 0xC578    8 words  p2ANl:seg_mod
DSP0 UploadCode 0x82E0    6 instr  p2ANl:seg_init
DSP0 sysmsg 0x2 a=0xC578 b=0xC404  p2ANl: run fnInit (sys_addmodule on dummy list _firstmod)
DSP0 P2_ANO.dsp     mod=0xC61D sync=0x8313 libs=['P2_IO.ol'] unresolved=none
DSP0 UploadCode 0x82E0   34 instr  P2_IO.ol:seg_pmco
DSP0 UploadData 0xC580  157 words  P2_IO.ol:seg_dmda
DSP0 UploadCode 0x8302   17 instr  P2_IO.ol:seg_exit
DSP0 UploadCode 0x831E   49 instr  P2_IO.ol:seg_init
DSP0 sysmsg 0x7 a=0x831E b=0x0  P2_IO.ol: run seg_init (sys_callfunction)
DSP0 UploadData 0xC61D    8 words  p2ANo:seg_mod
DSP0 UploadCode 0x8313   11 instr  p2ANo:seg_sync
DSP0 patch 0x831B := 0x8192  p2ANo: chain exit -> ret_sync
DSP0 SetValue   0xC406 = 0x00008313  _firstsync -> p2ANo
DSP0 CSineR4.dsp    mod=0xC625 sync=0x831E libs=[] unresolved=none
DSP0 UploadData 0xC625   17 words  CSINER4:seg_mod
DSP0 UploadCode 0x831E   26 instr  CSINER4:seg_sync
DSP0 patch 0x8335 := 0x8192  CSINER4: chain exit -> ret_sync
DSP0 patch 0x831B := 0x831E  p2ANo: chain exit -> CSINER4
```
(`p2ANl`/`p2ANo`/`CSINER4` are the modules' short names from seg_name.) The P2_ANO + CSineR4 PM pieces reuse
0x82E0.., the init overlay freed after PINIT's init.

---------------------------------------------------------------------------------------------------
## 2. DSP side: module ABI and the sysmsgs used [C, OS disassembly]

**sysMsg block** = OS `sysMsg` (0xC419): +1 = a, +2 = b, +3 = type, written as `[a, b, type]` to sysMsg+1.
Handlers enter with I0 = sysMsg and all end in the ack (except type 0x12).

| type | OS handler | semantics |
|---|---|---|
| 1 | sys_setvalue 0x81F2 | DM[a] = b |
| 2 | sys_addmodule 0x8218 | I0 = a (seg_mod). If mod[1] (fnInit) != 0: **CALL fnInit** (I0 = mod). Then `mod[0] = DM[b]; DM[b] = a` (insert at list position b) |
| 3 | sys_movemodule 0x8224 | `m = DM[a]; DM[a] = m[0]; m[0] = DM[b]; DM[b] = m` (unlink at a, relink at b) |
| 6 | sys_patchinstruction 0x81F6 | a = n, b = v; codeBuf (DM 0xC41D) holds n PM addresses; for each, PX = PM[addr]; **PX1 = v (low 16 bits only)**; PM[addr] = PX. If n > 1 and v < 0xC400 it also patches `jumpNextModule` (harmless). Then dummy writes PM 0x8006/0x8007 + FLUSH CACHE |
| 7 | sys_callfunction 0x8213 | CALL PM a with I0 = b; ack after the RTS |
| 8 | sys_readvalue 0x81E0 | GetValue (b != 0: DM[a + (wclk&1)], the current half of a pair) |
| 9 | sys_loadcode 0x822E | copy b instructions from codeBuf to PM a (3 DM words → 2 instructions). **Not used by Sim2k on pluto** |
| 0xA/0xB | sys_syncmsghead/tail 0x8242 | updateTCBCounter(R5 = a = count, R0 = b = base) |
| 0x11 | sys_clearMem 0x8263 | DM[a .. a+b-1] = 0 |

Notes:
- `codeBuf` is 96 words (0xC41D..0xC47C), so a patch can cover at most 96 addresses (Sim2k limits it to 0x60).
- Never send n = 0 to types 6, 9 or 0x11: the loop counter underflows.
- Patching only the low 16 bits is enough because every 21065L address is below 0x10000.

**seg_mod (module struct)** [C, from both the DSP code and Sim2k's address arithmetic]:
```
+0  next      +1 fnInit (-> seg_init 'init')   +2 fnSync (-> seg_sync 'sync')   +3 fnAsync (0 = none)
+4  reserved / 'changed' (module-internal, Sim2k never writes it [L])
+5+2k  asyncOut[k].value         +6+2k  asyncOut[k].exports = (n << 20) | headerListAddr     k < numAsyncOut
+5+2nA+j  syncOut[j]   (numSyncOut words + 1 spare: 'syncOutReservedForDoubleBuffering')
+6+2nA+nS+i  input[i] = DM address of the source (type-4 reloc to inputN, default _null)
+6+2nA+nS+numIn+i  preset parameter words (FUN_10c1ab90) [L], then private state (CSineMem ...)
```

**Chains** [C]:
- **Async chain**: in the main loop, `os_async` (0x81C8) walks `_firstasync` (0xC405) via word 0. For each node with
  fnAsync != 0 it CALLs it with I0 = module. Primary register set.
- `_firstmod` (0xC404) is never walked; it is a dummy list head for init-only modules.
- **Sync chain**, every word clock (IRQ1 → `os_sync`):
  - Switches to the secondary register set (M5 = 0, M6 = 1, M7 = −1, M13 = 0, M14 = 1, L = 0) and does wclk++,
    **I7 = wclk & 1**.
  - Runs the hook chain (`hookReturnJump` 0x818D; nodes added by `.ol` drivers with `os_addHook` / `os_remHook`).
  - Then `I8 = DM[_firstsync]; JUMP (M13,I8)`. `_firstsync` (0xC406) holds a **PM address**, initially `ret_sync` (0x8192).
  - Each `seg_sync` is straight-line code ending in `JUMP ret_sync (DB)` plus two delay-slot instructions (type-2 reloc).
  - Modules address their own struct with absolute (relocated) addresses; no register points at it.
- **Sync signals are double-buffered 2-word pairs**: written `DM(P, I7) = x`, read `x = DM(P, I7)`. `_null` and
  `fadeBuf` are pairs, and GetValue with b != 0 reads `DM[a + (wclk&1)]`.

Relocated P2_ANO on DSP0 (from `pulsar_modules.py link … --disasm`; inputs unconnected = `_null`, `DM(0xC5D9,I2)`
= P2_IO `_tx_pp1ptr` table):
```
08313  R0 = DM(0x0C400)                       ; wclk
08314  R0 = FEXT R0 BY 0:2
08315  I2 = R0
08316  R3 = 0x00000608
08317  R0 = DM(0xC514,I7)                     ; input0 (patched by connect)
08318  I1 = DM(0xC5D9,I2)                     ; current SPORT0 TX frame of P2_IO.ol
08319  R1 = FEXT R0 BY R3
0831A  R0 = DM(0xC514,I7)                     ; input1
0831B  JUMP 0x08192 (DB)                      ; -> ret_sync, patched to the next module
0831C  R1 = FEXT R0 BY R3, DM(9,I1) = R1      ; left -> TX word 9
0831D  DM(0xA,I1) = R1                        ; right -> TX word 10
```

---------------------------------------------------------------------------------------------------
## 3. Upload in state 2 and activation

### 3.1 UploadCode FUN_10c2d310, state 2 (asm 10c2d60c..10c2d7de) [C]
Pluto vt+0x170 (FUN_10c37210) returns 0, so this is the normal path, not SPI. No sysmsg 9 is used, and IIEP0/CEP0
are not written: the address comes from the frame header and the OS keeps CEP0 = −1.
**10 instructions per chunk**, each chunk one batched FIFO message (`T = dsp << 21`; IOP header =
`((dsp|0x10) << 21) | (0x120000 if batched else 0x100000) | reg`):
```
for off in range(0, n, 10):
    cnt = min(10, n - off)
    send_msg(T | 0x120000, [], batch)                  # batch start
    iop(0x1C, 0x20E0, batch)                           # DMAC0: flush, 48-bit
    iop(0x41, 1, batch)                                # IMEP0 = 1
    iop(0x1C, 0xE1, batch)                             # DMAC0: DEN, 48-bit packing
    words = pairs A,B -> [A47..16, B15..0 << 16 | A15..0, B47..16]; odd last A -> [A47..16, A15..0]
    force_wrap = 1                                     # board+0xac0
    send_msg(((len(words) << 4 | dsp) << 21) | (addr + off), words, batch)   # gets |0x20000000 + pad words
    force_wrap = 0
    iop(0x1C, 0x40, batch)                             # DMAC0: 32-bit
    iop(0x41, 2, batch)                                # IMEP0 = 2 (OS default)
    iop(0x12001C, 0x41)                                # non-batched -> wrapped; closes and sends the batch
```
Implemented in `pulsar_loader.Board.upload_code` (state 2 branch).

### 3.2 UploadData FUN_10c338b0 [C]
`pulsar_loader.Board.upload_data` already matches every state-2 path:
- n < 3: one SetValue per word.
- n ≤ 3 (DAT_10ca0f24 = 3): 14-word batched chunks.
- n > 3: two interleaved frames per round, stride 2 (the OS has IMEP0 = 2).

The one path not in `Board` is the **clearMem shortcut**, which Sim2k checks first: n > 0x20, every byte zero, and
the OS has `sys_clearMem` → `sysmsg(0x11, a = addr, b = n)`. `pulsar_modules.execute()` applies it to "data" ops,
i.e. only in state 2.

### 3.3 Activation, brd vt+0xa4 FUN_10c1b170 [C]
```
if mod has seg_asyn:
    prev = last async module before mod in this DSP's list
    sysmsg(2, a = mod.seg_mod, b = prev.seg_mod if prev else &_firstasync)   # runs fnInit, links into async chain
elif mod has seg_init:
    sysmsg(2, a = mod.seg_mod, b = &_firstmod)                               # only to run fnInit
if mod has seg_sync:                                                         # FUN_10c1b070
    prevS = sync predecessor, nextS = sync successor (the new module is not in the list yet)
    patch(mod's seg_sync reloc sites of 'ret_sync', nextS.entry if nextS else ret_sync)   # FUN_10c1a760
    entry = mod.seg_sync + sym('sync')            # 'sync' = 0 in every module
    if prevS: patch(prevS's 'ret_sync' sites, entry)
    else:     SetValue(_firstsync, entry)          # (dsp+0x14 & 1: set by Run, dsp+0x14 = 3)
```
- **patch(sites, value)** = brd vt+0xe0 FUN_10c1bb40:
  - n ≤ 0x60;
  - `UploadData(dsp, [PM addresses], codeBuf, n)`, where each PM address = section base + reloc offset;
  - `registerPatch` SetValue only if the OS has that symbol (none of puls2os0..5 do);
  - then `sysmsg(6, a = n, b = value)`.
- The sync predecessor is wired last, so the chain is never broken: the new module's exit already points at the
  rest of the chain when it becomes reachable.
- Run (pluto vt+0xd4 FUN_10c3aa50) sets dsp+0x14 = 3, so pluto vt+8 (FUN_10c3be00, initial chain set-up) is a
  no-op afterwards.

### 3.4 Unload, delmodule FUN_10c15aa0 [C]
1. Unlink (brd vt+0xa0 FUN_10c1b310):
   - sync (FUN_10c1b240): `tgt = next sync module's entry or ret_sync`. With no predecessor:
     `SetValue(_firstsync, tgt)`; otherwise `patch(prev 'ret_sync' sites, tgt)`.
   - async: `SetValue(prev.seg_mod or &_firstasync, next.seg_mod or 0)` (plain DM write).
2. Exit (FUN_10c1cbc0): if the module has seg_exit, `sysmsg(7, a = exit addr, b = mod.seg_mod)`.
3. Free (pluto vt+0x98 FUN_10c3b640): dmda, asyn, pmco, exit, mod, sync (shared ones are refcounted). When the last
   user of a library goes: `sysmsg(7, lib exit, 0)`, then free it.

Moving a module: async uses `sysmsg(3, link-before-mod, link-before-target)` (FUN_10c3b400). Sync uses a 3-JUMP
trampoline at `___lib_FPINV`+1 (0x806C on Pulsar2), FUN_10c334f0 / FUN_10c22960; without that symbol it is unlink +
relink. Neither is implemented in the tool.

### 3.5 fadeInOut, brd vt+0xd4 FUN_10c1b430 [C order, L meaning]
This is optional: a click-free re-route of a running module's sync input, enabled by config (DAT_10cab8cc).
OS element `fadeInOut` (0x82D3; os4 0x82EC, os5 0x82D4):
- It computes `fadePos = clip(fadePos + fadeAdd, ±32)` and `out = in · fadeTab[pos+32]`.
- The input instruction at `fadeRead` reads `DM(_null, I7)`, with patchable low 16 bits.
- The output goes to the pair `fadeBuf` (0xC534).
- `fadeJmp` is a patchable `JUMP ret_sync`.

Host sequence:
1. `SetValue(fadeAdd, ±1)`, `SetValue(fadePos, ±0x20)`.
2. Patch `fadeRead` to the source and `fadeJmp` to the module's entry.
3. Point the predecessor (or `_firstsync`) at `fadeInOut`.
4. Set the input word to `fadeBuf` and patch the `inputN` sites to `fadeBuf`.
5. Flip `fadeAdd`.
6. Wait until wclk has advanced 0x40, or 10 ms.
7. Set the input word to the new source, patch `inputN` to it, and restore the chain to the module's entry.

Not implemented; the plain connect in §4.2 is used instead.

---------------------------------------------------------------------------------------------------
## 4. Connections between modules

### 4.1 Pad descriptors (seg_desc / seg_name, FUN_10c017b0, FUN_10c18a90) [C]
**seg_desc** (top 32 bits of each DM word):

| word | content |
|---|---|
| [0] | numIn |
| [1] | numAsyncOut |
| [2] | numSyncOut |
| [3] | syncCycles (= seg_sync length) |
| [4] | asyncCycles |
| [5] | flags (host ORs in 0x40, \|1 if seg_sync, \|2 if seg_asyn) |
| [6+3p .. 8+3p] | pad p: **type**, min, max |

- Pads are numbered inputs first, then async outputs, then sync outputs.
- Type: `type & 0xC000` ≠ 0 means a **sync pad**. Low nibble 1 = scalar, 5 = string. 0x40000000 = host-writable
  output. Typical values: 0x8001 sync, 0x0001 async.
- If flags & 0x01000000, three board words follow (P2_ANO/ANI `plateflags1/2`).
- `.ol` libraries have a 3-word seg_desc: syncCycles, asyncCycles, flags.

**seg_name**: one character per DM word (low byte), NUL-separated. String 0 = short module name, 1 = long name;
pad p has 2p+2 = short, 2p+3 = long.

`ModuleClass.pads` in pulsar_modules.py parses both.

### 4.2 Linking an input: FUN_10c1b8f0 (brd vt+0xd8) [C, asm]
```
link_input(dst, i, addr):          # addr = DM address of the source pair/word, 0 -> default
    if addr == 0: addr = dst-local symbol 'inputN' if defined, else the seg_mod default (= OS _null)
    SetValue(dsp, dst.seg_mod + 6 + 2nA + nS + i, addr)                 # async code reads via this word
    patch(dst's seg_sync reloc sites of 'input%d' % i, addr)             # sync code reads DM(addr, I7)
```
- Only `seg_sync` is patched; `seg_asyn` reads through the pointer word (e.g. MIXGAIN: `I1 = DM(input+1, I0); R15 = DM(0, I1)`).
- If the connection is made **before** the module is uploaded, the same address is simply used at relocation time
  (pin path of §1.3), with no SetValue or patch.
- Sim2k optionally waits a few word clocks after a sync connect ("waitInSyncConnect", FUN_10c2de60).

### 4.3 Source address per connection kind: receiver vt+0xb0 FUN_10c17ad0 [C]

| source | address given to the input |
|---|---|
| same DSP, async out k | `src.seg_mod + 5 + 2k` |
| same DSP, sync out j | src vt+0x80: the allocated slot if any, else the local word `src.seg_mod + 5 + 2nA + j` |
| other DSP, sync out j | slot from **allocSyncOutput** on the source DSP |
| other DSP, async out k | a DM word on the receiver plus an export header on the source (§4.5) |

Overlap of the local syncOut words: the area has only nS + 1 words, so output j's second half is output j+1's first
half. Within one word clock writer and reader use the same I7, so a reader placed later in the chain still gets the
right value. Sim2k nevertheless gives connected sync outputs a slot. The pin path appears to allocate one even for
same-DSP sync connections [L]; our `Rack.connect` does that only for cross-DSP connections or with `slot=True`.

### 4.4 allocSyncOutput FUN_10c2e070 (pluto FUN_10c38900 append / 38a00 prepend / 38b30, 38c90 shrink) [C]
**Each DSP's EPB1 sync block** is DMA'd out every word clock by `os_sync`:
- Base `0xC080 + 0x20·d`; count = 3 + numCommSlots (3 after our boot's `tcb_init`).
- **Slot k = base + 4 + 2k** (a 2-word pair, selected by I7).
- Layout (updateTCBCounter): base+0/1 = 0; base+2/3 = bus headers (len = count−3, `cmdMask` 0xC0000000 = broadcast
  [L]) targeting base+4/base+5; the slots; EOM at base + 2·count.
- Receivers DMA with IMEP0 = 2, so **a slot address is the same DM address on every DSP** [L]. A module on any DSP
  reads it as `DM(slot, I7)`.

Growing and shrinking the block:
- **Append**: `count += 1; slot = base − 4 + 2·count; sysmsg(d, 0xB, a = count, b = base)`.
- **Prepend**: `count += 1; base −= 2; slot = base + 4; sysmsg(d, 0xA, count, base)`.
- Shrinking is the reverse.
- If a block would collide with the next DSP's block, FUN_10c38e00/38ea0 → moveSyncOutput FUN_10c34230 shift the
  neighbours (limit `board+0x12e0` = 0xC300). On failure: "Required audio transfer bandwidth between DSPs too high."
  Usage counter at `[slot − 0xC000]`.

Wiring the slot into the source module:
- FUN_10c346a0 stores the slot in `mod+0x1c[j]` and re-addresses the source's syncOut writes (the 0xAF type-3 sites)
  to it. For a module that is already running, Sim2k uses the OS `copySyncOut` element (0x81B1) to move without a
  glitch.
- Our tool allocates the slot before linking (no patch needed), or plain-patches the write sites.
- No host SRAM slot table (BAR+0x80000/0x80800) is touched here; that belongs to pc↔dsp streaming.

allocSyncInput FUN_10c2e250 is used only for host-sourced sync inputs (PC window 0xC300..0xC3FF, board+8 & 4).

### 4.5 Cross-DSP async: FUN_10c15d60 → pluto vt+0xa8 FUN_10c3b440 [C]
1. The receiver gets a DM word from its heap (pluto vt+0xac FUN_10c385d0).
2. Export header = `0x20000000 | (dst & 0xF) << 21 | (dst & 0x10) << 14 | slot`. FUN_10c2e420, then FUN_10c1bca0
   puts the board index in bits 25..28 (0 here). A host destination is `0x61E00000 | addr`.
3. The current value is copied: GetValue(src.seg_mod + 5 + 2k), then SetValue on the receiver (not done by our tool).
4. `UploadData(src dsp, headers, listAddr, n)`, then `SetValue(src.seg_mod + 6 + 2k, (n << 20) | listAddr)`.
   n = 0 means `SetValue(…, 0)`.

On the DSP, the async code calls `os_sendmsgPX2` with PX2 = value and R7 = export word. For each header it either
writes locally (own DSP) or queues a 10-word frame that leaves in that DSP's token slot (dsp_boot_analysis §4.3).

---------------------------------------------------------------------------------------------------
## 5. Parameters [C]
- **Async input pads** carry most parameters (gain, frequency, …): SetInPad @10c04c80 → FUN_10c4cb70 → FUN_10c45bf0.
  - Ignored while the input is wired to a module output.
  - If the pad has no host value slot yet, FUN_10c4c520 allocates one on the module's DSP (1 word async, n+1
    multiword, **2 words for a sync pad**), sleeps 2 ms and links the input to it (§4.2).
  - Then `SetValue(dsp, slot, value)`, plus `SetValue(slot+1, value)` for a sync pad.
  - Size > 1: `UploadData(addr+1, data)`, then `SetValue(addr, addr+1)`.
  - Values are the raw 32-bit pad value; float/fixed scaling happens in the caller (base.dll / device), not in Sim2k.
  - Pad min/max come from seg_desc [L].
- **SetOutPad** @10c04ed0 (pad type & 0x40000000), pluto vt+0xcc FUN_10c37db0: `SetValue(seg_mod + 5 + 2k, value)`.
- **ModuleSetValue** @10c050e0 → mod vt+0xf8 FUN_10c18fc0:
  - The symbol must be EXT and in a DM section of the module.
  - `addr = section base + (sym.value − vaddr) + index`, then a plain SetValue `((dsp|0x10) << 21) | addr, [value]`.
  - The value is remembered and replayed on reload. ModuleGetValue mirrors it with GetValue.
  - Rarely used (e.g. `delayCorrection`).
- The `changed` word (seg_mod+4) is never written by the host [L].

---------------------------------------------------------------------------------------------------
## 6. Implementation: tools/pulsar_modules.py
- `PlutoDsp(os_image, dspno)`: per-DSP linker state (heaps with the OS reserved, libraries, chain order, sync block).
  - `load(path, after=None)` → `(module, ops)`: allocate, link, upload, init, chain hook.
  - `unload(mod)`.
  - `link_input(mod, i, addr)`.
  - `alloc_sync_output(mod, j)`.
- `Rack()`: the six DSPs.
  - `load(path, dsp)`.
  - `connect(src, out_pad, dst, in_pad)`: out_pad counts async outputs first.
  - `disconnect`.
  - `set_in_pad(mod, i, value)`, `set_out_pad`, `module_set_value(mod, sym, value, idx)`.
- Ops are plain tuples (`code`, `data`, `set`, `sysmsg`, `patch`). `execute(board, ops)` replays them with
  `pulsar_loader.Board` (DSPs in state 2). `format_ops(ops)` prints them. The module itself never touches hardware.
- CLI:
  - `link FILES --dsp N [--ops] [--disasm]`: listing without opcode bytes; `--raw` adds them, for local use only.
  - `selftest`.
  - `dryrun --dsp N --log FILE`: CSineR4 → P2_ANO executed against `pulsar_loader.SimBar`, logging every BAR write.

Usage after `pulsar_loader.py boot` (same process, Board `b` with `b.syms` set):
```python
import pulsar_modules as pm
rack = pm.Rack()
_, ops  = rack.load("P2_AINIT.dsp path", 0)          # PINIT.ol is pulled in automatically
ano, o  = rack.load(".../P2_ANO.dsp", 0); ops += o   # P2_IO.ol (SPORT0 driver) is pulled in
sine, o = rack.load(".../CSineR4.dsp", 0); ops += o
ops += rack.connect(sine, 0, ano, 0) + rack.connect(sine, 1, ano, 1)
ops += rack.set_in_pad(sine, 0, phase_increment)     # raw 32-bit, scaling [?]
pm.execute(b, ops)
```

**Offline validation** (`pulsar_modules.py selftest`):
- P2_AINIT, P2_ANO and CSineR4 link against **all six puls2os images with zero unresolved symbols** (libraries
  PINIT.ol and P2_IO.ol pulled in).
- All 43 static branch/call targets of the relocated code land in OS code or in uploaded code: JUMP 0x8192 =
  `ret_sync`, CALL 0x826C = `os_sendmsgPX2`, library-internal CALLs inside the library, and P2_IO init patching the
  SPORT0 vector 0x8028 with a JUMP to its handler.
- Data references check out: P2_AINIT reads `InitPPlateAnalog` at PINIT's dmda, and P2_ANO reads `_tx_pp1ptr` in
  P2_IO's dmda.
- `dryrun` builds every frame (state-2 UploadCode batches, interleaved UploadData, codeBuf + sysmsg 6, sysmsg 2/7/0xB).
- Fork C's corpus run: 1062 of 1117 `.dsp` modules link on puls2os0. Of the rest, 12 `uc*` MIDI modules need puls2os5,
  and 43 belong to other boards (Pulsar1 `PULS_*`, xite/satellite, TCDEST/midi2pc).

**Not implemented / open:**
- Moving neighbour sync blocks when a DSP's block is full (≈13 slots per DSP).
- `copySyncOut` live re-addressing; moving modules (sysmsg 3 / FPINV trampoline); fadeInOut.
- Polyphonic `VoiceDef` substitution; mod2/mod3/dmd2/dmd3 heaps (not used on Pulsar2 [L]).
- Preset parameter words (FUN_10c1ab90); copying the current value on a cross-DSP async connect.
- [L] The cross-DSP slot broadcast (same DM address on every DSP) and the board-index bits of export headers are
  inferred, not yet seen on hardware.
- [?] The scaling of CSineR4's `f` input (phase increment per sample) for a given frequency.
