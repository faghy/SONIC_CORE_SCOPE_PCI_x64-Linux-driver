# SCOPE presets (.pre) and licensed modules

This document covers two things that SCOPE devices need beyond their saved state:

- **Presets**: the `.pre` files and the presets embedded in `.dev` files, and how a stored preset maps to the
  parameters of a device plan (`tools/scope_device.py`).
- **Licence unlock**: how SCOPE unlocks licensed DSP modules ("Effect Package I/II", the SC EQ/compressor
  modules, synth oscillators, ...) with the owner's key file and the board's micro-controller
  (`tools/pulsar_license.py`).

Legend: **[C]** = read from the code, **[L]** = likely, **[?]** = guess, **[V]** = verified on the corpus or on
the owner's key file. `FUN_x` / `Name @x` = function at address x. Image bases: base.dll 0x10000000,
Sim2k.dll 0x10c00000, PepPresets.dll 0x10000000 (see the preset section).

No SCOPE file contents, key material, board serials or derived words are reproduced here. Examples use
placeholders.

---------------------------------------------------------------------------------------------------
## P. Presets

### P.1 Container and header [C]
- A `.pre` file is either plain gzip (39 files, list format −2) or S3 (163 files, format −3). Both are opened
  by `scope_dev.unpack_container`.
- The plain payload is: u32 header version (1 or 2), `"Creamware Scope technology preset file\0"`, u32 size,
  then `size` bytes that a cwWindows MemInterpreter reads.
- Call chain in base.dll: `Presets::LoadFromExternalFile` @100b85e0 (header `FUN_10025c80`, version 1..2) →
  `Presets::Read` @100b3860 → `Presets::Read2(MemInterpreter&)` @100ab0d0.
- The script class `PepPresetList` only wraps base.dll `Presets` (PepPresets.dll `LoadPresetsFromFile`
  @10706610, `SavePresetsToFile` @10704400). `Controls@PresetList` (PresetList.pep) loads
  `<Presets dir>/<Caption>.pre` by default, where Caption is the device name.
- **Embedded presets [C]:** the same block (format −3) is the raw value of pad `PLData` of the device's
  `Controls@PresetList` script, usually a single preset called "Default". `LastRestoredPresets` holds the same
  kind of block (`SavePresetsToPad` @10704db0) [L].

### P.2 MemInterpreter alignment [C] (cwWindows.dll)
- Offsets are relative to the start of the block.
- LONG and DWORD values are 4-byte aligned (`GetLONG` @1023b060). `GetBlock` @1023b0f0 aligns to 8.
- BYTEs and strings are unaligned. Strings are NUL-terminated (`GetWXString` @1023af90).
- Padding bytes are uninitialised writer memory, not data.

### P.3 Presets block (`Presets::Read2` @100ab0d0) [C]
```
LONG format            -1 / -2 / -3   (>= 0: nothing; -1 = old ReadOld path, not in the corpus)
if format > -3:  BYTE, BYTE f, [if f: LONG, LONG], LONG          (unused)
LONG nParams; nParams x ParamUID: block(16) [+ DWORD module PCID if format -3]
                 (PresetParameter::Create @1006b3f0 -> UnresolvedPresetParameter::Read2 @10025e80)
LONG nPresets; nPresets x Preset
root PresetCategory (PresetCategory::Read2 @100aac10)
```
- **Preset header** (`PresetInfoUser::Read2Mem` @1009e9f0): string name, LONG number (−1 = none),
  LONG nInfo, then nInfo × `PresetInfo::Read2` @1007d0f0. Each PresetInfo is a string name, a ROCParam and a
  LONG type: 1 = date, 2 = author, 3 = description, 4 = category, 5 = ask-synapse.
- **Values:** LONG n, then n entries.
  - Format −2 (`Preset::Read2` @100b35e0): LONG ref, BYTE has, [ROCParam].
  - Format −3 (`Preset::Read2b` @100aa6a0): BYTE has, [ROCParam], LONG ref.
  - `ref` is an index into the ParamUID list.
- **After the values:** LONG nCat + nCat LONG category ids, then LONG nRef + sub-presets.
  - Format −2 nests a whole Presets block.
  - Format −3 uses ReferencePreset (`FUN_100740a0`): string device path, DWORD n + n DWORD PCID path,
    DWORD nVal × (block16 uid, DWORD pcid, BYTE has, [ROCParam]), DWORD nSub + nested sub-presets.
  - Only devices with child devices use this (Vocodizer Matrix, RMX, SL9000).
- **ROCParam in memory** (`FUN_1007b280`): LONG DataType. Type 10 = LONG n + n ROCParams; any other type =
  LONG size + block(size). Types: 1 = Long, 2 = Float, 3 = Double, 5 = String.
- **PresetCategory:** info header as above, LONG n + n LONG preset indices, LONG nSub + nested categories.

### P.4 Mapping to parameters [C/V]
- A ParamUID is the 16-byte uid of a `ROCParameter` (`RODModule::FindParameter` @100391a0, uid at +0x80).
- The .dev stores its ROCParameters in module extras chunk 0x110: a u32 count, then the objects
  (`ROCParameter::Serialize` @100858d0 = scope_dev `p_rocparameter`, field `raw16`). The RODParameter tree is in
  chunk 0x115.
- `ROCParameter.target` is the id of the pad the parameter drives. That is one of:
  - a DSP `ROCAtomPad` (the value is in the 48 kHz reference domain and is rescaled per Unit when written);
  - a script pad (knob `Val`, a converter input, a switch's `Switch`);
  - a `RODPad`.
- Restoring a preset writes the stored value to that pad [L: `PepPresetList::RestorePreset` @10704850].
- `scope_device.py` resolves each target as follows:
  1. It finds the plan parameter whose `Val`, followed through the emulated converters, reaches the target's
     net. It inverts the converter chain to get `Val`, then applies `val_to_display`.
  2. Targets on DSP pads that no knob drives become raw words (`pad_encode` at the current rate).
  3. Everything else (GUI state such as PosX/Show, scripts that are not emulated) is reported as `host`.

### P.5 Validation [V] (211 devices, 202 .pre)
- **Decoding:** all 202 files decode, and the read position lands exactly on `size`. 145 files pair with a
  device by Caption.
- **Volume:** 5701 presets (5239 from files, 462 embedded) hold 473k stored values:
  - 270k map to plan parameters;
  - 31k become raw DSP words;
  - 160k are host/GUI values.
- **Mapped display values:** 99.3% are inside the parameter's display range, and 89% are round at the
  display format's precision.
- **Outliers:** the other 0.7% (1987 values in about 30 devices) come mostly from:
  - buttons that share one Switch net;
  - SSB frequency-shift pads;
  - synth envelope pads whose converter chain is only partly emulated.

  They are reported in `check`.

### P.6 Implementation (`tools/scope_device.py`, section "presets")
- `read_pre_file(path)` and `read_preset_block(data)` decode the raw format.
- `device_parameters(dev)`, `embedded_presets(dev)`, `preset_caption(dev)` and `find_preset_file(dev)` read
  the device side. The preset search order is the `extra_dirs` argument, then `/var/lib/snd-pulsar/presets`,
  then the `Presets/` folders above the `.dev`.
- `presets(path, dev_path=None, pre_path=None)` → `[{index, name, source, category, values, uids, ...}]`. For
  a `.dev`, the file presets come first, then the embedded ones.
- `preset_values(dev_path, preset, pre_path=None, rate=48000, ...)` returns:
  - `params` = {param name: display value};
  - `vals` = the knob Val of each param;
  - `raw` = [{name, key, in, pad, value, unit, word}];
  - `host` = names of GUI/host-only values;
  - `check` = problems found.
- CLI: `scope_device.py presets FILE.dev [N|NAME] [--pre F.pre] [--rate R] [--dsp DIR] [--json]`.
- `pulsar-import-dsp` should also copy `app/Presets` to `/var/lib/snd-pulsar/presets`.


---------------------------------------------------------------------------------------------------
## L. Licensed modules

### L.0 TL;DR
1. A DSP module is licensed when its `.dsp` has a **`seg_id`** section. The six words are
   `manufacturerID, familyID, productID, componentID, levelID, versionID` (the symbol names are in the file).
   Example: both "Effect Package" atoms (`RMWMEPDA.dsp`, `XPN2402A.dsp`) are `(7, 1, 3, 0, 2, 0)`, the
   "SC Effects" product.
2. The module only works if it also has a **`magicProt`** word in `seg_mod` and that word holds the right value
   when its `seg_init` runs. Otherwise `seg_init` leaves the real code out (§1.5 of device_format.md).
3. The value is **computed by the board's micro-controller (uC)**, not by the host. The host sends it two words,
   the *key word* from the owner's key file and the *module id* derived from `seg_id`, and tells DSP5 where to
   put the uC's answer: the module's `magicProt` address. **[C]**
4. The key file (`*.v5`, "product registry") is the owner's licence list: one record per product, bound to one
   board serial. It is checked locally (checksum, decryption, serial, ranges, key syntax) and then by the uC.
5. Nothing goes to a server. **[C]** (no network code on this path; `GetTransferString` only builds the text
   that the user would send to Sonic Core when *requesting* a key.)

### L.1 Key file (`CPCheck`, base.dll) [C, V]
`CPCheck::RegistryPath` @10010300: `ExePath + "keyfile.v5"` unless `[KEYS] REGISTRY PATH` is set in the ini.
`ReadEntry` @100500d0 skips CR/LF, then reads **fixed 0x73-byte records** that start with `+`.

| offset | length | content |
|---|---|---|
| 0x00 | 1 | `+` |
| 0x02 | 9 | board serial string (§L.2) |
| 0x0C | 0x29 | product name, space padded (display only) |
| 0x35 | 14 | date `mm/dd/yy hh:mm` (display only) |
| 0x44 | 1 | `0` = permanent, `1` = temporary (time credit kept in the board EEPROM) |
| 0x45 | 12 | key string |
| 0x51 | 2 | record checksum, hex |
| 0x53 | 32 | encrypted fields: 32 hex digits = 8 fields of 4 hex digits |

Decryption (`DecryptFromRegistry` @10010220): k = `rec[0x4B] + rec[0x48]` (mod 13 if > 12); character i is
looked up in a 35-character table and the digit is `index − i mod 6 − k` (must be 0..15). The inverse,
`CryptForRegistry` @10010180, is not implemented in our tools.

Checksum (`ReadEntry`): the 0x73 bytes with the checksum field blanked and the encrypted part replaced by its
plain hex digits are summed as signed chars and printed with `"%lX"`; this repeats on the printed text while the
sum is above 0xFF. The result must equal the 2 stored characters; otherwise "Found corrupted data in the product
registry!".

Fields f0..f7 (`GetRegistryEntry` @1006cf00, `ValidateKey` @100625c0):

| field | meaning | used as |
|---|---|---|
| f0 | manufacturer | must equal seg_id word 0 |
| f1 | record type | 1 or 2 seen; selects the f7 rule below |
| f2 | family | must equal seg_id word 1 |
| f3 | component | range check only |
| f4 | product | must equal seg_id word 2 |
| f5 | version | range check only |
| f6 | level | must equal seg_id word 4 |
| f7 | hardware word | compared with the board's uC info word (below) |

The owner's file: 27 records, all checksums and decryptions valid, f1 = 2, f6 = 2, f7 = 0xFFFF **[V]**.

### L.2 Board serial [C, V]
`CPCheck::String2SnoLong` @1004ffb0: the 9-character serial holds 8 base-26 digits (26-character alphabet,
fixed position permutation) plus a check character (`FUN_10011520` @10011520). The value goes through
`FUN_10011350` @10011350 and `(x · 0x120DF) mod 0x1E521609`.

The result is the number the board's uC reports for **uC command 0x229** (`pulsar_loader` `uc_query`,
clock_rate.md §1.1). This was checked on the owner's card: the serial in the key file maps exactly to the
serial the card returns **[V]**. `CPCheck::GetSno` @10037080 reads it from Sim2k `HardwareInfo` word 0x16 per
board (board vt+0xf8).

`CPCheck::GetBoardType` @10037140 decodes the board family from the serial ("Pulsar2" for the owner's card).

The second per-board value, `HardwareInfo` word 0x19 (board vt+0xfc = `(reply of uC command 0x21F >> 16) &
0xFFFF`), is the "uC info word" checked against f7:

- f1 = 1: the info word must be 0xFFFF.
- f1 > 1 and a permanent record: f7 must equal the info word.

`pulsar_loader.finish_run` now keeps both values (`Board.uc_serial`, `Board.uc_info`).

### L.3 From seg_id to the two words [C]
Who asks: Sim2k `FUN_10c1bea0` (module upload) reads `seg_id` after uploading `seg_mod` and calls
`FUN_10c031f0` → base.dll `GPCallBack(1, name, w0..w5, &sno)` @100c0f90 → `CPCheck::RequestModuleKey`
@100c0b80 → `GetRegistryEntry`. The answer is the key word (return value) and the serial of the board the
key belongs to. If there is no key, Sim2k unloads the module with "Unregistered device detected".

`GetRegistryEntry` does the following:

1. It collects the serials of all installed boards (`GetSno`).
2. It looks for a valid record whose fields match the seg_id (f0, f2, f4, f6).
3. It checks that the record's serial is one of the installed boards and applies the f1/f7 rule.
4. It calls `ValidateKey(w0, w1, w2, f3, w4, f5, sno, key string, 0)`.

`ValidateKey` @100625c0:

- The key string must have 12 characters.
- `ValidateRange` @10010120 checks the ids.
- The serial must not be `000000000`, unless the ini switch `EnableHwTest` is set.
- `module id = GetModuleID(w0, w1, w2)` @100108c0:
  `x = ((w0<<8 ^ w1)<<8 ^ w2)<<8 ^ (w0>>8)`, `id = (x · 0x47882121) mod 0x79E9941B − 0x73002CF3`
  (signed 32/64-bit arithmetic; Sim2k `FUN_10c45e60` computes the same).
- The key string is normalized (`FUN_10011310`: upper case, O→0, I→1).
- `FUN_10037820` @10037820 decodes 8 base-26 digits (another position permutation, own check character).
  It then compares 2 more characters with a re-encoding that depends on the module id and the serial
  (`FUN_100115a0`).
- Key word = `FUN_1000fca0`(decoded value) @1000fca0: a fixed list of 15 add/xor/rotate steps
  (`DAT_10137160`).

**Not reimplemented:** the `FUN_100115a0` comparison. It is the key generator itself, so it is deliberately left
out. The uC performs the real check (§L.4), and the optional hardware check of §L.5 replaces it.

### L.4 Unlock on the board [C]
Board vt+0x184 = `FUN_10c2f2c0` @10c2f2c0 on Pulsar2 (vtable 0x10c87d74). It is called as
`(module's DSP, module, module id, key word)` and runs on the uC DSP (vt+0x224 → **DSP5**). It uses the DSP5 OS
symbols `ucMagicDest`, `ucDataOut`, `ucBytePosOut` and `ucCmdOut`:

```
dest = module.seg_mod + magicProt            (module on DSP5)
     = 0x22000000 | dsp<<21 | (seg_mod + magicProt)   (other DSP: board vt+0xa8 FUN_10c2e420(dsp, a, 0, len 1))
SetValue(5, ucMagicDest, dest)        Sleep(11)
SetValue(5, ucDataOut, key word)      SetValue(5, ucBytePosOut, 4)   Sleep(11)
SetValue(5, ucDataOut, module id)     SetValue(5, ucBytePosOut, 4)   Sleep(11)
SetValue(5, ucCmdOut, 0x220)          Sleep(11)
```

DSP5 shifts the bytes to the uC. The uC answers with the unlock word, and the OS's `ucAsync` writes it to
`ucMagicDest`, which is the module's `magicProt` (clock_rate.md §1.1 describes the same uC channel). Sim2k does
all this **after `seg_mod` and before the module's fnInit**, which is when `seg_init` checks the word.
With a module, the function returns 0x80000000 and ignores the mailbox.

`FUN_10c2f2c0` also touches DSP5 bit 0x1000 (`FUN_10c204e0`/`FUN_10c204b0`) when a global flag and board flag
0x1000 are set. It looks XTC-specific and is skipped [?].

### L.5 Hardware key check (registration path) [C]
`CPCheck::Registrate` @10062750 → `ValidateKey(..., param_9 = 1)` → `ROCAtom::RequestModuleMagic`
@10015770 → `SimCall(6)` → `FUN_10c45e60` → `FUN_10c2f2c0` with **no module**:

- `ucMagicDest = 0x63E0080B` (host SRAM mailbox, BAR+0x8202C) and `writeReg(mailbox, 1)`;
- then the same data and command;
- the reply = `readReg(mailbox)`, after which the mailbox is set to 0.

SCOPE accepts the key when the reply is above 0x7FFFFFFF. `pulsar_license.verify_on_board()` implements this.
It is optional and not run automatically.

### L.6 Which modules and devices [V]
- 231 of the 1117 `.dsp` files have a `seg_id`, and 179 of those also have `magicProt`. Modules with `seg_id` but without `magicProt`
  (e.g. LINVOL) never check the word and run without an unlock.
- Of the 95 complete device plans, **59** contain modules that need an unlock. The 40 "Package" devices are
  among them. The other 19 include SC-EQ M/S, SC-C M/S, SC-GC M/S, Vinco M/S, GRAPHEQ M/S, SC-SL9000 S and
  PSY Q, which `scope_device.py` does **not** flag today because it matches the atom name, not
  `seg_id`/`magicProt`.
- Products: SC Effects (7,1,3) in 47 devices, SC Synths (7,1,5) in 5, then single devices for Modular v3, Vinco,
  Interpole, PSY Q and others.
- With the owner's key file, 58 of the 59 are covered; 46 of those need no MIDI. Together with the 36 clean
  devices, **70** devices without MIDI become usable.

- **All atoms with `magicProt` use one procedure [V].** All 179 `magicProt` modules have a `seg_id`, and
  `magicProt` is always a `seg_mod` word; only its offset differs (e.g. 8, 9, 12 or 65). There are no other atom
  kinds: Effect Package atoms, the SC EQ/dynamics cores, synth atoms (e.g. `DEVM01M` on the EDS16i MIDI path and
  `LROCCA` on Poison's master gain, both "SC Synths" (7,1,5)) and Modular oscillators all go through §L.3 + §L.4
  with their own seg_id. Across all 211 devices, including incomplete ones, 123 contain such atoms (20 synths).
  The owner's file covers 121; the two missing products are 59 and 24 (Prodyssey, Arpeg02).

### L.7 Implementation: `tools/pulsar_license.py`
```
pulsar_license.py show  [--keys PATH] [--serial 0xSNO] [--uc-info 0xWORD] [--show-values]
pulsar_license.py check MODULE.dsp ... [--dsp DIR] [--keys PATH] [--serial 0xSNO] [--show-values]
```
PATH defaults to `/var/lib/snd-pulsar/license` (all `*.v5`; env `PULSAR_LICENSE`). The file belongs to the owner
and should be mode 0600, root. It is never copied into the repository or the package.

API:

- `License(path, board_sno, uc_info)` provides `.entries`, `.usable()`, `.find(seg_id)` and
  `.unlock_values(seg_id)`, which returns `(module_id, key_word, entry)` or raises `LicenseError`.
- `module_seg_id(cls)`, `magic_offset(cls)` and `needs_unlock(cls)` work on a `pulsar_modules.ModuleClass`.
- `uc_magic_ops(uc_syms, dsp, magic_addr, module_id, key)` returns the ops of §L.4 (`set` and `sleep`).
- `unlock_hook(lic, uc_syms)` returns a callable for `PlutoDsp.load(..., unlock=hook)` (pulsar_modules). It
  inserts the ops after the uploads and before fnInit.
- `verify_on_board(board, module_id, key)` runs the §L.5 check.

`pulsar_modules.execute`/`format_ops` know the new `("sleep", dsp, ms, label)` op.

Example (placeholder values):
```
$ pulsar_license.py check XPN2402A.dsp --serial 0x1234ABCD
XPN2402A.dsp   seg_id (7, 1, 3, 0, 2, 0) magicProt=seg_mod+9 -> licensed by 'SC Effects'
$ pulsar_modules ops for XPN2402A on DSP2:
DSP2 UploadData 0xC576   10 words  XPN2402A:seg_mod
...
DSP5 SetValue   0xC57B = 0x2240C57F  XPN2402A: ucMagicDest -> magicProt
DSP5 SetValue   0xC579 = 0x<key>     XPN2402A: uC data: key word
DSP5 SetValue   0xC578 = 0x00000004  ...
DSP5 SetValue   0xC579 = 0x<id>      XPN2402A: uC data: module id
DSP5 SetValue   0xC57A = 0x00000220  XPN2402A: uC command 0x220
DSP2 sysmsg 0x2 a=0xC576 b=0xC405    XPN2402A: run fnInit + link into async chain
```

### L.8 Open points
- Temporary records (`0x44 = '1'`): these need the EEPROM time-credit tables (`GetRestTimeModuleID`,
  `GetFreeTmpTab`) and are reported as unsupported.
- The DSP5 bit 0x1000 toggle in `FUN_10c2f2c0` (XTC only?) [?].
- Hardware test still pending: does `magicProt` hold a non-zero value after the sequence, and does the module
  produce audio?
