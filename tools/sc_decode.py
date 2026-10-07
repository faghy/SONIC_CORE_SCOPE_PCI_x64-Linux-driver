#!/usr/bin/env python3
"""
sc_decode.py - decode Creamware / Sonic Core "SC" (and "XG") DSP object files.

Reimplements the descrambler found in Sim2k.dll:
  FUN_10c0bd60 @ 0x10c0bd60  COFF loader (magic check, key fetch, headers)
  FUN_10c0b5d0 @ 0x10c0b5d0  read(): fread/memcpy + descramble at file pos
  FUN_10c0b550 @ 0x10c0b550  descramble(buf, filepos, len)

Scrambling (byte-wise XOR, depends only on absolute file position p and a
16-byte key K stored in clear at file offset 0x14..0x23 = the COFF
"optional header" slot, which the loader skips):

  'SC' (mode 1):  x = (3*p            + K[(p*p) & 15])                 & 0xff
  'XG' (mode 2):  x = (3*(p - 0x22)   + K[(((p+0x4de)**2) % 0xf911) & 15]) & 0xff

  plain[p] = cipher[p] ^ x       for all p except 0..1 (magic) and 0x14..0x23 (key)

The plaintext is an Analog Devices ADSP-21xxx (SHARC) COFF object:
  20-byte file header, 16-byte opt header (= key), N x 40-byte section headers,
  raw section data (PM: 6-byte big-endian words, DM: 5-byte big-endian 40-bit
  words), 10-byte relocations, 18-byte symbols, string table.

Usage:
  sc_decode.py in.dsp out.bin           write decoded file (magic rewritten to
                                         0x521c = standard 21k COFF magic,
                                         key area zeroed unless --keep-header)
  sc_decode.py --dump in.dsp [...]      print container structure
  sc_decode.py --dump -v in.dsp         also symbols and relocations
  sc_decode.py --extract DIR in.dsp     write each section's raw data to DIR
"""
import argparse
import os
import struct
import sys

MAGIC_SC = 0x4353  # bytes 'S','C'
MAGIC_XG = 0x4758  # bytes 'X','G'
MAGIC_COFF = 0x521C  # plain ADI 21k COFF (not scrambled)

KEY_OFF = 0x14
KEY_LEN = 16

STYP_PM = 0x1  # section holds 48-bit PM words (6 bytes each)
STYP_DM = 0x2  # section holds 40-bit DM words (5 bytes each)

RELOC_TYPES = {
    2: "PM24  (low 24 bits of 48-bit instr, bytes 3..5)",
    3: "PM32  (low 32 bits of 48-bit instr, bytes 2..5)",
    4: "DM32  (high 32 bits of 40-bit DM word, bytes 0..3)",
    6: "PM24R (PC-relative 24-bit, bytes 3..5)",
}

SCLASS = {2: "EXT", 3: "STAT", 6: "LABEL", 103: "FILE"}


class SCError(Exception):
    pass


def keystream_byte(mode, p, key):
    if mode == 2:
        q = p + 0x4DE
        idx = (q * q) % 0xF911
        c = 3 * (p - 0x22)
    else:
        idx = p * p  # 32-bit int product; only low 4 bits matter
        c = 3 * p
    return (c + key[idx & 0xF]) & 0xFF


def descramble(data):
    """Return (plain bytes, mode, key). mode 0 = file was not scrambled."""
    if len(data) < KEY_OFF + KEY_LEN:
        raise SCError("file too short")
    magic = struct.unpack_from("<H", data, 0)[0]
    if magic == MAGIC_COFF:
        return bytes(data), 0, None
    if magic == MAGIC_SC:
        mode = 1
    elif magic == MAGIC_XG:
        mode = 2
    else:
        raise SCError("unknown magic 0x%04x" % magic)
    key = bytes(data[KEY_OFF:KEY_OFF + KEY_LEN])
    out = bytearray(data)
    for p in range(2, len(out)):
        if KEY_OFF <= p < KEY_OFF + KEY_LEN:
            continue
        out[p] ^= keystream_byte(mode, p, key)
    return bytes(out), mode, key


class Section:
    pass


class Symbol:
    pass


class CoffObject:
    def __init__(self, plain, mode=0, key=None):
        self.raw = plain
        self.mode = mode
        self.key = key
        (self.magic, self.nscns, self.timdat, self.symptr, self.nsyms,
         self.opthdr, self.flags) = struct.unpack_from("<HHIIIHH", plain, 0)
        if self.nscns >= 0x100:
            raise SCError("implausible section count %d (bad decode?)" % self.nscns)
        off = 20 + self.opthdr
        self.sections = []
        for i in range(self.nscns):
            h = plain[off + 40 * i: off + 40 * i + 40]
            if len(h) < 40:
                raise SCError("truncated section header")
            s = Section()
            s.index = i + 1
            s.name = h[:8].split(b"\0")[0].decode("latin-1")
            (s.paddr, s.vaddr, s.size, s.scnptr, s.relptr, s.lnnoptr,
             s.nreloc, s.nlnno, s.flags) = struct.unpack_from("<IIIIIIHHI", h, 8)
            s.wordsize = 6 if s.flags & STYP_PM else 5
            s.space = "PM" if s.flags & STYP_PM else ("DM" if s.flags & STYP_DM else "?")
            s.data = plain[s.scnptr:s.scnptr + s.size] if s.scnptr else b""
            s.relocs = []
            for r in range(s.nreloc):
                ro = s.relptr + 10 * r
                vaddr, symndx, rtype = struct.unpack_from("<IIH", plain, ro)
                s.relocs.append((vaddr, symndx, rtype))
            self.sections.append(s)
        # string table follows the symbol table
        self.strtab_off = self.symptr + 18 * self.nsyms
        self.strtab = b""
        if self.nsyms and self.strtab_off + 4 <= len(plain):
            n = struct.unpack_from("<I", plain, self.strtab_off)[0]
            self.strtab = plain[self.strtab_off:self.strtab_off + n]
        self.symbols = []
        i = 0
        while i < self.nsyms:
            e = plain[self.symptr + 18 * i: self.symptr + 18 * i + 18]
            sym = Symbol()
            sym.index = i
            if e[:4] == b"\0\0\0\0":
                so = struct.unpack_from("<I", e, 4)[0]
                sym.name = self.strtab[so:].split(b"\0")[0].decode("latin-1")
            else:
                sym.name = e[:8].split(b"\0")[0].decode("latin-1")
            sym.value, sym.scnum, sym.type, sym.sclass, sym.numaux = \
                struct.unpack_from("<IhHBB", e, 8)
            self.symbols.append(sym)
            i += 1 + sym.numaux
        self.symbyidx = {s.index: s for s in self.symbols}

    def end_offset(self):
        if self.strtab:
            return self.strtab_off + len(self.strtab)
        return self.symptr + 18 * self.nsyms

    def words(self, sec):
        ws = sec.wordsize
        for i in range(0, len(sec.data) - ws + 1, ws):
            yield int.from_bytes(sec.data[i:i + ws], "big")


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    plain, mode, key = descramble(data)
    return CoffObject(plain, mode, key), plain


def dump(path, verbose=False, out=sys.stdout):
    obj, plain = load(path)
    w = out.write
    mname = {0: "plain COFF 0x521c", 1: "SC (mode 1)", 2: "XG (mode 2)"}[obj.mode]
    w("== %s  (%d bytes)\n" % (path, len(plain)))
    w("  scrambling : %s   key=%s\n" % (mname, obj.key.hex() if obj.key else "-"))
    w("  coff hdr   : nscns=%d timdat=0x%08x symptr=0x%x nsyms=%d opthdr=%d flags=0x%04x\n"
      % (obj.nscns, obj.timdat, obj.symptr, obj.nsyms, obj.opthdr, obj.flags))
    end = obj.end_offset()
    w("  layout     : strtab @0x%x len=%d -> end 0x%x  %s\n"
      % (obj.strtab_off, len(obj.strtab), end,
         "(== file size, OK)" if end == len(plain) else "(file size 0x%x MISMATCH)" % len(plain)))
    w("  %-2s %-8s %-3s %8s %8s %6s %6s %7s %7s %5s %5s\n"
      % ("#", "name", "spc", "paddr", "vaddr", "bytes", "words", "scnptr", "relptr", "nrel", "flags"))
    for s in obj.sections:
        nwords = s.size // s.wordsize
        rem = s.size % s.wordsize
        w("  %-2d %-8s %-3s %8x %8x %6d %6d%s %7x %7x %5d %5x\n"
          % (s.index, s.name, s.space, s.paddr, s.vaddr, s.size, nwords,
             "!" if rem else " ", s.scnptr, s.relptr, s.nreloc, s.flags))
    if verbose:
        for s in obj.sections:
            w("  -- section %d %s (%s), first words:\n" % (s.index, s.name, s.space))
            for i, word in enumerate(obj.words(s)):
                if i >= 8:
                    break
                w("     %06x: %0*x\n" % (s.vaddr + i, 2 * s.wordsize, word))
            for (va, si, rt) in s.relocs:
                sym = obj.symbyidx.get(si)
                w("     reloc @word %4d (abs 0x%x) type %d %-6s -> sym[%d] %s\n"
                  % (va, s.vaddr + va, rt, RELOC_TYPES.get(rt, "?").split()[0],
                     si, sym.name if sym else "?"))
        w("  -- symbols:\n")
        for sym in obj.symbols:
            sec = obj.sections[sym.scnum - 1].name if 0 < sym.scnum <= obj.nscns else \
                {0: "UNDEF", -1: "ABS", -2: "DEBUG"}.get(sym.scnum, str(sym.scnum))
            w("     [%3d] %-28s val=0x%06x sec=%-8s class=%-5s type=0x%x aux=%d\n"
              % (sym.index, sym.name, sym.value, sec, SCLASS.get(sym.sclass, sym.sclass),
                 sym.type, sym.numaux))
    return obj


def main():
    ap = argparse.ArgumentParser(description="Decode Creamware/SonicCore SC DSP files")
    ap.add_argument("--dump", action="store_true", help="print container structure")
    ap.add_argument("-v", "--verbose", action="store_true", help="with --dump: symbols, relocs, words")
    ap.add_argument("--extract", metavar="DIR", help="write each section's raw data into DIR")
    ap.add_argument("--keep-header", action="store_true",
                    help="keep SC magic and key bytes in output (default: magic->0x521c, key zeroed)")
    ap.add_argument("files", nargs="+")
    a = ap.parse_args()

    if a.dump or a.extract:
        rc = 0
        for f in a.files:
            try:
                obj = dump(f, a.verbose) if a.dump else load(f)[0]
            except (SCError, struct.error) as e:
                print("%s: ERROR %s" % (f, e), file=sys.stderr)
                rc = 1
                continue
            if a.extract:
                os.makedirs(a.extract, exist_ok=True)
                base = os.path.basename(f)
                for s in obj.sections:
                    p = os.path.join(a.extract, "%s.%d.%s.%s.%x.bin" % (base, s.index, s.name, s.space, s.vaddr))
                    with open(p, "wb") as fo:
                        fo.write(s.data)
        return rc

    if len(a.files) != 2:
        ap.error("need: in.dsp out.bin")
    src, dst = a.files
    with open(src, "rb") as f:
        data = f.read()
    plain, mode, key = descramble(data)
    out = bytearray(plain)
    if mode and not a.keep_header:
        struct.pack_into("<H", out, 0, MAGIC_COFF)
        out[KEY_OFF:KEY_OFF + KEY_LEN] = bytes(KEY_LEN)
    CoffObject(bytes(out))  # sanity parse
    with open(dst, "wb") as f:
        f.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
