#!/usr/bin/env python3
"""
pulsar_license.py - use the owner's SCOPE licence key file (*.v5) to unlock licensed DSP modules
("Effect Package I/II" and every other module that has a seg_id section and a magicProt word).

How SCOPE 5.1 does it (spec + evidence: docs/presets_license.md):

  1. The key file is the "product registry": one 0x73-byte record per product, read by
     base.dll CPCheck::ReadEntry @100500d0 (checksum + DecryptFromRegistry @10010220).
  2. When Sim2k uploads a module that has a `seg_id` section (6 words: manufacturer, family,
     product, component, level, version), it asks base.dll for the key (Sim2k FUN_10c1bea0 ->
     FUN_10c031f0 -> base GPCallBack(1) @100c0f90 -> CPCheck::RequestModuleKey @100c0b80 ->
     GetRegistryEntry @1006cf00 -> ValidateKey @100625c0).  The answer is a 32-bit key word and
     the board serial the key belongs to.
  3. Sim2k sends (key, moduleID) to the board micro-controller through DSP5 (board vt+0x184 =
     FUN_10c2f2c0): ucMagicDest = address of the module's `magicProt` word, ucDataOut/ucBytePosOut
     = key, then moduleID, ucCmdOut = 0x220.  The micro-controller, which holds the board secret,
     computes the unlock word, and DSP5 writes it straight into magicProt.  The host never sees it.
  4. The module's seg_init compares magicProt and only then copies its real code.

This module only *reads* the owner's key file, validates it the way SCOPE does, and produces the
values that step 3 sends to the owner's own board.  It contains no key generation: the encoder
side of the scheme (CPCheck::CryptForRegistry, the key check-character generator FUN_100115a0)
is deliberately not implemented, and the unlock word itself is computed by the board hardware.

Usage:
  pulsar_license.py show   [--keys PATH] [--serial 0xSNO] [--uc-info 0xWORD]
  pulsar_license.py check  MODULE.dsp ... [--dsp DIR] [--keys PATH] [--serial 0xSNO] [--show-values]
  PATH = a .v5 file or a directory (default /var/lib/snd-pulsar/license, all *.v5 in it).

Python 3 standard library only (plus pulsar_modules/sc_decode for `check`).
"""
import argparse
import glob
import os
import sys

DEFAULT_LICENSE_DIR = "/var/lib/snd-pulsar/license"
UC_DSP = 5                         # Pulsar2 board vt+0x224 FUN_10c314c0
UC_CMD_MAGIC = 0x220               # FUN_10c2f2c0 @10c2f5d3
UC_SLEEP_MS = 11                   # board vt+0x270(0xb) between the uC steps
UC_MBOX = 0x800 + 1 + 2 * UC_DSP   # host SRAM dword (BAR+0x8202C), used by the verify path
UC_MBOX_DEST = 0x63E00000 | UC_MBOX


class LicenseError(Exception):
    pass


def _i32(v):
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v & 0x80000000 else v


def _crem(a, b):
    """C/MSVC __allrem: remainder with the sign of the dividend."""
    r = abs(a) % abs(b)
    return -r if a < 0 else r


# ---------------------------------------------------------------------------------------------
# record layout (CPCheck::ReadEntry @100500d0, GetRegistryEntry @1006cf00)  [C]
# ---------------------------------------------------------------------------------------------
REC_LEN = 0x73
OFF_SERIAL, LEN_SERIAL = 0x02, 9          # board serial string (String2SnoLong)
OFF_NAME, LEN_NAME = 0x0C, 0x29           # product name (display only)
OFF_DATE, LEN_DATE = 0x35, 0x0E           # "mm/dd/yy hh:mm" (display only)
OFF_TEMP = 0x44                           # '0' = permanent, '1' = temporary (hours credit)
OFF_KEY, LEN_KEY = 0x45, 12               # key string, ValidateKey wants exactly 12 chars
OFF_CHK = 0x51                            # 2 hex chars, checksum of the record
OFF_ENC, LEN_ENC = 0x53, 32               # 32 chars -> 8 x 4 hex digits (f0..f7)

# f0 manufacturer, f1 type, f2 family, f3 component, f4 product, f5 version, f6 level, f7 hw word
FIELDS = ("manufacturer", "type", "family", "component", "product", "version", "level", "hw")

_REG_ALPHABET = "W5YJR6CN41DIT28SXOFZA7EB0LH9KVUM3PG"     # DecryptFromRegistry table
_B26 = "0XH345C12BMRZ8YGW9TAKE67LF"                       # DAT_101371e0 (FUN_10011520)
_SERIAL_POS = (7, 5, 8, 3, 0, 2, 1, 6, 4)                 # DAT_101371fc: 8 digits + check char
_KEY_POS = (4, 11, 9, 7, 2, 10, 0, 6, 1)                  # DAT_10137220: 8 digits + check char
# FUN_1000fca0: fixed transform of the decoded key (DAT_10137160: (op, value) pairs, op 0 = end;
# 1 = add, 2 = xor, 3 = rotate left by value & 31)
_KEY_XFORM = ((3, 0x9A860E74), (2, 0x4575B646), (3, 0x9C4065E3), (2, 0x756B3673), (3, 0x54AF605E),
              (1, 0x6B5CE557), (3, 0x7D745074), (2, 0x5BBF6464), (1, 0x7C404367), (3, 0x474B6564),
              (1, 0x743C3F64), (2, 0x3E0A4534), (2, 0x967F5040), (3, 0xED54B784), (1, 0x784CDE04))


def _decrypt_fields(enc, k):
    """CPCheck::DecryptFromRegistry @10010220: char i -> hex digit idx - i%6 - k."""
    if k > 12:
        k %= 13
    out = []
    for i, ch in enumerate(enc):
        p = _REG_ALPHABET.find(ch)
        if p < 0:
            return None
        d = p - i % 6 - k
        if not 0 <= d <= 15:
            return None
        out.append("0123456789ABCDEF"[d])
    return "".join(out)


def _record_checksum(rec_plain):
    """ReadEntry: sum of the 0x73 record bytes (signed chars; checksum field blanked, encrypted
    part replaced by its plain hex digits), printed with "%lX", repeated while the sum > 0xFF."""
    s = rec_plain
    while True:
        v = sum(b - 256 if b > 127 else b for b in s) & 0xFFFFFFFF
        s = ("%X" % v).encode()
        if v <= 0xFF:
            return s.decode()


def _b26(s, pos):
    """FUN_10011520: 8 base-26 digits at positions pos[0..7] + check character at pos[8]."""
    v = chk = 0
    for i in range(8):
        d = _B26.find(s[pos[i]])
        if d < 0:
            return None
        chk += d * i
        v = (v * 26 + d) & 0xFFFFFFFF
    return v if s[pos[8]] == _B26[chk % 26] else None


def _sno_map(v):
    """FUN_10011350 (also the first half of CPCheck::GetBoardType @10037140)."""
    u = (v * 0x152C0E79) % 0x1E521609
    if u > 0x1ADE9F6:
        if u < 0x35BD3EE:
            return u + 0x1E521609
        if u > 0x7FFFFFF:
            if u < 0x9ADE9F7:
                return u
            if u < 0xB5BD3EE:
                return u + 0x1E521609
        u += 0x3CA42C12
    return u & 0xFFFFFFFF


def serial_to_sno(serial):
    """CPCheck::String2SnoLong @1004ffb0: 9-character board serial -> the 32-bit number the board's
    micro-controller reports (uC command 0x229, pulsar_loader uc_query).  None if not valid."""
    if len(serial) != 9:
        return None
    v = _b26(serial, _SERIAL_POS)
    if v is None:
        return None
    return (_sno_map(v) * 0x120DF) % 0x1E521609


_BOARD_TYPES = {0x0: "Pulsar", 0x800000: "Pulsar2", 0x1000000: "Luna", 0x20000000: "Scope",
                0x20800000: "Elektra", 0x21000000: "Sixsharc", 0x40000000: "Pulsar SRB",
                0x41000000: "P-Sampler"}


def board_type(sno):
    """CPCheck::GetBoardType @10037140 (display only)."""
    u = _sno_map(sno)
    if u == 0:
        return "Sonic Core"
    t = u & 0xF7C00000
    name = _BOARD_TYPES.get(t)
    if name is None and (u & 0xF7F80000) == 0x41800000:
        name = "SCOPE XITE-1"
    return (name or "Sonic Core") + ("/NFR" if u & 0x8000000 else "")


def module_id(manufacturer, family, product):
    """CPCheck::GetModuleID @100108c0 (= Sim2k FUN_10c45e60 prologue)."""
    a, b, c = int(manufacturer), int(family), int(product)
    x = _i32((((((a << 8) ^ b) << 8) ^ c) << 8) ^ (a >> 8))
    return _i32(_crem(x * 0x47882121, 0x79E9941B) - 0x73002CF3)


def _validate_range(f0, f2, f4, f3, f6, f5):
    """CPCheck::ValidateRange @10010120 (argument order of ValidateKey)."""
    return (0 < f0 <= 0xFFFF and 0 < f2 <= 0xFF and 0 < f4 <= 0xFF and 0 <= f3 < 0x100
            and 0 < f6 <= 0xFFFF and 0 <= f5 < 0x8000)


def _normalize_key(k):
    """FUN_10011310: upper case, O -> 0, I -> 1."""
    return k.upper().replace("O", "0").replace("I", "1")


def key_word(key_string):
    """Key word Sim2k sends to the micro-controller: FUN_1000fca0(FUN_10011520(key string)).
    Only the user's own key string goes in; None if it is not a well-formed key."""
    k = _normalize_key(key_string)
    if len(k) != LEN_KEY:
        return None
    v = _b26(k, _KEY_POS)
    if v is None:
        return None
    for op, val in _KEY_XFORM:
        if op == 1:
            v = (v + val) & 0xFFFFFFFF
        elif op == 2:
            v ^= val
        elif op == 3:
            r = val & 31
            v = ((v << r) | (v >> (32 - r))) & 0xFFFFFFFF if r else v
    return v


# ---------------------------------------------------------------------------------------------
# key file
# ---------------------------------------------------------------------------------------------
class Entry:
    """One product record of the key file (no key material in repr/str)."""

    def __init__(self, raw, source, lineno):
        self.raw = raw
        self.source, self.lineno = source, lineno
        txt = raw.decode("latin-1")
        self.serial = txt[OFF_SERIAL:OFF_SERIAL + LEN_SERIAL]
        self.name = txt[OFF_NAME:OFF_NAME + LEN_NAME].strip()
        self.date = txt[OFF_DATE:OFF_DATE + LEN_DATE].strip()
        self.temporary = txt[OFF_TEMP] != "0"
        self._key = txt[OFF_KEY:OFF_KEY + LEN_KEY]
        self.fields = None
        self.sno = None
        self.errors = []
        self._check()

    def _check(self):
        txt = self.raw.decode("latin-1")
        k = ord(txt[0x4B]) + ord(txt[0x48])                   # ReadEntry: key of DecryptFromRegistry
        plain = _decrypt_fields(txt[OFF_ENC:OFF_ENC + LEN_ENC], k)
        if plain is None:
            self.errors.append("corrupted record (cannot decrypt)")
            return
        rec = bytearray(self.raw[:REC_LEN])
        rec[OFF_CHK:OFF_CHK + 2] = b"  "
        rec[OFF_ENC:OFF_ENC + LEN_ENC] = plain.encode()
        if _record_checksum(bytes(rec)) != txt[OFF_CHK:OFF_CHK + 2]:
            self.errors.append("corrupted record (checksum)")
            return
        self.fields = dict(zip(FIELDS, (int(plain[i:i + 4], 16) for i in range(0, 32, 4))))
        self.sno = serial_to_sno(self.serial)
        if self.sno is None:
            self.errors.append("invalid board serial")
        elif self.serial == "000000000":
            self.errors.append("null board serial (SCOPE accepts it only with EnableHwTest)")
        f = self.fields
        if not _validate_range(f["manufacturer"], f["family"], f["product"], f["component"],
                               f["level"], f["version"]):
            self.errors.append("ids out of range")
        if self.temporary:
            self.errors.append("temporary key (needs the board EEPROM time credit, not supported)")
        kn = _normalize_key(self._key)
        if kn == "0" * LEN_KEY:
            self.errors.append("empty key")
        elif key_word(kn) is None:
            self.errors.append("malformed key string")

    @property
    def valid(self):
        return not self.errors

    @property
    def ids(self):
        f = self.fields or {}
        return (f.get("manufacturer"), f.get("family"), f.get("product"), f.get("level"))

    @property
    def module_id(self):
        f = self.fields
        return module_id(f["manufacturer"], f["family"], f["product"])

    @property
    def key(self):
        """The key word for the micro-controller (keep it out of logs)."""
        return key_word(self._key)

    def describe(self, board_sno=None, uc_info=None, show_values=False):
        d = {"product": self.name, "date": self.date, "file": os.path.basename(self.source),
             "line": self.lineno, "ids": self.ids, "valid": self.valid, "errors": list(self.errors)}
        if self.fields:
            d["type"] = self.fields["type"]
        why = self.board_mismatch(board_sno, uc_info)
        if why:
            d["board"] = why
        if show_values and self.valid:
            d["module_id"] = "0x%08X" % (self.module_id & 0xFFFFFFFF)
            d["key_word"] = "0x%08X" % self.key
        return d

    def board_mismatch(self, board_sno=None, uc_info=None):
        """GetRegistryEntry: the serial must be one of the installed boards; type 1 records need the
        board's uC info word 0xFFFF, type > 1 permanent records need it equal to f7.  None = OK or
        unknown (board value not given)."""
        if not self.valid:
            return None
        if board_sno is not None and self.sno != (board_sno & 0xFFFFFFFF):
            return "for another board"
        if uc_info is not None:
            t, hw = self.fields["type"], self.fields["hw"]
            if t == 1 and uc_info != 0xFFFF:
                return "board info word mismatch"
            if t > 1 and not self.temporary and hw != uc_info:
                return "board info word mismatch"
        return None

    def __repr__(self):
        return "<Entry %r ids=%s %s>" % (self.name, self.ids, "ok" if self.valid else self.errors)


def key_files(path=None):
    path = path or os.environ.get("PULSAR_LICENSE", DEFAULT_LICENSE_DIR)
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "*.v5")) + glob.glob(os.path.join(path, "*.V5")))
    return [path] if os.path.exists(path) else []


def read_entries(path=None):
    """All '+' records of the key file(s).  ReadEntry reads fixed 0x73-byte records after CR/LF."""
    out = []
    for fn in key_files(path):
        with open(fn, "rb") as f:
            data = f.read()
        for n, line in enumerate(data.splitlines(), 1):
            if line[:1] == b"+" and len(line) >= REC_LEN:
                out.append(Entry(line[:REC_LEN], fn, n))
    return out


class License:
    """The owner's licences for one board.

    board_sno: the 32-bit serial the board's micro-controller reports (uC cmd 0x229, read by
    pulsar_loader at boot).  uc_info: (uC cmd 0x21F reply >> 16) & 0xFFFF.  Either may be None
    (then that check is skipped and reported as a warning)."""

    def __init__(self, path=None, board_sno=None, uc_info=None):
        self.path = path
        self.board_sno = board_sno
        self.uc_info = uc_info
        self.entries = read_entries(path)
        self.warnings = []
        if not self.entries:
            self.warnings.append("no licence records found in %s" % (path or DEFAULT_LICENSE_DIR))
        if board_sno is None:
            self.warnings.append("board serial unknown: records are not checked against the board")

    def usable(self):
        return [e for e in self.entries if e.valid and e.board_mismatch(self.board_sno, self.uc_info) is None]

    def find(self, seg_id):
        """GetRegistryEntry: record with f0 == manufacturer, f2 == family, f4 == product, f6 == level
        of the module's seg_id (manufacturerID, familyID, productID, componentID, levelID, versionID)."""
        manu, fam, prod, _comp, level = seg_id[:5]
        for e in self.usable():
            f = e.fields
            if (f["manufacturer"], f["family"], f["product"], f["level"]) == (manu, fam, prod, level):
                return e
        return None

    def unlock_values(self, seg_id):
        """(module_id, key_word, entry) for a module's seg_id, or raise LicenseError."""
        e = self.find(seg_id)
        if e is None:
            raise LicenseError("no valid licence for module ids %s (manufacturer %d, family %d, product %d)"
                               % (tuple(seg_id[:6]), seg_id[0], seg_id[1], seg_id[2]))
        f = e.fields
        if not _validate_range(seg_id[0], seg_id[1], seg_id[2], f["component"], seg_id[4], f["version"]):
            raise LicenseError("module ids out of range")
        mid = module_id(seg_id[0], seg_id[1], seg_id[2])
        if mid == 0:
            raise LicenseError("module id is 0")
        return mid & 0xFFFFFFFF, e.key, e


# ---------------------------------------------------------------------------------------------
# DSP side (pulsar_modules ModuleClass / Module)
# ---------------------------------------------------------------------------------------------
def module_seg_id(cls):
    """seg_id words of a pulsar_modules.ModuleClass, or None if the module is not licensed."""
    from pulsar_modules import _seg, _dm32
    s = _seg(cls.obj, "seg_id")
    if s is None:
        return None
    n = s.size // 5
    return tuple(_dm32(s, i) for i in range(min(6, n))) + (0,) * max(0, 6 - n)


def magic_offset(cls):
    """Word offset of `magicProt` inside seg_mod, or None (the module does not check a magic word)."""
    from pulsar_modules import _seg, _find_sym
    sym = _find_sym(cls.obj, "magicProt", False)
    seg = _seg(cls.obj, "seg_mod")
    if sym is None or seg is None or sym.scnum <= 0 or cls.obj.sections[sym.scnum - 1] is not seg:
        return None
    return sym.value - seg.vaddr


def needs_unlock(cls):
    return module_seg_id(cls) is not None and magic_offset(cls) is not None


def magic_dest(mod_dsp, addr, uc_dsp=UC_DSP):
    """ucMagicDest for a word on DSP mod_dsp (FUN_10c2f2c0 @10c2f492): a local address on the uC DSP,
    otherwise board vt+0xa8 FUN_10c2e420(dsp, addr, isPCI=0, len=1)."""
    if mod_dsp == uc_dsp:
        return addr
    return 0x20000000 | 1 << 25 | (mod_dsp & 0xF) << 21 | (mod_dsp & 0x10) << 14 | (addr & 0x1FFFF)


def uc_magic_ops(uc_syms, mod_dsp, magic_addr, mid, key, label="", uc_dsp=UC_DSP):
    """Op list (pulsar_modules.execute format) of Sim2k FUN_10c2f2c0 for one module; run it after the
    module's seg_mod is uploaded and before its fnInit (sysmsg 2)."""
    need = ("ucMagicDest", "ucDataOut", "ucBytePosOut", "ucCmdOut")
    if any(uc_syms.get(n, -1) < 0 for n in need):
        raise LicenseError("DSP%d OS has no micro-controller symbols (%s)" % (uc_dsp, ", ".join(need)))
    s = uc_syms
    t = label + ": " if label else ""
    return [
        ("set", uc_dsp, s["ucMagicDest"], magic_dest(mod_dsp, magic_addr, uc_dsp), t + "ucMagicDest -> magicProt"),
        ("sleep", uc_dsp, UC_SLEEP_MS, ""),
        ("set", uc_dsp, s["ucDataOut"], key, t + "uC data: key word"),
        ("set", uc_dsp, s["ucBytePosOut"], 4, t + "uC send 4 bytes"),
        ("sleep", uc_dsp, UC_SLEEP_MS, ""),
        ("set", uc_dsp, s["ucDataOut"], mid, t + "uC data: module id"),
        ("set", uc_dsp, s["ucBytePosOut"], 4, t + "uC send 4 bytes"),
        ("sleep", uc_dsp, UC_SLEEP_MS, ""),
        ("set", uc_dsp, s["ucCmdOut"], UC_CMD_MAGIC, t + "uC command 0x220 (magic -> ucMagicDest)"),
        ("sleep", uc_dsp, UC_SLEEP_MS, ""),
    ]


def unlock_hook(lic, uc_syms):
    """Callable for pulsar_modules PlutoDsp.load(..., unlock=...): returns the uC ops for a freshly
    placed module, [] for unlicensed modules; raises LicenseError when the licence is missing."""
    def hook(mod):
        off = magic_offset(mod.cls)
        seg_id = module_seg_id(mod.cls)
        if off is None or seg_id is None:
            return []
        mid, key, _e = lic.unlock_values(seg_id)
        return uc_magic_ops(uc_syms, mod.dsp, mod.seg_mod + off, mid, key, mod.name)
    return hook


def verify_on_board(board, mid, key, uc_dsp=UC_DSP):
    """Registration-time check of SCOPE (ValidateKey with param_9 = 1 -> RequestModuleMagic -> SimCall 6
    -> FUN_10c2f2c0 without a module): the uC answer goes to the host mailbox; > 0x7FFFFFFF = valid.
    board = pulsar_loader.Board (DSPs running).  Returns (ok, raw reply)."""
    s = board.syms[uc_dsp]
    mbox = 0x20000 + UC_MBOX                       # SRAM dword index (BAR+0x8202C)
    board.set_value(uc_dsp, s["ucMagicDest"], UC_MBOX_DEST)
    board.bar.wr(mbox, 1)
    for op in uc_magic_ops(s, None, 0, mid, key, uc_dsp=uc_dsp)[1:]:
        if op[0] == "set":
            board.set_value(op[1], op[2], op[3])
        else:
            board.sleep(op[2])
    r = board.bar.rd(mbox) & 0xFFFFFFFF
    board.bar.wr(mbox, 0)
    return r > 0x7FFFFFFF, r


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def _int(s):
    return None if s is None else int(s, 0)


def cmd_show(a):
    lic = License(a.keys, _int(a.serial), _int(a.uc_info))
    for w in lic.warnings:
        print("warning:", w)
    for e in lic.entries:
        d = e.describe(lic.board_sno, lic.uc_info, a.show_values)
        st = "ok" if e.valid and "board" not in d else "; ".join(d["errors"] + ([d["board"]] if "board" in d else []))
        extra = "  module_id=%s key=%s" % (d["module_id"], d["key_word"]) if "module_id" in d else ""
        ids = "/".join("-" if x is None else str(x) for x in d["ids"])
        print("%-36s ids %-12s %s%s" % (d["product"][:36], ids, st, extra))
    if lic.entries and lic.entries[0].sno is not None:
        e = lic.entries[0]
        print("board type of the key file: %s" % board_type(e.sno))
    return 0


def cmd_check(a):
    import pulsar_modules as pm
    lic = License(a.keys, _int(a.serial), _int(a.uc_info))
    for w in lic.warnings:
        print("warning:", w)
    rc = 0
    for name in a.modules:
        path = name if os.path.exists(name) else os.path.join(a.dsp, name)
        cls = pm.ModuleClass(path)
        sid, off = module_seg_id(cls), magic_offset(cls)
        if sid is None:
            print("%-14s not licensed (no seg_id)" % cls.name)
            continue
        if off is None:
            print("%-14s seg_id %s, no magicProt: runs without unlock" % (cls.name, sid))
            continue
        try:
            mid, key, e = lic.unlock_values(sid)
            v = "  module_id=0x%08X key=0x%08X" % (mid, key) if a.show_values else ""
            print("%-14s seg_id %s magicProt=seg_mod+%d -> licensed by %r%s" % (cls.name, sid, off, e.name, v))
        except LicenseError as ex:
            print("%-14s seg_id %s magicProt=seg_mod+%d -> %s" % (cls.name, sid, off, ex))
            rc = 1
    return rc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("show", "check"):
        p = sub.add_parser(n)
        p.add_argument("--keys", help="key file or directory (default %s)" % DEFAULT_LICENSE_DIR)
        p.add_argument("--serial", help="board serial number as reported by the uC (e.g. 0x12345678)")
        p.add_argument("--uc-info", help="board uC info word (uC command 0x21F reply >> 16)")
        p.add_argument("--show-values", action="store_true", help="print module ids and key words")
        if n == "check":
            p.add_argument("modules", nargs="+", help=".dsp files or names")
            p.add_argument("--dsp", default="/var/lib/snd-pulsar/dsp")
    a = ap.parse_args(argv)
    try:
        return cmd_show(a) if a.cmd == "show" else cmd_check(a)
    except (LicenseError, OSError) as e:
        print("error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
