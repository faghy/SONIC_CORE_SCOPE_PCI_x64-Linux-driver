# Creamware / Sonic Core "SC" DSP file format (.dsp / .ol / .21k)

Tool: `/home/faghy/puksar2/tools/sc_decode.py` (Python 3, stdlib only).

```
sc_decode.py in.dsp out.bin            # decoded file; magic rewritten to 0x521c, key zeroed
sc_decode.py --keep-header in out      # decoded file, keep 'SC' magic + key bytes
sc_decode.py --dump [-v] f1 f2 ...     # container structure (-v: words, relocs, symbols)
sc_decode.py --extract DIR f1 ...      # dump raw section data per section
```

## 1. Where the algorithm lives (Sim2k.dll, 32-bit, image base 0x10000000)

| Function | Role |
|---|---|
| `FUN_10c0bd60` @ 0x10c0bd60 | COFF object loader: reads 20-byte header, checks magic `0x4353` ('SC') / `0x4758` ('XG') (also knows `0x521c` = plain ADI COFF), fetches the key, re-reads header, reads section headers (0x28 B), symbols (0x12 B), string table. |
| `FUN_10c0b5d0` @ 0x10c0b5d0 | `read(buf,len)`: `fread` (or memcpy from memory image) then calls descrambler with the current file position (`this+0x38`). |
| `FUN_10c0b550` @ 0x10c0b550 | descrambler `(buf, filepos, len)`; mode in `this+0x4c`, 16-byte key in `this+0x50`. |
| `FUN_10c0bc70` @ 0x10c0bc70 | per-section: reads raw data (`size` bytes at `scnptr`) and relocs (10 B each at `relptr`). |
| `FUN_10c0aba0` @ 0x10c0aba0 | relocation apply (types 2,3,4,6) — "unknown relocation type %d". |
| `FUN_10c0a9c0` @ 0x10c0a9c0 | external symbol resolution ("Unknown external symbol '%s'"), "VoiceDef" special case. |
| `FUN_10c0bb60/bb90` | get/put 32-bit value in a 5-byte DM word (big-endian bytes 0..3). |

Loader sequence in `FUN_10c0bd60`:
1. mode=0; read 0x14 bytes (descrambled), then descramble bytes 0..1 again at pos 0 (=undo) -> magic in clear.
2. read 16 bytes at pos 0x14 and descramble again with same pos (=undo) -> **key = raw bytes 0x14..0x23**.
3. mode = (magic != 'SC') + 1  -> SC: 1, XG: 2. Seek 0, re-read header with real mode; restore magic.
4. `opthdr` (=16) bytes are skipped with fseek -> key area is never descrambled.

## 2. Descrambling algorithm

Byte-wise XOR keystream, depends only on absolute file offset `p` and the key `K[16]`:

```
SC (mode 1): ks(p) = (3*p          + K[(p*p) & 0xF])                   & 0xFF
XG (mode 2): ks(p) = (3*(p - 0x22) + K[(((p+0x4DE)^2) % 0xF911) & 0xF]) & 0xFF
plain[p] = cipher[p] ^ ks(p)   for p >= 2, excluding 0x14..0x23 (key)
```
(`p*p` is a 32-bit int product in the original; only the low 4 bits matter so overflow is irrelevant. In the XG mode the `%` is on a signed int but values stay positive for real file sizes.)

Why the earlier statistics showed ~56% agreement: there are 23 distinct keys across 1217 files.
Most common: `2627f6859715ad1dd294ddc476193931` (674 files, incl. puls2os*.21k, AINIT.ol, P2_AINIT.dsp,
c2_minit.dsp), `5944abaef2717e676864e8a38bbdd91f` (407 files, incl. PINIT.ol), `c39706a6…` (88), …
7 files use the 'XG' variant (SAITE2N, Saite60, As2_para, SPLADSP3, SPLTDSP3, SPLTDP3, SPLADP3 .dsp).

## 3. Container format: ADI 21k (SHARC) COFF

All little-endian on the container level; section *contents* are big-endian DSP words.

### File header (20 bytes, offset 0)
| off | size | field |
|---|---|---|
| 0 | 2 | magic: 'SC' 0x4353 / 'XG' 0x4758 (scrambled), 0x521c plain 21k COFF |
| 2 | 2 | nscns (< 0x100) |
| 4 | 4 | timestamp (time_t; 2008-08 for .ol/.dsp, 2010-11 for puls2os) |
| 8 | 4 | symptr (file offset of symbol table) |
| 12 | 4 | nsyms (entries incl. aux) |
| 16 | 2 | opthdr size = 16 (holds the scrambling key) |
| 18 | 2 | flags (0x0003 = RELFLG|EXEC for linked .21k, 0 for relocatable .ol/.dsp; bit 0x4000 tested by loader) |

### Optional header (16 bytes, offset 0x14): scrambling key (unscrambled).

### Section headers (40 bytes each, offset 0x24)
`name[8], paddr u32, vaddr u32, size u32 (bytes), scnptr u32, relptr u32, lnnoptr u32, nreloc u16, nlnno u16, flags u32`

- `flags & 1` -> **PM** section, 48-bit instruction/data words, **6 bytes big-endian** per word.
- `flags & 2` -> **DM** section, 40-bit words, **5 bytes big-endian**; the 32-bit value is bytes 0..3
  (byte 4 = 8 extra mantissa bits of 40-bit floats). Strings (seg_name, seg_attr) store one char per DM word in byte 3.
- `paddr/vaddr` = word address (21065L: PM internal block 0 at 0x8000, DM block 1 at 0xC000). 0 for relocatable objects.
- Word count = size / wordsize (verified to divide exactly in all files).

### Relocations (10 bytes each, at relptr)
`vaddr u32 (word offset within section), symndx u32, type u16` — applied in `FUN_10c0aba0`:
| type | patch location (within the target word) | meaning |
|---|---|---|
| 2 | PM bytes 3..5 (low 24 bits) | 24-bit absolute address (JUMP/CALL target) |
| 6 | PM bytes 3..5 | 24-bit PC-relative (value - (section_base + offset)) |
| 3 | PM bytes 2..5 (low 32 bits) | 32-bit immediate (e.g. `ureg = <addr>`) |
| 4 | DM bytes 0..3 | 32-bit data word |
Addend = current field contents minus symbol value (when symbol is section-defined); result = resolved symbol address + addend.

### Symbols (18 bytes each, at symptr) + string table
Standard COFF SYMENT: `name[8]` (or `0,0,0,0,strtab_off u32`), `value u32` (word offset/address), `scnum i16` (1-based; 0 = UNDEF/external import, e.g. `os_sendmsgPX2`, `InitPPlateAnalog`, `FScale`), `type u16`, `sclass u8` (2=EXT, 3=STATIC), `numaux u8`.
String table directly follows symbols: `u32 total length (incl. the 4 bytes)` + NUL-terminated names. **It ends exactly at EOF** in every file.

### Section naming conventions
- `.21k` DSP OS images (linked, absolute): `seg_rth` (runtime header / interrupt vector table @0x8000), `seg_init` (@0x809d), `seg_pmco` (PM code @0x8140), `seg_dmda` (DM data @0xC400). `puls2os%d.21k` filename built by `FUN_10c309a0` (Pulsar2 board class) / `FUN_10c28610`; index probably = DSP number.
- `.ol` (init overlays, relocatable): `seg_desc`, `seg_dmda` (DM), `seg_init`, `seg_exit` (PM).
- `.dsp` modules (relocatable): `seg_mod` (module struct: next, fnInit, fnSync, fnAsync, changed, asyncOut, syncOut, input…), `seg_desc` (numIn, numAsyncOut, numSyncOut, syncCycles, asyncCycles, flags, typeIn0…), `seg_name` (short/long module & IO names), `seg_init`/`seg_sync`/`seg_asyn`/`seg_exit`/`seg_pmco` (PM code), `seg_info`, `seg_id`, `seg_attr`, `seg_junc`, `seg_inda`, `seg_mod2/3`, ….
  The loader relocates them into OS-chosen addresses (`FUN_10c0a910` sets paddr of seg_pmco/dmda/inda/init/exda/exit) and links imports against the OS image symbols (e.g. `os_sendmsgPX2` is exported by puls2os*.21k).

## 4. Evidence of correctness
- All **1217** SC/XG files under `scope_full/` (1117 .dsp, 53 .ol, 47 .21k; 1210 SC + 7 XG) decode to COFF where:
  header + 16 + 40*nscns == first scnptr; every section size is a whole multiple of its word size;
  relocs fit exactly between sections; symbol table + string-table length ends **exactly at file size**.
- Symbol/section names are readable (`seg_init`, `os_sendmsgPX2`, `irq0_svc`, `ModuleNameLong`, …).
- PM words disassemble as sensible SHARC opcodes: `0x0A3E00000000` = RTS, `0x0B3E00000000` = RTI,
  `0x063E00008080` = JUMP 0x8080 (reset vector at 0x8005 of the 21065L IVT), `0x06BE0000xxxx` = CALL,
  `0x0F20..0F2D` = `ureg = imm32` (I0=0, I1=1, …, L register init). Padding words decode to all zeros.
- `seg_rth` (interrupt vector table, 157 words) is byte-identical in puls2os0..5; puls2os1 and 3 share seg_init/seg_pmco.
- Reloc types found across corpus: 4:16798, 3:15670, 2:6285, 6:702 — exactly the 4 types the loader implements.

Example (P2_AINIT.dsp seg_init, 6 PM words, relocs at word 0 type 3 -> InitPPlateAnalog, word 4 type 2 -> os_sendmsgPX2):
```
100000000000  a10000000005  a00700000006  700fee821770  06a000000000  0a3e00000000(RTS)
```

## 5. --dump output of the requested files
```
== scope_full/app/App/Dsp/puls2os0.21k  (11244 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x183d nsyms=192 opthdr=16 flags=0x0003
  layout     : strtab @0x25bd len=1583 -> end 0x2bec  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    696    116      472     72a     0     1
  3  seg_pmco PM      8140     8140   2496    416      72a    10ea     0     1
  4  seg_dmda DM      c400     c400   1875    375     10ea    183d     0     2
== scope_full/app/App/Dsp/puls2os1.21k  (11083 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x17cc nsyms=190 opthdr=16 flags=0x0003
  layout     : strtab @0x2528 len=1571 -> end 0x2b4b  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    588     98      472     6be     0     1
  3  seg_pmco PM      8140     8140   2496    416      6be    107e     0     1
  4  seg_dmda DM      c400     c400   1870    374     107e    17cc     0     2
== scope_full/app/App/Dsp/puls2os2.21k  (11101 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x17de nsyms=190 opthdr=16 flags=0x0003
  layout     : strtab @0x253a len=1571 -> end 0x2b5d  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    606    101      472     6d0     0     1
  3  seg_pmco PM      8140     8140   2496    416      6d0    1090     0     1
  4  seg_dmda DM      c400     c400   1870    374     1090    17de     0     2
== scope_full/app/App/Dsp/puls2os3.21k  (11083 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x17cc nsyms=190 opthdr=16 flags=0x0003
  layout     : strtab @0x2528 len=1571 -> end 0x2b4b  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    588     98      472     6be     0     1
  3  seg_pmco PM      8140     8140   2496    416      6be    107e     0     1
  4  seg_dmda DM      c400     c400   1870    374     107e    17cc     0     2
== scope_full/app/App/Dsp/puls2os4.21k  (12113 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x19de nsyms=206 opthdr=16 flags=0x0003
  layout     : strtab @0x285a len=1783 -> end 0x2f51  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    750    125      472     760     0     1
  3  seg_pmco PM      8140     8140   2844    474      760    127c     0     1
  4  seg_dmda DM      c400     c400   1890    378     127c    19de     0     2
== scope_full/app/App/Dsp/puls2os5.21k  (12539 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x4cecc2f4 symptr=0x1ac8 nsyms=216 opthdr=16 flags=0x0003
  layout     : strtab @0x29f8 len=1795 -> end 0x30fb  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_rth  PM      8000     8000    942    157       c4     472     0     1
  2  seg_init PM      809d     809d    690    115      472     724     0     1
  3  seg_pmco PM      8140     8140   3108    518      724    1348     0     1
  4  seg_dmda DM      c400     c400   1920    384     1348    1ac8     0     2
== scope_full/app/App/Dsp/PINIT.ol  (1943 bytes)
  scrambling : SC (mode 1)   key=5944abaef2717e676864e8a38bbdd91f
  coff hdr   : nscns=4 timdat=0x48ab296e symptr=0x55c nsyms=27 opthdr=16 flags=0x0000
  layout     : strtab @0x742 len=85 -> end 0x797  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_desc DM         0        0     15      3       c4      d3     0     2
  2  seg_dmda DM         0        0      5      1       d3      d8     0     2
  3  seg_init PM         0        0    552     92       d8     300    24     1
  4  seg_exit PM         0        0    204     34      3f0     4bc    16     1
== scope_full/app/App/Dsp/AINIT.ol  (1631 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x48ab296e symptr=0x46c nsyms=23 opthdr=16 flags=0x0000
  layout     : strtab @0x60a len=85 -> end 0x65f  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_desc DM         0        0     15      3       c4      d3     0     2
  2  seg_dmda DM         0        0      5      1       d3      d8     0     2
  3  seg_init PM         0        0    390     65       d8     25e    18     1
  4  seg_exit PM         0        0    216     36      312     3ea    13     1
== scope_full/app/App/Dsp/P2_AINIT.dsp  (1236 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x48ab2973 symptr=0x241 nsyms=26 opthdr=16 flags=0x0000
  layout     : strtab @0x415 len=191 -> end 0x4d4  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_mod  DM         0        0     40      8       c4      ec     1     2
  2  seg_desc DM         0        0     55     11       f6     12d     0     2
  3  seg_name DM         0        0    220     44      12d     209     0     2
  4  seg_init PM         0        0     36      6      209     22d     2     1
== scope_full/app/App/Dsp/c2_minit.dsp  (998 bytes)
  scrambling : SC (mode 1)   key=2627f6859715ad1dd294ddc476193931
  coff hdr   : nscns=4 timdat=0x48ab2971 symptr=0x1e3 nsyms=21 opthdr=16 flags=0x0000
  layout     : strtab @0x35d len=137 -> end 0x3e6  (== file size, OK)
  #  name     spc    paddr    vaddr  bytes  words  scnptr  relptr  nrel flags
  1  seg_mod  DM         0        0     30      6       c4      e2     1     2
  2  seg_desc DM         0        0     35      7       ec     10f     0     2
  3  seg_name DM         0        0    190     38      10f     1cd     0     2
  4  seg_init PM         0        0     12      2      1cd     1d9     1     1
```

## 6. Notes for the Linux loader
- `.21k` image: for each section, write `size/wordsize` words to `vaddr` in PM (48-bit) or DM (40/32-bit) space of the target SHARC; no relocations needed (nreloc=0). Entry: reset vector (0x8005 -> JUMP 0x8080).
- `.ol` / `.dsp`: relocatable; need a linker: choose base addresses per section, resolve UNDEF symbols against the OS image's EXT symbols (`--dump -v` on puls2os*.21k lists them), apply relocs per table above. Sim2k then sends them via UploadCode/UploadData messages (not analysed here).
