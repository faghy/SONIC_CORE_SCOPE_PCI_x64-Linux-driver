# SCOPE object files (.io / .dev / .mdl / .pro) and the P-Plate analog I/O modules

What is inside SCOPE's hardware-I/O device files (`App/Application/IOs/Hardware/*.io`), devices (`Devices/**/*.dev`),
modules (`*.mdl`) and projects (`*.pro`); how they bind to DSP modules; what the P-Plate analog I/O DSP modules
do; and the minimal module set for a first audible test (sine → analog out 1/2).

Tool: `tools/scope_dev.py` (Python 3, stdlib only) decodes the container and dumps the object tree.

Legend: **[C]** = read from the code, **[L]** = likely, **[?]** = guess. `FUN_x` = function at address x in the
named DLL (`decompiled/*.c`; Ghidra 12.1.4, image bases 0x10000000 base.dll, 0x10200000 cwWindows.dll,
0x10300000 wxvc.dll, 0x10c00000 Sim2k.dll). Related: `module_loading.md` (Sim2k linker/loader),
`sc_format.md` (scrambled COFF DSP files), `clock_rate.md`, `dsp_boot_analysis.md`.

---------------------------------------------------------------------------------------------------
## 0. TL;DR

1. **Container [C]**: `S3` files are gzip files written by a patched zlib 1.1.3 (statically linked in
   `wxvc.dll`): magic `53 33` instead of `1f 8b`, method 7, and a *scrambled* deflate stream (zlib header bytes
   XOR 0x21/0x63, 3 junk bits in front of every deflate block header). The payload is a cwArchive (MFC CArchive
   clone) stream: 128-byte `ScopeFileHeader`, then one serialized object tree. All 899 S3 files of the install
   decompress with matching adler32, CRC32 and ISIZE, and all 741 object archives parse to the last byte (§6).
2. **Object tree [C]**: hand-written per-class `Serialize()` methods in `base.dll` (+ PepBase plug-ins for GUI
   classes). Audio-relevant classes: `RODModule`/`RODBase` (a device or sub-module), `ROCAtom` (binding to one DSP
   module **by name**), `ROCAtomPad`/`ROCPad` (the atom's pads), `RODPad` (the device's visible pad, linked to an
   atom pad), `RODRouting` (a wire between two `RODPad`s), `ROCParameter`/`RODParameter`/`ROCParamList`
   (parameters), `<RefModule>` (a child loaded from another file, e.g. a mixer `.dev` inside a project).
   GUI objects (`GO*`, bitmaps) are skipped by `scope_dev.py` (§2.5).
3. **P-Plate Analog Dest.io [C]** = device `P-Plate Analog Dest` with atom `P-Plate Analog Dest` (= DSP module
   **P2_ANO.dsp**, matched by its seg_name ModuleNameLong), two **sync input** pads `LIn`/`RIn`, and a child
   device `P-Plate Analog Init Loader` (= **P2_AINIT.dsp**, async output `Prof`). No parameters, no gain.
   `P-Plate Analog Source.io` is the same with **P2_ANI.dsp** and sync outputs `LOut`/`ROut`.
   Libraries are pulled by symbol: P2_ANO/P2_ANI need **P2_IO.ol** (SPORT0 driver), P2_AINIT needs **PINIT.ol**
   (codec register init via DSP flag pins).
4. **Fixed DSP [C]**: module `flags` bits 17..20 hold *fixed DSP index + 1* (Sim2k `FUN_10c260b0`).
   P2_ANO/P2_ANI and all P-Plate ADAT/S-Mux/SPDIF audio modules: **DSP1**. P2_AINIT and the P-Plate MIDI modules:
   **DSP0**. ADAT timecode modules: DSP4. The .io files themselves leave the placement free (`dsp = -1`).
5. **Sine test [C/L]**: `CSineR4.dsp` ("Complex Sine Oscillator R4"): one sync input `f` (phase increment per
   sample, 2^32 = one cycle), two sync outputs `Cout`/`Sout` (full-scale 1.31 fixed). Minimal set:
   P2_AINIT (+PINIT.ol) on DSP0; P2_ANO (+P2_IO.ol) and CSineR4 on **DSP1**; `f` = round(F/fs·2^32)
   (1 kHz @ 44.1 kHz = 0x05CE13BD), `Cout → LIn`, `Sout → RIn` (§5.3).

---------------------------------------------------------------------------------------------------
## 1. Container: the "S3" file [C]

Call chain when SCOPE opens a device/project: `base.dll` `GOContainer::LoadChild` @1004cf20 /
`LoadSerializedFile` / `RODModule::LoadPresetFromFile` @10055050 → `wxGzipFileInputStream(name)`
(cwWindows @1023a0a0: `_open`, `gzdopen(fd, "rb")`) → `gzread` (wxvc.dll @10311460) → `cwArchive`.

### 1.1 gzip header (wxvc.dll `gz_open` FUN_10311100, `check_header` FUN_10310930)

| off | bytes | meaning |
|---|---|---|
| 0 | `53 33` | magic, table DAT_10481534 (the standard `1f 8b` table DAT_1048152c is also accepted) |
| 2 | `07` | method. `check_header` requires 7 after the "S3" magic and 8 after `1f 8b` |
| 3 | `00` | flags (FEXTRA/FNAME/FCOMMENT/FHCRC handled as in stock zlib; bits 5-7 must be 0) |
| 4 | `65 31 97 55 00 0b` | constant "mtime/xfl/os" bytes written by `gz_open` (`fprintf("%c…", …, 0x65,0x31,0x97,0x55,0,0xb)`) |
| 10 | … | compressed stream (below) |
| end-8 | u32 LE | CRC32 of the plain data |
| end-4 | u32 LE | plain size |

Opening mode `"wbc"`/`"rbc"`: the `c` flag selects method 7 and `inflateInit2(+15)`, i.e. a **zlib-wrapped** stream
(2-byte header + deflate + big-endian adler32), whereas method 8 uses raw deflate (`-15`) like stock gzip.
Two files in `App/Application` are plain gzip (`1f 8b 08`); `scope_dev.py` handles both.

### 1.2 Scrambled deflate (wxvc.dll `inflate` @10313150, `inflate_blocks` FUN_103119a0)

- `inflate()` METHOD state: if `(byte0 & 0x0f) != 8` the stream is marked *scrambled*
  (`z->state->[1] = 1`) and the byte is XORed with **0x21**; FLAG state XORs the second byte with **0x63**.
  Real files: `59 ff` → `78 9c`.
- `inflate_blocks()` TYPE state: when scrambled, **3 extra bits are consumed before every block header**
  (BFINAL+BTYPE). Everything else (Huffman tables, stored blocks, window, adler32) is stock deflate.
- Python's `zlib` cannot skip bits, so `scope_dev.py` contains a small table-driven inflater
  (`inflate_s3`, ~2 s for the largest 47 MB plain file).

### 1.3 cwArchive primitives (cwWindows.dll) [C]

Little-endian `u8/u16/u32/f32/f64`. Strings (`operator>>(wxString&)` @1023f770, `ReadStringLength` @1023f6e0):
`u8 n`; `0xff` → `u16 n`; `0xffff` → `u32 n`; `0xfffe` → UTF-16 string follows (length read again). No NUL.
Object references are **paths of object ids** (`RODModule::LoadReferenceID` @10016900): u32 words, bit 31 set =
"more follows"; `0` = null.

---------------------------------------------------------------------------------------------------
## 2. Archive layout [C]

### 2.1 ScopeFileHeader (base.dll `ScopeFileHeader::Serialize` @1002f450, `CheckHeader` @10008580)

128 bytes: 0x28 bytes magic text (`"Creamware Scope File\r\n\x1a@102106109"` = format 1, or
`"SONICCORE SCOPE File…"` = format 5), 14 × u32 (first = version `0x10000`/`0x10001`), 0x1c bytes, u32 checksum
(`CalculateChecksum` @100083c0: sum and rotate-xor over bytes 0..0x7b; 0 = unchecked). `scope_dev.py` verifies it.

### 2.2 Top level

`string class` (e.g. `RODBase`, `RODModule`) + that object's body (§2.3), then the
**reference-parameter trailer** written by `RODModule::SaveReferenceParams` @10076260 and read by
`ROCPad::LoadReference` @1007e320: records until **u32 0xfffffffd** (-3). The trailer is not decoded (it restores
values of parameters inside referenced modules); `.io` files have an empty trailer.

A child object is always written as `string class` + body; the parent reads the class name, creates the object
with `RIOObject::CreateRIOObject` (type table filled at run time by `RegisterRIOObject`, ids 0x157c..0x1643) and calls
its virtual `Serialize`.

### 2.3 Common object headers

**RIOObject** (`RIOObject::Serialize` @100822f0) — every object starts with:
```
u32    id            object id, unique per file, used by references (0 for ROCAtom/routings)
string pep           Pep plug-in class ("PepAtom", "App@IOLoader", "PepNGo"...; "" = none)
string name
u32    nvars                                 variables = pads/properties of an algo
nvars × { u32 named; [string varname if named]; u32 0xffffd8f1 (-9999); string class; body }
extras                                       (below)
```
**Extras** (`RIOObject::ReadExtraBytes` @10012b20, writer `WriteExtraBytesLength` @10012c30):
`u32 tag`: 0 = none; ≥ 8 = byte size of a chunk list `{u32 key, u32 len, len bytes}` terminated by key 0
(0x1e is the fixed tag of one 0x104 chunk); 2/4/0x16 and 1/3/5-7 are legacy fixed-size forms. Keys seen:
0x102 numVoices, 0x105 module key (copy protection), 0x106 GUI, 0x107 boardID, 0x108 PepAtom flag,
0x10e ?, **0x10f DSP placement** (§2.4), 0x114 param-list rect, 0x118 parameter converter.

**RIOVar** (`RIOVar::Serialize` @10082fb0): RIOObject + `u32 flag, u32 hasParam, [ROCParam], extras`.

**ROCParam** (`ROCParam::Serialize` @1007dc90): `u32 size, size bytes value, string type` (`"Long"`, `"String"`,
`"Double"`, `"MIDI"`, `"PClass"`, `"PROCParam"` = array), attribute list, and for `PROCParam` a `u32 count` followed by
`count` nested ROCParams. Attribute list (`FUN_10008ac0`): repeated `u32 type (0..0x29), u32 0, value, u32 0`,
terminated by -1. The value kind of every type was read from the exported vtables of `base.dll` (slot 0x34 =
`SerializeRead` of the Long/Short/Float/Double/Bool/String base class) [C]:

| type | name | kind | | type | name | kind |
|---|---|---|---|---|---|---|
| 0 | Long | i32 | | 0x0d | **Sync** (sync pad) | u8 |
| 1 | Short | u16 | | 0x0e..0x19 | Prop* (name, value, category, …) | string |
| 2 | Float | f32 | | 0x1a | NotifyPad | u8 |
| 3 | Double | f64 | | 0x1b | RestorePad | u16 |
| 4 | String | string | | 0x1c | Unit | u16 |
| 5 | Bool | u8 | | 0x1d | DynamicPad | u8 |
| 6 | DataType | u16 | | 0x1e/0x1f | NumVoices/VoiceOffset | u16 |
| 7 | DataSize | u16 | | 0x20/0x21 | BoardId/DSPId | u16 |
| 8 | DataElements | u16 | | 0x22/0x23 | OnSameDSP/ModularClass | i32 |
| 9 / 0x0a | RangeMin / RangeMax | f64 | | 0x24..0x27 | IgnoreType, RestorePadInSerialize, HiddenRouting, RestoreObject | u8 |
| 0x0c | Format | string | | 0x28 / 0x29 | SlotID / ModuleID | i32 / string |

### 2.4 Classes

| class | body after the RIOObject header | function |
|---|---|---|
| `ROCAlgo` | nothing (vars = the algo's pads/properties) | @10097980 |
| `ROCAtom` | `string module` (DSP module name), `string`, `u32` | @100bdbe0 |
| `ROCPad`, `ROCAtomPad` | RIOVar part + `u32 padflags` (low nibble 1 = module input, 2 = output; 0x20 dynamic) + extras | @10083270 |
| `ROCParameter` | `u8 hasRef [ref], u8, u8, f64 min, f64 max, f64 value, u8, u32, u32, u8, u32, u8, u32 flags [string if bit31], 16 bytes, ROCParam, extras` | @100858d0 |
| `RODParameter` | `u8 hasRef [ref], u16 n, n × child, extras` | @10085650 |
| `ROCParamList` | `u32 n, n × {u32 hasRef [ref], u32 size, bytes}, extras` | @10083320 |

**RODObject** (`RODObject::Load` @10087250), base of the "ROD" classes: RIOObject header, 16 bytes,
`u32 nviews` + `nviews × (u32 oldViewID, u32 index)`, `string guiClass` + GUI object (`GOContainer` tree,
"" = none), extras. Then:

**RODModule / RODBase** (`RODModule::Serialize` @100c27c0):
```
u32 algoFlag        0 = no algo; 1; 4 = 1 + flags word follows later
[string class + algo]                    ROCAtom (DSP module) or ROCAlgo (host-side logic)
u32 nChildren × { u32 k; k == 0: u32 index, string class, child module
                         k != 0: RefModule = SerializeRefInformation (@100bee90):
                                 u32 1, u32 size, blob{string path, u32 index, string class, ROCParamList,
                                 8 bytes position, u32 n, n × object} }
u32 nPads     × { u32, string "RODPad", RODPad }
[u32 modflags]                           if algoFlag > 1
u32 nParams   × { u32, string class, object, u32 isMain }
u32 nRoutings × { string "RODRouting", RODRouting }
extras                                    (0x107 boardID, 0x10f DSP placement, ...)
```
**RODPad** (@100b6bf0): RODObject + `i32 link` (−1 = none, else 1+type nibble) + `ref → ROCPad/ROCAtomPad` +
`u8 flags` + extras.

**RODRouting** (`SerializeRouting` @100ae620): RODObject + `u32 0, u32 0, ref padA, ref padB` + extras. A wire
between two `RODPad`s (the order is not source→dest: the direction follows from the pads, e.g. `In ← Out`).

**References**: a single id = an object of this file. A path `a|0x80000000, …, m` = object `a` inside the child
module whose `index` is `m` (that child is usually a `<RefModule>` loaded from another file, whose own ids are
`a`). Example (`Wave EZ Playback.pro`): `->6a6c.13` = pad id 0x6a6c (`In7`) of `STM 1632.dev`, loaded as child
index 0x13.

**DSP placement**, extras key 0x10f (`RODModule::SaveDSPPlacement` @10056320): records `u8 0, ref module,
u32 n, n × i32 dsp` (−1 = free) or `u8 1, ref module` (module flag 0x10), terminated by `u8 0xff`.

### 2.5 GUI objects and how scope_dev.py skips them [C for formats, heuristic for the skip]

`GOContainer::Load` @10087920 (RIOObject header, BlitData, bitmaps from cwWindows `BitmapBase`, child GO list,
extras) and ~30 GO/Pep display classes partly implemented in `PepBase/*.dll`. They carry no audio information,
so `scope_dev.py` does not decode them. After the GUI class name it looks for the next `ROD*/ROC*` class-name
record (the anchor) and tries start offsets up to 96 bytes in front of it, running the *rest of the enclosing
object* from each (`Parser.skip_gui`). Offsets right after a GOContainer end marker (extras chunk 0x106: tag 0xc,
key 0x106, len 4, value, 0) are tried first. Passes, in order: (1) accept only a parse that reads the anchor as a
class name (strict = legacy extras tags rejected, then lenient); (2) accept the parse that leaves the fewest bytes
of small u32 counters before the anchor (pads and routings end just before their parent's next record);
(3) for the last GUI object of a file, scan the whole tail (projects end with a long §2.2 trailer); (4) try later
anchors. While resyncing, pad links and routings must point to ids already defined (multi-element paths into
referenced files are only checked for plausibility). Results are memoized per (offset, pass, ids-defined-so-far),
which keeps the 28-47 MB mixer files at seconds instead of minutes. Correctness check: the parse must end exactly
at EOF (or at the §2.2 trailer that ends with -3), see §6.

---------------------------------------------------------------------------------------------------
## 3. The P-Plate analog devices [C]

`scope_dev.py --summary "P-Plate Analog Dest.io"` (ids are per file):
```
MODULE /P-Plate Analog Dest  (RODBase #1426)  atom=ROCAtom module='P-Plate Analog Dest'
   ATOMPAD LIn  #1427 dir=in type=Long          ROCParam Long, RangeMin -2^31, RangeMax 2^31-1, Sync=1
   ATOMPAD RIn  #1428 dir=in type=Long          (same)
MODULE /P-Plate Analog Dest/P-Plate Analog Init Loader  (RODBase #1429, child index 1)
        atom=ROCAtom module='P-Plate Analog Init Loader'   pep=PepAtom (extras 0x108)
   ATOMPAD Prof #142a dir=out type=Long         async (no Sync attribute)
   PAD .../Prof #142b link=->142a
   PAD /P-Plate Analog Dest/LIn #142c link=->1427
   PAD /P-Plate Analog Dest/RIn #142d link=->1428
extras: 0x107 boardID = 0; 0x10f placement: module 1426 dsp [-1], module 1429 dsp [-1]
```
`P-Plate Analog Source.io`: identical structure, atom `P-Plate Analog Source`, pads `LOut` #142f / `ROut` #1430
(`dir=out`, Sync=1), same Init Loader child.

Contents in words:
- **DSP modules**: the atom name is looked up in Sim2k's module registry, which is keyed by the two seg_name
  strings of every `.dsp` in `App/Dsp` (FUN_10c03390 compares class+0x18/+0x1c; duplicate check FUN_10c03450
  "modules %s and %s are both registered under '%s'") [C]. `P-Plate Analog Dest` = **P2_ANO.dsp**
  (short `p2ANo`), `P-Plate Analog Source` = **P2_ANI.dsp** (`p2ANi`), `P-Plate Analog Init Loader` =
  **P2_AINIT.dsp** (`p2ANl`). No file name appears anywhere in the .io file. Cross-check: the 144 `ROCAtom`s
  of all 124 `.io` files resolve to exactly one `.dsp` each by this rule (`scope_dev.py --summary --dsp App/Dsp`).
- **Init modules**: the Init Loader child is part of each device, so opening either device loads P2_AINIT, whose
  external `InitPPlateAnalog` pulls **PINIT.ol**; P2_ANO/P2_ANI reference `_tx_pp1ptr`/`_rx_pp1ptr` and pull
  **P2_IO.ol** (library resolution: `module_loading.md` §1.3). P2_ANO/P2_ANI carry flag 0x20000000 = one
  instance per board (FUN_10c1bd60/FUN_10c1be30 "Module of this type already loaded"); P2_AINIT does not.
- **Pads**: Dest has 2 sync inputs, Source 2 sync outputs (all type 0x8001 = sync scalar, range ±2^31 in the
  DSP descriptor); the Init Loader has one async output `Prof` (type 1).
- **Parameters**: none — no ROCParameter/RODParameter, no gain or level control. The codec levels are fixed by
  PINIT.ol (§4.3).
- **DSP constraint**: the .io files leave placement free (−1); the constraint comes from the DSP modules'
  `flags` word (§4.1): P2_ANO/P2_ANI must run on **DSP1**, P2_AINIT on **DSP0**.

Older projects (2002, `Projects/Examples/*.pro`) do not embed the .io device; they contain a `RODModule`
"Scope Analog Dest" whose algo is `ROCAlgo` pep=`App@IOLoader` with string/long properties
`OrgName = "Scope P-Plate Analog Dest"`, `Board = 1`, `Plate = 2`, `Type = 18`, `BoardId = 0` and dynamic sync pads
`LIn`/`RIn` [C]; the loader then instantiates the matching hardware device [L].

---------------------------------------------------------------------------------------------------
## 4. The DSP modules (sc_decode.py --dump -v, sharc_dis.py)

### 4.1 Descriptor flags and the fixed DSP [C]

Module class flags = seg_desc word 5 (Sim2k class+0x48, parsed in FUN_10c017b0):

| bit(s) | meaning | evidence |
|---|---|---|
| 0x20000000 | only one instance per board | FUN_10c1bd60 / FUN_10c1be30 |
| 0x01000000 | 2-3 extra seg_desc words follow the pads (`plateflags1/2`, `bp1/bp2_flags`) | FUN_10c017b0 |
| 0x001E0000 | `((flags >> 17) & 0xF) - 1` = **fixed DSP index**; 0xF = choose via plate; 0 = free | FUN_10c260b0 (returns that index `| board*8`) |
| 0x40000000 | returns DSP id 0xE (special) | FUN_10c260b0 |

| module | ModuleNameLong | flags | DSP | pads (sync S / async A) | plate words |
|---|---|---|---|---|---|
| P2_ANO.dsp | P-Plate Analog Dest | 0x21050000 | 1 | in: LIn, RIn (S) | 0xc, 0 |
| P2_ANI.dsp | P-Plate Analog Source | 0x21050000 | 1 | out: LOut, ROut (S) | 0xc, 0 |
| P2_AINIT.dsp | P-Plate Analog Init Loader | 0x01030000 | 0 | out: Prof (A) | 0x2000c0c, 0x8000000 |
| P2_DAO/DBO, P2_DAI/DBI | P-Plate ADAT A/B Dest/Source | 0x21050000 | 1 | 8 × S | 0xc, 0 |
| P2_SAO/SBO, P2_SAI/SBI | P-Plate S/Mux A/B Dest/Source | 0x21050000 | 1 | 4 × S | 0xc, 0 |
| P2_SPO / P2_SPI | P-Plate SP-DIF Dest/Source | 0x21050000 | 1 | 2 × S | 0xc, 0 |
| P2_MOU / P2MID_IN | P-Plate Midi Dest / P2 MIDI Input | 0x21030000 | 0 | 1 × A | 0xc / 0xc0c, 0 |
| p2tcread / p2tcdest | PII ADAT Timecode Reader/Dest | 0x000a0000 / 0x200b0000 | 4 | | – |
| PULS_DAC / PULS_ADC (Pulsar 1) | Pulsar analog out/in | 0x20850000 | 1 | 2 × S | – |
| CSineR4.dsp | Complex Sine Oscillator R4 | 0 | free | in f; out Cout, Sout (S) | – |

`plateflags1 = 0xc` is most likely a mask of compatible backplate ids (bit n = backplateID n; the P-Plate is
backplateID 2, `clock_rate.md`) [?]. DSP1 being the SPORT0/P-Plate DSP agrees with `dsp_boot_analysis.md`
(no SPORT/timer use by the OS on DSP0-3, sync-plate timer on DSP4, µC on DSP5) and with the timecode modules
being fixed to DSP4 [L]. **Consequence for the Linux loader: P2_ANO, P2_ANI, P2_IO.ol and their sync partners
must be linked on DSP1, not DSP0.**

### 4.2 P2_IO.ol — SPORT0 TDM driver (library, seg_init/seg_exit/seg_pmco/seg_dmda) [C, meanings L]

seg_dmda: `_tx_pp1` (0x2c words = 4 frames × 11), `_tx_pp2` (4 × 11), `_rx_pp1`/`_rx_pp2` (2 × 11 each), and
pointer tables `_tx_pp1buf/_tx_pp1ptr` (frame base addresses, 4 entries), `_tx_pp2buf/_tx_pp2ptr/_tx_pp2clr`,
`_rx_pp1ptr/_rx_pp1buf`, `_rx_pp2ptr/_rx_pp2buf` (2 alternating entries), `pp2_fixSPDIF`, `pp_testval`.
"pp1"/"pp2" = SPORT0 channel A / B (two data lines to the plate). Externals: `spr0_svc` (OS SPORT0-receive vector),
`imaskIRQ` (OS copy of IMASK), `wclk` (OS sample counter).

seg_init (run once by sysmsg 7):
```
BIT CLR IMASK {SPR0I SPT0I}
SRCTL0 = STCTL0 = MTCCS0 = MRCCS0 = MRCS0 = MTCS0 = 0      ; SPORT0 off, no multichannel mode
IIT0A = _tx_pp1, IIT0B = _tx_pp2, IIR0A = _rx_pp1, IIR0B = _rx_pp2   ; DMA chains, modify 1
CT0A = CT0B = 1, CR0A = CR0B = 8
copy 3 instructions (jmp_spr0) over the spr0_svc vector: JUMP spr0_asserted (DB); save ASTAT; save I2
SRCTL0 = STCTL0 = 0x01140171                                ; enable
IMASK |= SPR0I, imaskIRQ |= 0x400 ; MODE1 |= IRPTEN
```
`0x01140171` (ADSP-21065L SPORT control): SPEN_A + SPEN_B, SLEN = 23 → **24-bit words**, DMA enabled on both
channels, external clock and frame sync (the plate is the clock master) [L].

seg_pmco `spr0_asserted` (the SPORT0 receive interrupt, every sample): `I2 = wclk & 3`; disable TX; re-arm
`IIT0A = _tx_pp1buf[I2]`, `IIT0B = _tx_pp2buf[I2]`, `CT0x = 11`; re-enable; same for RX with
`_rx_pp1ptr[I2]`/`_rx_pp2ptr[I2]`, `CR0x = 11`; clear word 8 of `_tx_pp2clr[I2]`; RTI. So every sample period
SPORT0 moves one **11-slot frame per channel** in each direction.

seg_exit: SPORT0 off, IMASK/imaskIRQ cleared; if `pp2_fixSPDIF` ≠ 0 it leaves channel B transmitting a fixed
pattern (STCTL0 = 0x010001F0, TX0_B = 0x55CC71E3) [L: keeps the SPDIF receiver locked].

Frame slot map (from the sync code of all P2 I/O modules) [C]:

| SPORT0 channel | slots 0-7 | slot 8 | slots 9-10 |
|---|---|---|---|
| A (`pp1`) | ADAT A 1-8 (S/Mux A uses 0-3) | – | **analog L / R** (P2_ANO out, P2_ANI in) |
| B (`pp2`) | ADAT B 1-8 (S/Mux B uses 0-3) | SPDIF status/subcode (P2_SPO, cleared by the ISR) | SPDIF L / R |

### 4.3 PINIT.ol and P2_AINIT.dsp — codec init [C, register meanings L]

PINIT.ol seg_init `an_setup`: sets DSP flag pins via IOCTL/IOSTAT, latches the three flag inputs into
`InitPPlateAnalog = IOSTAT & 7` (board jumpers/state, bit 0 used below), switches the flags to outputs and
bit-bangs 16-bit words to the codec: `WriteCfg(R9)` sends `R9 | 0xA000` MSB first (data = flag 2, clock =
flag 0, strobe = flag 1 at the end, `Wait150ns` between edges). Sequence:

| word | | word | |
|---|---|---|---|
| 0x007 | reg 0 = 7 | 0x6nn, 0x7nn | regs 6/7 = 0x62, or 0x7C if `InitPPlateAnalog` bit 0 |
| 0x100, 0x103 | reg 1 = 0 then 3 (reset, run) | 0x47F, 0x57F | regs 4/5 = 0x7F |
| 0x240 / 0x245 | reg 2; 0x245 if OS `FScale` is 0x80000000 or 0x75999999 (96 / 88.2 kHz, FScale = rate/96k in 1.31, `clock_rate.md`), else 0x240 | 0x007 | reg 0 = 7 |

seg_exit `an_exit` writes 0x600, 0x700, 0x400, 0x500 (levels to 0 = mute). The `101` prefix + 5-bit address +
8-bit data matches an AKM AK4524-style control port (chip address, R/W, register) [?]; regs 4-7 are the per-channel
level registers, so **the DACs are unmuted by PINIT's init and muted again by its exit** [L].

P2_AINIT.dsp (`init` = fnInit, no sync/async code): `asyncOut[0] = InitPPlateAnalog`; if the module's word 6 is
non-zero it calls `os_sendmsgPX2` to report it to the host. The `Prof` output therefore shows the
professional/consumer level state read from the plate [L].

### 4.4 P2_ANO.dsp / P2_ANI.dsp — sync code [C]

P2_ANO seg_sync (11 cycles), relocated:
```
R0 = DM(wclk);  R0 = FEXT R0 BY 0:2;  I2 = R0        ; frame index = wclk & 3
R3 = 0x608                                           ; field: pos 8, len 24
R0 = DM(input0, I7);  I1 = DM(_tx_pp1ptr, I2)        ; left sample, current TX frame
R1 = FEXT R0 BY R3;   R0 = DM(input1, I7)            ; 24-bit = bits 8..31 of the 1.31 sample
JUMP ret_sync (DB);   R1 = FEXT R0 BY R3, DM(9,I1) = R1   ; slot 9 = left
                      DM(10,I1) = R1                      ; slot 10 = right
```
P2_ANI seg_sync (8 cycles): `I1 = DM(_rx_pp1ptr, I7)`, `syncOut[0] = FDEP(DM(9,I1)) << 8`,
`syncOut[1] = FDEP(DM(10,I1)) << 8`. `I7` is the OS sync double-buffer index (0/1), which selects the
`input+I7` word and the RX buffer [L]. Sample format on sync pads: 32-bit signed fixed point, full scale ±2^31;
the codec gets the top 24 bits.

External symbols needed: P2_ANO `_tx_pp1ptr` (P2_IO.ol), `wclk`, `ret_sync`, `_null` (OS), `input0/1` (links);
P2_ANI `_rx_pp1ptr`, `ret_sync`; P2_AINIT `InitPPlateAnalog` (PINIT.ol), `os_sendmsgPX2` (OS);
PINIT.ol `FScale` (OS); P2_IO.ol `spr0_svc`, `imaskIRQ`, `wclk` (OS).

---------------------------------------------------------------------------------------------------
## 5. Sine → analog out 1/2

### 5.1 CSineR4.dsp [C]

seg_name: `CSINER4` / "Complex Sine Oscillator R4"; pad 0 `f` "frequency input" (sync in, 0x8001),
pad 1 `Cout` "cosine signal output", pad 2 `Sout` "sine signal output" (sync out). flags 0 → any DSP.
syncCycles 26, no async/init code, `CSineMem` (seg_mod+9, 8 words): state x0, x1, phase, 0x40000000 and four
odd-polynomial coefficients (first ≈ π/8, i.e. sin(πx) scaled by 1/8). `CSineR4.NFO`: `[Attributes] f.Unit=1` (GUI unit of `f`).

Algorithm (seg_sync): `phase += f` (32-bit wrap); `x = |phase| − 0.5` and `x' = |phase + 0x40000000| − 0.5`
(triangle folds, a quarter cycle apart); each output = 7th-order odd polynomial of x, `<< 3`. Outputs are
computed from the previous sample's x (one-sample pipeline). So:

- `f` = phase increment per sample in units of 2^-32 cycles: **f = round(F / fs × 2^32)** (signed; ±).
- Outputs ≈ ±1.0 full scale (0 dBFS) — lower the monitor volume or add a gain stage.

### 5.2 How a device/project wires it [C]

In a `.dev` the oscillator is a child `RODBase` whose `ROCAtom.module = "Complex Sine Oscillator R4"`, with
`RODPad`s `f`, `Cout`, `Sout`; wiring is a `RODRouting` in the parent between two `RODPad` ids (e.g.
`SSB Modulator M.dev`: `->2c93 (RODPad f)` ↔ `->2ce7 (RODPad LongSyncAtomVar)`, `->2c39 (In2)` ↔ `->2c4f (Sout)`).
A project wires a hardware device the same way: `RODRouting` between the device's `RODPad LIn` and the source's
output `RODPad` (`Wave EZ Playback.pro`: `->60ac.13 (MixL of STM 1632.dev) ↔ ->17c (Scope Analog Dest LIn)`).
At run time base.dll turns each routing into a Sim2k connection between the two atoms' pads
(`module_loading.md` §4.2/§4.3).

### 5.3 Recommended minimal set for the first audible test

| step | module | DSP | notes |
|---|---|---|---|
| 1 | P2_AINIT.dsp (+ PINIT.ol) | **0** | codec reset/clock/levels, unmute (PINIT seg_init) |
| 2 | P2_ANO.dsp (+ P2_IO.ol) | **1** | SPORT0 TDM driver + analog out slots 9/10 |
| 3 | CSineR4.dsp | **1** (same DSP: no STDM transfer needed) | `input0` → a DM word holding f |
| 4 | links | | P2_ANO `input0` ← CSineR4 `syncOut[0]` (Cout), `input1` ← `syncOut[1]` (Sout) |

`f` for 1 kHz at 44.1 kHz: 1000/44100 × 2^32 = 97 391 548.5 → **0x05CE13BD**; at 48 kHz: 0x05555555.
Cout→L, Sout→R gives a 90° phase offset between channels (use Cout on both inputs for an in-phase test).
Order: SPORT and codec must be running (steps 1-2) before the chain is activated. Within one sample the producer
should run before the consumer (CSineR4 before P2_ANO in DSP1's sync chain; otherwise the output is simply one
sample late) [L]; chain insertion is described in `module_loading.md` §2/§3.3. Note that the offline example in
`module_loading.md` §1.5 links P2_ANO on DSP0 — per §4.1 it has to go to DSP1. P2_ANI (analog in, DSP1) can be
added the same way for a loopback test.

---------------------------------------------------------------------------------------------------
## 6. scope_dev.py usage and validation

```
tools/scope_dev.py FILE                 # object tree (ids, pads, links, params, extras)
tools/scope_dev.py --summary FILE       # modules, atoms (DSP module names), atom pads, RODPads, routes, refs
tools/scope_dev.py --gui FILE           # also show skipped GUI blobs (class, size)
tools/scope_dev.py --json FILE          # full tree as JSON
tools/scope_dev.py --plain OUT FILE     # write the decompressed archive
tools/scope_dev.py --check DIR|FILE ... # parse everything, report files that do not end exactly at EOF
```
Validation on the SCOPE 5.1 install (`--check` over the whole tree, 6 processes, ≈1 min):

| files | container | result |
|---|---|---|
| 899 S3 (382 .mdl, 211 .dev, 163 .pre, 124 .io, 19 .pro) + 4 gzip .mdl + 1 uncompressed .mdl | adler32, CRC32, ISIZE all match | decompress OK |
| 741 object archives (.io/.dev/.mdl/.pro) | | parse ends exactly at EOF / at the -3 trailer: **741/741** |
| 163 `.pre` presets | | different payload ("Creamware Scope technology preset file", read by `RODModule::LoadPresetFromFile` @10055050), not decoded |

Largest files: `STM 4896.dev` / `STM 2448.dev` (47 / 28 MB plain, ~1000 GUI blobs) parse in ≈20 s.

### 6.1 Known limitations

- GUI objects are skipped, not decoded (§2.5); the skip is heuristic but verified by the end-of-file check.
- The reference-parameter trailer (§2.2) and the RefModule restore lists are only partially decoded.
- `ROCParameter` fields between min/max/value and the label are dumped raw (meaning not needed for audio).
